#!/usr/bin/env bash
# check_relay.sh — WF-C Relay Preflight
#
# Verifies the Planner Relay is ready to run before every relay invocation.
# Prints PASS/FAIL per check plus an overall PASS/FAIL, with actionable
# diagnostics on any failure. Called automatically by run_planner_relay.sh;
# safe to run standalone at any time.
#
# Checks (in order):
#   1. browser-session container running
#   2. Chromium (noVNC) reachable
#   3. CDP reachable
#   4. expected ChatGPT conversation open (skipped if not configured)
#   5. login still valid
#   6. relay directories exist
#   7. selectors file exists
#   8. required files writable
#
# Exit code: 0 = PASS, 1 = FAIL

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

WORKFLOW_DIR="${WORKFLOW_DIR:-$REPO_ROOT/docs/roadmap/workflow}"
RELAY_DIR="${RELAY_DIR:-$REPO_ROOT/relay}"
CDP_URL="${CDP_URL:-http://localhost:9222}"
NOVNC_URL="${NOVNC_URL:-http://localhost:6080}"
RELAY_EXPECTED_CONVERSATION_URL="${RELAY_EXPECTED_CONVERSATION_URL:-}"
RELAY_VENV_PYTHON="${RELAY_VENV_PYTHON:-$REPO_ROOT/.relay-venv/bin/python}"
SELECTORS_FILE="$SCRIPT_DIR/selectors.yaml"

PASS_COUNT=0
FAIL_COUNT=0

pass() {
    echo "  PASS - $1"
    PASS_COUNT=$((PASS_COUNT + 1))
}
fail() {
    echo "  FAIL - $1"
    [[ -n "${2:-}" ]] && echo "         $2"
    FAIL_COUNT=$((FAIL_COUNT + 1))
}

echo "=== Relay Preflight ==="
echo ""

# ── 1. browser-session container running ────────────────────────────────────
echo "[1/8] browser-session container"
if ! command -v docker &>/dev/null; then
    fail "docker not found on PATH" "Install Docker, or run this preflight from the host that runs the browser-session container."
elif ! docker inspect orchestrator-browser-session &>/dev/null; then
    fail "orchestrator-browser-session container not found" "Start it: docker compose -f docker-compose.browser-session.yml up -d"
else
    running="$(docker inspect -f '{{.State.Running}}' orchestrator-browser-session 2>/dev/null)"
    if [[ "$running" == "true" ]]; then
        pass "container running"
    else
        fail "container exists but is not running" "docker compose -f docker-compose.browser-session.yml up -d"
    fi
fi
echo ""

# ── 2. Chromium (noVNC) reachable ───────────────────────────────────────────
echo "[2/8] Chromium reachable (noVNC)"
if curl -sf -o /dev/null --max-time 5 "$NOVNC_URL/"; then
    pass "noVNC reachable at $NOVNC_URL"
else
    fail "noVNC not reachable at $NOVNC_URL" "Check the container is up and NOVNC_BIND_HOST matches where you are running this check from."
fi
echo ""

# ── 3. CDP reachable ─────────────────────────────────────────────────────────
echo "[3/8] CDP reachable"
CDP_VERSION_JSON="$(curl -sf --max-time 5 "$CDP_URL/json/version" || true)"
if [[ -n "$CDP_VERSION_JSON" ]]; then
    pass "CDP reachable at $CDP_URL"
else
    fail "CDP not reachable at $CDP_URL/json/version" "Check the container is up, Chrome started, and CDP_BIND_HOST matches where you are running this check from."
fi
echo ""

# ── 4 & 5. Expected conversation open + login valid ─────────────────────────
# Single CDP query covers both checks (needs the relay venv for PyYAML).
PY_OUTPUT=""
if [[ -x "$RELAY_VENV_PYTHON" ]]; then
    PY_OUTPUT="$(CDP_URL="$CDP_URL" SELECTORS_FILE="$SELECTORS_FILE" \
        RELAY_EXPECTED_CONVERSATION_URL="$RELAY_EXPECTED_CONVERSATION_URL" \
        "$RELAY_VENV_PYTHON" - <<'PYEOF'
import json
import os
import sys
from urllib.error import URLError
from urllib.request import urlopen

import yaml

cdp_url = os.environ["CDP_URL"]
selectors_file = os.environ["SELECTORS_FILE"]
expected = os.environ.get("RELAY_EXPECTED_CONVERSATION_URL", "").strip()

with open(selectors_file) as f:
    sel = yaml.safe_load(f)

try:
    with urlopen(f"{cdp_url}/json", timeout=5) as resp:
        targets = json.loads(resp.read())
except (URLError, OSError) as exc:
    print("CDP_JSON_OK=0")
    print(f"CDP_JSON_ERROR={exc}")
    sys.exit(0)

print("CDP_JSON_OK=1")
pages = [t for t in targets if t.get("type") == "page"]
print(f"PAGE_COUNT={len(pages)}")

planner_page = next((p for p in pages if "chatgpt.com" in p.get("url", "")), None)
if planner_page is None and pages:
    planner_page = pages[0]

if planner_page is None:
    print("PLANNER_PAGE_FOUND=0")
    sys.exit(0)

print("PLANNER_PAGE_FOUND=1")
url = planner_page.get("url", "")
print(f"PLANNER_URL={url}")

login_patterns = sel.get("login_url_patterns", [])
logged_in = not any(pat in url for pat in login_patterns)
print(f"LOGIN_VALID={1 if logged_in else 0}")

if expected:
    print("CONVERSATION_CONFIGURED=1")
    match = url.rstrip("/") == expected.rstrip("/")
    print(f"CONVERSATION_MATCH={1 if match else 0}")
else:
    print("CONVERSATION_CONFIGURED=0")
PYEOF
)"
fi

field() {
    grep "^$1=" <<<"$PY_OUTPUT" | head -1 | cut -d= -f2-
}

echo "[4/8] Expected ChatGPT conversation open"
if [[ ! -x "$RELAY_VENV_PYTHON" ]]; then
    fail "relay venv not found at $RELAY_VENV_PYTHON" "Create it per docs/roadmap/done/workflow/wf-b-browser-session-runbook.md (playwright + pyyaml)."
elif [[ "$(field CDP_JSON_OK)" != "1" ]]; then
    fail "could not query CDP for open pages" "$(field CDP_JSON_ERROR)"
elif [[ "$(field PLANNER_PAGE_FOUND)" != "1" ]]; then
    fail "no ChatGPT page found in the browser session" "Open https://chatgpt.com in the persistent browser (via noVNC at $NOVNC_URL)."
elif [[ "$(field CONVERSATION_CONFIGURED)" != "1" ]]; then
    pass "not configured (RELAY_EXPECTED_CONVERSATION_URL unset) — skipping pin check"
elif [[ "$(field CONVERSATION_MATCH)" == "1" ]]; then
    pass "conversation URL matches: $(field PLANNER_URL)"
else
    fail "conversation URL mismatch" "Expected: $RELAY_EXPECTED_CONVERSATION_URL | Actual: $(field PLANNER_URL). Open the expected conversation tab yourself; the relay will not switch tabs automatically."
fi
echo ""

echo "[5/8] Login still valid"
if [[ ! -x "$RELAY_VENV_PYTHON" ]]; then
    fail "relay venv not found at $RELAY_VENV_PYTHON" "See check 4 above."
elif [[ "$(field CDP_JSON_OK)" != "1" ]]; then
    fail "could not query CDP for login state" "$(field CDP_JSON_ERROR)"
elif [[ "$(field PLANNER_PAGE_FOUND)" != "1" ]]; then
    fail "no ChatGPT page found in the browser session" "Open https://chatgpt.com in the persistent browser (via noVNC at $NOVNC_URL)."
elif [[ "$(field LOGIN_VALID)" == "1" ]]; then
    pass "logged in ($(field PLANNER_URL))"
else
    fail "not logged in (on a login/auth URL)" "Log in manually via noVNC at $NOVNC_URL, then re-run."
fi
echo ""

# ── 6. relay directories exist ───────────────────────────────────────────────
echo "[6/8] Relay directories exist"
if [[ -d "$RELAY_DIR" && -d "$WORKFLOW_DIR" ]]; then
    pass "$RELAY_DIR and $WORKFLOW_DIR exist"
else
    fail "missing directory" "RELAY_DIR=$RELAY_DIR (exists: $([[ -d "$RELAY_DIR" ]] && echo yes || echo no)), WORKFLOW_DIR=$WORKFLOW_DIR (exists: $([[ -d "$WORKFLOW_DIR" ]] && echo yes || echo no))"
fi
echo ""

# ── 7. selectors file exists ─────────────────────────────────────────────────
echo "[7/8] Selectors file exists"
if [[ -f "$SELECTORS_FILE" ]]; then
    pass "$SELECTORS_FILE"
else
    fail "$SELECTORS_FILE not found" "Restore scripts/relay/selectors.yaml from version control."
fi
echo ""

# ── 8. required files writable ───────────────────────────────────────────────
echo "[8/8] Required files writable"
WRITE_OK=1
for path in "$RELAY_DIR" "$WORKFLOW_DIR"; do
    if [[ ! -w "$path" ]]; then
        fail "$path is not writable"
        WRITE_OK=0
    fi
done
if [[ "$WRITE_OK" == "1" ]]; then
    pass "$RELAY_DIR and $WORKFLOW_DIR are writable"
fi
echo ""

echo "=== Summary: $PASS_COUNT passed, $FAIL_COUNT failed ==="
if [[ "$FAIL_COUNT" -eq 0 ]]; then
    echo "PASS"
    exit 0
else
    echo "FAIL"
    exit 1
fi
