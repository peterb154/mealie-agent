#!/usr/bin/env bash
# Wait for Postgres, apply migrations, then exec the agent command.
set -euo pipefail

: "${STRANDS_PG_DSN:?STRANDS_PG_DSN is required}"
MIGRATIONS_DIR="${STRANDS_PG_MIGRATIONS_DIR:-/app/migrations}"

echo "[entrypoint] waiting for Postgres..."
python - <<'PY'
import os, sys, time
import psycopg

dsn = os.environ["STRANDS_PG_DSN"]
deadline = time.time() + 60
last = None
while time.time() < deadline:
    try:
        with psycopg.connect(dsn, connect_timeout=3) as c:
            c.execute("SELECT 1")
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        last = exc
        time.sleep(1)
print(f"[entrypoint] Postgres not reachable: {last}", file=sys.stderr)
sys.exit(1)
PY

echo "[entrypoint] applying migrations from ${MIGRATIONS_DIR}..."
python -m strands_pg.migrate --dir "${MIGRATIONS_DIR}"

echo "[entrypoint] starting: $*"
exec "$@"
