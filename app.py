"""Mealie agent — per-user meal planning assistant for the family Mealie."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from strands import Agent
from strands.models.bedrock import BedrockModel

from strands_pg import (
    PgPromptStore,
    PgSessionManager,
    make_app,
    memory_tools,
)
from tools.auth import verify_mealie_jwt
from tools.mealie_client import MealieClient
from tools.mealplan import mealplan_tools
from tools.recipes import recipe_tools
from tools.shopping import shopping_tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

PROMPT_DIR = Path(__file__).parent / "prompts"
SYSTEM_PROMPT_PARTS = ["soul", "rules", "skills"]
MODEL_ID = os.environ.get("STRANDS_PG_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
MEALIE_URL = os.environ.get("MEALIE_URL", "").rstrip("/")

prompts = PgPromptStore()
prompts.seed_from_dir(PROMPT_DIR)


def _system_prompt_for(context: dict[str, Any]) -> str:
    base = prompts.assemble(SYSTEM_PROMPT_PARTS) or "You are a helpful assistant."
    email = context.get("email") or "unknown"
    household_id = context.get("household_id") or "unknown"
    return (
        f"{base}\n\n"
        f"## USER CONTEXT\n"
        f"You are talking to {email} (user_id={context.get('user_id')}).\n"
        f"Their household is {household_id}.\n"
    )


def build_agent(session_id: str, *, context: dict[str, Any] | None = None) -> Agent:
    """Construct a per-user agent.

    ``context`` comes from the Mealie JWT verifier: email, user_id,
    household_id, token. Tools that hit Mealie use the user's token so
    Mealie enforces RBAC. Memory is partitioned into personal (per-user)
    and household scopes.
    """
    if context is None:
        # Should only happen if someone hits /chat without auth — the app
        # is configured to require it, so this is a guard for typos.
        raise RuntimeError("mealie-agent requires an authenticated context")

    user_token = context["token"]
    email = context.get("email") or context["user_id"]
    household_id = context.get("household_id") or "default"

    user_client = MealieClient(MEALIE_URL, user_token)

    return Agent(
        model=BedrockModel(model_id=MODEL_ID),
        system_prompt=_system_prompt_for(context),
        tools=[
            *recipe_tools(user_client),
            *mealplan_tools(user_client),
            *shopping_tools(user_client),
            *memory_tools(
                namespaces={
                    "personal": f"user:{email}",
                    "household": f"household:{household_id}",
                }
            ),
        ],
        session_manager=PgSessionManager(session_id=session_id),
    )


app = make_app(
    build_agent,
    title="mealie-agent",
    prompt_store=prompts,
    auth_verifier=verify_mealie_jwt,
    cache_agents=False,          # user JWTs rotate — rebuild per request
    deploy=True,
    # GIT_SHA is baked in at build time via docker-compose build-arg from the
    # host's git checkout. Falls back to 'dev' outside of a built image.
    health_info=lambda: {"commit": os.environ.get("GIT_SHA", "dev")},
    health_path="/api/health",
)

# Static chat UI. Served at / (index.html) + /static/* for assets.
from fastapi import Request  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.types import Scope  # noqa: E402

_STATIC_DIR = Path(__file__).parent / "static"


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles that tells the browser never to cache — avoids the
    "I deployed but I still see old code" dance. These files are tiny
    (a few KB); the round-trip cost is cheaper than a support request."""

    async def get_response(self, path: str, scope: Scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-store"
        return resp


app.mount("/static", _NoCacheStaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def _index(request: Request) -> FileResponse:
    return FileResponse(
        str(_STATIC_DIR / "index.html"),
        headers={"Cache-Control": "no-store"},
    )
