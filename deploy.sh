#!/usr/bin/env bash
# deploy.sh — runs on the LXC HOST, not inside the container.
#
# Triggered by <agent>-deploy.service (installed by bootstrap-lxc.sh),
# which is in turn triggered by <agent>-deploy.path watching
# .deploy-trigger. The agent container's /api/deploy endpoint (enabled
# via `make_app(deploy=True)`) writes to that trigger file; this script
# handles the git pull + rebuild on the host.
#
# Because this runs on the host, it's not killed when docker recreates
# the agent container — that's the whole reason for the architecture.

set -euo pipefail

REPO_DIR="${DEPLOY_REPO_DIR:-$(dirname "$(readlink -f "$0")")}"
BRANCH="${DEPLOY_BRANCH:-main}"

cd "$REPO_DIR"

echo "[deploy] $(date -u +%Y-%m-%dT%H:%M:%SZ) triggered by $(cat .deploy-trigger 2>/dev/null | head -1)"
echo "[deploy] pulling $BRANCH..."
git fetch origin "$BRANCH"
git reset --hard "origin/$BRANCH"

echo "[deploy] rebuilding and restarting stack..."
# Export GIT_SHA so docker-compose's build-arg picks it up; bakes it into
# the image for /api/health to report.
export GIT_SHA="$(git rev-parse --short HEAD)"
docker compose up -d --build

echo "[deploy] done at $GIT_SHA"
