"""Pre-built memory tools, namespaced per session (and optionally per scope).

Two shapes, both opt-in at build time:

**Single namespace** (back-compat, most agents):

    tools = memory_tools(namespace=session_id)
    # -> [remember, recall]

**Multi-scope** (for agents with user+household or user+org memory):

    tools = memory_tools(namespaces={
        "personal":  f"user:{email}",
        "household": f"household:{group_id}",
    })
    # -> [remember_personal, recall_personal,
    #     remember_household, recall_household]

Each tool closes over its own namespace so storage stays partitioned. The
model picks which tool to call based on prompt rules ("save personal
preferences with remember_personal; save household plans with
remember_household"). When rolling out, update ``rules.md`` to reference
the right tool names for your scopes.
"""

from __future__ import annotations

import contextlib
from typing import Any

from strands import tool

from strands_pg.memory import PgMemoryStore


def memory_tools(
    namespace: str | None = None,
    *,
    namespaces: dict[str, str] | None = None,
    store: PgMemoryStore | None = None,
    top_k: int = 5,
) -> list[Any]:
    """Build memory tools. Pass ``namespace`` for a single-scope pair, or
    ``namespaces={scope_suffix: storage_namespace, ...}`` for multiple.

    Returns a list of Strands ``@tool`` callables ready to merge into an
    ``Agent(tools=[...])`` call.
    """
    if namespace is None and not namespaces:
        raise ValueError(
            "memory_tools requires either namespace=<str> or namespaces={suffix: ns}"
        )
    if namespace is not None and namespaces:
        raise ValueError("memory_tools: pass namespace OR namespaces, not both")

    mem = store or PgMemoryStore()

    if namespace is not None:
        # Single-scope: plain `remember` / `recall`.
        return _build_pair(mem, namespace, suffix="", top_k=top_k)

    # Multi-scope: `remember_<suffix>` / `recall_<suffix>` per entry.
    tools: list[Any] = []
    for suffix, ns in namespaces.items():
        if not suffix or not ns:
            raise ValueError(
                f"memory_tools namespaces entry is invalid: suffix={suffix!r} ns={ns!r}"
            )
        tools.extend(_build_pair(mem, ns, suffix=suffix, top_k=top_k))
    return tools


def _build_pair(
    mem: PgMemoryStore, namespace: str, *, suffix: str, top_k: int
) -> list[Any]:
    """Construct remember/recall tools bound to ``namespace``.

    When ``suffix`` is non-empty, the tool callables are renamed to
    ``remember_<suffix>`` / ``recall_<suffix>`` so the model can tell
    them apart in a multi-scope setup.
    """
    remember_name = f"remember_{suffix}" if suffix else "remember"
    recall_name = f"recall_{suffix}" if suffix else "recall"
    scope_desc = f" ({suffix})" if suffix else ""

    @tool
    def remember_fn(text: str) -> str:
        """Save a durable note.

        Args:
            text: The content to remember.
        """
        mid = mem.add(text, namespace=namespace)
        return f"Saved memory #{mid}"

    @tool
    def recall_fn(query: str, k: int = top_k) -> str:
        """Search durable notes by meaning. Returns top-k hits.

        Args:
            query: Natural-language search query.
            k: Maximum number of hits to return.
        """
        hits = mem.search(query, k=k, namespace=namespace)
        if not hits:
            return "No matches."
        return "\n".join(f"- [{h.id}] {h.text}" for h in hits)

    # Rename the tool callables so Strands emits them under the scoped
    # names. Set __name__ + __qualname__ + the tool_spec name so both the
    # agent's tool registry and the LLM's tool-use payloads see the new
    # identity. Update the docstring to mention the scope.
    remember_fn.__name__ = remember_name
    remember_fn.__qualname__ = remember_name
    remember_fn.__doc__ = (
        f"Save a durable note{scope_desc}.\n\nArgs:\n    text: The content to remember."
    )
    _retag_strands_tool(remember_fn, remember_name)

    recall_fn.__name__ = recall_name
    recall_fn.__qualname__ = recall_name
    recall_fn.__doc__ = (
        f"Search durable notes{scope_desc} by meaning. Returns top-k hits.\n\n"
        "Args:\n    query: Natural-language search query.\n    k: Max hits."
    )
    _retag_strands_tool(recall_fn, recall_name)

    return [remember_fn, recall_fn]


def _retag_strands_tool(tool_obj: Any, new_name: str) -> None:
    """Update a Strands tool's advertised name after ``@tool`` has wrapped it.

    Strands' ``@tool`` decorator stores the tool name on the returned
    object (as ``tool_name`` and inside ``tool_spec``). Different SDK
    versions use different attribute names; set whatever exists so the
    renamed tools register correctly across versions.
    """
    for attr in ("tool_name", "_tool_name", "name", "_name"):
        if hasattr(tool_obj, attr):
            with contextlib.suppress(AttributeError, TypeError):
                setattr(tool_obj, attr, new_name)

    spec = getattr(tool_obj, "tool_spec", None) or getattr(tool_obj, "_tool_spec", None)
    if isinstance(spec, dict) and "name" in spec:
        spec["name"] = new_name
