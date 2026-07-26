"""Mealie MCP server — multi-user federated mount for mcp-gateway (claude.ai).

Reuses the same tool implementations the chat agent uses (tools/*.py are
strands ``@tool`` factories; the original callables are recovered via
``__wrapped__``) and registers them with FastMCP under ``mealie_*`` names.

Identity (multi-user): the gateway forwards the Cloudflare Access email as
``X-MCP-User`` on every request. That maps to a per-user Mealie API token
in the ``mcp_user_tokens`` table; the token is introspected (lazily, cached
with a TTL per email) via the same ``verify_mealie_jwt`` the chat UI uses,
so calls run under that user's Mealie RBAC and their memory namespaces
resolve to the exact ``user:{email}`` / ``household:{id}`` namespaces Chef
Rex reads. An unknown email is an error — never someone else's data.

Security: when ``MCP_SHARED_SECRET`` is set (production), EVERY tool call
must present it as ``X-MCP-Secret`` — identity determines what reads
return, so reads are gated too — AND must carry ``X-MCP-User``. Every
CF-authenticated caller has an email, so a secret-bearing request without
one means the gateway stopped forwarding identity; erroring loudly beats
silently falling back to somebody's default identity. The
``MEALIE_API_TOKEN`` default identity only serves the no-secret (local
dev / direct scripting) configuration.

Provisioning anyone — including the admin; gateway traffic never uses the
default identity:
1. They create an API token in Mealie (user settings → API tokens).
2. ``INSERT INTO mcp_user_tokens (email, mealie_token) VALUES (lower('<cf-email>'), '<token>');``
3. Their email goes in the CF Access policy; they add the connector in claude.ai.
Token changes take effect within the cache TTL (10 min) — no restart needed.
"""

from __future__ import annotations

import functools
import hmac
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

from strands_pg._pool import get_pool
from strands_pg.memory import PgMemoryStore
from tools.auth import verify_mealie_jwt
from tools.mealie_client import MealieClient
from tools.mealplan import mealplan_tools
from tools.recipe_writes import recipe_write_tools
from tools.recipes import recipe_tools
from tools.shopping import shopping_tools

logger = logging.getLogger(__name__)

MEALIE_URL = os.environ.get("MEALIE_URL", "").rstrip("/")
MEALIE_API_TOKEN = os.environ.get("MEALIE_API_TOKEN", "")
MCP_SHARED_SECRET = os.environ.get("MCP_SHARED_SECRET", "")


@dataclass
class _Identity:
    email: str
    household_id: str
    client: MealieClient


# Re-resolve identities after this long, so token rotation/revocation in
# mcp_user_tokens lands without a restart.
_IDENTITY_TTL_SECONDS = 600.0


def _db_token_lookup(email: str) -> str | None:
    with get_pool().connection() as conn, conn.cursor() as cur:
        # lower() on the column too — don't let a mixed-case INSERT create a
        # "not provisioned" mystery.
        cur.execute(
            "SELECT mealie_token FROM mcp_user_tokens WHERE lower(email) = %s",
            (email.lower(),),
        )
        row = cur.fetchone()
    return row[0] if row else None


def build_mcp(
    *,
    memory_store: Any | None = None,
    token_lookup: Callable[[str], str | None] | None = None,
) -> FastMCP | None:
    """Build the FastMCP server, or None when MCP is not configured.

    Configured means: a default identity (``MEALIE_API_TOKEN``) and/or the
    shared secret (``MCP_SHARED_SECRET``, implying gateway-forwarded
    multi-user identities) is present. Existing deployments with neither
    keep working unchanged — no /mcp mount.

    ``memory_store`` / ``token_lookup`` override the Postgres-backed
    defaults (tests).
    """
    if not MEALIE_API_TOKEN and not MCP_SHARED_SECRET:
        logger.info("neither MEALIE_API_TOKEN nor MCP_SHARED_SECRET set — MCP not mounted")
        return None

    # Capture config at build time so each built server has fixed semantics
    # (and tests can build differently-configured instances side by side).
    secret = MCP_SHARED_SECRET
    default_token = MEALIE_API_TOKEN
    store = memory_store  # PgMemoryStore is constructed lazily below (needs DSN)
    lookup = token_lookup or _db_token_lookup
    # keyed by lowercased email ("" = default identity); value expires so
    # token rotation in mcp_user_tokens lands without a restart.
    identities: dict[str, tuple[_Identity, float]] = {}

    def _get_store() -> Any:
        nonlocal store
        if store is None:
            store = PgMemoryStore()
        return store

    def _resolve() -> _Identity:
        """Per-request identity: secret check, then X-MCP-User → client.

        All introspection is lazy and cached per email, so a Mealie outage
        at startup self-heals — the first successful call caches forever.
        Raises PermissionError/RuntimeError, which FastMCP surfaces as a
        tool error (callers see why instead of a silent wrong-user result).
        """
        headers = get_http_headers(include={"x-mcp-user", "x-mcp-secret"})
        if secret and not hmac.compare_digest(headers.get("x-mcp-secret", ""), secret):
            raise PermissionError("mealie tools require a valid X-MCP-Secret header")

        email = (headers.get("x-mcp-user") or "").strip().lower()
        if secret and not email:
            # Every CF-authenticated caller has an email, so a secret-bearing
            # request without one means the gateway stopped forwarding
            # identity. Fail loudly — silently serving a default identity
            # would hand one user another user's data.
            raise PermissionError(
                "X-MCP-User header required when MCP_SHARED_SECRET is set — "
                "if this request came through the gateway, identity "
                "forwarding is broken"
            )

        now = time.monotonic()
        cached = identities.get(email)
        if cached and cached[1] > now:
            return cached[0]

        if not email:
            # Only reachable with no secret configured (local dev / scripts).
            if not default_token:
                raise PermissionError(
                    "no X-MCP-User header and no default MEALIE_API_TOKEN configured"
                )
            token = default_token
        else:
            token = lookup(email)
            if token is None:
                raise PermissionError(
                    f"no Mealie token provisioned for {email!r} — "
                    "add a row to mcp_user_tokens"
                )

        context = verify_mealie_jwt(token)
        if context is None:
            raise RuntimeError(
                f"Mealie token introspection failed for {email or 'default identity'!r} "
                "— is Mealie up and the token valid?"
            )
        ident = _Identity(
            email=context.get("email") or context["user_id"],
            household_id=context.get("household_id") or "default",
            client=MealieClient(MEALIE_URL, token),
        )
        identities[email] = (ident, now + _IDENTITY_TTL_SECONDS)
        logger.info(
            "MCP identity resolved: %s (household %s)", ident.email, ident.household_id
        )
        return ident

    class _PerRequestClient:
        """Duck-types MealieClient; every method call resolves the caller's
        identity first, so the shared tool factories stay unchanged."""

        def __getattr__(self, name: str) -> Any:
            return getattr(_resolve().client, name)

    def _with_identity(fn: Any) -> Any:
        """Resolve identity BEFORE the tool body runs.

        The factories' tool bodies swallow exceptions into '(fetch error:
        ...)' strings; resolving up front means auth/identity failures
        surface as clean MCP tool errors instead.
        """

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            _resolve()
            return fn(*args, **kwargs)

        return wrapper

    lazy_client = _PerRequestClient()
    mcp = FastMCP("mealie")

    for t in [
        *recipe_tools(lazy_client),
        *recipe_write_tools(lazy_client),
        *mealplan_tools(lazy_client),
        *shopping_tools(lazy_client),
    ]:
        # Strands' @tool wraps the original callable; __wrapped__ recovers
        # it with signature + type hints intact for FastMCP introspection.
        fn = t.__wrapped__
        name = getattr(t, "tool_name", None) or fn.__name__
        mcp.tool(_with_identity(fn), name=f"mealie_{name}", description=t.__doc__ or fn.__doc__)

    # Memory tools are defined here rather than reusing strands_pg.memory_tools:
    # that factory bakes namespaces in at build time, but here they depend on
    # the per-request identity.
    def _remember(namespace: str, text: str) -> str:
        mid = _get_store().add(text, namespace=namespace)
        # Return the id so the caller can cite or undo what it just wrote.
        return f"Saved note [{mid}]. Use forget_note({mid}) to remove it."

    def _recall(namespace: str, query: str, k: int) -> str:
        hits = _get_store().search(query, k=k, namespace=namespace)
        if not hits:
            return "No matches."
        return "\n".join(f"- [{h.id}] {h.text}" for h in hits)

    def _namespace_for(scope: str, ident: _Identity) -> str:
        """Map the tool's scope argument onto a storage namespace."""
        s = (scope or "").strip().lower()
        if s == "personal":
            return f"user:{ident.email}"
        if s == "household":
            return f"household:{ident.household_id}"
        raise ValueError(f"scope must be 'personal' or 'household', got {scope!r}")

    def _list_notes(namespace: str, limit: int, offset: int) -> str:
        rows = _get_store().list(namespace=namespace, limit=limit, offset=offset)
        if not rows:
            return "No notes."
        lines = []
        for h in rows:
            when = h.created_at.strftime("%Y-%m-%d") if h.created_at else "unknown date"
            lines.append(f"- [{h.id}] ({when}) {h.text}")
        return "\n".join(lines)

    @mcp.tool(name="mealie_remember_personal")
    def remember_personal(text: str) -> str:
        """Save a durable personal note (food preferences, dislikes, goals)."""
        ident = _resolve()
        return _remember(f"user:{ident.email}", text)

    @mcp.tool(name="mealie_recall_personal")
    def recall_personal(query: str, k: int = 5) -> str:
        """Search your durable personal notes by meaning. Returns top-k hits."""
        ident = _resolve()
        return _recall(f"user:{ident.email}", query, k)

    @mcp.tool(name="mealie_remember_household")
    def remember_household(text: str) -> str:
        """Save a durable household note (shared preferences, family rules)."""
        ident = _resolve()
        return _remember(f"household:{ident.household_id}", text)

    @mcp.tool(name="mealie_recall_household")
    def recall_household(query: str, k: int = 5) -> str:
        """Search the household's durable notes by meaning. Returns top-k hits."""
        ident = _resolve()
        return _recall(f"household:{ident.household_id}", query, k)

    @mcp.tool(name="mealie_list_notes")
    def list_notes(scope: str = "household", limit: int = 50, offset: int = 0) -> str:
        """List durable notes in full, newest first, with ids and dates.

        Unlike recall_*, this is exhaustive rather than top-k by
        similarity — it's the only way to audit the store, spot
        duplicates, or find notes worth pruning. Call it before saving
        something you suspect is already there.

        The store is for STANDING facts: allergies, dislikes, household
        rules ('we double every pasta recipe'), preferred store. It is
        NOT for how one meal turned out — that belongs on the recipe via
        append_recipe_note or comment_recipe, where it stays attached to
        the thing it describes. Per-batch notes here crowd out real
        preferences, because recall_* returns top-k and a cluster of
        notes about one dinner will dominate any nearby query.

        Args:
            scope: 'household' (shared) or 'personal' (just you).
            limit: Maximum notes to return (default 50).
            offset: Skip this many for paging through a large store.
        """
        ident = _resolve()
        try:
            ns = _namespace_for(scope, ident)
        except ValueError as exc:
            return f"(error: {exc})"
        return _list_notes(ns, limit, offset)

    @mcp.tool(name="mealie_forget_note")
    def forget_note(note_id: int, scope: str = "household") -> str:
        """Delete one durable note by id. Permanent.

        Get ids from list_notes or recall_*. Deleting drops the note and
        its embedding together, so it stops coming back from recall_*
        immediately.

        The delete is scoped to YOUR notes in the given scope — an id
        belonging to someone else, or in the other scope, reports
        not_found and changes nothing. If you get not_found, re-check
        the id with list_notes rather than guessing.

        Args:
            note_id: Numeric id shown in brackets, e.g. 23 for '[23]'.
            scope: 'household' or 'personal' — must match where the note
                actually lives.
        """
        ident = _resolve()
        try:
            ns = _namespace_for(scope, ident)
        except ValueError as exc:
            return f"(error: {exc})"
        # Namespace-scoped: ids are sequential and visible to every
        # caller, so an unscoped delete would let one user remove
        # another user's notes by guessing an integer.
        removed = _get_store().delete(note_id, namespace=ns)
        if not removed:
            return f"note_id={note_id} status=not_found (no such note in {scope} scope)"
        return f"note_id={note_id} status=deleted"

    return mcp
