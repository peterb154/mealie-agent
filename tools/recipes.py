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
                SELECT slug, name, snippet
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
        lines = [
            f"- **[{name}]({_recipe_url(slug)})** — {snippet[:120].strip()}  \n"
            f"  `slug: {slug}`"
            for slug, name, snippet in rows
        ]
        return "\n".join(lines)

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

    return [search_recipes, get_recipe]
