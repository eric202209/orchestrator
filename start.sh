#!/bin/bash

# Orchestrator Network - Full Startup Script
# This script starts all components in the correct order.

set -euo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]}"
SCRIPT_DIR="${SCRIPT_PATH%/*}"
if [[ "${SCRIPT_DIR}" == "${SCRIPT_PATH}" ]]; then
    SCRIPT_DIR="."
fi
SCRIPT_DIR="$(CDPATH= cd -P -- "${SCRIPT_DIR}" && pwd -P)"
PROJECT_ROOT="${SCRIPT_DIR}"
FRONTEND_DIR="${PROJECT_ROOT}/frontend"
VENV_DIR="${PROJECT_ROOT}/venv"
LOG_DIR="${PROJECT_ROOT}/logs"
PID_DIR="${PROJECT_ROOT}/run"
PROCESS_HELPER="${PROJECT_ROOT}/scripts/maintenance/project_process.py"
QDRANT_HOME="${PROJECT_ROOT}/qdrant"
QDRANT_BIN_DIR="${QDRANT_HOME}/bin"
QDRANT_DATA_DIR="${QDRANT_HOME}/data"
QDRANT_SNAPSHOTS_DIR="${QDRANT_HOME}/snapshots"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Localhost alias (from .env, default to localhost)
LOCALHOST=${LOCALHOST:-localhost}
BACKEND_HOST=${BACKEND_HOST:-0.0.0.0}
BACKEND_PORT=${BACKEND_PORT:-8080}

load_env() {
    local env_file="${PROJECT_ROOT}/.env"
    [ -f "${env_file}" ] || return 0

    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        [[ -n "${line}" ]] || continue
        [[ "${line}" =~ ^[[:space:]]*# ]] && continue
        [[ "${line}" == *=* ]] || continue

        local key="${line%%=*}"
        local value="${line#*=}"

        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"

        case "${key}" in
            ORCHESTRATOR_GIT_SHA|ORCHESTRATOR_REPO_GIT_SHA|ORCHESTRATOR_BUILD_TIME|ORCHESTRATOR_CONFIG_SHA256|ORCHESTRATOR_CONFIG_SOURCE)
                # The canonical deploy wrapper locks these values before
                # startup. A stale .env identity must not override them.
                if [[ -v "${key}" ]]; then
                    continue
                fi
                ;;
        esac
        export "${key}=${value}"
    done < "${env_file}"
}

prepare_logs() {
    mkdir -p "${LOG_DIR}"
    mkdir -p "${PID_DIR}"
    mkdir -p "${PROJECT_ROOT}/checkpoints"
    mkdir -p "${QDRANT_BIN_DIR}"
    mkdir -p "${QDRANT_DATA_DIR}"
    mkdir -p "${QDRANT_SNAPSHOTS_DIR}"
    : > "${LOG_DIR}/backend.log"
    : > "${LOG_DIR}/worker.log"
    : > "${LOG_DIR}/beat.log"
    : > "${LOG_DIR}/frontend.log"
    : > "${LOG_DIR}/qdrant.log"
    : > "${LOG_DIR}/redis.log"
}

normalize_runtime_ownership() {
    local owner_uid
    local owner_gid

    owner_uid="$(stat -c '%u' "${PROJECT_ROOT}" 2>/dev/null || true)"
    owner_gid="$(stat -c '%g' "${PROJECT_ROOT}" 2>/dev/null || true)"
    [ -n "${owner_uid}" ] && [ -n "${owner_gid}" ] || return 0

    chown -R "${owner_uid}:${owner_gid}" \
        "${PROJECT_ROOT}/checkpoints" \
        "${PID_DIR}" \
        "${LOG_DIR}" \
        2>/dev/null || true
}

process_helper() {
    /usr/bin/python3 "${PROCESS_HELPER}" "$@"
}

record_service_process() {
    local service="$1"
    local pid="$2"
    process_helper record --root "${PROJECT_ROOT}" --service "${service}" --pid "${pid}"
    printf '%s\n' "${pid}" > "${PID_DIR}/${service}.pid"
}

require_port_available() {
    local host="$1"
    local port="$2"
    process_helper port-check --host "${host}" --port "${port}"
}

owned_processes_running() {
    local service result
    for service in backend worker beat frontend qdrant redis; do
        if process_helper status --root "${PROJECT_ROOT}" --service "${service}"; then
            return 0
        else
            result=$?
            if [ "${result}" -ne 1 ]; then
                return "${result}"
            fi
        fi
    done
    return 1
}

require_no_owned_service() {
    local service="$1"
    local message="${2:-Verified project-owned ${service} process is already running.}"
    if process_helper status --root "${PROJECT_ROOT}" --service "${service}"; then
        echo -e "${RED}❌ ${message}${NC}" >&2
        return 1
    else
        local result=$?
        [ "${result}" -eq 1 ] && return 0
        return "${result}"
    fi
}

ensure_venv() {
    echo -e "${BLUE}🔧 Checking virtual environment...${NC}"
    cd "${PROJECT_ROOT}"
    if [ ! -x "${VENV_DIR}/bin/python3" ]; then
        python3 -m venv "${VENV_DIR}"
        "${VENV_DIR}/bin/python" -m pip install -r requirements.txt
        echo -e "${GREEN}✅ Virtual environment created${NC}"
    else
        repair_relocated_venv
        echo -e "${GREEN}✅ Virtual environment exists${NC}"
    fi
    echo ""
}

repair_relocated_venv() {
    [ -x "${VENV_DIR}/bin/python3" ] || return 0

    # Python virtualenv console scripts embed the environment's absolute path
    # in their shebang. Rewrite only Python entry points under this venv when
    # that path no longer matches the relocated environment.
    "${VENV_DIR}/bin/python3" - "${VENV_DIR}" <<'PY'
from pathlib import Path
import sys


venv_dir = Path(sys.argv[1]).resolve()
bin_dir = venv_dir / "bin"
expected = {
    str(bin_dir / "python"),
    str(bin_dir / "python3"),
}

for script in bin_dir.iterdir():
    if not script.is_file() or script.is_symlink():
        continue

    payload = script.read_bytes()
    first_line, separator, remainder = payload.partition(b"\n")
    if not separator or not first_line.startswith(b"#!"):
        continue

    interpreter = first_line[2:].decode("utf-8", errors="replace").strip()
    interpreter = interpreter.split(maxsplit=1)[0]
    interpreter_name = Path(interpreter).name
    if interpreter in expected or interpreter_name not in {"python", "python3"}:
        continue
    if not interpreter.startswith("/") or not interpreter.endswith(
        ("/bin/python", "/bin/python3")
    ):
        continue
    if Path(interpreter).parent.parent.name != venv_dir.name:
        continue

    replacement = f"#!{bin_dir / interpreter_name}".encode("utf-8")
    script.write_bytes(replacement + b"\n" + remainder)


activation_files = {
    "activate": bin_dir / "activate",
    "activate.csh": bin_dir / "activate.csh",
    "activate.fish": bin_dir / "activate.fish",
}
for name, script in activation_files.items():
    if not script.is_file() or script.is_symlink():
        continue

    lines = script.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = False
    rewritten = []
    for raw_line in lines:
        newline = "\n" if raw_line.endswith("\n") else ""
        line = raw_line[:-1] if newline else raw_line
        stripped = line.lstrip()
        indentation = line[:len(line) - len(stripped)]
        if name == "activate" and stripped.startswith("export VIRTUAL_ENV/"):
            replacement = f"{indentation}export VIRTUAL_ENV={venv_dir}"
            if replacement != line:
                line = replacement
                changed = True
        elif name == "activate" and stripped.startswith("export VIRTUAL_ENV="):
            prefix, value = line.split("=", 1)
            if value.startswith("$(cygpath ") and value.endswith(")"):
                replacement = f"{prefix}=$(cygpath {venv_dir})"
                if replacement != line:
                    line = replacement
                    changed = True
            elif value.startswith("/"):
                replacement = f"{prefix}={venv_dir}"
                if replacement != line:
                    line = replacement
                    changed = True
        elif name == "activate.csh" and line.lstrip().startswith(
            "setenv VIRTUAL_ENV "
        ):
            replacement = (
                f"{line[:len(line) - len(line.lstrip())]}"
                f"setenv VIRTUAL_ENV {venv_dir}"
            )
            if replacement != line:
                line = replacement
                changed = True
        elif name == "activate.fish" and line.lstrip().startswith(
            "set -gx VIRTUAL_ENV "
        ):
            replacement = (
                f"{line[:len(line) - len(line.lstrip())]}"
                f"set -gx VIRTUAL_ENV {venv_dir}"
            )
            if replacement != line:
                line = replacement
                changed = True
        rewritten.append(line + newline)

    if changed:
        script.write_text("".join(rewritten), encoding="utf-8")
PY
}

ensure_frontend_deps() {
    echo -e "${BLUE}📦 Checking frontend dependencies...${NC}"
    cd "${FRONTEND_DIR}"
    if [ ! -d "node_modules" ]; then
        pnpm install
        echo -e "${GREEN}✅ Frontend dependencies installed${NC}"
    else
        echo -e "${GREEN}✅ Frontend dependencies exist${NC}"
    fi
    echo ""
}

ensure_database() {
    echo -e "${BLUE}🗄️  Checking database...${NC}"
    cd "${PROJECT_ROOT}"
    if [ ! -f "${PROJECT_ROOT}/orchestrator.db" ]; then
        "${VENV_DIR}/bin/python" -c "from app.database import init_db; init_db(); print('✅ Database initialized')"
    else
        echo -e "${GREEN}✅ Database exists${NC}"
    fi
    echo ""
}

# Function to stop existing processes
stop_existing() {
    echo -e "${YELLOW}⚠️  Stopping verified project-owned processes...${NC}"
    process_helper stop-all --root "${PROJECT_ROOT}" --timeout 30
    echo ""
}

# Function to start Redis
start_redis() {
    echo -e "${BLUE}📦 Starting Redis...${NC}"
    
    require_port_available 127.0.0.1 6379
    command -v redis-server >/dev/null 2>&1 || {
        echo -e "${RED}❌ redis-server is unavailable${NC}" >&2
        return 1
    }
    cd "${PROJECT_ROOT}"
    setsid nohup redis-server \
        --bind 127.0.0.1 --port 6379 --daemonize no --dir /tmp \
        >> "${LOG_DIR}/redis.log" 2>&1 &
    local redis_pid=$!
    record_service_process redis "${redis_pid}"
    for _ in {1..10}; do
        if ! kill -0 "${redis_pid}" 2>/dev/null; then
            echo -e "${RED}❌ Project Redis failed to start${NC}" >&2
            return 1
        fi
        if redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -q '^PONG$'; then
            echo -e "${GREEN}✅ Project Redis started on 127.0.0.1:6379${NC}"
            echo ""
            return 0
        fi
        sleep 1
    done
    echo -e "${RED}❌ Project Redis did not become ready${NC}" >&2
    return 1
}

# Function to start Qdrant
start_qdrant() {
    echo -e "${BLUE}🔍 Starting Qdrant...${NC}"

    require_port_available 127.0.0.1 6333

    local QDRANT_BIN="${QDRANT_BIN_DIR}/qdrant"
    local QDRANT_STORAGE="${QDRANT_DATA_DIR}/qdrant"
    mkdir -p "${QDRANT_STORAGE}"

    if [ ! -x "${QDRANT_BIN}" ]; then
        echo -e "${RED}❌ Qdrant binary not found at ${QDRANT_BIN} — knowledge layer unavailable${NC}"
        echo ""
        return 0
    fi

    cd "${QDRANT_HOME}"
    QDRANT__STORAGE__STORAGE_PATH="${QDRANT_STORAGE}" \
    QDRANT__STORAGE__SNAPSHOTS_PATH="${QDRANT_SNAPSHOTS_DIR}" \
        setsid nohup "${QDRANT_BIN}" \
        >> "${LOG_DIR}/qdrant.log" 2>&1 &
    local qdrant_pid=$!
    record_service_process qdrant "${qdrant_pid}"
    normalize_runtime_ownership

    local qdrant_ok=false
    for _ in {1..15}; do
        if curl -fsS http://localhost:6333/healthz > /dev/null 2>&1; then
            qdrant_ok=true
            break
        fi
        sleep 1
    done

    if [ "${qdrant_ok}" = true ]; then
        echo -e "${GREEN}✅ Qdrant started on port 6333${NC}"
        echo -e "${GREEN}📝 Qdrant logs: tail -f logs/qdrant.log${NC}"
    else
        echo -e "${RED}❌ Qdrant failed to start — knowledge layer unavailable${NC}"
        echo -e "${YELLOW}Check logs: cat logs/qdrant.log${NC}"
    fi
    echo ""
}

# Function to start backend
start_backend() {
    echo -e "${BLUE}🔧 Starting Backend (uvicorn)...${NC}"
    
    cd "${PROJECT_ROOT}"

    # Create log directory if it doesn't exist
    mkdir -p "${LOG_DIR}"

    # Load environment variables from .env file
    load_env
    echo -e "${GREEN}✅ Environment loaded from .env${NC}"
    
    require_port_available "${BACKEND_HOST}" "${BACKEND_PORT}"
    
    # Start backend in background with comprehensive timeout configuration
    # LOGS DIRECTIVE: Write directly to root logs/ for history preservation.
    setsid nohup "${VENV_DIR}/bin/uvicorn" app.main:app \
        --host "${BACKEND_HOST}" \
        --port "${BACKEND_PORT}" \
        --timeout-keep-alive 5 \
        --proxy-headers \
        --forwarded-allow-ips "*" \
        --access-log \
        >> "${LOG_DIR}/backend.log" 2>&1 &
    local backend_pid=$!
    record_service_process backend "${backend_pid}"
    normalize_runtime_ownership
    
    local backend_ok=false
    for _ in {1..15}; do
        if ! kill -0 "${backend_pid}" 2>/dev/null; then
            break
        fi
        if curl -fsS "http://127.0.0.1:${BACKEND_PORT}/health" > /dev/null 2>&1; then
            backend_ok=true
            break
        fi
        sleep 1
    done

    if [ "${backend_ok}" = true ]; then
        echo -e "${GREEN}✅ Backend started on ${BACKEND_HOST}:${BACKEND_PORT}${NC}"
        echo -e "${GREEN}🆔 Backend PID: ${backend_pid}${NC}"
        echo -e "${GREEN}📝 Backend logs: tail -f logs/backend.log${NC}"
    else
        process_helper stop --root "${PROJECT_ROOT}" --service backend --timeout 10 || true
        echo -e "${RED}❌ Backend failed to start!${NC}"
        echo -e "${YELLOW}Check logs: cat logs/backend.log${NC}"
        return 1
    fi
    echo ""
}

# Function to start workers
start_workers() {
    echo -e "${BLUE}👷 Starting Celery Workers...${NC}"
    
    cd "${PROJECT_ROOT}"
    
    # Load environment variables from .env file
    load_env
    echo -e "${GREEN}✅ Environment loaded for workers${NC}"
    normalize_runtime_ownership
    
    if process_helper status --root "${PROJECT_ROOT}" --service worker; then
        echo -e "${RED}❌ A verified project Worker is already running.${NC}" >&2
        return 1
    else
        local worker_status=$?
        if [ "${worker_status}" -ne 1 ]; then
            return "${worker_status}"
        fi
    fi
    
    # Start worker in background
    # LOGS DIRECTIVE: Write directly to root logs/ for history preservation.
    # Concurrency defaults to a bounded value rather than Celery's default
    # (one prefork child per CPU core). Each worker process opens its own
    # SQLAlchemy connection pool onto the same SQLite file and holds one
    # connection per in-flight orchestration task for that task's full
    # (potentially many-minute) duration; on a high-core-count host,
    # unbounded concurrency spawned enough worker processes to exhaust
    # pool + SQLite lock capacity under real load -- see
    # docs/roadmap/done/phase18/phase18l-r-runtime-verification-report.md,
    # "DB Connection Pool Exhaustion". Override with CELERY_WORKER_CONCURRENCY
    # in .env if you need more parallel task slots and have verified your
    # host/DB can handle it.
    setsid nohup "${VENV_DIR}/bin/celery" \
        -A app.celery_app worker \
        --concurrency="${CELERY_WORKER_CONCURRENCY:-4}" \
        --loglevel=info \
        >> "${LOG_DIR}/worker.log" 2>&1 &
    local worker_pid=$!
    record_service_process worker "${worker_pid}"
    normalize_runtime_ownership

    local worker_ok=false
    for _ in {1..20}; do
        if ! kill -0 "${worker_pid}" 2>/dev/null; then
            break
        fi
        if grep -q "ready" "${LOG_DIR}/worker.log" 2>/dev/null; then
            worker_ok=true
            break
        fi
        sleep 1
    done
    
    if [ "${worker_ok}" = true ] || kill -0 "${worker_pid}" 2>/dev/null; then
        echo -e "${GREEN}✅ Celery worker started${NC}"
        echo -e "${GREEN}🆔 Worker PID: ${worker_pid}${NC}"
        echo -e "${GREEN}📝 Worker logs: tail -f logs/worker.log${NC}"
    else
        process_helper stop --root "${PROJECT_ROOT}" --service worker --timeout 10 || true
        echo -e "${RED}❌ Worker failed to start!${NC}"
        echo -e "${YELLOW}Check logs: cat logs/worker.log${NC}"
        return 1
    fi

    require_no_owned_service beat \
        "Refusing to preserve a potentially mixed-version scheduler."
    setsid nohup "${VENV_DIR}/bin/celery" \
        -A app.celery_app beat \
        --loglevel=info \
        --schedule="${PID_DIR}/celerybeat-schedule" \
        >> "${LOG_DIR}/beat.log" 2>&1 &
    local beat_pid=$!
    record_service_process beat "${beat_pid}"
    normalize_runtime_ownership

    sleep 1
    if ! kill -0 "${beat_pid}" 2>/dev/null; then
        process_helper stop --root "${PROJECT_ROOT}" --service beat --timeout 10 || true
        echo -e "${RED}❌ Celery Beat failed to start!${NC}"
        echo -e "${YELLOW}Check logs: cat logs/beat.log${NC}"
        return 1
    fi
    echo -e "${GREEN}✅ Celery Beat started${NC}"
    echo -e "${GREEN}🆔 Beat PID: ${beat_pid}${NC}"
    echo ""
}

# Check worker processes from procfs without any PATH-resolved helper.
worker_only_check_processes() {
    local proc_entry proc_dir arg_index
    local -a process_argv
    for proc_entry in /proc/[0-9]*/cmdline; do
        [[ -e "${proc_entry}" ]] || continue
        proc_dir="${proc_entry%/cmdline}"
        if [[ ! -r "${proc_entry}" ]]; then
            [[ -d "${proc_dir}" ]] || continue
            echo "worker process table cannot be completely observed" >&2
            return 2
        fi
        process_argv=()
        mapfile -d '' -t process_argv < "${proc_entry}" || {
            [[ -d "${proc_dir}" ]] || continue
            echo "worker process command line cannot be observed" >&2
            return 2
        }
        for ((arg_index = 0; arg_index + 2 < ${#process_argv[@]}; arg_index++)); do
            if [[ "${process_argv[arg_index]}" == "-A" \
                && "${process_argv[arg_index + 1]}" == "app.celery_app" \
                && "${process_argv[arg_index + 2]}" == "worker" ]]; then
                return 0
            fi
        done
    done
    return 1
}

# Start one worker attempt for the guarded Phase 39 refresh path. This entry
# point deliberately does not source .env, use PATH-based command lookup,
# touch worker/Beat PID files, inspect logs, stop processes, or start Beat.
worker_only_verify_fixed_executable() {
    local executable="$1"
    local link_owner parent_metadata parent_owner parent_mode parent_kind parent_mode_value
    local resolved target_metadata target_owner target_mode target_kind target_mode_value
    local target_directory directory_metadata directory_owner directory_mode directory_kind directory_mode_value

    [[ -f "${executable}" && -x "${executable}" ]] || return 1
    [[ -L "${executable}" ]] || return 0

    # Follow only system-managed links to non-writable, root-owned binaries.
    link_owner="$(/usr/bin/stat -c '%u' -- "${executable}" 2>/dev/null)" || return 1
    parent_metadata="$(/usr/bin/stat -c '%u %a %F' -- "${executable%/*}" 2>/dev/null)" || return 1
    read -r parent_owner parent_mode parent_kind <<< "${parent_metadata}"
    [[ "${parent_kind}" == "directory" && "${parent_owner}" == "${link_owner}" ]] || return 1
    [[ "${parent_mode}" =~ ^[0-7]{3,4}$ ]] || return 1
    parent_mode_value=$((8#${parent_mode}))
    (( (parent_mode_value & 022) == 0 )) || return 1

    resolved="$(/usr/bin/readlink -f -- "${executable}" 2>/dev/null)" || return 1
    [[ "${resolved}" == /* && -f "${resolved}" && -x "${resolved}" ]] || return 1
    target_metadata="$(/usr/bin/stat -c '%u %a %F' -- "${resolved}" 2>/dev/null)" || return 1
    read -r target_owner target_mode target_kind <<< "${target_metadata}"
    [[ "${target_kind}" == "regular file" && "${target_owner}" == "0" ]] || return 1
    [[ "${target_mode}" =~ ^[0-7]{3,4}$ ]] || return 1
    target_mode_value=$((8#${target_mode}))
    (( (target_mode_value & 022) == 0 )) || return 1

    target_directory="${resolved%/*}"
    while :; do
        directory_metadata="$(/usr/bin/stat -c '%u %a %F' -- "${target_directory}" 2>/dev/null)" || return 1
        read -r directory_owner directory_mode directory_kind <<< "${directory_metadata}"
        [[ "${directory_kind}" == "directory" && "${directory_owner}" == "0" ]] || return 1
        [[ "${directory_mode}" =~ ^[0-7]{3,4}$ ]] || return 1
        directory_mode_value=$((8#${directory_mode}))
        (( (directory_mode_value & 022) == 0 )) || return 1
        [[ "${target_directory}" == "/" ]] && break
        target_directory="${target_directory%/*}"
        [[ -n "${target_directory}" ]] || target_directory="/"
    done
}

start_worker_only() {
    if [[ "$#" -ne 1 ]]; then
        echo "start_worker_only requires exactly one absolute pin path" >&2
        return 64
    fi

    local pin_path="$1"
    if [[ "${pin_path}" != /* || "${pin_path}" == *$'\n'* || "${pin_path}" == *$'\r'* ]]; then
        echo "worker candidate pin path must be absolute and contain no line breaks" >&2
        return 64
    fi

    local env_file="${PROJECT_ROOT}/.env"
    if [[ -e "${env_file}" || -L "${env_file}" ]]; then
        if [[ ! -f "${env_file}" || -L "${env_file}" ]]; then
            echo "worker .env must be a regular file" >&2
            return 78
        fi
        local line key value
        while IFS= read -r line || [[ -n "${line}" ]]; do
            line="${line%$'\r'}"
            [[ -n "${line}" ]] || continue
            [[ "${line}" =~ ^[[:space:]]*# ]] && continue
            [[ "${line}" == *=* ]] || continue
            key="${line%%=*}"
            value="${line#*=}"
            key="${key#"${key%%[![:space:]]*}"}"
            key="${key%"${key##*[![:space:]]}"}"
            if [[ "${key}" == export[[:space:]]* ]]; then
                key="${key#export}"
                key="${key#"${key%%[![:space:]]*}"}"
                key="${key%"${key##*[![:space:]]}"}"
            fi
            case "${key}" in
                PATH|WORKER_CANDIDATE_PIN_PATH)
                    echo "worker .env must not set ${key}" >&2
                    return 78
                    ;;
                CELERY_WORKER_CONCURRENCY)
                    value="${value#"${value%%[![:space:]]*}"}"
                    value="${value%"${value##*[![:space:]]}"}"
                    value="${value#\"}"
                    value="${value%\"}"
                    value="${value#\'}"
                    value="${value%\'}"
                    if [[ "${value}" != "4" ]]; then
                        echo "worker .env concurrency differs from the approved value" >&2
                        return 78
                    fi
                    ;;
            esac
        done < "${env_file}"
    fi

    local env_bin="/usr/bin/env"
    local nohup_bin="/usr/bin/nohup"
    local python_bin="${VENV_DIR}/bin/python3"
    local celery_bin="${VENV_DIR}/bin/celery"
    local executable
    for executable in "${env_bin}" "${nohup_bin}"; do
        if ! worker_only_verify_fixed_executable "${executable}"; then
            echo "required worker launcher executable is unavailable or unexpected" >&2
            return 78
        fi
    done
    if [[ ! -f "${python_bin}" || ! -x "${python_bin}" || -L "${python_bin}" ]]; then
        echo "worker interpreter is unavailable or symlinked" >&2
        return 78
    fi
    local python_links celery_links
    python_links="$(/usr/bin/stat -c '%h' -- "${python_bin}" 2>/dev/null || true)"
    if [[ "${python_links}" != "1" ]] || ! /usr/bin/cmp -s -- "${python_bin}" /usr/bin/python3.12; then
        echo "worker interpreter does not match the approved Python bytes" >&2
        return 78
    fi
    if [[ ! -f "${celery_bin}" || ! -x "${celery_bin}" || -L "${celery_bin}" ]]; then
        echo "worker executable is unavailable or unexpected" >&2
        return 78
    fi
    celery_links="$(/usr/bin/stat -c '%h' -- "${celery_bin}" 2>/dev/null || true)"
    if [[ "${celery_links}" != "1" ]]; then
        echo "worker executable has an unexpected hardlink count" >&2
        return 78
    fi
    local celery_shebang
    IFS= read -r celery_shebang < "${celery_bin}" || {
        echo "worker executable has no verified interpreter line" >&2
        return 78
    }
    if [[ "${celery_shebang}" != "#!${python_bin}" ]]; then
        echo "worker executable interpreter differs from the approved interpreter" >&2
        return 78
    fi
    if [[ ! -d "${LOG_DIR}" || -L "${LOG_DIR}" || ! -f "${LOG_DIR}/worker.log" || -L "${LOG_DIR}/worker.log" ]]; then
        echo "worker log path is unavailable or unexpected" >&2
        return 78
    fi

    # Refuse a second worker without reading the stale worker PID file or
    # relying on pgrep/ps command resolution through the caller's PATH.
    local process_check_status=0
    if worker_only_check_processes; then
        echo "an existing Celery worker is still present" >&2
        return 78
    else
        process_check_status=$?
        if [[ "${process_check_status}" -ne 1 ]]; then
            echo "worker process table cannot be safely verified" >&2
            return 78
        fi
    fi

    local fixed_path="${VENV_DIR}/bin:/usr/bin:/bin"
    local worker_pid
    cd "${PROJECT_ROOT}" || return 78
    "${env_bin}" -i \
        "PATH=${fixed_path}" \
        "HOME=/root" \
        "LANG=C.UTF-8" \
        "WORKER_CANDIDATE_PIN_PATH=${pin_path}" \
        "${nohup_bin}" "${celery_bin}" \
        -A app.celery_app worker \
        --concurrency=4 \
        --loglevel=info \
        >> "${LOG_DIR}/worker.log" 2>&1 &
    worker_pid=$!
    printf 'LAUNCH_ATTEMPTED pid=%s\n' "${worker_pid}"
}

# Function to start frontend
start_frontend() {
    echo -e "${BLUE}🎨 Starting Frontend (Vite)...${NC}"
    
    cd "${FRONTEND_DIR}"
    
    require_port_available 0.0.0.0 3000
    
    # Start frontend in background
    # LOGS DIRECTIVE: Write directly to root logs/ for history preservation.
    setsid nohup pnpm dev >> "${LOG_DIR}/frontend.log" 2>&1 &
    local frontend_pid=$!
    record_service_process frontend "${frontend_pid}"
    normalize_runtime_ownership
    
    local frontend_ok=false
    for _ in {1..15}; do
        if ! kill -0 "${frontend_pid}" 2>/dev/null; then
            break
        fi
        sleep 1
        if curl -fsS "http://127.0.0.1:3000" > /dev/null 2>&1; then
            frontend_ok=true
            break
        fi
    done

    if [ "${frontend_ok}" = true ]; then
        echo -e "${GREEN}✅ Frontend started on port 3000${NC}"
        echo -e "${GREEN}🆔 Frontend PID: ${frontend_pid}${NC}"
        echo -e "${GREEN}📝 Frontend logs: tail -f logs/frontend.log${NC}"
    else
        process_helper stop --root "${PROJECT_ROOT}" --service frontend --timeout 10 || true
        echo -e "${RED}❌ Frontend failed to start!${NC}"
        echo -e "${YELLOW}Check logs: cat logs/frontend.log${NC}${NC}"
        return 1
    fi
    echo ""
}

# Function to check health
check_health() {
    echo -e "${BLUE}🏥 Checking service health...${NC}"
    
    sleep 2
    
    local success=true
    
    # Check backend
    if curl -s "http://127.0.0.1:${BACKEND_PORT}/health" > /dev/null 2>&1; then
        echo -e "${GREEN}✅ Backend is healthy${NC}"
    else
        echo -e "${RED}❌ Backend is not responding${NC}"
        success=false
    fi
    
    # Check frontend
    if curl -s http://localhost:3000 > /dev/null 2>&1; then
        echo -e "${GREEN}✅ Frontend is healthy${NC}"
    else
        echo -e "${RED}❌ Frontend is not responding${NC}"
        success=false
    fi
    
    # Check Redis
    if redis-cli ping > /dev/null 2>&1; then
        echo -e "${GREEN}✅ Redis is responding${NC}"
    else
        echo -e "${RED}❌ Redis is not responding${NC}"
        success=false
    fi

    # A worker process and its log-level "ready" message are not sufficient:
    # Celery remote control can become available just after process startup.
    # Require the worker started by this topology to answer within a bounded
    # monotonic window; the helper fails closed on PID exit, no response,
    # broker/probe failure, or an unexpected node.
    local worker_pid=""
    if process_helper status --root "${PROJECT_ROOT}" --service worker; then
        worker_pid="$(cat "${PID_DIR}/worker.pid")"
    fi
    local expected_worker_node="celery@$(hostname)"
    if [ -n "${worker_pid}" ] && "${VENV_DIR}/bin/python3" \
        "${PROJECT_ROOT}/scripts/maintenance/wait_for_celery_worker.py" \
        --celery "${VENV_DIR}/bin/celery" \
        --pid "${worker_pid}" \
        --expected-node "${expected_worker_node}" \
        --timeout-seconds "${CELERY_CONTROL_READINESS_TIMEOUT_SECONDS:-30}" \
        --interval-seconds "${CELERY_CONTROL_READINESS_INTERVAL_SECONDS:-1}"; then
        echo -e "${GREEN}✅ Celery worker is responding${NC}"
    else
        echo -e "${RED}❌ Celery worker is not responding${NC}"
        echo -e "${YELLOW}Expected worker node: ${expected_worker_node}${NC}"
        echo -e "${YELLOW}Check logs: tail -40 logs/worker.log${NC}"
        success=false
    fi

    # Beat has no remote-control ping; its singleton PID is the bounded
    # startup liveness contract. Durable dispatch freshness remains visible
    # through /api/v1/ops/health.
    if process_helper status --root "${PROJECT_ROOT}" --service beat; then
        echo -e "${GREEN}✅ Celery Beat is running${NC}"
    else
        echo -e "${RED}❌ Celery Beat is not running${NC}"
        success=false
    fi

    # Check Qdrant
    if curl -fsS http://localhost:6333/healthz > /dev/null 2>&1; then
        echo -e "${GREEN}✅ Qdrant is responding${NC}"
    else
        echo -e "${YELLOW}⚠️  Qdrant is not responding (knowledge layer degraded)${NC}"
    fi
    
    echo ""
    
    if [ "$success" = true ]; then
        echo -e "${GREEN}🎉 All services operational!${NC}"
    else
        echo -e "${RED}⚠️  Some services failed health checks${NC}"
        echo "Check logs: tail -20 logs/backend.log logs/frontend.log logs/worker.log"
        return 1
    fi
}

# Main execution
main() {
    echo "🚀 Starting Orchestrator Network..."
    echo ""
    echo "========================================"
    echo "  Orchestrator Network Startup Script"
    echo "========================================"
    echo ""
    
    load_env
    prepare_logs

    # A start is either from a stopped topology or an explicit interactive
    # restart. Preserving any existing component risks a mixed-version stack.
    if owned_processes_running; then
        if [ -t 0 ]; then
            read -p "Verified project-owned processes are running. Stop them and restart? (y/n): " -n 1 -r
            echo
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                stop_existing
            else
                echo -e "${RED}Startup cancelled; existing topology was not changed.${NC}"
                return 1
            fi
        else
            echo -e "${RED}Existing processes detected in non-interactive mode. Verified project-owned processes are running.${NC}"
            echo -e "${YELLOW}Run ./stop_all.sh first; refusing a mixed-version start.${NC}"
            return 1
        fi
    else
        local owned_status=$?
        if [ "${owned_status}" -ne 1 ]; then
            echo -e "${RED}Process identity could not be established; refusing startup.${NC}" >&2
            return "${owned_status}"
        fi
    fi
    
    # Bootstrap local runtime automatically
    ensure_venv
    ensure_frontend_deps
    ensure_database

    # Start all services in order
    start_redis
    start_qdrant
    start_backend
    start_workers
    start_frontend
    
    # Check health
    check_health
    
    # Display URLs
    echo "========================================"
    echo "  🎉 All services started successfully!"
    echo "========================================"
    echo ""
    echo "📱 Frontend Dashboard: http://localhost:3000"
    echo "🔧 Backend API: http://${LOCALHOST}:${BACKEND_PORT}"
    echo "📚 API Docs: http://${LOCALHOST}:${BACKEND_PORT}/docs"
    echo "🐘 Redis: localhost:6379"
    echo ""
    echo "📝 View logs (permanent storage):"
    echo "  Backend:    tail -f logs/backend.log"
    echo "  Worker:     tail -f logs/worker.log"
    echo "  Frontend:   tail -f logs/frontend.log"
    echo "  Qdrant:     tail -f logs/qdrant.log"
    echo ""
    echo "🛑 To stop all services:"
    echo "  ./stop_all.sh"
    echo ""
}

# The dedicated entry point dispatches before the full-stack startup path.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    if [[ "${1-}" == "start_worker_only" ]]; then
        shift
        start_worker_only "$@"
    else
        main
    fi
fi
