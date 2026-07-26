"""Prune logic for the recipe mirror.

Two things are worth testing here, and neither needs a database:

- ``_select_orphans`` — the judgement call about how much deletion is
  believable. Pure function, no SQL.
- ``_confirm_gone`` — the check that stops a paginated-drain miss from
  being read as a deletion.

The DELETE statement itself is one parameterised line and isn't
meaningfully testable without Postgres; asserting on a hand-rolled fake
cursor would only prove we wrote the string we wrote.

Run:  uv run --with-requirements requirements.txt --with pytest -m pytest tests/
"""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_recipes  # noqa: E402


def _rows(n, start=0):
    return [(f"id-{i}", f"recipe-{i}") for i in range(start, start + n)]


# --- _select_orphans: how much deletion is believable ----------------------


def test_nothing_orphaned_when_all_live():
    cands, refusal = sync_recipes._select_orphans(_rows(5), {f"id-{i}" for i in range(5)})
    assert (cands, refusal) == ([], None)


def test_finds_the_single_orphan():
    """The real case: one recipe deleted in Mealie, still in the mirror,
    so search_recipes offers a slug get_recipe can't load."""
    live = {f"id-{i}" for i in range(20)} - {"id-7"}
    cands, refusal = sync_recipes._select_orphans(_rows(20), live)
    assert cands == [("id-7", "recipe-7")]
    assert refusal is None


def test_refuses_a_suspiciously_large_share():
    """A drain that dies after one page reports ~100 of 7000 live. Acting
    on that difference would destroy the index search runs on."""
    cands, refusal = sync_recipes._select_orphans(_rows(100), {f"id-{i}" for i in range(10)})
    assert len(cands) == 90
    assert refusal is not None and "90/100" in refusal


def test_force_overrides_the_guard():
    _, refusal = sync_recipes._select_orphans(
        _rows(100), {f"id-{i}" for i in range(10)}, force=True
    )
    assert refusal is None


def test_small_mirrors_are_not_permanently_unprunable():
    """5 rows with 1 orphan is 20% — over the ratio, but refusing it would
    mean a small or fresh deployment could never prune at all."""
    cands, refusal = sync_recipes._select_orphans(_rows(5), {f"id-{i}" for i in range(4)})
    assert cands == [("id-4", "recipe-4")]
    assert refusal is None


def test_guard_still_applies_once_past_the_floor():
    live = {f"id-{i}" for i in range(14)}  # 6 orphaned of 20 = 30%
    cands, refusal = sync_recipes._select_orphans(_rows(20), live)
    assert len(cands) == 6
    assert refusal is not None


def test_a_real_dedupe_stays_under_the_guard():
    """#16 Phase 1 removes 271 of ~7134 — about 4%, so the intended use
    doesn't need --force."""
    live = {f"id-{i}" for i in range(7134)} - {f"id-{i}" for i in range(271)}
    cands, refusal = sync_recipes._select_orphans(_rows(7134), live)
    assert len(cands) == 271
    assert refusal is None


def test_empty_mirror_is_a_noop():
    assert sync_recipes._select_orphans([], set()) == ([], None)


# --- _confirm_gone: absence in the drain is a hypothesis, 404 is proof -----


class FakeMealie:
    """404s for ids in ``deleted``; anything else is alive. ``errors``
    raise a non-404 so we can check those aren't treated as gone."""

    def __init__(self, deleted=(), errors=()):
        self.deleted, self.errors = set(deleted), set(errors)
        self.asked = []

    def get_recipe(self, rid):
        self.asked.append(rid)
        if rid in self.errors:
            raise httpx.HTTPStatusError(
                "boom", request=httpx.Request("GET", "http://x"),
                response=httpx.Response(500),
            )
        if rid in self.deleted:
            raise httpx.HTTPStatusError(
                "not found", request=httpx.Request("GET", "http://x"),
                response=httpx.Response(404),
            )
        return {"id": rid}


def test_only_confirmed_404s_are_deletable():
    mc = FakeMealie(deleted={"id-1"})
    gone, survivors = sync_recipes._confirm_gone(mc, [("id-1", "a"), ("id-2", "b")])
    assert gone == [("id-1", "a")]
    assert survivors == ["b"]


def test_a_row_missed_by_the_drain_is_spared():
    """The bug this exists for: a concurrent deletion shifts later rows
    across the cursor, so a live recipe goes unread and looks orphaned.
    Mealie still has it, so it must survive."""
    mc = FakeMealie(deleted=set())
    gone, survivors = sync_recipes._confirm_gone(mc, [("id-9", "missed-by-pagination")])
    assert gone == []
    assert survivors == ["missed-by-pagination"]


def test_transport_errors_never_count_as_deleted():
    mc = FakeMealie(errors={"id-3"})
    gone, survivors = sync_recipes._confirm_gone(mc, [("id-3", "c")])
    assert gone == []
    assert survivors == ["c"]


def test_connection_errors_never_count_as_deleted():
    class Flaky:
        def get_recipe(self, rid):
            raise httpx.ConnectError("down")

    gone, survivors = sync_recipes._confirm_gone(Flaky(), [("id-4", "d")])
    assert gone == []
    assert survivors == ["d"]


# --- _live_recipe_ids: a short read must not look like deletions -----------


class Pager:
    def __init__(self, pages, total=None):
        self.pages, self.total = pages, total

    def list_recipes(self, page=1, per_page=100, updated_after=None):
        items = self.pages[page - 1] if page <= len(self.pages) else []
        return {"items": items, "total_pages": len(self.pages), "total": self.total}


def test_walks_every_page():
    pages = [[{"id": f"id-{i}"} for i in range(p * 2, p * 2 + 2)] for p in range(3)]
    assert sync_recipes._live_recipe_ids(Pager(pages, total=6)) == {f"id-{i}" for i in range(6)}


def test_short_read_raises_instead_of_implying_deletions():
    """Mealie says 500 recipes exist but the walk produced 6. Returning
    that quietly would mark 494 live recipes as orphans."""
    pages = [[{"id": f"id-{i}"} for i in range(p * 2, p * 2 + 2)] for p in range(3)]
    with pytest.raises(RuntimeError, match="6 of 500"):
        sync_recipes._live_recipe_ids(Pager(pages, total=500))


def test_missing_total_is_tolerated():
    """Older Mealie versions may not report total; don't hard-fail on it."""
    pages = [[{"id": "id-0"}, {"id": "id-1"}]]
    assert sync_recipes._live_recipe_ids(Pager(pages, total=None)) == {"id-0", "id-1"}
