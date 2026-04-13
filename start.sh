#!/bin/bash

# Orchestrator Network - Full Startup Script
# This script starts all components in the correct order.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}"
FRONTEND_DIR="${PROJECT_ROOT}/frontend"
VENV_DIR="${PROJECT_ROOT}/venv"
LOG_DIR="${PROJECT_ROOT}/logs"
PID_DIR="${PROJECT_ROOT}/run"

echo "🚀 Starting Orchestrator Network..."
echo ""

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

        export "${key}=${value}"
    done < "${env_file}"
}

prepare_logs() {
    mkdir -p "${LOG_DIR}"
    mkdir -p "${PID_DIR}"
    : > "${LOG_DIR}/backend.log"
    : > "${LOG_DIR}/worker.log"
    : > "${LOG_DIR}/frontend.log"
}

cleanup_pid_file() {
    local pid_file="$1"
    [ -f "${pid_file}" ] || return 0

    local pid
    pid="$(cat "${pid_file}" 2>/dev/null || true)"
    if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
        return 0
    fi

    rm -f "${pid_file}"
}

ensure_venv() {
    echo -e "${BLUE}🔧 Checking virtual environment...${NC}"
    cd "${PROJECT_ROOT}"
    if [ ! -d "${VENV_DIR}" ]; then
        python3 -m venv "${VENV_DIR}"
        "${VENV_DIR}/bin/pip" install -r requirements.txt
        echo -e "${GREEN}✅ Virtual environment created${NC}"
    else
        echo -e "${GREEN}✅ Virtual environment exists${NC}"
    fi
    echo ""
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

# Function to check if a process is running (by port for services)
check_process() {
    local name="$1"
    local port=""
    
    # Map service names to ports
    case "$name" in
        "uvicorn app.main:app")
            port="${BACKEND_PORT}"
            ;;
        "celery -A app.celery_app worker")
            # Workers don't have a specific port, fall back to pgrep
            if pgrep -f "$name" > /dev/null; then
                return 0
            fi
            return 1
            ;;
        "vite")
            port=3000
            ;;
        "redis-server")
            port=6379
            ;;
        *)
            # Fallback to pgrep for unknown services
            if pgrep -f "$name" > /dev/null; then
                return 0
            fi
            return 1
            ;;
    esac
    
    # Check port using lsof (most reliable)
    if command -v lsof &> /dev/null; then
        if lsof -i :$port &> /dev/null; then
            return 0
        fi
    fi
    
    # Fallback: netstat
    if command -v netstat &> /dev/null; then
        if netstat -tlnp 2>/dev/null | grep -q ":$port "; then
            return 0
        fi
    fi
    
    # Last resort: fuser
    if command -v fuser &> /dev/null; then
        if fuser $port/tcp &> /dev/null; then
            return 0
        fi
    fi
    
    return 1
}

# Function to stop existing processes
stop_existing() {
    echo -e "${YELLOW}⚠️  Stopping existing processes...${NC}"
    cleanup_pid_file "${PID_DIR}/backend.pid"
    cleanup_pid_file "${PID_DIR}/worker.pid"
    cleanup_pid_file "${PID_DIR}/frontend.pid"

    # Stop backend
    if [ -f "${PID_DIR}/backend.pid" ]; then
        kill "$(cat "${PID_DIR}/backend.pid")" 2>/dev/null || true
        rm -f "${PID_DIR}/backend.pid"
        echo -e "${GREEN}✅ Backend stopped${NC}"
    fi
    if check_process "uvicorn app.main:app"; then
        pkill -f "uvicorn app.main:app" || true
        echo -e "${GREEN}✅ Backend stopped${NC}"
    fi

    # Stop workers
    if [ -f "${PID_DIR}/worker.pid" ]; then
        kill "$(cat "${PID_DIR}/worker.pid")" 2>/dev/null || true
        rm -f "${PID_DIR}/worker.pid"
        echo -e "${GREEN}✅ Workers stopped${NC}"
    fi
    if check_process "celery -A app.celery_app worker"; then
        pkill -f "celery -A app.celery_app worker" || true
        echo -e "${GREEN}✅ Workers stopped${NC}"
    fi

    # Stop frontend
    if [ -f "${PID_DIR}/frontend.pid" ]; then
        kill "$(cat "${PID_DIR}/frontend.pid")" 2>/dev/null || true
        rm -f "${PID_DIR}/frontend.pid"
        echo -e "${GREEN}✅ Frontend stopped${NC}"
    fi
    if check_process "vite"; then
        pkill -f "vite" || true
        pkill -f "pnpm dev" || true
        echo -e "${GREEN}✅ Frontend stopped${NC}"
    fi
    
    sleep 2
    echo ""
}

# Function to start Redis
start_redis() {
    echo -e "${BLUE}📦 Starting Redis...${NC}"
    
    if ! check_process "redis-server"; then
        # Start Redis with specific working directory to prevent dump.rdb in workspace
        redis-server --daemonize yes --dir /tmp
        echo -e "${GREEN}✅ Redis started (working dir: /tmp)${NC}"
    else
        echo -e "${GREEN}✅ Redis already running${NC}"
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
    
    # Kill any existing backend
    if check_process "uvicorn app.main:app"; then
        pkill -f "uvicorn app.main:app" || true
        sleep 1
    fi
    
    # Start backend in background with comprehensive timeout configuration
    # LOGS DIRECTIVE: Write directly to logs/ directory (not /tmp/) for history preservation
    cleanup_pid_file "${PID_DIR}/backend.pid"
    nohup "${VENV_DIR}/bin/uvicorn" app.main:app \
        --host "${BACKEND_HOST}" \
        --port "${BACKEND_PORT}" \
        --timeout-keep-alive 5 \
        --proxy-headers \
        --forwarded-allow-ips "*" \
        --access-log \
        >> "${LOG_DIR}/backend.log" 2>&1 &
    local backend_pid=$!
    echo "${backend_pid}" > "${PID_DIR}/backend.pid"
    
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
        rm -f "${PID_DIR}/backend.pid"
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
    
    # Kill any existing workers
    if check_process "celery -A app.celery_app worker"; then
        pkill -f "celery -A app.celery_app worker"
        sleep 1
    fi
    
    # Start worker in background
    # LOGS DIRECTIVE: Write directly to logs/ directory (not /tmp/) for history preservation
    cleanup_pid_file "${PID_DIR}/worker.pid"
    nohup "${VENV_DIR}/bin/celery" \
        -A app.celery_app worker \
        --loglevel=info \
        >> "${LOG_DIR}/worker.log" 2>&1 &
    local worker_pid=$!
    echo "${worker_pid}" > "${PID_DIR}/worker.pid"

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
    
    if [ "${worker_ok}" = true ] || check_process "celery -A app.celery_app worker"; then
        echo -e "${GREEN}✅ Celery worker started${NC}"
        echo -e "${GREEN}🆔 Worker PID: ${worker_pid}${NC}"
        echo -e "${GREEN}📝 Worker logs: tail -f logs/worker.log${NC}"
    else
        rm -f "${PID_DIR}/worker.pid"
        echo -e "${RED}❌ Worker failed to start!${NC}"
        echo -e "${YELLOW}Check logs: cat logs/worker.log${NC}"
        return 1
    fi
    echo ""
}

# Function to start frontend
start_frontend() {
    echo -e "${BLUE}🎨 Starting Frontend (Vite)...${NC}"
    
    cd "${FRONTEND_DIR}"
    
    # Kill any existing frontend
    if check_process "vite"; then
        pkill -f "vite" || true
        pkill -f "pnpm dev" || true
        sleep 1
    fi

    if check_process "vite"; then
        echo -e "${RED}❌ Port 3000 is still occupied after stopping existing frontend processes.${NC}"
        echo -e "${YELLOW}Run: pgrep -af 'vite|pnpm dev'${NC}"
        echo -e "${YELLOW}Then stop the stale process before retrying.${NC}"
        return 1
    fi
    
    # Start frontend in background
    # LOGS DIRECTIVE: Write directly to logs/ directory (not /tmp/) for history preservation
    cleanup_pid_file "${PID_DIR}/frontend.pid"
    nohup pnpm dev >> "${LOG_DIR}/frontend.log" 2>&1 &
    local frontend_pid=$!
    echo "${frontend_pid}" > "${PID_DIR}/frontend.pid"
    
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
        rm -f "${PID_DIR}/frontend.pid"
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
    
    echo ""
    
    if [ "$success" = true ]; then
        echo -e "${GREEN}🎉 All services operational!${NC}"
    else
        echo -e "${RED}⚠️  Some services failed health checks${NC}"
        echo "Check logs: tail -20 logs/backend.log logs/frontend.log logs/worker.log"
    fi
}

# Main execution
main() {
    echo "========================================"
    echo "  Orchestrator Network Startup Script"
    echo "========================================"
    echo ""
    
    load_env
    prepare_logs

    # Ask if user wants to stop existing processes
    if check_process "uvicorn app.main:app" || check_process "vite" || check_process "celery"; then
        read -p "Existing processes detected. Stop them and restart? (y/n): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            stop_existing
        fi
    fi
    
    # Bootstrap local runtime automatically
    ensure_venv
    ensure_frontend_deps
    ensure_database

    # Start all services in order
    start_redis
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
    echo ""
    echo "🛑 To stop all services:"
    echo "  pkill -f 'uvicorn app.main:app'"
    echo "  pkill -f 'celery -A app.celery_app worker'"
    echo "  pkill -f 'vite'"
    echo ""
}

# Run main function
main

