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
    manage: bool = False,
) -> list[Any]:
    """Build memory tools. Pass ``namespace`` for a single-scope pair, or
    ``namespaces={scope_suffix: storage_namespace, ...}`` for multiple.

    ``manage=True`` adds ``list_*`` / ``forget_*`` alongside each
    remember/recall pair. Off by default — it doubles the tool count per
    scope, and a short-lived store doesn't need them. Turn it on when the
    store lives long enough for duplicates and stale entries to become a
    retrieval problem: ``recall`` is top-k by similarity, so it can only
    surface what resembles a query you already thought to ask, never what
    is actually in there.

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
        return _build_pair(mem, namespace, suffix="", top_k=top_k, manage=manage)

    # Multi-scope: `remember_<suffix>` / `recall_<suffix>` per entry.
    tools: list[Any] = []
    for suffix, ns in namespaces.items():
        if not suffix or not ns:
            raise ValueError(
                f"memory_tools namespaces entry is invalid: suffix={suffix!r} ns={ns!r}"
            )
        tools.extend(_build_pair(mem, ns, suffix=suffix, top_k=top_k, manage=manage))
    return tools


def _build_pair(
    mem: PgMemoryStore, namespace: str, *, suffix: str, top_k: int, manage: bool = False
) -> list[Any]:
    """Construct remember/recall tools bound to ``namespace``.

    When ``suffix`` is non-empty, the tool callables are renamed to
    ``remember_<suffix>`` / ``recall_<suffix>`` so the model can tell
    them apart in a multi-scope setup.
    """
    remember_name = f"remember_{suffix}" if suffix else "remember"
    recall_name = f"recall_{suffix}" if suffix else "recall"
    list_name = f"list_{suffix}_notes" if suffix else "list_notes"
    forget_name = f"forget_{suffix}_note" if suffix else "forget_note"
    update_name = f"update_{suffix}_note" if suffix else "update_note"
    scope_desc = f" ({suffix})" if suffix else ""

    @tool
    def remember_fn(text: str) -> str:
        """Save a durable note.

        Args:
            text: The content to remember.
        """
        mid = mem.add(text, namespace=namespace)
        if manage:
            # Hand back a citable id so the caller can undo what it wrote.
            return f"Saved note [{mid}]. Remove it with {forget_name}({mid})."
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
    _retag_strands_tool(remember_fn, remember_name, remember_fn.__doc__)

    recall_fn.__name__ = recall_name
    recall_fn.__qualname__ = recall_name
    recall_fn.__doc__ = (
        f"Search durable notes{scope_desc} by meaning. Returns top-k hits.\n\n"
        "Args:\n    query: Natural-language search query.\n    k: Max hits."
    )
    _retag_strands_tool(recall_fn, recall_name, recall_fn.__doc__)

    if not manage:
        return [remember_fn, recall_fn]

    @tool
    def list_fn(limit: int = 50, offset: int = 0) -> str:
        """List durable notes in full, newest first, with ids and dates."""
        rows = mem.list(namespace=namespace, limit=limit, offset=offset)
        if not rows:
            return "No notes."
        out = []
        for h in rows:
            created = getattr(h, "created_at", None)
            when = created.strftime("%Y-%m-%d") if created else "unknown date"
            out.append(f"- [{h.id}] ({when}) {h.text}")
        return "\n".join(out)

    @tool
    def forget_fn(note_id: int) -> str:
        """Delete one durable note by id. Permanent."""
        # Scoped to this tool's namespace: ids are sequential and shown
        # to callers, so an unscoped delete would reach other tenants.
        removed = mem.delete(note_id, namespace=namespace)
        if not removed:
            return f"note_id={note_id} status=not_found (no such note in this scope)"
        return f"note_id={note_id} status=deleted"

    list_fn.__name__ = list_name
    list_fn.__qualname__ = list_name
    list_fn.__doc__ = (
        f"List durable notes{scope_desc} in full, newest first, with ids and dates.\n\n"
        "Unlike recall, this is exhaustive rather than top-k by similarity — "
        "it is the only way to audit the store, spot duplicates, or find "
        "stale notes worth removing. Check here before saving something "
        "that may already be recorded.\n\n"
        "The store is for STANDING facts: allergies, dislikes, household "
        "rules, preferences that hold across time. Notes about one specific "
        "occasion crowd those out, because recall returns top-k and a "
        "cluster about a single event will dominate any nearby query.\n\n"
        "Args:\n"
        "    limit: Maximum notes to return (default 50).\n"
        "    offset: Skip this many, for paging through a large store."
    )
    _retag_strands_tool(list_fn, list_name, list_fn.__doc__)

    forget_fn.__name__ = forget_name
    forget_fn.__qualname__ = forget_name
    forget_fn.__doc__ = (
        f"Delete one durable note{scope_desc} by id. Permanent.\n\n"
        f"Get ids from {list_name} or {recall_name} — they are the numbers "
        "shown in brackets. Deleting drops the note and its embedding "
        f"together, so it stops coming back from {recall_name} immediately.\n\n"
        f"To CHANGE what a note says, use {update_name} instead — it keeps "
        "the note's id and original date. Delete is for notes that "
        "shouldn't exist at all.\n\n"
        "Confirm with the user before deleting anything they did not "
        "explicitly ask you to remove. A note in the wrong scope reports "
        "not_found and changes nothing; re-check the id rather than "
        "guessing another.\n\n"
        "Args:\n"
        "    note_id: Numeric id shown in brackets, e.g. 23 for '[23]'."
    )
    _retag_strands_tool(forget_fn, forget_name, forget_fn.__doc__)

    @tool
    def update_fn(note_id: int, text: str) -> str:
        """Rewrite one durable note in place, keeping its id and date."""
        if not text.strip():
            return "(error: note text is empty)"
        changed = mem.update(note_id, text.strip(), namespace=namespace)
        if not changed:
            return f"note_id={note_id} status=not_found (no such note in this scope)"
        return f"note_id={note_id} status=updated"

    update_fn.__name__ = update_name
    update_fn.__qualname__ = update_name
    update_fn.__doc__ = (
        f"Rewrite one durable note{scope_desc} in place, keeping its id "
        "and its original creation date.\n\n"
        f"Prefer this over {forget_name} + {remember_name} when you are "
        "correcting or tightening what a note SAYS. Deleting and re-saving "
        "restamps an old standing fact as though it were learned today, "
        "which makes the dates in the audit view lie, and it can lose the "
        "note entirely if the re-save fails.\n\n"
        "Pass the COMPLETE new text — it replaces the old text, it does "
        "not append. A note in the wrong scope reports not_found and "
        "changes nothing.\n\n"
        "Args:\n"
        "    note_id: Numeric id shown in brackets, e.g. 23 for '[23]'.\n"
        "    text: The full replacement text for the note."
    )
    _retag_strands_tool(update_fn, update_name, update_fn.__doc__)

    return [remember_fn, recall_fn, list_fn, forget_fn, update_fn]


def _retag_strands_tool(tool_obj: Any, new_name: str, description: str | None = None) -> None:
    """Update a Strands tool's advertised name and description after
    ``@tool`` has wrapped it.

    Strands' ``@tool`` decorator snapshots the name AND the docstring
    into ``tool_spec`` at decoration time. Reassigning ``__doc__``
    afterwards therefore changes nothing the model ever sees — the spec
    keeps the original one-liner. ``description`` has to be written into
    the spec explicitly or every scoped tool advertises the generic text
    it was defined with.

    Different SDK versions use different attribute names; set whatever
    exists so this keeps working across versions.
    """
    for attr in ("tool_name", "_tool_name", "name", "_name"):
        if hasattr(tool_obj, attr):
            with contextlib.suppress(AttributeError, TypeError):
                setattr(tool_obj, attr, new_name)

    spec = getattr(tool_obj, "tool_spec", None) or getattr(tool_obj, "_tool_spec", None)
    if isinstance(spec, dict):
        if "name" in spec:
            spec["name"] = new_name
        if description:
            spec["description"] = description
