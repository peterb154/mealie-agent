"""Mealie JWT auth-verifier.

Given a bearer token, forward it to Mealie's ``/api/users/self`` to
validate. Mealie returns 200 + the user doc if the token is real, else
401. We translate that into the ``(token) -> dict | None`` shape the
strands-pg ``make_app(auth_verifier=...)`` expects.

Returned context:
    session_id     — "user:{id}"  (stable across email changes)
    email          — for display + identity file lookup
    user_id        — Mealie user UUID
    household_id   — used for household-scope memory namespace
    group_id       — parent group UUID (usually 1 group per install)
    token          — the user's raw JWT (tools use it to call Mealie as them)
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

MEALIE_URL = os.environ.get("MEALIE_URL", "").rstrip("/")


def verify_mealie_jwt(token: str) -> dict | None:
    """Validate ``token`` against Mealie's introspection endpoint."""
    if not MEALIE_URL:
        logger.error("MEALIE_URL not set — auth_verifier cannot function")
        return None
    try:
        resp = httpx.get(
            f"{MEALIE_URL}/api/users/self",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        logger.warning("mealie introspection request failed: %s", exc)
        return None
    if resp.status_code != 200:
        return None
    user = resp.json()
    user_id = user.get("id")
    if not user_id:
        return None
    return {
        "session_id": f"user:{user_id}",
        "email": user.get("email"),
        "user_id": user_id,
        "household_id": user.get("householdId") or user.get("household_id"),
        "group_id": user.get("groupId") or user.get("group_id"),
        "token": token,
    }
