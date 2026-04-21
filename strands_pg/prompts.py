"""Database-backed prompt store.

Prompts (soul.md, rules.md, any named prompt) live as rows in the ``prompts``
table. Agents read them at build time. They can be updated live via the
``/prompts/{name}`` API — no rebuild, no volume mount, no restart.

The first time an agent boots against a fresh database, ``seed_from_dir()``
copies ``<name>`` entries from ``./prompts/<name>.md`` files on disk. After
that, the DB is the source of truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from strands_pg._pool import get_pool

logger = logging.getLogger(__name__)


@dataclass
class Prompt:
    """One prompt stored in the DB."""

    name: str
    body: str


class PgPromptStore:
    """Read/write named prompts by key."""

    def __init__(self, dsn: str | None = None) -> None:
        self._pool = get_pool(dsn)

    def get(self, name: str) -> Prompt | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT name, body FROM prompts WHERE name = %s", (name,))
            row = cur.fetchone()
        if row is None:
            return None
        return Prompt(name=row[0], body=row[1])

    def put(self, name: str, body: str) -> Prompt:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO prompts (name, body)
                VALUES (%s, %s)
                ON CONFLICT (name) DO UPDATE
                  SET body = EXCLUDED.body,
                      updated_at = now()
                RETURNING name, body
                """,
                (name, body),
            )
            row = cur.fetchone()
            assert row is not None
            conn.commit()
        return Prompt(name=row[0], body=row[1])

    def list(self) -> list[Prompt]:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT name, body FROM prompts ORDER BY name")
            rows = cur.fetchall()
        return [Prompt(name=r[0], body=r[1]) for r in rows]

    def delete(self, name: str) -> bool:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM prompts WHERE name = %s", (name,))
            conn.commit()
            return cur.rowcount > 0

    def seed_from_dir(self, directory: str | Path, *, overwrite: bool = False) -> list[str]:
        """Load every ``*.md`` file in ``directory`` into the prompts table.

        By default only seeds names that don't already exist (DB is the source
        of truth after first boot). Pass ``overwrite=True`` to force. Returns
        the list of names that were written.
        """
        path = Path(directory)
        if not path.is_dir():
            return []

        written: list[str] = []
        for md in sorted(path.glob("*.md")):
            name = md.stem
            body = md.read_text(encoding="utf-8")
            if overwrite or self.get(name) is None:
                self.put(name, body)
                written.append(name)
        if written:
            logger.info("seeded prompts from %s: %s", path, ", ".join(written))
        return written

    def assemble(self, names: list[str], separator: str = "\n\n") -> str:
        """Concatenate named prompts in order. Missing names are skipped."""
        parts: list[str] = []
        for n in names:
            p = self.get(n)
            if p is not None:
                parts.append(p.body)
        return separator.join(parts).strip()
