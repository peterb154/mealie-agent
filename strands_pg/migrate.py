"""Tiny migration runner.

Numbered SQL files in a directory, applied in order. Applied filenames are
tracked in ``schema_migrations``. No rollback, no generators, no ORM. If this
outgrows itself, swap in alembic.

Usage:

    strands-pg-migrate                          # apply from ./migrations
    strands-pg-migrate --dir /app/migrations    # custom path
    STRANDS_PG_DSN=postgres://... strands-pg-migrate
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from psycopg import Connection, connect

from strands_pg._pool import resolve_dsn

logger = logging.getLogger(__name__)

_FILENAME_RE = re.compile(r"^(\d+)_.+\.sql$")


def _applied(conn: Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute("SELECT filename FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def _discover(migrations_dir: Path) -> list[Path]:
    files = [p for p in migrations_dir.iterdir() if p.is_file() and _FILENAME_RE.match(p.name)]
    files.sort(key=lambda p: int(_FILENAME_RE.match(p.name).group(1)))  # type: ignore[union-attr]
    return files


def apply(dsn: str | None = None, migrations_dir: str | Path = "migrations") -> list[str]:
    """Apply any pending SQL files. Returns the list of filenames applied."""
    resolved_dsn = resolve_dsn(dsn)
    path = Path(migrations_dir)
    if not path.is_dir():
        raise FileNotFoundError(f"migrations dir not found: {path.resolve()}")

    applied: list[str] = []
    with connect(resolved_dsn) as conn:
        conn.autocommit = False
        already = _applied(conn)
        conn.commit()

        for f in _discover(path):
            if f.name in already:
                logger.debug("skip %s (already applied)", f.name)
                continue
            sql = f.read_text(encoding="utf-8")
            logger.info("applying %s", f.name)
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)",
                    (f.name,),
                )
            conn.commit()
            applied.append(f.name)

    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply strands_pg SQL migrations.")
    parser.add_argument("--dir", default="migrations", help="migrations directory")
    parser.add_argument("--dsn", default=None, help="Postgres DSN (falls back to STRANDS_PG_DSN)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    applied = apply(dsn=args.dsn, migrations_dir=args.dir)
    if applied:
        print(f"applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("no pending migrations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
