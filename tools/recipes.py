"""Recipe tools — semantic search (local pgvector) + detail fetch (Mealie API).

``recipe_tools(user_client)`` returns a list of ``@tool`` callables. Build
them per-request inside ``build_agent`` so each user's Mealie calls go
through their own JWT (Mealie handles RBAC).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from strands import tool

from strands_pg._pool import get_pool
from tools.embedding import embed
from tools.mealie_client import MealieClient

logger = logging.getLogger(__name__)

_MEALIE_URL = os.environ.get("MEALIE_URL", "").rstrip("/")
# Mealie v3's Nuxt route pattern is /g/{groupSlug}/r/{recipeSlug}. We took
# the group slug from the user's context ("home" for this install); if a
# future install runs with a different group, thread it through context.
_MEALIE_GROUP_SLUG = os.environ.get("MEALIE_GROUP_SLUG", "home")


def _recipe_url(slug: str) -> str:
    """Mealie's frontend recipe page URL. The agent surfaces these so the
    user can click through to the full recipe in Mealie."""
    if not _MEALIE_URL:
        return slug
    return f"{_MEALIE_URL}/g/{_MEALIE_GROUP_SLUG}/r/{slug}"

# --- caps -------------------------------------------------------------------
# How much recipe JSON we hand back to the LLM per get_recipe call.
# Full Mealie recipe docs are ~3-10 KB; we trim to the fields the agent
# actually needs for menu planning + grocery answers.
_RECIPE_FIELDS = (
    "id",
    "slug",
    "name",
    "description",
    "recipeCategory",
    "tags",
    "recipeIngredient",
    "recipeInstructions",
    "totalTime",
    "prepTime",
    "cookTime",
    "recipeYield",
)


def _trim_recipe(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r[k] for k in _RECIPE_FIELDS if k in r}


def recipe_tools(user_client: MealieClient) -> list[Any]:
    """Build the recipe tool pair bound to ``user_client`` (per-request JWT)."""

    @tool
    def search_recipes(query: str, k: int = 10) -> str:
        """Semantic search over the recipe library. Returns top-k hits with
        slug, name, and snippet so the agent can pick one to fetch in full.

        Args:
            query: Natural-language description — ingredients, cuisine, mood.
            k: Maximum number of hits (default 10).
        """
        try:
            qvec = embed(query)
        except Exception as exc:  # noqa: BLE001 — surface as tool-level error
            logger.exception("embed failed")
            return f"(embedding error: {exc})"
        pool = get_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT slug, name, snippet, rating
                FROM recipe_embeddings
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (qvec, k),
            )
            rows = cur.fetchall()
        if not rows:
            return "No recipes found. Has the sync run?"
        # Tool output is markdown the agent can quote verbatim. Full URL on
        # each entry so the agent's response is clickable even if the model
        # forgets the base URL.
        def _fmt(slug: str, name: str, snippet: str, rating: float | None) -> str:
            star = f" ⭐ {rating}/5" if rating and rating > 0 else ""
            return (
                f"- **[{name}]({_recipe_url(slug)})**{star} — "
                f"{snippet[:120].strip()}  \n  `slug: {slug}`"
            )
        return "\n".join(_fmt(s, n, sn, r) for s, n, sn, r in rows)

    @tool
    def search_recipes_text(query: str, tag_name: str = "", cookbook_slug: str = "") -> str:
        """Keyword search over Mealie's recipe library. Use this when the
        user gave a specific name, keyword, or phrase that semantic
        embeddings might miss ("funeral meatballs", "tater tot casserole").

        Args:
            query: Literal words to search for in recipe names + text.
            tag_name: Optional tag name (e.g., 'Maryjean'). Combines with query.
            cookbook_slug: Optional cookbook slug (e.g., 'mary-jean-s-cookbook').
                Filters results to that cookbook's saved query. Use
                list_cookbooks to discover slugs.
        """
        try:
            body = user_client.search_recipes_text(
                query, tag_name=tag_name or None, cookbook_slug=cookbook_slug or None
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("search_recipes_text failed")
            return f"(search error: {exc})"
        items = body.get("items") or []
        if not items:
            return "No matches."
        def _fmt(it: dict[str, Any]) -> str:
            r = it.get("rating")
            star = f" ⭐ {r}/5" if isinstance(r, (int, float)) and r > 0 else ""
            return (
                f"- **[{it['name']}]({_recipe_url(it['slug'])})**{star}  \n"
                f"  `slug: {it['slug']}`"
            )
        return "\n".join(_fmt(it) for it in items[:15])

    @tool
    def top_rated_recipes(
        limit: int = 15,
        min_rating: float = 4.0,
        favorites_only: bool = False,
    ) -> str:
        """Recipes the CURRENT user has rated highly (or favorited) in
        Mealie. Use this when the user asks for 'our favorites',
        'top rated', 'what we liked', etc.

        Ratings are per-user, so results reflect the signed-in user's
        ratings — not an aggregate. Household-wide preferences belong in
        remember_household.

        Args:
            limit: Maximum number of recipes to return (default 15).
            min_rating: Only include recipes rated this high or better
                (default 4.0 — 4-star and up).
            favorites_only: If true, return only recipes the user
                marked as a favorite, ignoring numeric rating.
        """
        try:
            items = user_client.top_rated_recipes(
                min_rating=min_rating,
                favorites_only=favorites_only,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("top_rated_recipes failed")
            return f"(fetch error: {exc})"
        if not items:
            kind = "favorites" if favorites_only else f"recipes rated ≥ {min_rating}"
            return f"No {kind} found for the current user."
        lines: list[str] = []
        for it in items:
            star = f" ⭐ {it['rating']}/5" if it.get("rating") else ""
            heart = " ❤️" if it.get("isFavorite") else ""
            desc = (it.get("description") or "")[:100]
            desc = desc + "…" if len(it.get("description") or "") > 100 else desc
            suffix = f" — {desc}" if desc else ""
            lines.append(
                f"- **[{it['name']}]({_recipe_url(it['slug'])})**{star}{heart}{suffix}  \n"
                f"  `slug: {it['slug']}`"
            )
        return "\n".join(lines)

    @tool
    def list_cookbooks() -> str:
        """List all cookbooks visible to the current user.

        A Mealie 'cookbook' is a saved filter (e.g., all recipes with
        tag X). Use this when the user mentions a cookbook by name so you
        can pass its slug to search_recipes_text.
        """
        try:
            items = user_client.list_cookbooks()
        except Exception as exc:  # noqa: BLE001
            logger.exception("list_cookbooks failed")
            return f"(fetch error: {exc})"
        if not items:
            return "No cookbooks."
        return "\n".join(
            f"- **{cb['name']}** (slug: `{cb['slug']}`) — filter: `{cb.get('queryFilterString', '')}`"
            for cb in items
        )

    @tool
    def get_recipe(slug: str) -> str:
        """Fetch full recipe detail by slug. Returns ingredients + steps.

        Args:
            slug: Recipe slug as shown by search_recipes (e.g. 'chicken-tikka').
        """
        try:
            r = user_client.get_recipe(slug)
        except Exception as exc:  # noqa: BLE001
            logger.exception("get_recipe failed for slug=%s", slug)
            return f"(fetch error: {exc})"
        trimmed = _trim_recipe(r)
        ingredients = trimmed.pop("recipeIngredient", []) or []
        steps = trimmed.pop("recipeInstructions", []) or []

        out: list[str] = []
        name = trimmed.get("name", slug)
        out.append(f"# [{name}]({_recipe_url(slug)})")
        if desc := trimmed.get("description"):
            out.append(desc)
        if trimmed.get("totalTime"):
            out.append(f"\ntotal time: {trimmed['totalTime']}  "
                       f"yield: {trimmed.get('recipeYield', '?')}")
        if ingredients:
            out.append("\n## Ingredients")
            for ing in ingredients:
                out.append(f"- {ing.get('display') or ing.get('note') or ing.get('food', {}).get('name', '?')}")
        if steps:
            out.append("\n## Instructions")
            for i, s in enumerate(steps, 1):
                out.append(f"{i}. {s.get('text', '').strip()}")
        return "\n".join(out)

    return [
        search_recipes,
        search_recipes_text,
        top_rated_recipes,
        list_cookbooks,
        get_recipe,
    ]
