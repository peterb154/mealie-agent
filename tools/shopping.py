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


def _build_note(display: str, search: str, url_template: str) -> str:
    """Compose a shopping-list note: display text + (if a store URL
    template is set) a trailing search link keyed on the cleaner
    `search` term. Used by bulk_add_to_shopping_list."""
    display = display.strip()
    if not url_template:
        return display
    q = urllib.parse.quote_plus((search or display).strip())
    return f"{display} — {url_template.format(q=q)}"


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
    def create_shopping_list(name: str) -> str:
        """Create a new shopping list in the user's household.

        Mealie ships with no shopping list by default — use this when
        list_shopping_lists returns nothing and the user wants to
        start adding items, or when they explicitly ask for a new list.

        Args:
            name: Display name for the new list (e.g., 'Groceries',
                'Costco run', 'Weekend party'). Keep it short.
        """
        try:
            lst = user_client.create_shopping_list(name=name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("create_shopping_list failed")
            return f"(create failed: {exc})"
        return f"Created list '{lst.get('name', name)}' (id={lst.get('id')})"

    @tool
    def delete_shopping_list(list_id: str) -> str:
        """Delete an entire shopping list (and all its items).

        Destructive — confirm with the user before calling. Use
        clear_shopping_list instead if they only want the items gone
        but the list itself kept.

        Args:
            list_id: Target list UUID from list_shopping_lists.
        """
        try:
            user_client.delete_shopping_list(list_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("delete_shopping_list failed")
            return f"(delete failed: {exc})"
        return f"deleted list: {list_id}"

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
                and lines starting with '#' are ignored (section headers
                in drafts are fine). Two formats accepted per line:

                  ``2.5 lb chuck beef``
                    — the whole string becomes BOTH the display text AND
                    the grocery-search query. Fine for simple ingredients.

                  ``2.5 lb chuck beef | chuck roast``
                    — display text BEFORE the pipe, clean search query
                    AFTER. Use this when the display has quantities,
                    units, or prep notes ('diced', '1-inch cubes')
                    that would pollute a grocery-store search. Stripping
                    those down to the ingredient name makes the link
                    actually useful.

            store_search_url: Optional URL template for appending a
                grocery-search link to each item. Use ``{q}`` as the
                query placeholder (e.g.,
                ``https://www.hy-vee.com/grocery/search?q={q}``). Empty
                = plain notes. Recall the user's store from
                ``recall_personal`` and pass it here; falls back to the
                ``GROCERY_SEARCH_URL`` env default if one is configured.
        """
        url_template = store_search_url or _DEFAULT_STORE_URL
        parsed: list[tuple[str, str]] = []
        for raw in items.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                display, search = (s.strip() for s in line.split("|", 1))
            else:
                display, search = line, line
            parsed.append((display, search or display))
        if not parsed:
            return "(no items — nothing to add)"
        added: list[str] = []
        failed: list[tuple[str, str]] = []
        for display, search in parsed:
            note = _build_note(display, search, url_template)
            try:
                user_client.add_to_shopping_list(list_id=list_id, note=note, quantity=0.0)
                added.append(display)
            except Exception as exc:  # noqa: BLE001
                logger.exception("bulk_add: %s failed", display)
                failed.append((display, str(exc)))
        out = [f"Added {len(added)} item(s) to the list."]
        if failed:
            out.append(f"\n{len(failed)} failed:")
            out.extend(f"  - {name}: {err}" for name, err in failed)
        return "\n".join(out)

    @tool
    def delete_shopping_item(item_id: str) -> str:
        """Remove a single item from a shopping list.

        Args:
            item_id: Item UUID from show_shopping_list.
        """
        try:
            user_client.delete_shopping_item(item_id)
        except Exception as exc:  # noqa: BLE001
            return f"(delete failed: {exc})"
        return f"deleted: {item_id}"

    @tool
    def clear_shopping_list(list_id: str, checked_only: bool = False) -> str:
        """Wipe every item off a shopping list in one call.

        Args:
            list_id: Target list UUID.
            checked_only: If true, delete only already-checked items
                (post-shopping cleanup). Default false = clear everything.
        """
        try:
            deleted, failed = user_client.clear_shopping_list(
                list_id, checked_only=checked_only
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("clear_shopping_list failed")
            return f"(clear failed: {exc})"
        scope = "checked items" if checked_only else "items"
        if failed:
            return f"Cleared {deleted} {scope}; {failed} failed."
        return f"Cleared {deleted} {scope}."

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
        create_shopping_list,
        delete_shopping_list,
        show_shopping_list,
        add_to_shopping_list,
        bulk_add_to_shopping_list,
        check_shopping_item,
        delete_shopping_item,
        clear_shopping_list,
    ]
