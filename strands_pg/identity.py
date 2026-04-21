"""Database-backed identity store.

Identities are per-user profile docs — the body gets prepended to an agent's
system prompt so the model has persistent context about who it's talking to
(rig, preferences, location, constraints). Identities live in the DB with a
many-to-one ``email -> user_id`` mapping so a user with several addresses
(work + personal + satellite messenger) resolves to one profile.

First-boot seeding from ``./identities/*.md`` is supported: files may include
YAML-ish frontmatter with ``title``, ``tags``, and ``emails: [a@x, b@y]``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from strands_pg._pool import get_pool

logger = logging.getLogger(__name__)


@dataclass
class Identity:
    """One identity row."""

    user_id: str
    title: str | None
    body: str
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    emails: list[str] = field(default_factory=list)


class PgIdentity:
    """CRUD for identities + email mapping, with file-based seed helper."""

    def __init__(self, dsn: str | None = None) -> None:
        self._pool = get_pool(dsn)

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def get(self, user_id: str) -> Identity | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, title, body, tags, metadata
                FROM identities WHERE user_id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "SELECT email FROM identity_emails WHERE user_id = %s ORDER BY email",
                (user_id,),
            )
            emails = [r[0] for r in cur.fetchall()]
        return Identity(
            user_id=row[0],
            title=row[1],
            body=row[2],
            tags=list(row[3] or []),
            metadata=dict(row[4] or {}),
            emails=emails,
        )

    def get_by_email(self, email: str) -> Identity | None:
        """Resolve an email to its identity. ``None`` if the email is unmapped."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM identity_emails WHERE email = %s",
                (email,),
            )
            row = cur.fetchone()
        return self.get(row[0]) if row else None

    def list(self) -> list[Identity]:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT user_id FROM identities ORDER BY user_id")
            ids = [r[0] for r in cur.fetchall()]
        return [i for i in (self.get(uid) for uid in ids) if i is not None]

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def put(
        self,
        user_id: str,
        body: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        emails: list[str] | None = None,
    ) -> Identity:
        """Upsert an identity and replace its email mappings."""
        tags = list(tags or [])
        metadata = dict(metadata or {})
        emails = list(emails or [])

        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO identities (user_id, title, body, tags, metadata)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                  SET title = EXCLUDED.title,
                      body = EXCLUDED.body,
                      tags = EXCLUDED.tags,
                      metadata = EXCLUDED.metadata,
                      updated_at = now()
                """,
                (user_id, title, body, tags, Jsonb(metadata)),
            )
            cur.execute("DELETE FROM identity_emails WHERE user_id = %s", (user_id,))
            for email in emails:
                cur.execute(
                    "INSERT INTO identity_emails (email, user_id) VALUES (%s, %s) "
                    "ON CONFLICT (email) DO UPDATE SET user_id = EXCLUDED.user_id",
                    (email, user_id),
                )
            conn.commit()

        identity = self.get(user_id)
        assert identity is not None
        return identity

    def delete(self, user_id: str) -> bool:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM identities WHERE user_id = %s", (user_id,))
            conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # seed from disk
    # ------------------------------------------------------------------

    def seed_from_dir(
        self,
        directory: str | Path,
        *,
        overwrite: bool = False,
    ) -> list[str]:
        """Load ``*.md`` files as identities. Returns user_ids written.

        Each file's stem becomes the ``user_id``. Optional YAML-ish frontmatter
        between ``---`` markers is parsed for ``title``, ``tags`` (list), and
        ``emails`` (list).
        """
        path = Path(directory)
        if not path.is_dir():
            return []

        written: list[str] = []
        for md in sorted(path.glob("*.md")):
            user_id = md.stem
            if not overwrite and self.get(user_id) is not None:
                continue
            text = md.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(text)
            self.put(
                user_id=user_id,
                body=body.strip(),
                title=meta.get("title"),
                tags=meta.get("tags", []),
                emails=meta.get("emails", []),
                metadata={
                    k: v for k, v in meta.items() if k not in {"title", "tags", "emails"}
                },
            )
            written.append(user_id)

        if written:
            logger.info("seeded identities from %s: %s", path, ", ".join(written))
        return written


# ---------------------------------------------------------------------------
# Internal: minimal YAML-ish frontmatter parser.
# We only need title/tags/emails plus scalars; full YAML would pull in PyYAML.
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    fm_raw, body = match.group(1), match.group(2)
    meta: dict[str, Any] = {}
    for line in fm_raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [x.strip() for x in value[1:-1].split(",") if x.strip()]
        else:
            meta[key] = value
    return meta, body
