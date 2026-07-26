"""memory_tools(manage=True) — the list/forget pair for the chat agent.

No Postgres: a stub store stands in, and it enforces namespace scoping
the same way the real one does so the isolation test means something.

Run:  uv run --with-requirements requirements.txt --with pytest -m pytest tests/
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from strands_pg.memory_tools import memory_tools

NAMESPACES = {"personal": "user:brian@example.com", "household": "household:h-1"}


class StubStore:
    def __init__(self):
        self.rows = {}
        self._next = 1

    def add(self, text, namespace=None, **kw):
        mid = self._next
        self._next += 1
        self.rows[mid] = (namespace, text)
        return mid

    def search(self, query, k=5, namespace=None, **kw):
        return []

    def list(self, namespace=None, limit=50, offset=0, **kw):
        hits = [
            SimpleNamespace(id=i, namespace=ns, text=t, metadata={}, distance=0.0,
                            created_at=datetime(2026, 7, 26))
            for i, (ns, t) in sorted(self.rows.items(), reverse=True)
            if ns == namespace
        ]
        return hits[offset : offset + limit]

    def delete(self, memory_id, namespace=None):
        row = self.rows.get(memory_id)
        if row is None or (namespace is not None and row[0] != namespace):
            return False
        del self.rows[memory_id]
        return True


@pytest.fixture
def store():
    return StubStore()


def _by_name(tools):
    """Tools keyed by the name Strands will actually advertise."""
    return {getattr(t, "tool_name", None) or t.__wrapped__.__name__: t.__wrapped__ for t in tools}


def test_manage_off_is_unchanged(store):
    """Default must stay back-compatible for other framework consumers."""
    names = set(_by_name(memory_tools(namespaces=NAMESPACES, store=store)))
    assert names == {"remember_personal", "recall_personal",
                     "remember_household", "recall_household"}


def test_manage_on_adds_a_scoped_pair_per_namespace(store):
    names = set(_by_name(memory_tools(namespaces=NAMESPACES, store=store, manage=True)))
    assert names == {
        "remember_personal", "recall_personal", "list_personal_notes", "forget_personal_note",
        "remember_household", "recall_household", "list_household_notes", "forget_household_note",
    }


def test_single_scope_names_are_unsuffixed(store):
    names = set(_by_name(memory_tools(namespace="solo", store=store, manage=True)))
    assert names == {"remember", "recall", "list_notes", "forget_note"}


def test_list_shows_ids_and_dates_and_is_namespace_scoped(store):
    t = _by_name(memory_tools(namespaces=NAMESPACES, store=store, manage=True))
    t["remember_household"]("we double every pasta recipe")
    t["remember_personal"]("brian hates cilantro")

    household = t["list_household_notes"]()
    assert "we double every pasta recipe" in household
    assert "(2026-07-26)" in household
    assert "cilantro" not in household  # personal note must not leak

    assert "cilantro" in t["list_personal_notes"]()


def test_forget_is_scoped_to_its_own_namespace(store):
    """The household tool must not be able to delete a personal note by
    id, even though ids are global and sequential."""
    t = _by_name(memory_tools(namespaces=NAMESPACES, store=store, manage=True))
    saved = t["remember_personal"]("brian hates cilantro")
    note_id = int(saved.split("[")[1].split("]")[0])

    assert "not_found" in t["forget_household_note"](note_id)
    assert note_id in store.rows  # untouched

    assert "deleted" in t["forget_personal_note"](note_id)
    assert note_id not in store.rows

    # Second delete of the same id is a clean not_found, not a crash.
    assert "not_found" in t["forget_personal_note"](note_id)


def test_remember_returns_a_citable_id_when_manage_is_on(store):
    t = _by_name(memory_tools(namespaces=NAMESPACES, store=store, manage=True))
    out = t["remember_personal"]("no bottom-feeder fish")
    assert "forget_personal_note(" in out
    assert int(out.split("[")[1].split("]")[0]) in store.rows


def test_list_paging(store):
    t = _by_name(memory_tools(namespaces=NAMESPACES, store=store, manage=True))
    for i in range(3):
        t["remember_household"](f"note {i}")
    assert len(t["list_household_notes"](limit=2).splitlines()) == 2
    assert len(t["list_household_notes"](limit=2, offset=2).splitlines()) == 1
