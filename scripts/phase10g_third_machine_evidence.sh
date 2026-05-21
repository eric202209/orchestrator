#!/usr/bin/env bash
# phase10g_third_machine_evidence.sh
# Hardware evidence capture for the Phase 10G third-machine smoke.
#
# Run from WSL2 after wsl-start.sh --backend-only is up, OR pass
# --launch-backend to start the backend as part of timing.
#
# Required:
#   SMOKE_EMAIL=<email>       login account for the smoke (codex-agent-test@example.com)
#   SMOKE_PASSWORD=<password> login password
#
# Optional:
#   CLONE_START_EPOCH=<N>     `date +%s` captured at git clone time; enables cold-start timing
#   BACKEND_PORT=8080
#   PROJECTS_DIR=<wsl-path>   mirrors WINDOWS_PROJECTS_DIR from .env (e.g. /home/user/projects)
#   ORCHESTRATOR_DIR=<path>   defaults to $HOME/orchestrator

set -euo pipefail

CYAN='\033[0;36m'; GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
step()  { echo -e "\n${CYAN}==> $1${NC}"; }
ok()    { echo -e "    ${GREEN}[OK]${NC} $1"; }
fail()  { echo -e "    ${RED}[FAIL]${NC} $1"; EVIDENCE_FAILURES=$((EVIDENCE_FAILURES + 1)); }
warn()  { echo -e "    ${YELLOW}[WARN]${NC} $1"; }
info()  { echo -e "    $1"; }

# ─── Config ────────────────────────────────────────────────────────────────────
BACKEND_PORT="${BACKEND_PORT:-8080}"
API_BASE="http://localhost:${BACKEND_PORT}/api/v1"
SMOKE_EMAIL="${SMOKE_EMAIL:?Set SMOKE_EMAIL=<login email>}"
SMOKE_PASSWORD="${SMOKE_PASSWORD:?Set SMOKE_PASSWORD=<password>}"
CLONE_START_EPOCH="${CLONE_START_EPOCH:-}"
ORCHESTRATOR_DIR="${ORCHESTRATOR_DIR:-"$HOME/orchestrator"}"
PROJECTS_DIR="${PROJECTS_DIR:-}"
SMOKE_LABEL="Phase 10G Third Machine"
SMOKE_STRING="${SMOKE_LABEL}: Ready"
POLL_INTERVAL=10
SMOKE_TIMEOUT=1800  # 30 minutes
EVIDENCE_FAILURES=0
SCRIPT_START_EPOCH=$(date +%s)
PEAK_RAM_MB=0
PEAK_VRAM_MB=0
RAM_TOTAL_MB=0
MONITOR_PID=""
WINDOWS_HOST=$(ip route | grep default | awk '{print $3}' 2>/dev/null | head -1 || true)
# ──────────────────────────────────────────────────────────────────────────────

# ─── Hardware helpers ──────────────────────────────────────────────────────────
_get_ram_used_mb() {
    free -m 2>/dev/null | awk '/^Mem:/{print $3}' || echo 0
}

_get_ram_total_mb() {
    free -m 2>/dev/null | awk '/^Mem:/{print $2}' || echo 0
}

_get_vram_used_mb() {
    # Try native WSL2 GPU path first, fall back to PowerShell
    if command -v nvidia-smi &>/dev/null; then
        nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 || echo 0
    elif command -v powershell.exe &>/dev/null; then
        powershell.exe -NoProfile -Command \
          "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits" \
          2>/dev/null | tr -d '\r\n' || echo 0
    else
        echo 0
    fi
}

_start_monitor() {
    local tmp_ram tmp_vram
    tmp_ram=$(mktemp)
    tmp_vram=$(mktemp)
    echo 0 > "$tmp_ram"
    echo 0 > "$tmp_vram"
    # Export paths so the subshell can write to them
    MONITOR_RAM_FILE="$tmp_ram"
    MONITOR_VRAM_FILE="$tmp_vram"
    export MONITOR_RAM_FILE MONITOR_VRAM_FILE

    (
        while true; do
            ram=$(_get_ram_used_mb)
            vram=$(_get_vram_used_mb)
            peak_ram=$(cat "$MONITOR_RAM_FILE")
            peak_vram=$(cat "$MONITOR_VRAM_FILE")
            [ "$ram" -gt "$peak_ram" ] 2>/dev/null && echo "$ram" > "$MONITOR_RAM_FILE" || true
            [ "$vram" -gt "$peak_vram" ] 2>/dev/null && echo "$vram" > "$MONITOR_VRAM_FILE" || true
            sleep 5
        done
    ) &
    MONITOR_PID=$!
}

_stop_monitor() {
    [ -n "$MONITOR_PID" ] && kill "$MONITOR_PID" 2>/dev/null || true
    PEAK_RAM_MB=$(cat "${MONITOR_RAM_FILE:-/dev/null}" 2>/dev/null || echo 0)
    PEAK_VRAM_MB=$(cat "${MONITOR_VRAM_FILE:-/dev/null}" 2>/dev/null || echo 0)
    rm -f "${MONITOR_RAM_FILE:-}" "${MONITOR_VRAM_FILE:-}" 2>/dev/null || true
}

trap '_stop_monitor' EXIT
# ──────────────────────────────────────────────────────────────────────────────

# ─── API helpers ───────────────────────────────────────────────────────────────
_api() {
    local method=$1 path=$2 data=${3:-}
    if [ -n "$data" ]; then
        curl -sf -X "$method" "${API_BASE}${path}" \
          -H "Content-Type: application/json" \
          -H "Authorization: Bearer ${TOKEN:-}" \
          -d "$data"
    else
        curl -sf -X "$method" "${API_BASE}${path}" \
          -H "Authorization: Bearer ${TOKEN:-}"
    fi
}

_json() {
    python3 -c "import sys,json; d=json.load(sys.stdin); print(d$1)" 2>/dev/null || echo ""
}

_wait_for_port() {
    local port=$1 label=$2 timeout=$3
    local elapsed=0
    while [ $elapsed -lt $timeout ]; do
        if curl -sf -o /dev/null --connect-timeout 2 "http://localhost:$port" 2>/dev/null; then
            return 0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
        info "  waiting for $label on :$port (${elapsed}s)"
    done
    return 1
}
# ──────────────────────────────────────────────────────────────────────────────

step "Phase 10G Third Machine — Hardware Evidence Capture"
info "$(date '+%Y-%m-%d %H:%M:%S') | start epoch: ${SCRIPT_START_EPOCH}"
RAM_TOTAL_MB=$(_get_ram_total_mb)
info "System RAM: ${RAM_TOTAL_MB} MB total"

# ─── Ollama status ─────────────────────────────────────────────────────────────
step "Ollama status"
OLLAMA_STATUS="unknown"
if command -v ollama &>/dev/null; then
    OLLAMA_BINARY="installed"
else
    OLLAMA_BINARY="not installed"
fi
if [ -n "$WINDOWS_HOST" ] && curl -sf -o /dev/null --connect-timeout 3 \
    "http://${WINDOWS_HOST}:11434/api/tags" 2>/dev/null; then
    OLLAMA_STATUS="reachable (unexpected — third-machine test requires Ollama absent)"
    warn "Ollama is reachable. Third-machine test expects it absent. Uninstall Ollama before this run."
else
    OLLAMA_STATUS="not reachable (expected)"
    ok "Ollama not reachable — WARN/degraded path will be exercised"
fi
info "Ollama binary: ${OLLAMA_BINARY} | API: ${OLLAMA_STATUS}"
# ──────────────────────────────────────────────────────────────────────────────

# ─── Wait for backend ──────────────────────────────────────────────────────────
step "Backend readiness"
if ! _wait_for_port "$BACKEND_PORT" "orchestrator" 120; then
    fail "Backend on :${BACKEND_PORT} did not become reachable within 120s"
    echo "  Start wsl-start.sh --backend-only first, then re-run this script."
    exit 1
fi
BACKEND_READY_EPOCH=$(date +%s)

if [ -n "$CLONE_START_EPOCH" ]; then
    COLD_START_SECONDS=$((BACKEND_READY_EPOCH - CLONE_START_EPOCH))
    ok "Cold start (clone → backend): ${COLD_START_SECONDS}s ($(( COLD_START_SECONDS / 60 ))m $(( COLD_START_SECONDS % 60 ))s)"
else
    warn "CLONE_START_EPOCH not set — cold start time not calculated"
    COLD_START_SECONDS="not measured"
fi
# ──────────────────────────────────────────────────────────────────────────────

# ─── Start hardware monitor ────────────────────────────────────────────────────
_start_monitor
INITIAL_RAM=$(_get_ram_used_mb)
INITIAL_VRAM=$(_get_vram_used_mb)
info "RAM at smoke start: ${INITIAL_RAM} MB | VRAM: ${INITIAL_VRAM} MB"
# ──────────────────────────────────────────────────────────────────────────────

# ─── Setup check ───────────────────────────────────────────────────────────────
step "Setup check (wsl-start.sh --check --backend-only)"
CHECK_RESULT="not run"
if [ -f "${ORCHESTRATOR_DIR}/wsl-start.sh" ]; then
    if ORCHESTRATOR_DIR="${ORCHESTRATOR_DIR}" \
       "${ORCHESTRATOR_DIR}/wsl-start.sh" --check --backend-only 2>&1 | \
       grep -q "0 failure"; then
        CHECK_RESULT="passed"
        ok "Setup check passed"
    else
        CHECK_RESULT="failed"
        fail "Setup check reported failures"
    fi
else
    CHECK_RESULT="skipped — wsl-start.sh not found at ${ORCHESTRATOR_DIR}"
    warn "$CHECK_RESULT"
fi
# ──────────────────────────────────────────────────────────────────────────────

# ─── Backend health ────────────────────────────────────────────────────────────
step "Backend health"
HEALTH_JSON=$(curl -sf "http://localhost:${BACKEND_PORT}/health" || echo '{}')
HEALTH_API=$(echo "$HEALTH_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('checks',{}).get('api','?'))" 2>/dev/null || echo "?")
HEALTH_DB=$(echo "$HEALTH_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('checks',{}).get('database','?'))" 2>/dev/null || echo "?")
HEALTH_REDIS=$(echo "$HEALTH_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('checks',{}).get('redis','?'))" 2>/dev/null || echo "?")
HEALTH_PROFILE=$(echo "$HEALTH_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('details',{}).get('runtime_profile','?'))" 2>/dev/null || echo "?")

if [ "$HEALTH_API" = "ok" ] && [ "$HEALTH_DB" = "ok" ] && [ "$HEALTH_REDIS" = "ok" ]; then
    ok "api=${HEALTH_API} database=${HEALTH_DB} redis=${HEALTH_REDIS} runtime_profile=${HEALTH_PROFILE}"
else
    fail "Health check incomplete: api=${HEALTH_API} database=${HEALTH_DB} redis=${HEALTH_REDIS}"
fi
# ──────────────────────────────────────────────────────────────────────────────

# ─── API docs reachable ─────────────────────────────────────────────────────────
step "API docs"
if curl -sf -o /dev/null "http://localhost:${BACKEND_PORT}/docs"; then
    DOCS_READY_EPOCH=$(date +%s)
    ok "Swagger UI reachable at :${BACKEND_PORT}/docs"
    if [ -n "$CLONE_START_EPOCH" ]; then
        COLD_START_FULL=$((DOCS_READY_EPOCH - CLONE_START_EPOCH))
        ok "Cold start (clone → API docs): ${COLD_START_FULL}s ($(( COLD_START_FULL / 60 ))m $(( COLD_START_FULL % 60 ))s)"
        COLD_START_SECONDS=$COLD_START_FULL
    fi
else
    fail "Swagger UI not reachable at :${BACKEND_PORT}/docs"
fi
# ──────────────────────────────────────────────────────────────────────────────

# ─── Login ──────────────────────────────────────────────────────────────────────
step "Smoke login"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
LOGIN_RESPONSE=$(curl -sf -X POST "${API_BASE}/auth/tokens" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"${SMOKE_EMAIL}\",\"password\":\"${SMOKE_PASSWORD}\"}" || echo '{}')
TOKEN=$(echo "$LOGIN_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || echo "")
if [ -z "$TOKEN" ]; then
    fail "Login failed — check SMOKE_EMAIL / SMOKE_PASSWORD"
    exit 1
fi
ok "Logged in as ${SMOKE_EMAIL}"
# ──────────────────────────────────────────────────────────────────────────────

# ─── Create project ─────────────────────────────────────────────────────────────
step "Create smoke project"
PROJ_NAME="phase-10g-third-machine-smoke-${TIMESTAMP}"
PROJ_RESPONSE=$(curl -sf -X POST "${API_BASE}/projects" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"${PROJ_NAME}\",\"description\":\"Phase 10G third-machine hardware evidence smoke\"}")
PROJECT_ID=$(echo "$PROJ_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
WORKSPACE_PATH=$(echo "$PROJ_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('workspace_path',''))" 2>/dev/null || echo "")
ok "Project id=${PROJECT_ID} workspace=${WORKSPACE_PATH}"
# ──────────────────────────────────────────────────────────────────────────────

# ─── Create session ─────────────────────────────────────────────────────────────
step "Create smoke session"
SESS_RESPONSE=$(curl -sf -X POST "${API_BASE}/sessions" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"${SMOKE_LABEL} smoke ${TIMESTAMP}\",\"project_id\":${PROJECT_ID},\"execution_mode\":\"automatic\"}")
SESSION_ID=$(echo "$SESS_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
ok "Session id=${SESSION_ID}"
# ──────────────────────────────────────────────────────────────────────────────

# ─── Create tasks ───────────────────────────────────────────────────────────────
step "Submit 3-task smoke workload"
T1=$(curl -sf -X POST "${API_BASE}/tasks" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"title\":\"Create README.md\",\"description\":\"Create README.md containing exactly the line: ${SMOKE_STRING}\",\"project_id\":${PROJECT_ID}}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
T2=$(curl -sf -X POST "${API_BASE}/tasks" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"title\":\"Create smoke_status.py\",\"description\":\"Create scripts/smoke_status.py that prints exactly: ${SMOKE_STRING}\",\"project_id\":${PROJECT_ID}}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
T3=$(curl -sf -X POST "${API_BASE}/tasks" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"title\":\"Create test_smoke_status.py\",\"description\":\"Create tests/test_smoke_status.py using unittest that imports scripts.smoke_status and asserts it prints: ${SMOKE_STRING}\",\"project_id\":${PROJECT_ID}}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
ok "Tasks created: ${T1}, ${T2}, ${T3}"

# ─── Start session ──────────────────────────────────────────────────────────────
curl -sf -X POST "${API_BASE}/sessions/${SESSION_ID}/start" \
  -H "Authorization: Bearer ${TOKEN}" > /dev/null
ok "Session ${SESSION_ID} started"
SMOKE_START_EPOCH=$(date +%s)
# ──────────────────────────────────────────────────────────────────────────────

# ─── Poll for completion ────────────────────────────────────────────────────────
step "Polling for task completion (timeout ${SMOKE_TIMEOUT}s)"
DONE_COUNT=0
ELAPSED=0
while [ $ELAPSED -lt $SMOKE_TIMEOUT ]; do
    DONE_COUNT=0
    for tid in $T1 $T2 $T3; do
        status=$(curl -sf "${API_BASE}/tasks/${tid}" \
          -H "Authorization: Bearer ${TOKEN}" \
          | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "?")
        case "$status" in
            done|failed|cancelled) DONE_COUNT=$((DONE_COUNT + 1)) ;;
        esac
    done
    [ $DONE_COUNT -eq 3 ] && break
    sleep $POLL_INTERVAL
    ELAPSED=$((ELAPSED + POLL_INTERVAL))
    info "  [${ELAPSED}s] tasks complete: ${DONE_COUNT}/3"
done

SMOKE_END_EPOCH=$(date +%s)
SMOKE_DURATION=$((SMOKE_END_EPOCH - SMOKE_START_EPOCH))

if [ $DONE_COUNT -eq 3 ]; then
    ok "All 3 tasks reached terminal state in ${SMOKE_DURATION}s"
else
    warn "Timed out after ${SMOKE_TIMEOUT}s with ${DONE_COUNT}/3 tasks complete"
fi
# ──────────────────────────────────────────────────────────────────────────────

# ─── Final task/session state ───────────────────────────────────────────────────
step "Final API state"
SESS_STATUS=$(curl -sf "${API_BASE}/sessions/${SESSION_ID}" \
  -H "Authorization: Bearer ${TOKEN}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'), 'is_active=' + str(d.get('is_active','?')))" 2>/dev/null || echo "?")
info "Session ${SESSION_ID}: ${SESS_STATUS}"

ALL_DONE=true
for tid in $T1 $T2 $T3; do
    task_json=$(curl -sf "${API_BASE}/tasks/${tid}" -H "Authorization: Bearer ${TOKEN}" || echo '{}')
    tstatus=$(echo "$task_json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?')+'/'+d.get('workspace_status','?'))" 2>/dev/null || echo "?")
    info "Task ${tid}: ${tstatus}"
    case "$tstatus" in done*) ;; *) ALL_DONE=false ;; esac
done
# ──────────────────────────────────────────────────────────────────────────────

# ─── WSL artifact verification ──────────────────────────────────────────────────
step "WSL artifact verification"
ARTIFACT_PASS=true
if [ -n "$PROJECTS_DIR" ] && [ -n "$WORKSPACE_PATH" ]; then
    # Map container path (/app/projects/foo) → WSL path ($PROJECTS_DIR/foo)
    WORKSPACE_LEAF="${WORKSPACE_PATH##*/app/projects/}"
    WSL_WORKSPACE="${PROJECTS_DIR}/${WORKSPACE_LEAF}"
    info "Checking: ${WSL_WORKSPACE}"
    for artifact in "README.md" "scripts/smoke_status.py" "tests/test_smoke_status.py"; do
        if [ -f "${WSL_WORKSPACE}/${artifact}" ]; then
            ok "${artifact} exists"
        else
            fail "${artifact} missing in ${WSL_WORKSPACE}"
            ARTIFACT_PASS=false
        fi
    done
    # Verify README content
    if [ -f "${WSL_WORKSPACE}/README.md" ]; then
        if grep -qF "$SMOKE_STRING" "${WSL_WORKSPACE}/README.md"; then
            ok "README.md contains expected string"
        else
            fail "README.md missing expected string: ${SMOKE_STRING}"
            ARTIFACT_PASS=false
        fi
    fi
    # Run smoke_status.py
    if [ -f "${WSL_WORKSPACE}/scripts/smoke_status.py" ]; then
        SCRIPT_OUT=$(python3 "${WSL_WORKSPACE}/scripts/smoke_status.py" 2>/dev/null || echo "")
        if [ "$SCRIPT_OUT" = "$SMOKE_STRING" ]; then
            ok "smoke_status.py output matches"
        else
            fail "smoke_status.py output mismatch: '${SCRIPT_OUT}'"
            ARTIFACT_PASS=false
        fi
    fi
    # Run unittest
    if [ -f "${WSL_WORKSPACE}/tests/test_smoke_status.py" ]; then
        if (cd "$WSL_WORKSPACE" && python3 -m pytest tests/test_smoke_status.py -q 2>/dev/null || \
            python3 -m unittest tests.test_smoke_status 2>/dev/null); then
            ok "unittest passed"
        else
            fail "unittest failed"
            ARTIFACT_PASS=false
        fi
    fi
else
    warn "PROJECTS_DIR not set — skipping file verification"
    warn "Set PROJECTS_DIR to the WSL path matching WINDOWS_PROJECTS_DIR in .env"
    ARTIFACT_PASS="skipped"
fi
# ──────────────────────────────────────────────────────────────────────────────

# ─── Final hardware readings ────────────────────────────────────────────────────
_stop_monitor
FINAL_RAM=$(_get_ram_used_mb)
FINAL_VRAM=$(_get_vram_used_mb)
# ──────────────────────────────────────────────────────────────────────────────

# ─── Evidence summary ───────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN} Phase 10G Third Machine — Evidence Summary${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "Date:              $(date '+%Y-%m-%d')"
echo "Account:           ${SMOKE_EMAIL}"
echo "Project id:        ${PROJECT_ID}"
echo "Session id:        ${SESSION_ID}"
echo "Tasks:             ${T1}, ${T2}, ${T3}"
echo "Workspace (ctner): ${WORKSPACE_PATH}"
echo ""
echo "--- Cold start ---"
echo "Cold start time:   ${COLD_START_SECONDS}s (clone → API docs)"
echo ""
echo "--- Backend ---"
echo "Health:            api=${HEALTH_API} database=${HEALTH_DB} redis=${HEALTH_REDIS}"
echo "Runtime profile:   ${HEALTH_PROFILE}"
echo ""
echo "--- Ollama ---"
echo "Binary:            ${OLLAMA_BINARY}"
echo "API:               ${OLLAMA_STATUS}"
echo ""
echo "--- Hardware (peak during smoke) ---"
echo "System RAM total:  ${RAM_TOTAL_MB} MB"
echo "Peak RAM used:     ${PEAK_RAM_MB} MB"
echo "Initial RAM:       ${INITIAL_RAM} MB"
echo "Final RAM:         ${FINAL_RAM} MB"
echo "Peak VRAM used:    ${PEAK_VRAM_MB} MB  (0 = nvidia-smi not available from WSL)"
echo "Initial VRAM:      ${INITIAL_VRAM} MB"
echo "Final VRAM:        ${FINAL_VRAM} MB"
echo ""
echo "--- Smoke ---"
echo "Setup check:       ${CHECK_RESULT}"
echo "Task duration:     ${SMOKE_DURATION}s"
echo "Tasks terminal:    ${DONE_COUNT}/3"
echo "Session:           ${SESS_STATUS}"
echo "All tasks done:    ${ALL_DONE}"
echo "WSL artifacts:     ${ARTIFACT_PASS}"
echo ""
echo "--- Pass criteria ---"
[ "${COLD_START_SECONDS}" != "not measured" ] && \
  [ "${COLD_START_SECONDS}" -le 1800 ] 2>/dev/null && \
  echo "Cold start ≤30min: PASS (${COLD_START_SECONDS}s)" || \
  echo "Cold start ≤30min: NOT MEASURED (set CLONE_START_EPOCH)"
[ "$HEALTH_API" = "ok" ] && echo "Backend healthy:   PASS" || echo "Backend healthy:   FAIL"
[ "$ALL_DONE" = "true" ] && echo "3 tasks done:      PASS" || echo "3 tasks done:      FAIL"
[ "$ARTIFACT_PASS" = "true" ] && echo "WSL artifacts:     PASS" || echo "WSL artifacts:     ${ARTIFACT_PASS}"
[ "${PEAK_VRAM_MB}" -le 7500 ] 2>/dev/null && \
  echo "VRAM ≤7.5GB:       PASS (${PEAK_VRAM_MB} MB)" || \
  echo "VRAM ≤7.5GB:       ${PEAK_VRAM_MB} MB (0=not measured)"
echo ""
echo "Evidence failures: ${EVIDENCE_FAILURES}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
# ──────────────────────────────────────────────────────────────────────────────
