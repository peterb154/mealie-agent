# my-agent

A strands-pg agent. Stamped from
[strands-pgsql-agent-framework](https://github.com/peterb154/strands-pgsql-agent-framework)
— you own every file in this directory.

## Quickstart

```bash
cp .env.example .env
# edit AWS_PROFILE to one with Bedrock access (SSO-backed is fine)

docker compose up --build
# agent on :8000, Postgres on :5432 (internal)

# in another shell:
curl -s localhost:8000/health
curl -s -X POST localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"session_id":"you@example.com","message":"hello"}'
```

## First time on a fresh host?

If you're deploying to a fresh Debian/Ubuntu LXC or VPS that doesn't yet have
Docker installed, run `bootstrap-lxc.sh` first. It's idempotent — safe to
re-run. Installs Docker + compose, sets log rotation, wires a systemd unit
that auto-starts any `/opt/*/docker-compose.yml` stacks on reboot.

```bash
# one-time, as root:
bash bootstrap-lxc.sh
```

If your host already has Docker configured the way you want, skip this and
go straight to Quickstart. The script is yours to edit — add host-level
setup specific to your agent (tuning, extra packages, etc.) in place and
commit it alongside the rest.

## What's here

- **`app.py`** — where you build your Strands `Agent`. This is the main
  extension point; read it top to bottom.
- **`strands_pg/`** — vendored framework code. You own it. Bugfix upstream?
  Patch it here, send a PR if you feel like it.
- **`migrations/`** — framework migrations `001-099`, plus your own `100+`.
  Applied in order on boot by `python -m strands_pg.migrate`.
- **`tools/`** — where your domain tools go. Strands `@tool`-decorated
  functions. Import them in `app.py` and add to `tools=[...]`.
- **`prompts/`** — `soul.md` + `rules.md` get seeded into the DB on first
  boot. Edit there or live via `PUT /prompts/{name}`.
- **`Dockerfile`**, **`docker-compose.yml`**, **`entrypoint.sh`** — your
  container stack. Edit freely.
- **`db/Dockerfile`** — Postgres 17 + pgvector + PostGIS.

## Adding a domain tool

```python
# tools/orders.py
from strands import tool
from strands_pg._pool import get_pool

@tool
def search_orders(customer: str) -> str:
    """Look up orders for a given customer."""
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, total FROM orders WHERE customer = %s", (customer,))
        return "\n".join(f"#{r[0]}: ${r[1]}" for r in cur.fetchall())
```

```python
# app.py
from tools.orders import search_orders

# in build_agent:
tools=[search_orders, *memory_tools(namespace=session_id)],
```

## Adding a domain table

Add `migrations/100_orders.sql` (framework uses 001-099, agents start at 100):

```sql
CREATE TABLE IF NOT EXISTS orders (
    id       BIGSERIAL PRIMARY KEY,
    customer TEXT NOT NULL,
    total    NUMERIC NOT NULL,
    ...
);
```

`docker compose up --build` reapplies migrations on boot.

## Deploying to production

For a real deployment (Proxmox LXC, VPS, anything without your laptop's
`~/.aws`), use a dedicated IAM user with least privilege. Don't reuse your
admin credentials. Don't ship a profile-based config.

### 1. IAM user + least-privilege policy

Create a fresh IAM user named after this agent (e.g. `strands-pg-my-agent`)
and attach a policy like this. Scope it narrowly — just the models you
actually invoke, nothing else:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeClaudeAndTitan",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2:0",
        "arn:aws:bedrock:us-east-1:<ACCOUNT_ID>:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
      ]
    }
  ]
}
```

Two notes:

- `STRANDS_PG_MODEL_ID=us.anthropic...` is a Bedrock **inference profile**,
  not a raw foundation model. Invoking it needs permissions on **both** the
  profile ARN *and* the foundation-model ARNs the profile routes to. The
  example above grants both. Leave out the FM ARNs and you'll get
  `AccessDeniedException` at runtime.
- Titan v2 embeddings go through the foundation-model ARN directly (no
  inference profile). Keep that line even if you only chat with Claude.

Generate an access key pair for the user. Save the secret somewhere safe —
you can't view it again after this screen.

### 2. Put the keys in `.env` on the host

```bash
STRANDS_PG_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=AKIAxxxxxxxxxxxxxxxx
AWS_SECRET_ACCESS_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### 3. Drop the `~/.aws` mount from `docker-compose.yml`

The bind mount fails to start the container on a host that doesn't have
`~/.aws`. Delete this block from the `agent` service:

```yaml
    volumes:
      - ${HOME}/.aws:/root/.aws
```

### 4. Rotate

Rotate the access key on a schedule — quarterly minimum. The IAM user
has no other permissions, so the blast radius of a leak is limited to
Bedrock usage on the specific models above (an attacker can burn your
budget, not much else). Still, rotate.

## Updating from upstream

Re-run the installer into a scratch directory and `diff -r` against this one:

```bash
bash install.sh /tmp/fresh-stamp --ref v0.2.0
diff -r /tmp/fresh-stamp .
```

Pick what you want, patch what you don't. The code is yours.
