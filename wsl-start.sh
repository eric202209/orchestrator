#!/usr/bin/env bash
# wsl-start.sh
# Windows llama.cpp AI Stack — Start Script
# Run from WSL2 Ubuntu: ./wsl-start.sh
# Options:
#   --check           validate setup without starting services
#   --no-frontend     skip frontend dev server
#   --backend-only    same as --no-frontend
#   --skip-ollama     skip Ollama check (if already running)

set -euo pipefail

CYAN='\033[0;36m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
GRAY='\033[0;37m'
NC='\033[0m'

step()  { echo -e "\n${CYAN}==> $1${NC}"; }
ok()    { echo -e "    ${GREEN}[OK]${NC} $1"; }
fail()  { echo -e "    ${RED}[FAIL]${NC} $1"; exit 1; }
warn()  { echo -e "    ${YELLOW}[WARN]${NC} $1"; }
info()  { echo -e "    ${GRAY}$1${NC}"; }

# ─── Configuration ────────────────────────────────────────────────────────────
# All values below can be overridden by environment variables before running:
#   LLAMA_EXE_WIN=/mnt/d/AI/llama.cpp/llama-server.exe ./wsl-start.sh
#   LLAMA_MODEL_PATH="D:\\AI\\models\\qwen-7b.gguf" LLAMA_CTX=4096 ./wsl-start.sh
LLAMA_EXE="${LLAMA_EXE:-"E:\\AI\\llama.cpp\\llama-server.exe"}"
LLAMA_EXE_WIN="${LLAMA_EXE_WIN:-"/mnt/e/AI/llama.cpp/llama-server.exe"}"
# Windows-style path passed to llama-server.exe (override with LLAMA_MODEL_PATH)
MODEL_PATH="${LLAMA_MODEL_PATH:-"E:\\AI\\models\\Qwen\\Qwen2.5-Coder-14B-Instruct-Q5_K_M.gguf"}"
LLAMA_PORT="${LLAMA_PORT:-8001}"
# Context window. Default 6144 suits 14B on 16GB+ VRAM.
# For 8GB VRAM, set LLAMA_CTX=4096 before running.
LLAMA_CTX="${LLAMA_CTX:-6144}"
# Batch: 1024 avoids GPU stall long enough to freeze display during prompt processing
LLAMA_BATCH="${LLAMA_BATCH:-1024}"
LLAMA_UBATCH="${LLAMA_UBATCH:-512}"
# Threads: override with LLAMA_THREADS / LLAMA_THREADS_BATCH for your CPU
LLAMA_THREADS="${LLAMA_THREADS:-4}"
LLAMA_THREADS_BATCH="${LLAMA_THREADS_BATCH:-8}"
OLLAMA_PORT=11434
ORCHESTRATOR_DIR="${ORCHESTRATOR_DIR:-"$HOME/orchestrator"}"
EXPECTED_RUNTIME_PROFILE="${EXPECTED_RUNTIME_PROFILE:-low_resource}"
EXPECTED_OLLAMA_ABSENT="${EXPECTED_OLLAMA_ABSENT:-true}"
COMPOSE_FILE="docker-compose.windows.yml"
BACKEND_PORT=8080
FRONTEND_PORT=3000
# Resolve Windows host IP from WSL2 default gateway.
# Falls back to /etc/resolv.conf nameserver (also reliable in WSL2).
# If both fail, exits early — all Windows-side services depend on this address.
WINDOWS_HOST=$(ip route | grep default | awk '{print $3}' 2>/dev/null | head -1)
if [ -z "$WINDOWS_HOST" ]; then
    WINDOWS_HOST=$(grep -m1 nameserver /etc/resolv.conf 2>/dev/null | awk '{print $2}')
fi
if [ -z "$WINDOWS_HOST" ]; then
    echo -e "    ${RED}[FAIL]${NC} Cannot determine Windows host IP (tried ip route and /etc/resolv.conf)."
    echo    "    Set WINDOWS_HOST manually: export WINDOWS_HOST=<your-windows-ip>"
    exit 1
fi
# ──────────────────────────────────────────────────────────────────────────────

# ─── Flags ────────────────────────────────────────────────────────────────────
CHECK_ONLY=false
NO_FRONTEND=false
SKIP_OLLAMA=false
for arg in "$@"; do
    case $arg in
        --check) CHECK_ONLY=true ;;
        --no-frontend|--backend-only) NO_FRONTEND=true ;;
        --skip-ollama) SKIP_OLLAMA=true ;;
        *) fail "Unknown option: $arg" ;;
    esac
done
# ──────────────────────────────────────────────────────────────────────────────

wait_port() {
    local port=$1
    local label=$2
    local timeout=${3:-30}
    local host=${4:-localhost}
    local elapsed=0
    while [ $elapsed -lt $timeout ]; do
        if curl -s -o /dev/null --connect-timeout 2 "http://${host}:$port" 2>/dev/null; then
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        info "Waiting for $label on port $port... (${elapsed}s)"
    done
    return 1
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

env_value() {
    local key=$1
    local env_file="$ORCHESTRATOR_DIR/.env"
    [ -f "$env_file" ] || return 0
    grep -E "^${key}=" "$env_file" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' || true
}

check_failures=0
check_warnings=0

check_ok() {
    ok "$1"
}

check_warn() {
    check_warnings=$((check_warnings + 1))
    warn "$1"
}

check_fail() {
    check_failures=$((check_failures + 1))
    echo -e "    ${RED}[FAIL]${NC} $1"
}

check_command() {
    local cmd=$1
    local label=${2:-$1}
    if command_exists "$cmd"; then
        check_ok "$label available"
    else
        check_fail "$label not found"
    fi
}

check_env_equals() {
    local key=$1
    local expected=$2
    local actual
    actual=$(env_value "$key")
    if [ "$actual" = "$expected" ]; then
        check_ok "$key=$expected"
    else
        check_fail "$key expected '$expected' but found '${actual:-<unset>}'"
    fi
}

check_env_nonempty() {
    local key=$1
    local actual
    actual=$(env_value "$key")
    if [ -n "$actual" ]; then
        check_ok "$key set"
    else
        check_fail "$key is unset"
    fi
}

check_env_true() {
    local key=$1
    local actual
    actual=$(env_value "$key")
    case "$actual" in
        True|true|TRUE|1|yes|YES)
            check_ok "$key=$actual"
            ;;
        *)
            check_fail "$key expected True but found '${actual:-<unset>}'"
            ;;
    esac
}

run_preflight_check() {
    echo -e "\n${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN} Windows llama.cpp setup check${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    step "Host tools"
    check_command curl curl
    check_command docker docker
    check_command powershell.exe PowerShell
    if [ "$NO_FRONTEND" = false ]; then
        check_command pnpm pnpm
    else
        check_ok "frontend skipped"
    fi

    step "Windows host"
    check_ok "Windows host resolved to ${WINDOWS_HOST}"
    if curl -sf -o /dev/null --connect-timeout 3 "http://${WINDOWS_HOST}:${OLLAMA_PORT}/api/tags" 2>/dev/null; then
        if [ "$EXPECTED_OLLAMA_ABSENT" = true ]; then
            check_fail "Ollama is reachable; third-machine no-Ollama test expects it absent"
        else
            check_ok "Ollama reachable"
        fi
    else
        if [ "$EXPECTED_OLLAMA_ABSENT" = true ]; then
            check_ok "Ollama not reachable; WARN/degraded path will be exercised"
        else
            check_warn "Ollama not reachable"
        fi
    fi
    if curl -sf -o /dev/null --connect-timeout 3 "http://${WINDOWS_HOST}:${LLAMA_PORT}/v1/models" 2>/dev/null; then
        check_ok "llama-server reachable on ${WINDOWS_HOST}:${LLAMA_PORT}"
    else
        check_warn "llama-server is not currently reachable; startup will attempt to launch it"
    fi

    step "llama-server launch inputs"
    if [ -f "$LLAMA_EXE_WIN" ]; then
        check_ok "llama-server.exe found at $LLAMA_EXE_WIN"
    else
        check_fail "llama-server.exe not found at $LLAMA_EXE_WIN"
    fi
    if [[ "$LLAMA_CTX" =~ ^[0-9]+$ ]] && [ "$LLAMA_CTX" -le 4096 ]; then
        check_ok "LLAMA_CTX=$LLAMA_CTX"
    elif [[ ! "$LLAMA_CTX" =~ ^[0-9]+$ ]]; then
        check_fail "LLAMA_CTX must be numeric, found '$LLAMA_CTX'"
    else
        check_warn "LLAMA_CTX=$LLAMA_CTX; use 4096 for 8 GB VRAM third-machine runs"
    fi
    check_ok "LLAMA_MODEL_PATH=$MODEL_PATH"

    step ".env"
    if [ ! -f "$ORCHESTRATOR_DIR/.env" ]; then
        check_fail ".env not found at $ORCHESTRATOR_DIR/.env"
    else
        check_ok ".env found at $ORCHESTRATOR_DIR/.env"
        check_env_equals AGENT_BACKEND openai_responses_api
        check_env_equals OPENAI_BASE_URL "http://host.docker.internal:8001/v1"
        check_env_nonempty OPENAI_API_KEY
        check_env_equals AGENT_MODEL local
        check_env_true PLANNING_REPAIR_ENABLED
        check_env_equals PLANNING_REPAIR_BASE_URL "http://host.docker.internal:8001/v1"
        check_env_equals PLANNING_REPAIR_MODEL local
        check_env_equals EMBEDDING_PROVIDER ollama
        check_env_equals RUNTIME_PROFILE "$EXPECTED_RUNTIME_PROFILE"

        local projects_dir
        projects_dir=$(env_value WINDOWS_PROJECTS_DIR)
        if [ -z "$projects_dir" ]; then
            check_fail "WINDOWS_PROJECTS_DIR is unset"
        elif [[ "$projects_dir" == /mnt/* || "$projects_dir" == *:* || "$projects_dir" == *\\* ]]; then
            check_fail "WINDOWS_PROJECTS_DIR must be a WSL2 ext4 path, not '$projects_dir'"
        elif [[ "$projects_dir" == /* ]]; then
            check_ok "WINDOWS_PROJECTS_DIR=$projects_dir"
            if [ -d "$projects_dir" ]; then
                check_ok "WINDOWS_PROJECTS_DIR exists"
            else
                check_warn "WINDOWS_PROJECTS_DIR does not exist yet; startup will create it"
            fi
        else
            check_fail "WINDOWS_PROJECTS_DIR must be absolute: $projects_dir"
        fi
    fi

    step "Docker compose"
    if command_exists docker && [ -f "$ORCHESTRATOR_DIR/.env" ]; then
        local projects_dir
        projects_dir=$(env_value WINDOWS_PROJECTS_DIR)
        if [ -n "$projects_dir" ]; then
            (
                cd "$ORCHESTRATOR_DIR"
                WINDOWS_PROJECTS_DIR="$projects_dir" docker compose -f "$COMPOSE_FILE" config --quiet
            ) >/dev/null 2>&1 && check_ok "docker compose config valid" || check_fail "docker compose config failed"
        fi
    fi

    step "Running services"
    local health_url="http://localhost:${BACKEND_PORT}/health"
    local docs_url="http://localhost:${BACKEND_PORT}/docs"
    local health_payload
    health_payload=$(curl -sf --connect-timeout 3 "$health_url" 2>/dev/null || true)
    if [ -z "$health_payload" ]; then
        check_warn "Backend health not reachable at $health_url; this is expected before startup"
    else
        check_ok "Backend health reachable"
        if [[ "$health_payload" == *'"status":"healthy"'* || "$health_payload" == *'"status": "healthy"'* ]]; then
            check_ok "Backend status healthy"
        else
            check_fail "Backend status is not healthy: $health_payload"
        fi
        for check_name in api database redis; do
            if [[ "$health_payload" == *"\"$check_name\":\"ok\""* || "$health_payload" == *"\"$check_name\": \"ok\""* ]]; then
                check_ok "Health check $check_name=ok"
            else
                check_fail "Health check $check_name is not ok: $health_payload"
            fi
        done
        if [[ "$health_payload" == *"\"runtime_profile\":\"$EXPECTED_RUNTIME_PROFILE\""* || "$health_payload" == *"\"runtime_profile\": \"$EXPECTED_RUNTIME_PROFILE\""* ]]; then
            check_ok "Backend runtime_profile=$EXPECTED_RUNTIME_PROFILE"
        else
            check_fail "Backend runtime_profile is not $EXPECTED_RUNTIME_PROFILE: $health_payload"
        fi
    fi
    if curl -sf -o /dev/null --connect-timeout 3 "$docs_url" 2>/dev/null; then
        check_ok "API docs reachable"
    else
        check_warn "API docs not reachable at $docs_url; this is expected before startup"
    fi

    echo -e "\n${CYAN}Check complete:${NC} ${check_failures} failure(s), ${check_warnings} warning(s)"
    if [ "$check_failures" -gt 0 ]; then
        exit 1
    fi
}

if [ "$CHECK_ONLY" = true ]; then
    run_preflight_check
    exit 0
fi

# ─── 1. Ollama ────────────────────────────────────────────────────────────────
# Ollama provides embeddings for Qdrant / the knowledge layer.
# If it is not available the backend and dashboard still launch — semantic
# search and knowledge retrieval will be degraded until Ollama is running.
OLLAMA_OK=false
if [ "$SKIP_OLLAMA" = true ]; then
    OLLAMA_OK=true
else
    step "Checking Ollama (embeddings for Qdrant / knowledge layer)"
    info "Windows host resolved to: ${WINDOWS_HOST}"
    # -f makes curl exit non-zero on HTTP 4xx/5xx so a bad response isn't treated as success
    if curl -sf -o /dev/null --connect-timeout 3 "http://${WINDOWS_HOST}:${OLLAMA_PORT}/api/tags" 2>/dev/null; then
        ok "Ollama already running"
        OLLAMA_OK=true
    else
        info "Starting Ollama via PowerShell..."
        powershell.exe -Command "Start-Process ollama -ArgumentList 'serve' -WindowStyle Hidden" 2>/dev/null || true
        sleep 4
        if curl -sf -o /dev/null --connect-timeout 5 "http://${WINDOWS_HOST}:${OLLAMA_PORT}/api/tags" 2>/dev/null; then
            ok "Ollama started"
            OLLAMA_OK=true
        else
            # NOTE: Ollama not detected — embeddings / knowledge layer will be unavailable.
            # The backend and frontend dashboard will still start normally.
            # To enable semantic search, run 'ollama serve' in PowerShell and restart.
            warn "Ollama not detected on ${WINDOWS_HOST}:${OLLAMA_PORT} — continuing without it."
            warn "Qdrant embeddings and knowledge retrieval will be degraded."
            warn "To fix: run 'ollama serve' in PowerShell, then restart this script."
        fi
    fi
fi

# ─── 2. llama-server ──────────────────────────────────────────────────────────
step "Checking llama-server (Vulkan)"
if curl -sf -o /dev/null --connect-timeout 3 "http://${WINDOWS_HOST}:${LLAMA_PORT}/v1/models" 2>/dev/null; then
    ok "llama-server already running on port $LLAMA_PORT"
else
    if [ ! -f "$LLAMA_EXE_WIN" ]; then
        fail "llama-server.exe not found at $LLAMA_EXE_WIN"
    fi

    info "Starting llama-server (model load takes 20-60 seconds)..."
    powershell.exe -Command "
        Start-Process -FilePath '$LLAMA_EXE' \`
            -ArgumentList '-m \"$MODEL_PATH\" --host 0.0.0.0 --port $LLAMA_PORT -ngl 99 -c $LLAMA_CTX -b $LLAMA_BATCH -ub $LLAMA_UBATCH --threads $LLAMA_THREADS --threads-batch $LLAMA_THREADS_BATCH -ctk q8_0 -ctv q8_0 --flash-attn --jinja' \`
            -WindowStyle Normal
    " 2>/dev/null &

    if wait_port $LLAMA_PORT "llama-server" 90 "$WINDOWS_HOST"; then
        ok "llama-server ready on port $LLAMA_PORT"
    else
        fail "llama-server did not start within 90s. Check the llama-server window for errors."
    fi
fi

# ─── 3. Docker backend ────────────────────────────────────────────────────────
step "Starting Docker backend"

[ -f "$ORCHESTRATOR_DIR/.env" ] || fail ".env not found at $ORCHESTRATOR_DIR/.env"

PROJECTS_DIR=$(grep "^WINDOWS_PROJECTS_DIR=" "$ORCHESTRATOR_DIR/.env" | cut -d= -f2)
PROJECTS_DIR=${PROJECTS_DIR:-"$HOME/projects"}
mkdir -p "$PROJECTS_DIR"

cd "$ORCHESTRATOR_DIR"
export WINDOWS_PROJECTS_DIR="$PROJECTS_DIR"
docker compose -f "$COMPOSE_FILE" up -d 2>&1 | tail -5

if wait_port $BACKEND_PORT "orchestrator backend" 60; then
    ok "Backend ready on port $BACKEND_PORT"
else
    fail "Backend did not become healthy within 60s. Run: docker compose -f $COMPOSE_FILE logs"
fi

# ─── 4. Frontend ──────────────────────────────────────────────────────────────
if [ "$NO_FRONTEND" = false ]; then
    step "Starting frontend"
    cd "$ORCHESTRATOR_DIR/frontend"
    nohup env VITE_API_URL="http://localhost:${BACKEND_PORT}/api/v1" pnpm dev \
        > /tmp/frontend.log 2>&1 &
    echo $! > /tmp/frontend.pid
    sleep 5
    if wait_port $FRONTEND_PORT "frontend" 30; then
        ok "Frontend ready on port $FRONTEND_PORT"
    else
        warn "Frontend may still be starting. Check http://localhost:$FRONTEND_PORT in a moment."
        info "Logs: tail -f /tmp/frontend.log"
    fi
fi

# ─── Summary ──────────────────────────────────────────────────────────────────
echo -e "\n${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN} Stack is up${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo    "  llama-server : http://${WINDOWS_HOST}:${LLAMA_PORT}/v1/models"
if [ "$OLLAMA_OK" = true ]; then
    echo "  Ollama       : http://${WINDOWS_HOST}:${OLLAMA_PORT}/api/tags"
else
    echo -e "  Ollama       : ${YELLOW}NOT RUNNING${NC} — embeddings/knowledge layer degraded"
fi
echo    "  Backend API  : http://localhost:${BACKEND_PORT}/health"
echo    "  API docs     : http://localhost:${BACKEND_PORT}/docs"
if [ "$NO_FRONTEND" = false ]; then
    echo "  Dashboard    : http://localhost:${FRONTEND_PORT}"
fi
echo ""
echo    "  Stop backend : cd ~/orchestrator && docker compose -f $COMPOSE_FILE down"
echo    "  Stop llama   : Close the llama-server window in Windows"
if [ "$NO_FRONTEND" = false ]; then
    echo "  Stop frontend: kill \$(cat /tmp/frontend.pid)"
fi
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
