"""Meal-plan tools — Mealie API calls bound to the user's JWT.

Mealie enforces household isolation, so we don't check here.
"""

from __future__ import annotations

import logging
from datetime import date as _date
from datetime import timedelta
from typing import Any

from strands import tool

from tools.mealie_client import MealieClient

logger = logging.getLogger(__name__)


def _parse_iso_date(s: str) -> _date:
    return _date.fromisoformat(s)


def mealplan_tools(user_client: MealieClient) -> list[Any]:
    """Per-request meal-plan tools bound to the user's JWT."""

    @tool
    def list_meal_plan(start_date: str | None = None, days: int = 7) -> str:
        """List scheduled meals in a date window for the user's household.

        Args:
            start_date: ISO date (YYYY-MM-DD). Defaults to today.
            days: How many days forward from start_date (default 7).
        """
        start = _parse_iso_date(start_date) if start_date else _date.today()
        end = start + timedelta(days=days)
        try:
            items = user_client.list_meal_plans(start=start.isoformat(), end=end.isoformat())
        except Exception as exc:  # noqa: BLE001
            logger.exception("list_meal_plan failed")
            return f"(fetch error: {exc})"
        if not items:
            return f"No meal-plan entries between {start} and {end}."
        lines: list[str] = []
        for it in items:
            title = it.get("title") or (it.get("recipe") or {}).get("name") or "?"
            etype = it.get("entryType", "meal")
            lines.append(f"- {it['date']}  {etype}: {title}  ({it.get('id')})")
        return "\n".join(lines)

    @tool
    def meal_plan_history(days_back: int = 30, start_date: str = "") -> str:
        """What the household has cooked / planned in the recent past.
        Use this BEFORE recommending meals so you don't suggest something
        they just ate.

        Args:
            days_back: How many days of history to scan (default 30).
            start_date: ISO date to walk back from. Defaults to today.
        """
        end = _parse_iso_date(start_date) if start_date else _date.today()
        start = end - timedelta(days=days_back)
        try:
            items = user_client.list_meal_plans(start=start.isoformat(), end=end.isoformat())
        except Exception as exc:  # noqa: BLE001
            logger.exception("meal_plan_history failed")
            return f"(fetch error: {exc})"
        if not items:
            return f"No meal-plan entries between {start} and {end}."
        # Most-recent first so the agent sees this-week before last-month.
        items.sort(key=lambda it: it.get("date", ""), reverse=True)
        lines: list[str] = []
        for it in items:
            title = it.get("title") or (it.get("recipe") or {}).get("name") or "?"
            etype = it.get("entryType", "meal")
            lines.append(f"- {it['date']}  {etype}: {title}")
        return "\n".join(lines)

    @tool
    def add_to_meal_plan(
        date: str,
        entry_type: str = "dinner",
        recipe_slug: str = "",
        title: str = "",
    ) -> str:
        """Schedule a recipe (or free-text meal) on a date. Defaults to dinner.

        Args:
            date: ISO date (YYYY-MM-DD) to schedule the meal on. Resolve
                relative dates like 'tonight' with current_time first.
            entry_type: One of 'breakfast', 'lunch', 'dinner', 'side'.
                Defaults to 'dinner' — only pass something else when the
                user explicitly asks for another meal.
            recipe_slug: Mealie recipe slug. Omit for a free-text entry.
            title: Free-text title (used when recipe_slug is empty).
        """
        recipe_id: str | None = None
        if recipe_slug:
            try:
                r = user_client.get_recipe(recipe_slug)
                recipe_id = r.get("id")
            except Exception as exc:  # noqa: BLE001
                return f"(recipe lookup failed: {exc})"
        if not recipe_id and not title:
            return "(need either recipe_slug or title)"
        try:
            result = user_client.add_to_meal_plan(
                date=date, entry_type=entry_type, recipe_id=recipe_id, title=title
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("add_to_meal_plan failed")
            return f"(add failed: {exc})"
        return f"Scheduled: {date} {entry_type} — {title or recipe_slug} (id={result.get('id')})"

    return [list_meal_plan, meal_plan_history, add_to_meal_plan]
