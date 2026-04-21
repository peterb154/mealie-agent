"""Shopping-list tools — Mealie API, user-JWT scoped."""

from __future__ import annotations

import logging
import os
import urllib.parse
from typing import Any

from strands import tool

from tools.mealie_client import MealieClient

logger = logging.getLogger(__name__)

# Optional deployment-level default. Individual users can override at call
# time via the bulk_add_to_shopping_list `store_search_url` arg, or the
# agent can recall their preferred store from memory. `{q}` is the query
# placeholder. Empty = no links appended (plain notes).
_DEFAULT_STORE_URL = os.environ.get("GROCERY_SEARCH_URL", "").strip()


def _grocery_link(item: str, url_template: str) -> str:
    """Return the item text, with a trailing grocery-store search URL if
    ``url_template`` is non-empty. Otherwise returns the item unchanged."""
    item = item.strip()
    if not url_template:
        return item
    q = urllib.parse.quote_plus(item)
    return f"{item} — {url_template.format(q=q)}"


def shopping_tools(user_client: MealieClient) -> list[Any]:
    """Per-request shopping-list tools bound to the user's JWT."""

    @tool
    def list_shopping_lists() -> str:
        """Show all shopping lists visible to the current user."""
        try:
            lists = user_client.list_shopping_lists()
        except Exception as exc:  # noqa: BLE001
            logger.exception("list_shopping_lists failed")
            return f"(fetch error: {exc})"
        if not lists:
            return "No shopping lists."
        return "\n".join(f"- [{lst['id']}] {lst['name']}" for lst in lists)

    @tool
    def show_shopping_list(list_id: str) -> str:
        """Show items on a shopping list.

        Args:
            list_id: UUID from list_shopping_lists.
        """
        try:
            lst = user_client.get_shopping_list(list_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("get_shopping_list failed")
            return f"(fetch error: {exc})"
        items = lst.get("listItems") or []
        if not items:
            return f"{lst.get('name', 'list')}: (empty)"
        lines = [f"# {lst.get('name', 'list')}"]
        for it in items:
            mark = "x" if it.get("checked") else " "
            note = it.get("note") or it.get("display") or "?"
            lines.append(f"- [{mark}] {note}  ({it.get('id')})")
        return "\n".join(lines)

    @tool
    def add_to_shopping_list(list_id: str, note: str, quantity: float = 1.0) -> str:
        """Add a free-text item to a shopping list.

        Args:
            list_id: Target shopping-list UUID.
            note: What to buy (e.g., '1 lb ground beef').
            quantity: Numeric quantity (default 1).
        """
        try:
            item = user_client.add_to_shopping_list(
                list_id=list_id, note=note, quantity=quantity
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("add_to_shopping_list failed")
            return f"(add failed: {exc})"
        return f"Added: {note} (id={item.get('id')})"

    @tool
    def bulk_add_to_shopping_list(
        list_id: str,
        items: str,
        store_search_url: str = "",
    ) -> str:
        """Add many items to a shopping list in one call.

        Args:
            list_id: Target shopping-list UUID (from list_shopping_lists).
            items: Newline-separated items, one per line. Blank lines
                and lines starting with '#' are ignored (handy for
                letting the agent include section headers in its draft).
            store_search_url: Optional URL template for appending a
                grocery-search link to each item. Use ``{q}`` as the
                query placeholder (e.g.,
                ``https://www.hy-vee.com/grocery/search?q={q}``). Leave
                empty for plain-text notes. The agent should recall the
                user's preferred store from personal memory and pass it
                here; otherwise falls back to the GROCERY_SEARCH_URL env
                default if one is set.
        """
        url_template = store_search_url or _DEFAULT_STORE_URL
        lines = [
            ln.strip()
            for ln in items.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if not lines:
            return "(no items — nothing to add)"
        added: list[str] = []
        failed: list[tuple[str, str]] = []
        for line in lines:
            note = _grocery_link(line, url_template)
            try:
                user_client.add_to_shopping_list(list_id=list_id, note=note, quantity=1.0)
                added.append(line)
            except Exception as exc:  # noqa: BLE001
                logger.exception("bulk_add: %s failed", line)
                failed.append((line, str(exc)))
        out = [f"Added {len(added)} item(s) to the list."]
        if failed:
            out.append(f"\n{len(failed)} failed:")
            out.extend(f"  - {name}: {err}" for name, err in failed)
        return "\n".join(out)

    @tool
    def check_shopping_item(item_id: str, checked: bool = True) -> str:
        """Mark a shopping-list item checked/unchecked.

        Args:
            item_id: Item UUID from show_shopping_list.
            checked: True to mark done, False to uncheck.
        """
        try:
            user_client.check_shopping_item(item_id, checked=checked)
        except Exception as exc:  # noqa: BLE001
            return f"(update failed: {exc})"
        return f"{'checked' if checked else 'unchecked'}: {item_id}"

    return [
        list_shopping_lists,
        show_shopping_list,
        add_to_shopping_list,
        bulk_add_to_shopping_list,
        check_shopping_item,
    ]
