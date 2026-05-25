#!/usr/bin/env bash
# Compact WSL2 Docker/Ollama start path for Windows laptops.

set -euo pipefail

CYAN='\033[0;36m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
GRAY='\033[0;37m'
NC='\033[0m'

step() { echo -e "\n${CYAN}==> $1${NC}"; }
ok() { echo -e "    ${GREEN}[OK]${NC} $1"; }
warn() { echo -e "    ${YELLOW}[WARN]${NC} $1"; }
info() { echo -e "    ${GRAY}$1${NC}"; }
fail() { echo -e "    ${RED}[FAIL]${NC} $1"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORCHESTRATOR_DIR="${ORCHESTRATOR_DIR:-"$(cd "$SCRIPT_DIR/.." && pwd)"}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.windows.yml}"
BACKEND_PORT="${BACKEND_PORT:-8080}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"

CHECK_ONLY=false
BUILD=false
FORCE_RECREATE=false
NO_FRONTEND=false
SKIP_OLLAMA=false
START_OLLAMA=false
INGEST_KNOWLEDGE=false

for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=true ;;
        --build) BUILD=true ;;
        --force-recreate) FORCE_RECREATE=true ;;
        --no-frontend|--backend-only) NO_FRONTEND=true ;;
        --skip-ollama) SKIP_OLLAMA=true ;;
        --start-ollama) START_OLLAMA=true ;;
        --ingest-knowledge) INGEST_KNOWLEDGE=true ;;
        -h|--help)
            cat <<'EOF'
Usage: ./wsl-start.sh [options]

Compact WSL2 Docker/Ollama mode options:
  --check             validate setup without starting services
  --build             rebuild Docker images before starting
  --force-recreate    recreate Docker containers
  --no-frontend       skip frontend dev server
  --backend-only      same as --no-frontend
  --skip-ollama       skip host Ollama reachability check
  --start-ollama      try to start Windows-host Ollama through PowerShell
  --ingest-knowledge  ingest knowledge/ into the active Docker runtime
EOF
            exit 0
            ;;
        *) fail "Unknown option for Ollama laptop mode: $arg" ;;
    esac
done

if [ "$(id -u)" -eq 0 ] && [ "${ALLOW_ROOT_WSL_START:-}" != "true" ]; then
    fail "Do not run wsl-start.sh as root. Run it as your WSL user."
fi

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

env_file_value() {
    local key="$1"
    local default_value="${2:-}"
    local env_file="$ORCHESTRATOR_DIR/.env"
    local value=""

    if [ -f "$env_file" ]; then
        value="$(grep -E "^[[:space:]]*${key}=" "$env_file" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' || true)"
        value="${value%\"}"
        value="${value#\"}"
        value="${value%\'}"
        value="${value#\'}"
    fi

    if [ -n "$value" ]; then
        printf '%s' "$value"
    else
        printf '%s' "$default_value"
    fi
}

resolve_windows_host() {
    if [ -n "${WINDOWS_HOST:-}" ]; then
        printf '%s' "$WINDOWS_HOST"
        return 0
    fi
    if command_exists getent && getent hosts host.docker.internal >/dev/null 2>&1; then
        getent hosts host.docker.internal | awk '{print $1}' | head -1
        return 0
    fi
    if command_exists ip; then
        ip route | awk '/default/ {print $3; exit}'
        return 0
    fi
    awk '/nameserver/ {print $2; exit}' /etc/resolv.conf 2>/dev/null || true
}

workspace_root_value() {
    local value
    value="$(env_file_value WORKSPACE_ROOT)"
    if [ -n "$value" ]; then
        printf '%s' "$value"
        return 0
    fi
    printf '%s/projects' "$HOME"
}

validate_workspace_root() {
    local workspace_root="$1"
    if [ -z "$workspace_root" ]; then
        fail "WORKSPACE_ROOT is empty. Set it to a WSL ext4 path, for example $HOME/projects."
    fi
    if [[ "$workspace_root" == "~/"* ]]; then
        fail "WORKSPACE_ROOT must be expanded, not '$workspace_root'. Use $HOME/${workspace_root#~/}."
    fi
    if [[ "$workspace_root" != /* ]]; then
        fail "WORKSPACE_ROOT must be absolute, not '$workspace_root'."
    fi
    if [[ "$workspace_root" == /mnt/* || "$workspace_root" == *:* || "$workspace_root" == *\\* ]]; then
        fail "WORKSPACE_ROOT must be a WSL ext4 path, not '$workspace_root'."
    fi
    if [[ "$workspace_root" == /root || "$workspace_root" == /root/* ]]; then
        fail "WORKSPACE_ROOT must not point under /root. Use your WSL user path."
    fi
}

wait_http() {
    local url="$1"
    local label="$2"
    local timeout="${3:-60}"
    local elapsed=0
    while [ "$elapsed" -lt "$timeout" ]; do
        if curl -sS --connect-timeout 3 "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        info "Waiting for $label... (${elapsed}s)"
    done
    return 1
}

wait_frontend() {
    local url="http://localhost:${FRONTEND_PORT}"
    local pid="$1"
    local timeout="${2:-40}"
    local elapsed=0

    while [ "$elapsed" -lt "$timeout" ]; do
        if curl -sS --connect-timeout 2 "$url" >/dev/null 2>&1; then
            return 0
        fi
        if ! kill -0 "$pid" >/dev/null 2>&1; then
            return 2
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        info "Waiting for frontend... (${elapsed}s)"
    done

    return 1
}

ensure_runtime_files() {
    step "Preparing runtime files"
    mkdir -p "$ORCHESTRATOR_DIR/checkpoints" "$ORCHESTRATOR_DIR/logs" "$ORCHESTRATOR_DIR/knowledge" "$ORCHESTRATOR_DIR/data"
    [ -f "$ORCHESTRATOR_DIR/orchestrator.db" ] || : > "$ORCHESTRATOR_DIR/orchestrator.db"
    ok "Runtime files are present"
}

check_ollama() {
    local windows_host="$1"
    if [ "$SKIP_OLLAMA" = true ]; then
        warn "Skipping Ollama check"
        return 0
    fi

    step "Checking Windows-host Ollama"
    info "Windows host: $windows_host"
    for ollama_host in "$windows_host" host.docker.internal localhost 127.0.0.1; do
        if curl -fsS --connect-timeout 3 "http://${ollama_host}:${OLLAMA_PORT}/api/tags" >/dev/null 2>&1; then
            ok "Ollama reachable on http://${ollama_host}:${OLLAMA_PORT}"
            return 0
        fi
    done

    if [ "$START_OLLAMA" = true ] && command_exists powershell.exe; then
        info "Starting Ollama through PowerShell"
        powershell.exe -NoProfile -Command "Start-Process ollama -ArgumentList 'serve' -WindowStyle Hidden" >/dev/null 2>&1 || true
        if wait_http "http://localhost:${OLLAMA_PORT}/api/tags" "Ollama" 20; then
            ok "Ollama started"
            return 0
        fi
    fi

    warn "Ollama is not reachable on ${windows_host}, host.docker.internal, or localhost port ${OLLAMA_PORT}."
    warn "Start Ollama on Windows with OLLAMA_HOST=0.0.0.0, or rerun with --start-ollama."
}

knowledge_ingest_command() {
    echo "docker compose -f $COMPOSE_FILE exec -T orchestrator python scripts/ingest_knowledge.py --source-dir /app --qdrant-url http://qdrant:6333"
}

maybe_ingest_knowledge() {
    local ingest_cmd
    ingest_cmd="$(knowledge_ingest_command)"

    if [ "$INGEST_KNOWLEDGE" = true ]; then
        step "Ingesting knowledge"
        info "$ingest_cmd"
        docker compose -f "$COMPOSE_FILE" exec -T orchestrator \
            python scripts/ingest_knowledge.py \
            --source-dir /app \
            --qdrant-url http://qdrant:6333
        ok "Knowledge ingest completed"
        return 0
    fi

    local readiness
    readiness="$(curl -sf --connect-timeout 5 "http://localhost:${BACKEND_PORT}/api/v1/admin/knowledge-readiness?probe_embedding=false" 2>/dev/null || true)"
    if [[ "$readiness" == *"knowledge_files_exist_but_sqlite_empty"* || "$readiness" == *"knowledge_files_exist_but_qdrant_empty"* ]]; then
        warn "Knowledge files exist, but the active Docker runtime is not fully ingested."
        warn "Run: ./wsl-start.sh --ingest-knowledge --no-frontend"
        info "Equivalent command: $ingest_cmd"
    fi
}

run_preflight_check() {
    local windows_host="$1"
    local workspace_root="$2"
    local failures=0
    local warnings=0

    check_ok() { ok "$1"; }
    check_warn() { warnings=$((warnings + 1)); warn "$1"; }
    check_fail() { failures=$((failures + 1)); echo -e "    ${RED}[FAIL]${NC} $1"; }

    echo -e "\n${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN} WSL Docker/Ollama setup check${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    step "Host tools"
    command_exists curl && check_ok "curl available" || check_fail "curl not found"
    command_exists docker && check_ok "docker available" || check_fail "docker not found"
    if [ "$NO_FRONTEND" = false ]; then
        command_exists pnpm && check_ok "pnpm available" || check_fail "pnpm not found"
    else
        check_ok "frontend skipped"
    fi

    step "Repository"
    [ -f "$ORCHESTRATOR_DIR/.env" ] && check_ok ".env found" || check_fail ".env not found"
    [ -f "$ORCHESTRATOR_DIR/$COMPOSE_FILE" ] && check_ok "$COMPOSE_FILE found" || check_fail "$COMPOSE_FILE not found"

    step "Workspace"
    if validate_workspace_root "$workspace_root" >/dev/null 2>&1; then
        check_ok "WORKSPACE_ROOT=$workspace_root"
    else
        check_fail "WORKSPACE_ROOT must be an absolute WSL ext4 path; found '$workspace_root'"
    fi
    if [ -d "$workspace_root" ]; then
        check_ok "WORKSPACE_ROOT exists"
        [ -w "$workspace_root" ] && check_ok "WORKSPACE_ROOT writable by current user" || check_warn "WORKSPACE_ROOT is not writable by current user"
    else
        check_warn "WORKSPACE_ROOT does not exist yet; startup will create it"
    fi

    step "Runtime profile"
    local runtime_profile agent_backend ollama_model
    runtime_profile="$(env_file_value RUNTIME_PROFILE)"
    agent_backend="$(env_file_value AGENT_BACKEND)"
    ollama_model="$(env_file_value OLLAMA_AGENT_MODEL)"
    [ "$runtime_profile" = "compact_local" ] && check_ok "RUNTIME_PROFILE=compact_local" || check_warn "RUNTIME_PROFILE is '${runtime_profile:-<unset>}'; Phase 10W-post recommends compact_local"
    [ "$agent_backend" = "direct_ollama" ] && check_ok "AGENT_BACKEND=direct_ollama" || check_warn "AGENT_BACKEND is '${agent_backend:-<unset>}'"
    [ -n "$ollama_model" ] && check_ok "OLLAMA_AGENT_MODEL=$ollama_model" || check_warn "OLLAMA_AGENT_MODEL is unset"

    step "Windows host"
    [ -n "$windows_host" ] && check_ok "Windows host resolved to $windows_host" || check_fail "Could not resolve Windows host"
    if [ "$SKIP_OLLAMA" = true ]; then
        check_warn "Ollama check skipped"
    else
        local ollama_reachable=false
        for ollama_host in "$windows_host" host.docker.internal localhost 127.0.0.1; do
            if [ -n "$ollama_host" ] && curl -fsS --connect-timeout 3 "http://${ollama_host}:${OLLAMA_PORT}/api/tags" >/dev/null 2>&1; then
                check_ok "Ollama reachable at http://${ollama_host}:${OLLAMA_PORT}"
                ollama_reachable=true
                break
            fi
        done
        if [ "$ollama_reachable" = false ]; then
            check_warn "Ollama not reachable on ${windows_host}, host.docker.internal, or localhost port ${OLLAMA_PORT}"
        fi
    fi

    step "Docker compose"
    if command_exists docker && [ -f "$ORCHESTRATOR_DIR/$COMPOSE_FILE" ]; then
        (
            cd "$ORCHESTRATOR_DIR"
            WORKSPACE_ROOT="$workspace_root" docker compose -f "$COMPOSE_FILE" config --quiet
        ) >/dev/null 2>&1 && check_ok "compose config valid" || check_fail "compose config failed"
    fi

    echo -e "\n${CYAN}Check complete:${NC} ${failures} failure(s), ${warnings} warning(s)"
    [ "$failures" -eq 0 ] || exit 1
}

WINDOWS_HOST_RESOLVED="$(resolve_windows_host)"
WORKSPACE_ROOT_VALUE="$(workspace_root_value)"

if [ "$CHECK_ONLY" = true ]; then
    run_preflight_check "$WINDOWS_HOST_RESOLVED" "$WORKSPACE_ROOT_VALUE"
    exit 0
fi

[ -f "$ORCHESTRATOR_DIR/.env" ] || fail ".env not found at $ORCHESTRATOR_DIR/.env"
[ -f "$ORCHESTRATOR_DIR/$COMPOSE_FILE" ] || fail "$COMPOSE_FILE not found at $ORCHESTRATOR_DIR/$COMPOSE_FILE"
[ -n "$WINDOWS_HOST_RESOLVED" ] || fail "Could not determine Windows host IP. Set WINDOWS_HOST manually."

validate_workspace_root "$WORKSPACE_ROOT_VALUE"
ensure_runtime_files
mkdir -p "$WORKSPACE_ROOT_VALUE"

if [ ! -w "$WORKSPACE_ROOT_VALUE" ]; then
    warn "WORKSPACE_ROOT is not writable by current user: $WORKSPACE_ROOT_VALUE"
    warn "Containers run as root, but host-side project inspection may be awkward."
fi

check_ollama "$WINDOWS_HOST_RESOLVED"

step "Starting Docker backend"
cd "$ORCHESTRATOR_DIR"
export WORKSPACE_ROOT="$WORKSPACE_ROOT_VALUE"
docker_args=(compose -f "$COMPOSE_FILE" up -d)
[ "$BUILD" = true ] && docker_args+=(--build)
[ "$FORCE_RECREATE" = true ] && docker_args+=(--force-recreate)
info "docker ${docker_args[*]}"
docker "${docker_args[@]}"

if wait_http "http://localhost:${BACKEND_PORT}/health" "orchestrator backend" 90; then
    ok "Backend ready on port $BACKEND_PORT"
else
    fail "Backend did not respond within 90s. Run: docker compose -f $COMPOSE_FILE logs"
fi

maybe_ingest_knowledge

if [ "$NO_FRONTEND" = false ]; then
    step "Starting frontend"
    cd "$ORCHESTRATOR_DIR/frontend"
    if [ -x ./node_modules/.bin/vite ]; then
        nohup env VITE_API_URL="http://localhost:${BACKEND_PORT}/api/v1" ./node_modules/.bin/vite --host 0.0.0.0 \
            > "$ORCHESTRATOR_DIR/logs/frontend.log" 2>&1 &
        frontend_pid="$!"
        echo "$frontend_pid" > "$ORCHESTRATOR_DIR/logs/frontend.pid"
        if wait_frontend "$frontend_pid" 40; then
            ok "Frontend ready on port $FRONTEND_PORT"
        else
            frontend_status=$?
            if [ "$frontend_status" -eq 2 ]; then
                warn "Frontend exited before it became reachable."
                tail -20 "$ORCHESTRATOR_DIR/logs/frontend.log" 2>/dev/null || true
            else
                warn "Frontend may still be starting. Check logs/frontend.log."
            fi
        fi
    elif ! command_exists pnpm; then
        warn "pnpm is not installed; skipping frontend"
    elif curl -fsS --connect-timeout 2 "http://localhost:${FRONTEND_PORT}" >/dev/null 2>&1; then
        ok "Frontend already reachable on port $FRONTEND_PORT"
    else
        nohup env VITE_API_URL="http://localhost:${BACKEND_PORT}/api/v1" pnpm dev \
            > "$ORCHESTRATOR_DIR/logs/frontend.log" 2>&1 &
        frontend_pid="$!"
        echo "$frontend_pid" > "$ORCHESTRATOR_DIR/logs/frontend.pid"
        if wait_frontend "$frontend_pid" 40; then
            ok "Frontend ready on port $FRONTEND_PORT"
        else
            frontend_status=$?
            if [ "$frontend_status" -eq 2 ]; then
                warn "Frontend exited before it became reachable."
                tail -20 "$ORCHESTRATOR_DIR/logs/frontend.log" 2>/dev/null || true
            else
                warn "Frontend may still be starting. Check logs/frontend.log."
            fi
        fi
    fi
fi

echo -e "\n${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN} Stack is up${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo "  Backend API : http://localhost:${BACKEND_PORT}/health"
echo "  API docs    : http://localhost:${BACKEND_PORT}/docs"
echo "  Qdrant      : http://localhost:6333/dashboard"
echo "  Ollama      : http://${WINDOWS_HOST_RESOLVED}:${OLLAMA_PORT}/api/tags"
if [ "$NO_FRONTEND" = false ]; then
    echo "  Frontend    : http://localhost:${FRONTEND_PORT}"
fi
echo ""
echo "  Stop backend : docker compose -f $COMPOSE_FILE down"
if [ "$NO_FRONTEND" = false ]; then
    echo "  Stop frontend: kill \$(cat logs/frontend.pid)"
fi
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
