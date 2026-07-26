"""Prune logic for the recipe mirror.

The guard is the point: an incomplete Mealie drain is indistinguishable
from someone deleting most of the library, and acting on the wrong one
wipes the vector index that semantic search runs on.

Run:  uv run --with-requirements requirements.txt --with pytest -m pytest tests/
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_recipes  # noqa: E402


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if sql.strip().startswith("SELECT"):
            self.conn.fetched = [(rid, slug) for rid, slug in self.conn.rows]
        elif "DELETE" in sql:
            doomed = set(params[0])
            self.conn.deleted += [r for r in self.conn.rows if r[0] in doomed]
            self.conn.rows = [r for r in self.conn.rows if r[0] not in doomed]

    def fetchall(self):
        return self.conn.fetched


class FakeConn:
    def __init__(self, rows):
        self.rows = list(rows)
        self.deleted = []
        self.fetched = []
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


def _mirror(n, start=0):
    return [(f"id-{i}", f"recipe-{i}") for i in range(start, start + n)]


def test_nothing_to_prune_when_all_live():
    conn = FakeConn(_mirror(5))
    assert sync_recipes._prune(conn, {f"id-{i}" for i in range(5)}) == 0
    assert conn.deleted == []


def test_removes_only_the_orphan():
    """The real case today: one recipe deleted in Mealie, still in the
    mirror, so search_recipes offers a slug that get_recipe can't load."""
    conn = FakeConn(_mirror(20))
    live = {f"id-{i}" for i in range(20)} - {"id-7"}
    assert sync_recipes._prune(conn, live) == 1
    assert conn.deleted == [("id-7", "recipe-7")]
    assert len(conn.rows) == 19


def test_refuses_a_suspiciously_large_prune():
    """A drain that dies after one page reports ~100 live recipes out of
    7000. Deleting the difference would destroy the mirror."""
    conn = FakeConn(_mirror(100))
    live = {f"id-{i}" for i in range(10)}  # 90% would be deleted
    assert sync_recipes._prune(conn, live) == 0
    assert conn.deleted == []
    assert len(conn.rows) == 100


def test_force_overrides_the_guard():
    conn = FakeConn(_mirror(100))
    live = {f"id-{i}" for i in range(10)}
    assert sync_recipes._prune(conn, live, force=True) == 90
    assert len(conn.rows) == 10


def test_guard_boundary_allows_a_normal_dedupe():
    """Phase 1 of #16 deletes 271 of ~7134 rows — about 4%, comfortably
    under the guard, so a real dedupe won't need --force."""
    conn = FakeConn(_mirror(7134))
    live = {f"id-{i}" for i in range(7134)} - {f"id-{i}" for i in range(271)}
    assert sync_recipes._prune(conn, live) == 271


def test_empty_mirror_is_a_noop():
    conn = FakeConn([])
    assert sync_recipes._prune(conn, set()) == 0


@pytest.mark.parametrize("ratio,expected", [(0.5, 40), (0.05, 0)])
def test_ratio_is_configurable(ratio, expected):
    conn = FakeConn(_mirror(100))
    live = {f"id-{i}" for i in range(60)}  # 40% orphaned
    assert sync_recipes._prune(conn, live, max_delete_ratio=ratio) == expected


def test_live_ids_walks_every_page():
    class FakeMealie:
        def list_recipes(self, page=1, per_page=100, updated_after=None):
            if page > 3:
                return {"items": [], "total_pages": 3}
            start = (page - 1) * 2
            return {
                "items": [{"id": f"id-{start}"}, {"id": f"id-{start + 1}"}],
                "total_pages": 3,
            }

    assert sync_recipes._live_recipe_ids(FakeMealie()) == {f"id-{i}" for i in range(6)}
