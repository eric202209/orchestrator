#!/bin/bash
# Phase 21B (B5): one-command deploy/rebuild loop for the dogfood window.
#
# This environment runs the orchestrator natively (uvicorn/celery/vite via
# start.sh), not via Docker — verified 2026-07-08: no `docker` binary, no
# running containers, only native venv processes. So "rebuild" here means
# what start.sh's own bootstrap already does (ensure_venv/ensure_frontend_deps
# reinstall changed deps) plus a clean, deterministic, NON-INTERACTIVE
# stop -> start -> smoke-check cycle, replacing the standing "remember to
# restart" reminder in system-state.md with a single command.
#
# Docker/WSL profiles remain development-only and are not interchangeable
# with this native OpenClaw dogfood contract.
#
# Usage: scripts/maintenance/dogfood_deploy.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

if [ "$#" -ne 0 ]; then
    echo "Usage: scripts/maintenance/dogfood_deploy.sh" >&2
    echo "The dogfood admission gate cannot be skipped." >&2
    exit 2
fi

if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
    echo "FAIL: dogfood deployment requires a clean committed worktree." >&2
    git status --short >&2
    exit 1
fi

DEPLOY_SHA="$(git rev-parse HEAD)"
DEPLOY_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [ -f .env ]; then
    DEPLOY_CONFIG_SHA256="$(sha256sum .env | awk '{print $1}')"
else
    DEPLOY_CONFIG_SHA256="$(printf '%s' '.env:absent' | sha256sum | awk '{print $1}')"
fi

export ORCHESTRATOR_GIT_SHA="${DEPLOY_SHA}"
export ORCHESTRATOR_REPO_GIT_SHA="${DEPLOY_SHA}"
export ORCHESTRATOR_BUILD_TIME="${DEPLOY_BUILD_TIME}"
export ORCHESTRATOR_CONFIG_SHA256="${DEPLOY_CONFIG_SHA256}"
export ORCHESTRATOR_CONFIG_SOURCE="native-dogfood:.env"

echo "=== Dogfood Deploy: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "Repo: $REPO_ROOT"
echo "Commit: ${DEPLOY_SHA}"
echo "Build time: ${DEPLOY_BUILD_TIME}"
echo "Configuration SHA-256: ${DEPLOY_CONFIG_SHA256}"

echo "--- Stopping existing processes (non-interactive, deterministic) ---"
./stop_all.sh

echo "--- Synchronizing revision dependencies ---"
if [ ! -x venv/bin/pip ]; then
    python3 -m venv venv
fi
venv/bin/pip install -r requirements.txt
(
    cd frontend
    npm ci --legacy-peer-deps --include=optional
)

echo "--- Starting stack (non-interactive) ---"
# start.sh checks `[ -t 0 ]` to decide whether to prompt for restart; piping
# stdin from /dev/null forces the non-interactive branch, and since
# stop_all.sh already cleared every process, there is nothing left for that
# branch to ask about.
./start.sh < /dev/null

echo "--- Deployment admission gate ---"
PYTHONPATH="${REPO_ROOT}" venv/bin/python3 \
    scripts/maintenance/dogfood_admission.py \
    --expected-sha "${DEPLOY_SHA}" \
    --expected-build-time "${DEPLOY_BUILD_TIME}" \
    --expected-config-sha256 "${DEPLOY_CONFIG_SHA256}"

echo "=== Deploy complete ==="
