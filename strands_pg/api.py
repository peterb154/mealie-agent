"""FastAPI factory for a strands_pg agent.

Strands binds a ``SessionManager`` to an ``Agent`` at construction time, so we
take a *factory* — ``agent_factory(session_id) -> Agent`` — rather than a
single agent instance. Callers decide whether to cache agents by session_id
or rebuild per request; default behavior caches in-process.
"""

from __future__ import annotations

import inspect
import logging
import os
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from strands.types.exceptions import SessionException

from strands_pg.prompts import PgPromptStore

if TYPE_CHECKING:
    from strands.agent.agent import Agent

logger = logging.getLogger(__name__)

AgentFactory = Callable[..., "Agent"]
AuthVerifier = Callable[[str], dict[str, Any] | None]


def commit_sha(repo_dir: str | Path | None = None, length: int = 7) -> str:
    """Read the current commit SHA from a ``.git`` directory without shelling out.

    Useful for ``/health`` to advertise the deployed revision (so callers like
    n8n can verify a deploy actually landed). Reads ``.git/HEAD`` and chases
    the ref one level — enough for a normal checkout. Returns ``"unknown"`` on
    any failure.

    ``repo_dir``: directory containing ``.git``. Defaults to CWD (the container
    image typically runs in ``/app`` with the repo mounted or copied there).
    """
    base = Path(repo_dir) if repo_dir else Path.cwd()
    git_dir = base / ".git"
    if not git_dir.exists():
        return "unknown"
    try:
        # Worktree layout: .git is a file pointing at gitdir
        if git_dir.is_file():
            content = git_dir.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir: "):
                git_dir = Path(content[len("gitdir: ") :])
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref_path = git_dir / head[len("ref: ") :]
            if ref_path.exists():
                return ref_path.read_text(encoding="utf-8").strip()[:length]
            # Packed refs fallback
            packed = git_dir / "packed-refs"
            if packed.exists():
                target_ref = head[len("ref: ") :]
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.startswith("#") or line.startswith("^"):
                        continue
                    parts = line.split(" ", 1)
                    if len(parts) == 2 and parts[1] == target_ref:
                        return parts[0][:length]
            return "unknown"
        # Detached HEAD
        return head[:length]
    except OSError:
        return "unknown"


def _message_text(message: dict[str, Any]) -> str:
    """Join the ``text`` blocks of a Strands message into plain text.

    A Strands message is ``{"role": ..., "content": [block, ...]}`` where a
    block may be ``{"text": ...}``, ``{"toolUse": ...}``, ``{"toolResult":
    ...}``, etc. We keep only the text blocks — so a tool-use-only assistant
    message or a tool-result-only user message collapses to ``""`` and gets
    filtered out of the rendered transcript.
    """
    parts = [
        block["text"]
        for block in message.get("content", [])
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts).strip()


class ChatRequest(BaseModel):
    # session_id is required only when no auth_verifier is configured.
    # When auth is on, session_id is derived from the verifier's result.
    session_id: str | None = Field(None, min_length=1)
    message: str = Field(..., min_length=1)


class ChatResponse(BaseModel):
    session_id: str
    response: str


class HistoryTurn(BaseModel):
    role: str  # "user" | "assistant"
    text: str


class HistoryResponse(BaseModel):
    session_id: str
    turns: list[HistoryTurn]


class ResetResponse(BaseModel):
    session_id: str
    cleared: bool


class PromptBody(BaseModel):
    body: str = Field(..., min_length=1)


class PromptOut(BaseModel):
    name: str
    body: str


def make_app(
    agent_factory: AgentFactory,
    *,
    cache_agents: bool = True,
    title: str = "strands-pg agent",
    prompt_store: PgPromptStore | None = None,
    deploy: bool = False,
    auth_verifier: AuthVerifier | None = None,
    health_info: Callable[[], dict[str, Any]] | None = None,
    health_path: str = "/health",
) -> FastAPI:
    """Build a FastAPI app exposing /health, /chat, and /prompts endpoints.

    ``prompt_store``: if provided, /prompts endpoints are registered and the
    agent factory is dropped from the cache whenever a prompt changes (so the
    next request builds a fresh agent with the updated prompt). If None, no
    /prompts endpoints are registered.

    ``deploy``: if True, registers a ``POST /api/deploy`` endpoint that
    writes a timestamp to ``$DEPLOY_TRIGGER`` (default
    ``/opt/<agent>/.deploy-trigger``). Pair with the host-side systemd
    units installed by ``bootstrap-lxc.sh`` — a ``.path`` unit watches the
    trigger file and fires a ``.service`` unit that runs ``deploy.sh`` on
    the host. Orchestrating from outside the container is critical: a
    rebuild kills the container, which would kill any in-container
    orchestrator mid-command. Auth via ``DEPLOY_TOKEN`` env var as a
    bearer token.

    ``auth_verifier``: opt-in per-request auth for ``/chat`` and
    ``/chat/stream``. A callable ``(token: str) -> dict | None``:
    - given the token after ``Authorization: Bearer ``
    - returns a context dict on success (MUST include key ``session_id``,
      plus whatever else the agent_factory needs) or ``None`` on failure
    - a 401 response is returned on missing/invalid tokens

    When ``auth_verifier`` is set, ``/chat`` bodies do NOT need to include
    ``session_id`` — it's taken from the verifier's return value. The
    full context dict is passed to ``agent_factory`` as ``context=dict``
    IF the factory's signature accepts it (detected via ``inspect``).

    Typical use: a Mealie/Auth0/OIDC-backed agent. For a Mealie example,
    the verifier introspects the user's JWT via ``GET /api/users/self``
    and returns ``{session_id, email, user_id, group_id, household_id}``
    which build_agent uses for per-household memory namespacing.
    """
    app = FastAPI(title=title)
    agents: dict[str, Any] = {}

    # Introspect once: does agent_factory accept context=? Most camping-db-era
    # factories have signature (session_id: str, extra_prompt: str = ""), which
    # doesn't accept context. Mealie-style factories take context. We pass
    # context only when the factory advertises it.
    _factory_sig = inspect.signature(agent_factory)
    _factory_accepts_context = (
        "context" in _factory_sig.parameters
        or any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in _factory_sig.parameters.values()
        )
    )

    def get_agent(session_id: str, context: dict[str, Any] | None = None) -> Any:
        if cache_agents and session_id in agents:
            return agents[session_id]
        if context is not None and _factory_accepts_context:
            agent = agent_factory(session_id, context=context)
        else:
            agent = agent_factory(session_id)
        if cache_agents:
            agents[session_id] = agent
        return agent

    def invalidate_agents() -> None:
        agents.clear()

    def _authed_context(authorization: str) -> dict[str, Any]:
        """Validate bearer token and return the verifier's context dict.

        Only called when auth_verifier is configured. Raises 401 on any
        failure. The returned dict must carry a ``session_id`` key.
        """
        assert auth_verifier is not None  # noqa: S101 — guarded by caller
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")
        token = authorization[len("Bearer ") :].strip()
        if not token:
            raise HTTPException(status_code=401, detail="Empty bearer token")
        try:
            ctx = auth_verifier(token)
        except Exception as exc:  # noqa: BLE001 — upstream verifier errors → 401
            logger.warning("auth_verifier raised: %s", exc)
            ctx = None
        if not ctx or "session_id" not in ctx:
            raise HTTPException(status_code=401, detail="Invalid token")
        return ctx

    @app.get(health_path)
    def health() -> dict[str, Any]:
        out: dict[str, Any] = {"status": "ok"}
        if health_info is not None:
            try:
                extras = health_info() or {}
                if isinstance(extras, dict):
                    out.update(extras)
            except Exception:  # noqa: BLE001 — health must never fail
                logger.exception("health_info() raised; returning bare status")
        return out

    @app.post("/chat", response_model=ChatResponse)
    def chat(req: ChatRequest, authorization: str = Header(default="")) -> ChatResponse:
        context: dict[str, Any] | None = None
        session_id: str | None = req.session_id

        if auth_verifier is not None:
            context = _authed_context(authorization)
            session_id = context["session_id"]
        elif not session_id:
            raise HTTPException(
                status_code=400, detail="session_id required (no auth_verifier configured)"
            )

        try:
            agent = get_agent(session_id, context=context)
            result = agent(req.message)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — surface as 500 to the client
            logger.exception("chat failed for session_id=%s", session_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return ChatResponse(session_id=session_id, response=str(result))

    @app.post("/chat/stream")
    async def chat_stream(
        req: ChatRequest, authorization: str = Header(default="")
    ) -> EventSourceResponse:
        """Same as /chat but streams events as Server-Sent Events.

        Event shape is normalized across Strands SDK versions:
          - ``event: text``       text delta chunks
          - ``event: thinking``   reasoning-text deltas (when model streams them)
          - ``event: tool_use``   tool name being invoked
          - ``event: done``       terminal event (empty data)
          - ``event: error``      any exception (data = error message)
        """
        context: dict[str, Any] | None = None
        session_id: str | None = req.session_id

        if auth_verifier is not None:
            context = _authed_context(authorization)
            session_id = context["session_id"]
        elif not session_id:
            raise HTTPException(
                status_code=400, detail="session_id required (no auth_verifier configured)"
            )

        return EventSourceResponse(
            _stream_agent(get_agent, session_id, req.message, context),
            headers={
                # nginx (incl. NPM) buffers proxied responses by default,
                # which breaks SSE — events queue up until the buffer fills
                # or the connection closes. This tells nginx to stream.
                "X-Accel-Buffering": "no",
                # Some CDNs/proxies gzip text/* responses; a partial gzip
                # block never flushes. Opt out explicitly.
                "Cache-Control": "no-cache, no-transform",
            },
        )

    def _resolve_session(
        req_session_id: str | None, authorization: str
    ) -> tuple[str, dict[str, Any] | None]:
        """Shared session resolution for the history/reset endpoints.

        Mirrors the /chat logic: with auth on, session_id comes from the
        verifier (the request can't spoof it); without auth, it must be a
        query param.
        """
        if auth_verifier is not None:
            context = _authed_context(authorization)
            return context["session_id"], context
        if not req_session_id:
            raise HTTPException(
                status_code=400, detail="session_id required (no auth_verifier configured)"
            )
        return req_session_id, None

    @app.get("/chat/history", response_model=HistoryResponse)
    def chat_history(
        limit: int = 20,
        session_id: str | None = None,
        authorization: str = Header(default=""),
    ) -> HistoryResponse:
        """Return the recent user/assistant turns for the session.

        ``limit`` caps how many text messages are returned (the UI maps
        "N turns" to ``2*N`` messages). Tool-use / tool-result blocks are
        dropped — only conversational text is replayed into the window.
        """
        limit = max(1, min(limit, 200))
        sid, context = _resolve_session(session_id, authorization)
        try:
            agent = get_agent(sid, context=context)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — surface as 500
            logger.exception("history failed for session_id=%s", sid)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        turns: list[HistoryTurn] = []
        for message in agent.messages:
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _message_text(message)
            if text:
                turns.append(HistoryTurn(role=role, text=text))
        return HistoryResponse(session_id=sid, turns=turns[-limit:])

    @app.post("/chat/reset", response_model=ResetResponse)
    def chat_reset(
        session_id: str | None = None,
        authorization: str = Header(default=""),
    ) -> ResetResponse:
        """Wipe the session's stored conversation — a true fresh start.

        Deletes the session row, which cascades to its agents and messages
        (see migrations/001_init.sql). Per-namespace memory lives in a
        separate table and is intentionally left untouched.
        """
        sid, context = _resolve_session(session_id, authorization)
        try:
            agent = get_agent(sid, context=context)
            manager = getattr(agent, "session_manager", None)
            deleter = getattr(manager, "delete_session", None)
            if deleter is None:
                raise HTTPException(
                    status_code=501, detail="session manager does not support reset"
                )
            try:
                deleter(sid)
            except SessionException:
                # Nothing stored yet (user never chatted) — treat as success.
                logger.info("reset: no session to delete for %s", sid)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — surface as 500
            logger.exception("reset failed for session_id=%s", sid)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        # Evict only this session's cached agent (it holds stale in-memory
        # messages after the DB rows are gone). Don't call invalidate_agents()
        # here — that clears every user's agent, punishing the whole process
        # for one user's reset in cache_agents=True deployments.
        agents.pop(sid, None)
        return ResetResponse(session_id=sid, cleared=True)

    if prompt_store is not None:

        @app.get("/prompts", response_model=list[PromptOut])
        def list_prompts() -> list[PromptOut]:
            return [PromptOut(name=p.name, body=p.body) for p in prompt_store.list()]

        @app.get("/prompts/{name}", response_model=PromptOut)
        def get_prompt(name: str) -> PromptOut:
            p = prompt_store.get(name)
            if p is None:
                raise HTTPException(status_code=404, detail=f"prompt {name!r} not found")
            return PromptOut(name=p.name, body=p.body)

        @app.put("/prompts/{name}", response_model=PromptOut)
        def put_prompt(name: str, req: PromptBody) -> PromptOut:
            p = prompt_store.put(name, req.body)
            invalidate_agents()
            return PromptOut(name=p.name, body=p.body)

        @app.delete("/prompts/{name}")
        def delete_prompt(name: str) -> dict[str, bool]:
            ok = prompt_store.delete(name)
            if not ok:
                raise HTTPException(status_code=404, detail=f"prompt {name!r} not found")
            invalidate_agents()
            return {"deleted": True}

    if deploy:
        _register_deploy_endpoint(app)

    return app


# ---------------------------------------------------------------------------
# deploy endpoint
# ---------------------------------------------------------------------------


def _register_deploy_endpoint(app: FastAPI) -> None:
    """POST /api/deploy — writes a trigger file, returns {"status": "ok"}.

    Orchestration happens on the host via systemd (.path watches the file,
    .service fires deploy.sh). The endpoint itself is ~instant: auth
    check, write timestamp, return. No Popen, no sleep, no race.
    """
    deploy_token = os.environ.get("DEPLOY_TOKEN", "")
    deploy_trigger = os.environ.get("DEPLOY_TRIGGER", "/opt/app/.deploy-trigger")

    @app.post("/api/deploy")
    def deploy(authorization: str = Header(default="")) -> dict[str, str]:
        if not deploy_token:
            raise HTTPException(status_code=503, detail="DEPLOY_TOKEN not configured")
        if authorization != f"Bearer {deploy_token}":
            raise HTTPException(status_code=401, detail="Invalid deploy token")

        try:
            Path(deploy_trigger).write_text(
                f"{datetime.now(UTC).isoformat()}\n", encoding="utf-8"
            )
        except OSError as exc:
            logger.exception("could not write deploy trigger")
            raise HTTPException(
                status_code=500, detail=f"trigger write failed: {exc}"
            ) from exc

        logger.info("deploy trigger written to %s", deploy_trigger)
        # Response shape: n8n-compatible check `$json.status === "ok"`.
        return {"status": "ok", "action": "triggered", "trigger": deploy_trigger}


async def _stream_agent(
    get_agent: Callable[..., Any],
    session_id: str,
    message: str,
    context: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, str]]:
    """Bridge Strands' native stream_async into normalized SSE events.

    Strands emits dict events with keys like ``data``, ``reasoningText``,
    ``current_tool_use``, ``complete``. We collapse those into a stable
    ``{event, data}`` shape so SSE consumers don't break when the SDK's
    internal event shape evolves.
    """
    try:
        agent = get_agent(session_id, context=context)
        seen_tool_ids: set[str] = set()
        async for ev in agent.stream_async(message):
            if "reasoningText" in ev and ev["reasoningText"]:
                yield {"event": "thinking", "data": ev["reasoningText"]}
            elif "current_tool_use" in ev:
                tool = ev["current_tool_use"] or {}
                tool_id = tool.get("toolUseId") or ""
                if tool_id and tool_id not in seen_tool_ids:
                    seen_tool_ids.add(tool_id)
                    yield {"event": "tool_use", "data": tool.get("name", "") or ""}
            elif "data" in ev and ev["data"]:
                yield {"event": "text", "data": ev["data"]}
        yield {"event": "done", "data": ""}
    except Exception as exc:  # noqa: BLE001 — surface via SSE error event
        logger.exception("/chat/stream failed for session_id=%s", session_id)
        yield {"event": "error", "data": str(exc)}
