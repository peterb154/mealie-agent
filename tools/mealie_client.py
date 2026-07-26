"""Thin wrapper around Mealie's REST API.

One class: ``MealieClient``. All tool wrappers share the same instance shape.
Two constructors of note:

- ``MealieClient.from_env()`` — uses ``MEALIE_API_TOKEN`` for the service
  (long-lived, scoped to the `mealie-agent` service account in Mealie).
  Use for sync jobs and for anything that should run "as the agent."
- ``MealieClient(token=jwt)`` — per-request, with the end user's JWT from
  the auth_verifier. Mealie enforces RBAC (household isolation etc.) based
  on that token, so we don't re-check authz on our side.

Endpoints are named after what they do, not what they return. Paginated
list endpoints return the raw dict (``items`` + ``page`` + ``total``) so
callers can decide between "just the page" and "drain everything."
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _slugify(name: str) -> str:
    """Approximate Mealie's organizer slug rule (python-slugify defaults).

    Only used to *try* an exact lookup — callers fall back to search when
    it misses, so an imperfect match here costs a round trip, not
    correctness."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class MealieClient:
    """Small, synchronous HTTPX wrapper — one token per instance."""

    def __init__(self, base_url: str, token: str, *, timeout: httpx.Timeout | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._user_id: str | None = None
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout or DEFAULT_TIMEOUT,
        )

    @classmethod
    def from_env(cls) -> MealieClient:
        url = os.environ["MEALIE_URL"]
        token = os.environ["MEALIE_API_TOKEN"]
        return cls(url, token)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MealieClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # --- auth ---------------------------------------------------------------

    def whoami(self) -> dict[str, Any] | None:
        """Introspect the current token. 200 → user dict, 401 → None."""
        r = self._client.get("/api/users/self")
        if r.status_code == 401:
            return None
        r.raise_for_status()
        return r.json()

    def self_user_id(self) -> str:
        """This token's Mealie user UUID, cached for the instance lifetime.

        Rating writes need it in the path — Mealie exposes ``self`` on the
        read side (``/api/users/self/ratings``) but not the write side
        (``/api/users/{id}/ratings/{slug}``)."""
        if self._user_id is None:
            me = self.whoami()
            if not me or not me.get("id"):
                raise RuntimeError("could not resolve Mealie user id — is the token valid?")
            self._user_id = me["id"]
        return self._user_id

    # --- recipes ------------------------------------------------------------

    def list_recipes(
        self, *, page: int = 1, per_page: int = 50, updated_after: str | None = None
    ) -> dict[str, Any]:
        """Paginated list. ``updated_after`` is an ISO-8601 timestamp for
        incremental sync. Uses Mealie's ``queryFilter`` DSL."""
        params: dict[str, Any] = {"page": page, "perPage": per_page, "orderBy": "date_updated"}
        if updated_after:
            params["queryFilter"] = f'date_updated >= "{updated_after}"'
        r = self._client.get("/api/recipes", params=params)
        r.raise_for_status()
        return r.json()

    def get_recipe(self, slug: str) -> dict[str, Any]:
        r = self._client.get(f"/api/recipes/{slug}")
        r.raise_for_status()
        return r.json()

    def self_ratings(self) -> list[dict[str, Any]]:
        """Return the current user's per-recipe ratings + favorite flags.
        Mealie's recipe-level `rating` field is always null — the real data
        lives here, keyed by recipeId."""
        r = self._client.get("/api/users/self/ratings")
        r.raise_for_status()
        return (r.json() or {}).get("ratings", [])

    def set_rating(
        self, slug: str, *, rating: float | None = None, is_favorite: bool | None = None
    ) -> None:
        """Set the current user's rating and/or favorite flag on a recipe.

        Mealie MERGES: on an existing row it only assigns the fields that
        are non-null, so rating and favorite move independently. The
        corollary is that the API cannot un-set a rating — there is no
        delete-rating endpoint and ``rating: null`` is ignored on update.
        Pass ``rating=0`` to clear; Mealie renders 0 as unrated."""
        payload: dict[str, Any] = {}
        if rating is not None:
            payload["rating"] = rating
        if is_favorite is not None:
            payload["isFavorite"] = is_favorite
        if not payload:
            raise ValueError("set_rating needs a rating and/or is_favorite")
        r = self._client.post(f"/api/users/{self.self_user_id()}/ratings/{slug}", json=payload)
        r.raise_for_status()

    def add_comment(self, recipe_id: str, text: str) -> dict[str, Any]:
        """Append a comment to a recipe. Comments are per-user and purely
        additive — nothing existing can be clobbered. Keyed by recipe ID,
        not slug."""
        r = self._client.post("/api/comments", json={"recipeId": recipe_id, "text": text})
        r.raise_for_status()
        return r.json() if r.content else {}

    def create_recipe(self, name: str) -> str:
        """Create an empty recipe and return its slug.

        Mealie's create endpoint takes a name and nothing else; every
        other field has to land in a follow-up patch. Callers that want a
        fully-populated recipe should use ``patch_recipe`` right after."""
        r = self._client.post("/api/recipes", json={"name": name})
        r.raise_for_status()
        return r.json()  # response_model=str — a bare JSON string (the slug)

    def patch_recipe(self, slug: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Partial update — ONLY the keys present in ``fields`` change.

        Mealie's PATCH handler dumps the parsed body with
        ``exclude_unset=True``, so omitted keys are genuinely left alone
        (unlike PUT, which replaces the whole document). Note that keys
        which ARE present replace wholesale: sending ``recipeIngredient``
        overwrites the entire ingredient list, it does not merge."""
        if not fields:
            raise ValueError("patch_recipe called with no fields")
        r = self._client.patch(f"/api/recipes/{slug}", json=fields)
        r.raise_for_status()
        return r.json()

    def parse_ingredients(self, lines: list[str]) -> list[dict[str, Any]]:
        """Run Mealie's NLP parser over free-text ingredient lines.

        Foods and units the instance already knows come back with real
        ids; anything it can't resolve comes back name-only. That
        distinction matters — see ``_structured_ingredient``."""
        r = self._client.post(
            "/api/parser/ingredients", json={"parser": "nlp", "ingredients": lines}
        )
        r.raise_for_status()
        return r.json() or []

    def append_recipe_note(self, slug: str, title: str, text: str) -> dict[str, Any]:
        """Append one entry to a recipe's ``notes`` array, keeping the
        existing ones.

        Read-modify-write: Mealie has no note-level endpoint, so a naive
        patch of ``notes`` would drop everything already there."""
        recipe = self.get_recipe(slug)
        notes = list(recipe.get("notes") or [])
        notes.append({"title": title, "text": text})
        return self.patch_recipe(slug, {"notes": notes})

    # --- organizers (tags / categories) -------------------------------------

    def find_organizer(self, kind: str, name: str) -> dict[str, Any] | None:
        """Look up one tag/category by name. None if it doesn't exist.

        Tries the exact slug endpoint first: ``search`` is lexical and
        paginated, so a common prefix can push the exact match off the
        first page — and a missed match means we create a duplicate."""
        r = self._client.get(f"/api/organizers/{kind}/slug/{_slugify(name)}")
        if r.status_code == 200 and r.json():
            return r.json()
        # Our slug rule may not match Mealie's for exotic names; fall back
        # to search and compare on the name itself.
        g = self._client.get(f"/api/organizers/{kind}", params={"search": name, "perPage": 100})
        g.raise_for_status()
        items = (g.json() or {}).get("items") or []
        return next((i for i in items if (i.get("name") or "").lower() == name.lower()), None)

    def resolve_organizers(self, kind: str, names: list[str]) -> list[dict[str, Any]]:
        """Map tag/category NAMES onto Mealie organizer objects, creating
        any that don't exist yet. ``kind`` is 'tags' or 'categories'.

        Recipes reference tags by id, so a name-only payload silently
        attaches nothing — this is the lookup that makes names work."""
        if kind not in ("tags", "categories"):
            raise ValueError(f"unknown organizer kind: {kind!r}")
        out: list[dict[str, Any]] = []
        for raw in names:
            name = raw.strip()
            if not name:
                continue
            match = self.find_organizer(kind, name)
            if match is None:
                c = self._client.post(f"/api/organizers/{kind}", json={"name": name})
                if c.status_code in (409, 422):
                    # Lost a race, or Mealie slugified it onto something
                    # that already exists. Re-read rather than fail.
                    match = self.find_organizer(kind, name)
                if match is None:
                    c.raise_for_status()
                    match = c.json()
            out.append({"id": match["id"], "name": match["name"], "slug": match["slug"]})
        return out

    def top_rated_recipes(
        self,
        *,
        min_rating: float = 4.0,
        favorites_only: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return the current user's top-rated recipes, enriched with
        slug/name/description. Joins /api/users/self/ratings with the
        recipe-detail endpoint."""
        ratings = self.self_ratings()
        # Filter + sort client-side.
        picks: list[dict[str, Any]] = []
        for row in ratings:
            rating = row.get("rating")
            is_fav = row.get("isFavorite") or False
            if favorites_only:
                if not is_fav:
                    continue
            else:
                if not (isinstance(rating, int | float) and rating >= min_rating):
                    continue
            picks.append(row)
        picks.sort(
            key=lambda r: (r.get("rating") or 0, r.get("isFavorite") or False),
            reverse=True,
        )
        picks = picks[:limit]

        enriched: list[dict[str, Any]] = []
        for row in picks:
            rid = row.get("recipeId")
            if not rid:
                continue
            try:
                rr = self._client.get(f"/api/recipes/{rid}")
                rr.raise_for_status()
                r = rr.json()
            except httpx.HTTPError:
                continue
            enriched.append(
                {
                    "id": rid,
                    "slug": r.get("slug"),
                    "name": r.get("name"),
                    "description": (r.get("description") or "").strip(),
                    "rating": row.get("rating"),
                    "isFavorite": row.get("isFavorite") or False,
                }
            )
        return enriched

    def search_recipes_text(
        self,
        query: str,
        *,
        tag_name: str | None = None,
        cookbook_slug: str | None = None,
        per_page: int = 20,
    ) -> dict[str, Any]:
        """Mealie's own lexical search. Useful when the user typed an
        exact recipe name and we don't need embeddings. Optionally filter
        by a tag NAME or a cookbook SLUG (cookbook resolves to its saved
        filter; Mealie combines search + queryFilter natively)."""
        params: dict[str, Any] = {"search": query, "perPage": per_page, "page": 1}
        if cookbook_slug:
            cb = self._client.get(f"/api/households/cookbooks/{cookbook_slug}")
            cb.raise_for_status()
            qf = (cb.json() or {}).get("queryFilterString")
            if qf:
                params["queryFilter"] = qf
        elif tag_name:
            # Wrap the name in double quotes so Mealie's DSL treats it as a
            # literal (tag names can contain spaces).
            params["queryFilter"] = f'tags.name CONTAINS ALL ["{tag_name}"]'
        r = self._client.get("/api/recipes", params=params)
        r.raise_for_status()
        return r.json()

    # --- cookbooks ----------------------------------------------------------

    def list_cookbooks(self) -> list[dict[str, Any]]:
        r = self._client.get("/api/households/cookbooks", params={"perPage": 100})
        r.raise_for_status()
        body = r.json()
        return body.get("items", body) if isinstance(body, dict) else body

    # --- meal plans ---------------------------------------------------------

    def list_meal_plans(self, *, start: str, end: str) -> list[dict[str, Any]]:
        r = self._client.get(
            "/api/households/mealplans", params={"start_date": start, "end_date": end}
        )
        r.raise_for_status()
        body = r.json()
        # Mealie returns {"items": [...], ...} for paginated; degrade gracefully.
        return body.get("items", body) if isinstance(body, dict) else body

    def add_to_meal_plan(
        self, *, date: str, entry_type: str, recipe_id: str | None = None, title: str = "",
    ) -> dict[str, Any]:
        """``entry_type`` is one of breakfast/lunch/dinner/side."""
        payload: dict[str, Any] = {"date": date, "entryType": entry_type, "title": title}
        if recipe_id:
            payload["recipeId"] = recipe_id
        r = self._client.post("/api/households/mealplans", json=payload)
        r.raise_for_status()
        return r.json()

    def delete_meal_plan_entry(self, entry_id: int | str) -> None:
        """Remove one scheduled meal from the household meal plan."""
        r = self._client.delete(f"/api/households/mealplans/{entry_id}")
        if r.status_code >= 400:
            r.raise_for_status()

    def update_meal_plan_entry(
        self,
        entry_id: int | str,
        *,
        date: str | None = None,
        entry_type: str | None = None,
        title: str | None = None,
        recipe_id: str | None = None,
    ) -> dict[str, Any]:
        """Move/edit one scheduled meal. GETs the existing entry, applies
        any provided fields onto it, and PUTs the whole thing back —
        Mealie's UpdatePlanEntry requires id/groupId/userId, so we keep
        the full payload and let Mealie ignore the extras (recipe,
        householdId)."""
        g = self._client.get(f"/api/households/mealplans/{entry_id}")
        g.raise_for_status()
        payload = dict(g.json())
        if date is not None:
            payload["date"] = date
        if entry_type is not None:
            payload["entryType"] = entry_type
        if title is not None:
            payload["title"] = title
        if recipe_id is not None:
            payload["recipeId"] = recipe_id
        r = self._client.put(f"/api/households/mealplans/{entry_id}", json=payload)
        r.raise_for_status()
        return r.json()

    # --- shopping lists -----------------------------------------------------

    def list_shopping_lists(self) -> list[dict[str, Any]]:
        r = self._client.get("/api/households/shopping/lists")
        r.raise_for_status()
        body = r.json()
        return body.get("items", body) if isinstance(body, dict) else body

    def get_shopping_list(self, list_id: str) -> dict[str, Any]:
        r = self._client.get(f"/api/households/shopping/lists/{list_id}")
        r.raise_for_status()
        return r.json()

    def create_shopping_list(self, name: str) -> dict[str, Any]:
        """Create a new shopping list scoped to the user's household."""
        r = self._client.post(
            "/api/households/shopping/lists", json={"name": name}
        )
        r.raise_for_status()
        return r.json()

    def delete_shopping_list(self, list_id: str) -> None:
        """Delete a shopping list and all of its items."""
        r = self._client.delete(f"/api/households/shopping/lists/{list_id}")
        if r.status_code >= 400:
            r.raise_for_status()

    def add_to_shopping_list(
        self, *, list_id: str, note: str, quantity: float = 1.0
    ) -> dict[str, Any]:
        """Add a free-text note. Recipe-ingredient-bound items would use a
        different endpoint; we keep this narrow for conversational use."""
        payload = {
            "shoppingListId": list_id,
            "note": note,
            "quantity": quantity,
            "isFood": False,
            "checked": False,
        }
        r = self._client.post("/api/households/shopping/items", json=payload)
        r.raise_for_status()
        return r.json()

    def check_shopping_item(self, item_id: str, *, checked: bool = True) -> dict[str, Any]:
        r = self._client.put(
            f"/api/households/shopping/items/{item_id}", json={"checked": checked}
        )
        r.raise_for_status()
        return r.json()

    def delete_shopping_item(self, item_id: str) -> None:
        """Remove one item from a shopping list."""
        r = self._client.delete(f"/api/households/shopping/items/{item_id}")
        # 200 or 204 both mean success on Mealie.
        if r.status_code >= 400:
            r.raise_for_status()

    def clear_shopping_list(
        self, list_id: str, *, checked_only: bool = False
    ) -> tuple[int, int]:
        """Delete every item on the list (or only the checked ones if
        ``checked_only`` is True). Returns (deleted, failed)."""
        lst = self.get_shopping_list(list_id)
        items = lst.get("listItems") or []
        target = [it for it in items if (not checked_only or it.get("checked"))]
        deleted = 0
        failed = 0
        for it in target:
            iid = it.get("id")
            if not iid:
                continue
            try:
                self.delete_shopping_item(iid)
                deleted += 1
            except httpx.HTTPError:
                failed += 1
        return deleted, failed
