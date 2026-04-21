"""Postgres-backed SessionManager for Strands.

Implements ``SessionRepository`` against Postgres and subclasses
``RepositorySessionManager`` — the same pattern ``FileSessionManager`` and
``S3SessionManager`` use. Messages, agent state, and session metadata are
stored as JSONB so Strands' own ``to_dict``/``from_dict`` can round-trip
without schema churn.

Tables (see ``migrations/001_init.sql``):

- ``sessions(session_id PK, data JSONB, ...)``
- ``session_agents(session_id, agent_id, data JSONB, ...)``
- ``session_messages(session_id, agent_id, message_id, data JSONB, ...)``
"""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg.types.json import Jsonb
from strands.session.repository_session_manager import RepositorySessionManager
from strands.session.session_repository import SessionRepository
from strands.types.exceptions import SessionException
from strands.types.session import Session, SessionAgent, SessionMessage

from strands_pg._pool import get_pool

logger = logging.getLogger(__name__)


class PgSessionManager(RepositorySessionManager, SessionRepository):
    """Postgres-backed session manager for a single Strands agent session."""

    def __init__(
        self,
        session_id: str,
        dsn: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize with a session_id. Pool is created lazily from dsn or env."""
        self._pool = get_pool(dsn)
        super().__init__(session_id=session_id, session_repository=self, **kwargs)

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------

    def create_session(self, session: Session, **kwargs: Any) -> Session:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions (session_id, data)
                VALUES (%s, %s)
                ON CONFLICT (session_id) DO NOTHING
                """,
                (session.session_id, Jsonb(session.to_dict())),
            )
            if cur.rowcount == 0:
                raise SessionException(f"Session {session.session_id} already exists")
            conn.commit()
        return session

    def read_session(self, session_id: str, **kwargs: Any) -> Session | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT data FROM sessions WHERE session_id = %s", (session_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return Session.from_dict(_as_dict(row[0]))

    def delete_session(self, session_id: str, **kwargs: Any) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
            if cur.rowcount == 0:
                raise SessionException(f"Session {session_id} does not exist")
            conn.commit()

    # ------------------------------------------------------------------
    # agents
    # ------------------------------------------------------------------

    def create_agent(
        self, session_id: str, session_agent: SessionAgent, **kwargs: Any
    ) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO session_agents (session_id, agent_id, data)
                VALUES (%s, %s, %s)
                ON CONFLICT (session_id, agent_id) DO UPDATE
                  SET data = EXCLUDED.data,
                      updated_at = now()
                """,
                (session_id, session_agent.agent_id, Jsonb(session_agent.to_dict())),
            )
            conn.commit()

    def read_agent(
        self, session_id: str, agent_id: str, **kwargs: Any
    ) -> SessionAgent | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT data FROM session_agents
                WHERE session_id = %s AND agent_id = %s
                """,
                (session_id, agent_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return SessionAgent.from_dict(_as_dict(row[0]))

    def update_agent(
        self, session_id: str, session_agent: SessionAgent, **kwargs: Any
    ) -> None:
        previous = self.read_agent(session_id=session_id, agent_id=session_agent.agent_id)
        if previous is None:
            raise SessionException(
                f"Agent {session_agent.agent_id} in session {session_id} does not exist"
            )
        session_agent.created_at = previous.created_at
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE session_agents
                   SET data = %s, updated_at = now()
                 WHERE session_id = %s AND agent_id = %s
                """,
                (Jsonb(session_agent.to_dict()), session_id, session_agent.agent_id),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # messages
    # ------------------------------------------------------------------

    def create_message(
        self,
        session_id: str,
        agent_id: str,
        session_message: SessionMessage,
        **kwargs: Any,
    ) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO session_messages
                    (session_id, agent_id, message_id, data)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    session_id,
                    agent_id,
                    session_message.message_id,
                    Jsonb(session_message.to_dict()),
                ),
            )
            conn.commit()

    def read_message(
        self,
        session_id: str,
        agent_id: str,
        message_id: int,
        **kwargs: Any,
    ) -> SessionMessage | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT data FROM session_messages
                WHERE session_id = %s AND agent_id = %s AND message_id = %s
                """,
                (session_id, agent_id, message_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return SessionMessage.from_dict(_as_dict(row[0]))

    def update_message(
        self,
        session_id: str,
        agent_id: str,
        session_message: SessionMessage,
        **kwargs: Any,
    ) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE session_messages
                   SET data = %s, updated_at = now()
                 WHERE session_id = %s AND agent_id = %s AND message_id = %s
                """,
                (
                    Jsonb(session_message.to_dict()),
                    session_id,
                    agent_id,
                    session_message.message_id,
                ),
            )
            if cur.rowcount == 0:
                raise SessionException(
                    f"Message {session_message.message_id} not found "
                    f"(session={session_id}, agent={agent_id})"
                )
            conn.commit()

    def list_messages(
        self,
        session_id: str,
        agent_id: str,
        limit: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> list[SessionMessage]:
        sql = """
            SELECT data FROM session_messages
            WHERE session_id = %s AND agent_id = %s
            ORDER BY message_id ASC
            OFFSET %s
        """
        params: list[Any] = [session_id, agent_id, offset]
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)

        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [SessionMessage.from_dict(_as_dict(r[0])) for r in rows]


def _as_dict(value: Any) -> dict[str, Any]:
    """psycopg returns JSONB as dict; some adapters hand back str. Normalize."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise TypeError(f"unexpected JSONB payload type: {type(value)!r}")
