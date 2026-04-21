"""Shopping-list tools — Mealie API, user-JWT scoped."""

from __future__ import annotations

import logging
from typing import Any

from strands import tool

from tools.mealie_client import MealieClient

logger = logging.getLogger(__name__)


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

    return [list_shopping_lists, show_shopping_list, add_to_shopping_list, check_shopping_item]
