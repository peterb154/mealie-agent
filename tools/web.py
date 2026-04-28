"""Web-search tool — Brave Search API.

Used sparingly for things Rex's other tools can't answer: ingredient
substitutions, food-safety questions, "how do I X in Mealie", etc.
Recipe lookups should still go through search_recipes — those hit our
local 5,400+ catalog and are far more precise than the open web.

Requires BRAVE_API_KEY in the environment. Free tier is 2k queries/mo
at 1 q/s; plenty for personal use. If unset, the tool returns a clear
"not configured" message rather than crashing the agent."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from strands import tool

logger = logging.getLogger(__name__)

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_TIMEOUT_SECONDS = 10.0


def web_tools() -> list[Any]:
    """Tools that don't need user-scoped state."""

    @tool
    def web_search(query: str, max_results: int = 5) -> str:
        """Search the public web via Brave Search.

        Use for general cooking knowledge, Mealie usage questions, or
        anything the kitchen-specific tools can't answer. Do NOT use
        for recipe discovery — search_recipes / search_recipes_text
        hit the household's local catalog and give better, owned
        results.

        Args:
            query: Search query (natural language is fine).
            max_results: How many results to return (default 5, max 10).
        """
        api_key = os.environ.get("BRAVE_API_KEY", "").strip()
        if not api_key:
            return "(web_search unavailable: BRAVE_API_KEY not set)"
        try:
            n = max(1, min(int(max_results), 10)) if max_results else 5
        except (TypeError, ValueError):
            n = 5
        try:
            r = httpx.get(
                _BRAVE_ENDPOINT,
                params={
                    "q": query,
                    "count": n,
                    # Strip <strong> highlights from descriptions — pure
                    # noise to the LLM and costs tokens on every call.
                    "text_decorations": False,
                },
                headers={
                    "X-Subscription-Token": api_key,
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                },
                timeout=_TIMEOUT_SECONDS,
            )
            r.raise_for_status()
            results = (r.json().get("web") or {}).get("results") or []
        except Exception as exc:  # noqa: BLE001
            logger.exception("web_search failed")
            return f"(search error: {exc})"
        if not results:
            return f"(no results for: {query})"
        lines = []
        for item in results:
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            desc = (item.get("description") or "").strip()
            lines.append(f"- {title}\n  {url}\n  {desc}")
        return "\n".join(lines)

    return [web_search]
