"""Shared Postgres connection-pool helper.

A single ``ConnectionPool`` per process is enough for small purpose-built agents.
We resolve DSN from the ``STRANDS_PG_DSN`` env var by default; callers can pass
an explicit DSN to override.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from pgvector.psycopg import register_vector
from psycopg import Connection
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

_DEFAULT_DSN_ENV = "STRANDS_PG_DSN"

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def resolve_dsn(dsn: str | None = None) -> str:
    """Return an explicit DSN, falling back to ``STRANDS_PG_DSN``."""
    if dsn:
        return dsn
    env_dsn = os.environ.get(_DEFAULT_DSN_ENV)
    if not env_dsn:
        raise RuntimeError(
            f"No Postgres DSN provided and {_DEFAULT_DSN_ENV} is not set. "
            "Pass dsn=... or set the env var (e.g. "
            "'postgresql://strands:strands@db:5432/strands')."
        )
    return env_dsn


def _configure_connection(conn: Connection) -> None:
    """Register pgvector's type adapter on every new pool connection.

    pgvector-python registers per-connection, not globally. If we skip this,
    the pool hands out connections where vector params get bound as
    ``double precision[]`` and queries fail with ``operator does not exist:
    vector <=> double precision[]``.
    """
    try:
        register_vector(conn)
    except Exception:  # noqa: BLE001 — extension may not be installed yet on fresh DB
        logger.warning(
            "pgvector adapter not registered; memory queries will fail "
            "until the 'vector' extension is loaded"
        )


def get_pool(dsn: str | None = None, **pool_kwargs: Any) -> ConnectionPool:
    """Return the shared connection pool, creating it lazily on first call."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            resolved = resolve_dsn(dsn)
            _pool = ConnectionPool(
                conninfo=resolved,
                min_size=pool_kwargs.pop("min_size", 1),
                max_size=pool_kwargs.pop("max_size", 10),
                kwargs={"autocommit": False},
                configure=_configure_connection,
                open=True,
                **pool_kwargs,
            )
            logger.info("strands_pg connection pool opened")
    return _pool


def close_pool() -> None:
    """Close the shared pool. Safe to call during shutdown or in tests."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None
