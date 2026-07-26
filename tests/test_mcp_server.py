"""Integration tests for the multi-user MCP server.

Runs a mock Mealie and the real MCP server (mounted via make_app(lifespan)
exactly like app.py) over real streamable-http. No Postgres needed — the
memory store and token lookup are injected.

Run:  uv run --with-requirements requirements.txt --with pytest -m pytest tests/
"""

import asyncio
import os
import socket
import threading
import time
from datetime import datetime
from types import SimpleNamespace

# Env BEFORE importing mcp_server (module reads env at import).
os.environ["MEALIE_API_TOKEN"] = "tok-brian"
os.environ["MCP_SHARED_SECRET"] = "sekrit"

import pytest
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

import mcp_server
from strands_pg.api import make_app

USERS = {
    "tok-brian": {"id": "u-brian", "email": "brian@example.com", "householdId": "h-fam", "groupId": "g-1"},
    "tok-amy": {"id": "u-amy", "email": "amy@example.com", "householdId": "h-amy", "groupId": "g-1"},
}
TOKENS = {"brian@example.com": "tok-brian", "amy@example.com": "tok-amy"}

calls: list[str] = []  # tokens seen on shopping-list creates
ratings: list[tuple] = []  # (token, user_id, slug, body) seen on rating writes


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _mock_mealie() -> FastAPI:
    app = FastAPI()

    def _token(authorization: str) -> str:
        tok = authorization.removeprefix("Bearer ")
        if tok not in USERS:
            raise HTTPException(status_code=401)
        return tok

    @app.get("/api/users/self")
    def users_self(authorization: str = Header(default="")):
        return USERS[_token(authorization)]

    @app.get("/api/households/cookbooks")
    def cookbooks(authorization: str = Header(default="")):
        _token(authorization)
        return {"items": [{"name": "Family Faves", "slug": "family-faves", "queryFilterString": "tag=fav"}]}

    @app.post("/api/users/{user_id}/ratings/{slug}")
    def set_rating(user_id: str, slug: str, body: dict, authorization: str = Header(default="")):
        ratings.append((_token(authorization), user_id, slug, body))
        return {}

    @app.post("/api/households/shopping/lists")
    def create_list(body: dict, authorization: str = Header(default="")):
        calls.append(_token(authorization))
        return {"id": f"L-{len(calls)}", "name": body["name"]}

    return app


class StubMemoryStore:
    """In-memory stand-in. Enforces namespace scoping on delete exactly
    like the real Postgres store, so the isolation test is meaningful."""

    def __init__(self):
        self.added = []
        self.rows = {}  # id -> (namespace, text)
        self.edited = set()
        self._next = 1

    def add(self, text, namespace=None, **kw):
        self.added.append((namespace, text))
        mid = self._next
        self._next += 1
        self.rows[mid] = (namespace, text)
        return mid

    def search(self, query, k=5, namespace=None, **kw):
        return []

    def list(self, namespace=None, limit=100, offset=0, **kw):
        hits = [
            SimpleNamespace(id=i, namespace=ns, text=t, metadata={},
                            distance=0.0, created_at=datetime(2026, 7, 26),
                            updated_at=datetime(2026, 7, 28) if i in self.edited else None)
            for i, (ns, t) in sorted(self.rows.items(), reverse=True)
            if ns == namespace
        ]
        return hits[offset : offset + limit]

    def delete(self, memory_id, *, namespace):
        row = self.rows.get(memory_id)
        if row is None or (namespace is not None and row[0] != namespace):
            return False
        del self.rows[memory_id]
        return True

    def update(self, memory_id, text, *, namespace):
        row = self.rows.get(memory_id)
        if row is None or row[0] != namespace:
            return False
        self.rows[memory_id] = (row[0], text)
        self.edited.add(memory_id)
        return True


def _serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        if server.started:
            return
        time.sleep(0.1)
    raise RuntimeError(f"server on :{port} did not start")


STUB_STORE = StubMemoryStore()


def _build_app(mcp):
    mcp_app = mcp.http_app(path="/")
    app = make_app(
        lambda session_id, context=None: (_ for _ in ()).throw(RuntimeError("unused")),
        lifespan=mcp_app.lifespan,
    )
    app.mount("/mcp", mcp_app)
    return app


@pytest.fixture(scope="module")
def urls():
    """(secret_mode_url, dev_mode_url) — both servers up, one mock Mealie."""
    mealie_port, secret_port, dev_port = _free_port(), _free_port(), _free_port()
    mcp_server.MEALIE_URL = f"http://127.0.0.1:{mealie_port}"
    # tools/auth reads its own module-level MEALIE_URL
    import tools.auth

    tools.auth.MEALIE_URL = mcp_server.MEALIE_URL
    _serve(_mock_mealie(), mealie_port)

    # Secret (production/multi-user) mode — config captured at build time.
    secret_mcp = mcp_server.build_mcp(memory_store=STUB_STORE, token_lookup=TOKENS.get)
    _serve(_build_app(secret_mcp), secret_port)

    # Dev (no-secret) mode.
    mcp_server.MCP_SHARED_SECRET = ""
    try:
        dev_mcp = mcp_server.build_mcp(memory_store=STUB_STORE, token_lookup=TOKENS.get)
    finally:
        mcp_server.MCP_SHARED_SECRET = "sekrit"
    _serve(_build_app(dev_mcp), dev_port)

    return (
        f"http://127.0.0.1:{secret_port}/mcp/",
        f"http://127.0.0.1:{dev_port}/mcp/",
    )


def _client(url, secret=None, user=None) -> Client:
    headers = {}
    if secret:
        headers["X-MCP-Secret"] = secret
    if user:
        headers["X-MCP-User"] = user
    return Client(StreamableHttpTransport(url, headers=headers or None))


def _call(url, tool, args, secret=None, user=None):
    async def go():
        async with _client(url, secret, user) as c:
            return await c.call_tool(tool, args)

    return asyncio.run(go())


def _expect_error(url, tool, args, needle, secret=None, user=None):
    try:
        r = _call(url, tool, args, secret=secret, user=user)
        assert getattr(r, "is_error", False), f"{tool} unexpectedly succeeded"
        text = r.content[0].text
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
    assert needle in text, f"expected {needle!r} in error, got: {text}"


def test_tool_listing(urls):
    secret_url, _ = urls

    async def go():
        async with _client(secret_url, secret="sekrit") as c:
            return {t.name for t in await c.list_tools()}

    tools = asyncio.run(go())
    assert len(tools) == 32
    assert all(t.startswith("mealie_") for t in tools)
    assert {"mealie_search_recipes", "mealie_remember_personal", "mealie_recall_household"} <= tools
    assert {"mealie_rate_recipe", "mealie_comment_recipe", "mealie_update_recipe"} <= tools
    assert {"mealie_list_notes", "mealie_forget_note", "mealie_update_note"} <= tools


def test_forwarded_identity_uses_that_users_token(urls):
    secret_url, _ = urls
    _call(secret_url, "mealie_create_shopping_list", {"name": "Amy list"},
          secret="sekrit", user="Amy@Example.com")  # case-insensitive
    assert calls[-1] == "tok-amy"

    _call(secret_url, "mealie_create_shopping_list", {"name": "Brian list"},
          secret="sekrit", user="brian@example.com")
    assert calls[-1] == "tok-brian"


def test_rating_writes_land_under_the_forwarded_users_own_id(urls):
    """Ratings are per-user AND the user id is in the path — a mix-up here
    would write one person's verdict onto another's account."""
    secret_url, _ = urls
    _call(secret_url, "mealie_rate_recipe", {"slug": "brownies", "rating": 5},
          secret="sekrit", user="amy@example.com")
    assert ratings[-1] == ("tok-amy", "u-amy", "brownies", {"rating": 5.0})

    _call(secret_url, "mealie_rate_recipe", {"slug": "brownies", "mark_favorite": True},
          secret="sekrit", user="brian@example.com")
    assert ratings[-1] == ("tok-brian", "u-brian", "brownies", {"isFavorite": True})


def test_memory_namespaces_follow_identity(urls):
    secret_url, _ = urls
    _call(secret_url, "mealie_remember_personal", {"text": "hates cilantro"},
          secret="sekrit", user="amy@example.com")
    assert STUB_STORE.added[-1] == ("user:amy@example.com", "hates cilantro")

    _call(secret_url, "mealie_remember_household", {"text": "taco tuesday"},
          secret="sekrit", user="amy@example.com")
    assert STUB_STORE.added[-1] == ("household:h-amy", "taco tuesday")


def _text(result):
    return result.content[0].text


def test_list_notes_is_scoped_and_shows_ids_and_dates(urls):
    secret_url, _ = urls
    _call(secret_url, "mealie_remember_household", {"text": "we double every pasta recipe"},
          secret="sekrit", user="brian@example.com")
    out = _text(_call(secret_url, "mealie_list_notes", {"scope": "household"},
                      secret="sekrit", user="brian@example.com"))
    assert "we double every pasta recipe" in out
    assert "2026-07-26" in out

    # Amy's household is different — she must not see it.
    amy = _text(_call(secret_url, "mealie_list_notes", {"scope": "household"},
                      secret="sekrit", user="amy@example.com"))
    assert "pasta" not in amy


def test_forget_note_cannot_reach_another_users_notes(urls):
    """Note ids are sequential and printed to every caller, so an
    unscoped delete would let anyone remove anyone's notes by guessing."""
    secret_url, _ = urls
    saved = _text(_call(secret_url, "mealie_remember_personal", {"text": "brian hates cilantro"},
                        secret="sekrit", user="brian@example.com"))
    note_id = int(saved.split("[")[1].split("]")[0])

    # Amy, with the exact id, in the same scope name.
    stolen = _text(_call(secret_url, "mealie_forget_note", {"note_id": note_id, "scope": "personal"},
                         secret="sekrit", user="amy@example.com"))
    assert "not_found" in stolen
    assert STUB_STORE.rows[note_id][1] == "brian hates cilantro"  # still there

    # Right scope, wrong owner's scope name — also refused.
    wrong_scope = _text(_call(secret_url, "mealie_forget_note",
                              {"note_id": note_id, "scope": "household"},
                              secret="sekrit", user="brian@example.com"))
    assert "not_found" in wrong_scope
    assert note_id in STUB_STORE.rows

    # The owner, in the right scope, succeeds.
    ok = _text(_call(secret_url, "mealie_forget_note", {"note_id": note_id, "scope": "personal"},
                     secret="sekrit", user="brian@example.com"))
    assert "deleted" in ok
    assert note_id not in STUB_STORE.rows


def test_update_note_rewrites_in_place_and_is_scoped(urls):
    secret_url, _ = urls
    saved = _text(_call(secret_url, "mealie_remember_household", {"text": "we double pasta"},
                        secret="sekrit", user="brian@example.com"))
    nid = int(saved.split("[")[1].split("]")[0])

    ok = _text(_call(secret_url, "mealie_update_note",
                     {"note_id": nid, "text": "we double every pasta recipe"},
                     secret="sekrit", user="brian@example.com"))
    assert "updated" in ok
    assert STUB_STORE.rows[nid][1] == "we double every pasta recipe"

    # Amy's household is different — her update must not land.
    stolen = _text(_call(secret_url, "mealie_update_note",
                         {"note_id": nid, "text": "overwritten"},
                         secret="sekrit", user="amy@example.com"))
    assert "not_found" in stolen
    assert STUB_STORE.rows[nid][1] == "we double every pasta recipe"


def test_bad_scope_is_rejected(urls):
    secret_url, _ = urls
    out = _text(_call(secret_url, "mealie_list_notes", {"scope": "everyone"},
                      secret="sekrit", user="brian@example.com"))
    assert "personal" in out and "household" in out


def test_remember_returns_the_new_note_id(urls):
    secret_url, _ = urls
    out = _text(_call(secret_url, "mealie_remember_personal", {"text": "no bottom-feeder fish"},
                      secret="sekrit", user="brian@example.com"))
    assert "forget_note(" in out
    assert int(out.split("[")[1].split("]")[0]) in STUB_STORE.rows


def test_secret_mode_requires_identity_header(urls):
    """Gateway stopped forwarding X-MCP-User? Loud error, never a default identity."""
    secret_url, _ = urls
    _expect_error(secret_url, "mealie_list_cookbooks", {}, "X-MCP-User", secret="sekrit")


def test_unknown_user_is_not_provisioned(urls):
    secret_url, _ = urls
    _expect_error(secret_url, "mealie_list_cookbooks", {}, "no Mealie token provisioned",
                  secret="sekrit", user="stranger@example.com")


def test_missing_or_wrong_secret_rejects_reads_and_writes(urls):
    secret_url, _ = urls
    _expect_error(secret_url, "mealie_list_cookbooks", {}, "X-MCP-Secret")
    _expect_error(secret_url, "mealie_create_shopping_list", {"name": "x"}, "X-MCP-Secret")
    _expect_error(secret_url, "mealie_list_cookbooks", {}, "X-MCP-Secret", secret="wrong")


def test_dev_mode_default_identity(urls):
    """No secret configured → header-less requests use MEALIE_API_TOKEN."""
    _, dev_url = urls
    _call(dev_url, "mealie_create_shopping_list", {"name": "dev list"})
    assert calls[-1] == "tok-brian"
    _call(dev_url, "mealie_remember_personal", {"text": "dev note"})
    assert STUB_STORE.added[-1] == ("user:brian@example.com", "dev note")
