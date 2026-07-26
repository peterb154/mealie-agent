"""Semantic memory store backed by pgvector.

One row = one remembered fact. ``namespace`` partitions memory per-user /
per-topic inside a single agent (e.g. email address, user id).

Embeddings are computed by a caller-supplied callable — typically Bedrock
Titan / Cohere, or a local Ollama. Defaults to a Bedrock Titan v2 embedder
(1024 dims) when boto3 is available; fall back to passing your own.

Phase-2 option: swap this out for pgai-vectorizer so the DB manages embedding
sync via triggers. Left as an exercise — wire whichever embedder suits the
agent.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from strands_pg._pool import get_pool

logger = logging.getLogger(__name__)

Embedder = Callable[[str], list[float]]


@dataclass
class MemoryHit:
    """One result from a memory search."""

    id: int
    namespace: str
    text: str
    metadata: dict[str, Any]
    distance: float  # cosine distance in [0, 2]; lower = closer
    created_at: datetime | None = None  # populated by list(); None from search()


class PgMemoryStore:
    """Add/search/delete semantic memories."""

    def __init__(
        self,
        embedder: Embedder | None = None,
        dsn: str | None = None,
        default_namespace: str = "default",
    ) -> None:
        self._pool = get_pool(dsn)
        self._embedder = embedder or _default_embedder()
        self._default_namespace = default_namespace

    def add(
        self,
        text: str,
        metadata: dict[str, Any] | None = None,
        namespace: str | None = None,
    ) -> int:
        """Insert a memory, computing its embedding. Returns the new row id."""
        ns = namespace or self._default_namespace
        embedding = self._embedder(text)

        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memories (namespace, text, metadata, embedding)
                VALUES (%s, %s, %s, %s::vector)
                RETURNING id
                """,
                (ns, text, Jsonb(metadata or {}), embedding),
            )
            row = cur.fetchone()
            assert row is not None
            conn.commit()
            return int(row[0])

    def search(
        self,
        query: str,
        k: int = 5,
        namespace: str | None = None,
    ) -> list[MemoryHit]:
        """KNN search by cosine distance."""
        ns = namespace or self._default_namespace
        query_vec = self._embedder(query)

        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, namespace, text, metadata,
                       embedding <=> %s::vector AS distance
                FROM memories
                WHERE namespace = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (query_vec, ns, query_vec, k),
            )
            rows = cur.fetchall()

        return [
            MemoryHit(
                id=int(r[0]),
                namespace=r[1],
                text=r[2],
                metadata=r[3] if isinstance(r[3], dict) else {},
                distance=float(r[4]),
            )
            for r in rows
        ]

    def update(self, memory_id: int, text: str, *, namespace: str) -> bool:
        """Replace a memory's text in place. Returns True if a row changed.

        Preserves ``id`` and ``created_at`` — that's the whole point.
        Delete-then-re-add loses both, restamping an old standing fact as
        created today, and isn't atomic: a failure between the two calls
        destroys the note with no rollback.

        The embedding is recomputed in the same statement as the text. If
        those ever move apart, ``search`` keeps matching the OLD wording
        while returning the NEW text, which is the quiet kind of wrong.

        ``namespace`` is required and takes no ``None``: it is purely an
        authorization filter here (``id`` is already unique), so there is
        no sensible unscoped update. A mismatch changes nothing and
        returns False — surface that as not-found rather than
        wrong-owner, which would leak that the row exists.
        """
        embedding = self._embedder(text)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE memories
                SET text = %s, embedding = %s::vector
                WHERE id = %s AND namespace = %s
                """,
                (text, embedding, memory_id, namespace),
            )
            conn.commit()
            return cur.rowcount > 0

    def delete(self, memory_id: int, namespace: str | None = None) -> bool:
        """Delete by id. Returns True if a row was removed.

        ALWAYS pass ``namespace`` in a multi-tenant deployment. Ids are
        small sequential integers and are routinely shown to callers
        (search results carry them), so an unscoped delete lets anyone
        who can guess an integer remove another tenant's memories.
        Scoping turns that into a no-op that returns False.

        ``namespace=None`` deletes by id alone, which is only safe when
        the whole store belongs to one tenant.
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            if namespace is None:
                cur.execute("DELETE FROM memories WHERE id = %s", (memory_id,))
            else:
                cur.execute(
                    "DELETE FROM memories WHERE id = %s AND namespace = %s",
                    (memory_id, namespace),
                )
            conn.commit()
            return cur.rowcount > 0

    def list(
        self,
        namespace: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryHit]:
        """Most-recent-first list. Convenience; not embedded.

        Unlike ``search``, this is exhaustive within the namespace — it's
        the primitive for auditing, deduping, and pruning a store, none
        of which a top-k semantic query can do.
        """
        ns = namespace or self._default_namespace
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, namespace, text, metadata, created_at
                FROM memories
                WHERE namespace = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                (ns, limit, offset),
            )
            rows = cur.fetchall()
        return [
            MemoryHit(
                id=int(r[0]),
                namespace=r[1],
                text=r[2],
                metadata=r[3] if isinstance(r[3], dict) else {},
                distance=0.0,
                created_at=r[4],
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Default embedder
# ---------------------------------------------------------------------------


def _default_embedder() -> Embedder:
    """Pick a sensible default: Bedrock Titan v2 if configured, else raise."""
    model_id = os.environ.get("STRANDS_PG_EMBED_MODEL", "amazon.titan-embed-text-v2:0")
    provider = os.environ.get("STRANDS_PG_EMBED_PROVIDER", "bedrock")

    if provider == "bedrock":
        return _bedrock_embedder(model_id)
    raise RuntimeError(
        f"Unknown STRANDS_PG_EMBED_PROVIDER={provider!r}. "
        "Pass embedder=... explicitly or set provider to 'bedrock'."
    )


def _bedrock_embedder(model_id: str) -> Embedder:
    """Bedrock embedding via boto3. Titan v2 returns 1024-dim by default.

    Client is created lazily on first embed() call so app import doesn't
    require AWS creds just to boot /health.
    """
    client_holder: dict[str, Any] = {}

    def embed(text: str) -> list[float]:
        import json

        if "client" not in client_holder:
            try:
                import boto3
            except ImportError as exc:
                raise RuntimeError(
                    "boto3 is required for the default Bedrock embedder. "
                    "Install with `pip install strands-pg[bedrock]` or "
                    "pass embedder=... yourself."
                ) from exc
            region = os.environ.get("AWS_REGION", "us-east-1")
            client_holder["client"] = boto3.client("bedrock-runtime", region_name=region)

        body = json.dumps({"inputText": text})
        resp = client_holder["client"].invoke_model(modelId=model_id, body=body)
        payload = json.loads(resp["body"].read())
        return list(payload["embedding"])

    return embed
