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
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class MealieClient:
    """Small, synchronous HTTPX wrapper — one token per instance."""

    def __init__(self, base_url: str, token: str, *, timeout: httpx.Timeout | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
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
