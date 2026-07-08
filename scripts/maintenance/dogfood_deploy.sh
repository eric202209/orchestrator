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
# If a Docker-based deployment is later adopted (docker-compose.windows.yml),
# this script's stop/start steps should be swapped for
# `docker compose -f docker-compose.windows.yml build && ... up -d`; the
# smoke-check step is deployment-mode-agnostic and does not need to change.
#
# Usage: scripts/maintenance/dogfood_deploy.sh [--skip-smoke]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

SKIP_SMOKE=false
for arg in "$@"; do
    case "$arg" in
        --skip-smoke) SKIP_SMOKE=true ;;
        *) echo "Unknown argument: $arg" >&2; exit 2 ;;
    esac
done

echo "=== Dogfood Deploy: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "Repo: $REPO_ROOT"
echo "Commit: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

if ! git diff --quiet 2>/dev/null; then
    echo "WARNING: working tree has uncommitted changes; deployed code may not match any commit." >&2
fi

echo "--- Stopping existing processes (non-interactive, deterministic) ---"
./stop_all.sh

echo "--- Starting stack (non-interactive) ---"
# start.sh checks `[ -t 0 ]` to decide whether to prompt for restart; piping
# stdin from /dev/null forces the non-interactive branch, and since
# stop_all.sh already cleared every process, there is nothing left for that
# branch to ask about.
./start.sh < /dev/null

if [ "$SKIP_SMOKE" = true ]; then
    echo "Skipping smoke check (--skip-smoke)."
    exit 0
fi

echo "--- Smoke check ---"
# start.sh already waits for services in its own startup sequence; a short
# extra grace period covers uvicorn's first-request import cost.
sleep 3

# /api/v1/ops/* requires an authenticated admin user (get_current_admin_user).
# Phase 21C found this the hard way: an unauthenticated curl gets a 401, which
# `curl -f` turns into an empty body, which this script used to misreport as
# "did not respond" on an otherwise-healthy deploy. Use the project's
# standing eval@local.dev live-verification identity
# (docs/roadmap/workflow/development-workflow.md) via the same in-process
# token-generation pattern scripts/maintenance/phase10k_p2_live_pilot_runner.py
# already uses, rather than a password login flow.
EVAL_TOKEN="$(PYTHONPATH="$REPO_ROOT" venv/bin/python3 -c "
from app.auth import create_access_token
print(create_access_token(data={'sub': 'eval@local.dev'}))
" 2>/dev/null || true)"

if [ -z "$EVAL_TOKEN" ]; then
    echo "FAIL: could not generate an eval@local.dev token (is the venv/DB reachable?)." >&2
    exit 1
fi

HEALTH_JSON="$(curl -fsS -H "Authorization: Bearer $EVAL_TOKEN" http://localhost:8080/api/v1/ops/health || true)"
if [ -z "$HEALTH_JSON" ]; then
    echo "FAIL: /api/v1/ops/health did not respond." >&2
    exit 1
fi
echo "$HEALTH_JSON" | python3 -m json.tool || echo "$HEALTH_JSON"

BUILD_JSON="$(curl -fsS -H "Authorization: Bearer $EVAL_TOKEN" http://localhost:8080/api/v1/ops/build-identity || true)"
if [ -z "$BUILD_JSON" ]; then
    echo "FAIL: /api/v1/ops/build-identity did not respond." >&2
    exit 1
fi
echo "$BUILD_JSON" | python3 -m json.tool || echo "$BUILD_JSON"

echo "=== Deploy complete ==="
