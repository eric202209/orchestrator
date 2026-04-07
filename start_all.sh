#!/bin/bash

# Orchestrator Network - Full System Start Script
# Run this from /root/.openclaw/workspace/projects/orchestrator/

set -e

echo "🚀 Orchestrator Network - Full Startup"
echo "======================================"
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

# Localhost alias (from .env, default: localhost)
# Set LOCALHOST= in .env to override default (e.g., 172.17.0.2 for Docker)
LOCALHOST=${LOCALHOST:-localhost}

# Function to check if port is in use
check_port() {
    local port=$1
    
    # Try lsof first
    if command -v lsof &> /dev/null; then
        if lsof -i :$port &> /dev/null; then
            return 0
        fi
    fi
    
    # Fallback: check if any running process contains the port
    # Use grep to filter the process list
    if ps aux 2>/dev/null | grep -v grep | grep -q "port $port\|--port $port"; then
        return 0
    fi
    
    # Special case: vite/dev servers often don't show port in command line
    # Check for common dev server processes
    if ps aux 2>/dev/null | grep -v grep | grep -qE "(vite|pnpm dev|webpack-dev-server)"; then
        # If any of these are running, assume a port might be in use
        # We'll let the actual service start command handle port conflicts
        return 0
    fi
    
    return 1
}

# Function to stop all processes
stop_all() {
    echo -e "${YELLOW}⚠️  Stopping all processes...${NC}"
    
    # Stop backend
    if check_port 8080; then
        pkill -f "uvicorn app.main:app" 2>/dev/null || true
        sleep 1
        echo -e "${GREEN}✅ Backend stopped${NC}"
    fi
    
    # Stop workers
    if pgrep -f "celery -A app.celery_app worker" > /dev/null; then
        pkill -f "celery -A app.celery_app worker" 2>/dev/null || true
        sleep 1
        echo -e "${GREEN}✅ Workers stopped${NC}"
    fi
    
    # Stop frontend
    if check_port 3000; then
        pkill -f "vite" 2>/dev/null || true
        pkill -f "pnpm dev" 2>/dev/null || true
        sleep 1
        echo -e "${GREEN}✅ Frontend stopped${NC}"
    fi
    
    echo ""
}

# Function to ensure Redis is running
ensure_redis() {
    echo -e "${BLUE}📦 Checking Redis...${NC}"
    
    if ! check_port 6379; then
        redis-server --daemonize yes
        echo -e "${GREEN}✅ Redis started${NC}"
    else
        echo -e "${GREEN}✅ Redis already running${NC}"
    fi
}

# Function to ensure virtual environment exists
ensure_venv() {
    echo -e "${BLUE}🔧 Checking virtual environment...${NC}"
    
    if [ ! -d "venv" ]; then
        echo -e "${RED}❌ Virtual environment not found!${NC}"
        echo "Creating virtual environment..."
        python3 -m venv venv
        source venv/bin/activate
        pip install -r requirements.txt
        echo -e "${GREEN}✅ Virtual environment created${NC}"
    else
        echo -e "${GREEN}✅ Virtual environment exists${NC}"
    fi
}

# Function to install frontend dependencies
ensure_frontend_deps() {
    echo -e "${BLUE}📦 Checking frontend dependencies...${NC}"
    
    cd frontend
    
    if [ ! -d "node_modules" ]; then
        echo "Installing frontend dependencies..."
        pnpm install
        echo -e "${GREEN}✅ Frontend dependencies installed${NC}"
    else
        echo -e "${GREEN}✅ Frontend dependencies exist${NC}"
    fi
    
    cd ..
}

# Function to run database migrations
run_migrations() {
    echo -e "${BLUE}🗄️  Checking database...${NC}"
    
    if [ ! -f "orchestrator.db" ]; then
        echo "Creating database tables..."
        python3 -c "
from app.database import init_db
init_db()
print('✅ Database initialized')
"
    else
        echo -e "${GREEN}✅ Database exists${NC}"
    fi
}

# Function to start backend
start_backend() {
    echo -e "${BLUE}🔧 Starting Backend...${NC}"
    
    # Check if already running
    if check_port 8080; then
        echo -e "${YELLOW}⚠️  Backend already running on port 8080${NC}"
        local confirm
        read -p "Restart it? (y/n): " -n 1 -r
        echo
        if [[ $confirm =~ ^[Yy]$ ]]; then
            pkill -f "uvicorn app.main:app" 2>/dev/null || true
            sleep 1
        else
            echo -e "${GREEN}✅ Backend already running${NC}"
            return 0
        fi
    fi
    
    nohup venv/bin/uvicorn app.main:app \
        --host 0.0.0.0 \
        --port 8080 \
        > /tmp/backend.log 2>&1 &
    
    sleep 3
    
    if check_port 8080; then
        echo -e "${GREEN}✅ Backend started on port 8080${NC}"
    else
        echo -e "${RED}❌ Backend failed to start${NC}"
        echo "Check logs: tail -20 /tmp/backend.log"
        exit 1
    fi
}

# Function to start workers
start_workers() {
    echo -e "${BLUE}👷 Starting Celery Workers...${NC}"
    
    # Check if already running
    if pgrep -f "celery -A app.celery_app worker" > /dev/null; then
        echo -e "${YELLOW}⚠️  Workers already running${NC}"
        local confirm
        read -p "Restart them? (y/n): " -n 1 -r
        echo
        if [[ $confirm =~ ^[Yy]$ ]]; then
            pkill -f "celery -A app.celery_app worker" 2>/dev/null || true
            sleep 1
        else
            echo -e "${GREEN}✅ Workers already running${NC}"
            return 0
        fi
    fi
    
    nohup venv/bin/celery \
        -A app.celery_app worker \
        --loglevel=info \
        > /tmp/worker.log 2>&1 &
    
    sleep 5
    
    if pgrep -f "celery -A app.celery_app worker" > /dev/null; then
        echo -e "${GREEN}✅ Workers started${NC}"
    else
        echo -e "${RED}❌ Workers failed to start${NC}"
        echo "Check logs: tail -50 /tmp/worker.log"
        exit 1
    fi
}

# Function to start frontend
start_frontend() {
    echo -e "${BLUE}🎨 Starting Frontend...${NC}"
    
    # Check if already running
    if check_port 3000; then
        echo -e "${YELLOW}⚠️  Frontend already running on port 3000${NC}"
        local confirm
        read -p "Restart it? (y/n): " -n 1 -r
        echo
        if [[ $confirm =~ ^[Yy]$ ]]; then
            pkill -f "vite" 2>/dev/null || true
            pkill -f "pnpm dev" 2>/dev/null || true
            sleep 2
        else
            echo -e "${GREEN}✅ Frontend already running${NC}"
            return 0
        fi
    fi
    
    cd frontend
    nohup pnpm dev > /tmp/frontend.log 2>&1 &
    cd ..
    
    sleep 5
    
    if check_port 3000; then
        echo -e "${GREEN}✅ Frontend started on port 3000${NC}"
    else
        echo -e "${RED}❌ Frontend failed to start${NC}"
        echo "Check logs: tail -20 /tmp/frontend.log"
        exit 1
    fi
}

# Function to verify everything is working
verify() {
    echo -e "${BLUE}🏥 Verifying services...${NC}"
    sleep 2
    
    local success=true
    
    if curl -s http://${LOCALHOST}:8080/health > /dev/null 2>&1; then
        echo -e "${GREEN}✅ Backend healthy${NC}"
    else
        echo -e "${RED}❌ Backend not responding${NC}"
        success=false
    fi
    
    if curl -s http://${LOCALHOST}:3000 > /dev/null 2>&1; then
        echo -e "${GREEN}✅ Frontend healthy${NC}"
    else
        echo -e "${RED}❌ Frontend not responding${NC}"
        success=false
    fi
    
    if redis-cli ping > /dev/null 2>&1; then
        echo -e "${GREEN}✅ Redis responding${NC}"
    else
        echo -e "${RED}❌ Redis not responding${NC}"
        success=false
    fi
    
    echo ""
    
    if [ "$success" = true ]; then
        echo -e "${GREEN}🎉 All services are running!${NC}"
        echo ""
        echo "📱 Dashboard: http://${LOCALHOST}:3000"
        echo "🔧 API Docs: http://${LOCALHOST}:8080/docs"
        echo ""
        echo "📝 View logs:"
        echo "  Backend:    tail -f /tmp/backend.log"
        echo "  Workers:    tail -f /tmp/worker.log"
        echo "  Frontend:   tail -f /tmp/frontend.log"
    else
        echo -e "${RED}⚠️  Some services failed to start${NC}"
        echo "Check the logs above for errors."
    fi
}

# Main
echo "========================================"
echo "  Full Orchestrator Startup"
echo "========================================"
echo ""

# Check which services are already running
backend_running=false
frontend_running=false
workers_running=false

if check_port 8080; then
    backend_running=true
fi

if check_port 3000; then
    frontend_running=true
fi

if pgrep -f "celery -A app.celery_app worker" > /dev/null; then
    workers_running=true
fi

# If all services are running, ask if user wants to restart
if [ "$backend_running" = true ] && [ "$frontend_running" = true ] && [ "$workers_running" = true ]; then
    echo -e "${YELLOW}⚠️  All services already running${NC}"
    echo ""
    read -p "Restart all services? (y/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        stop_all
        # Fall through to start services
    else
        echo "Keeping existing services running."
        verify
        exit 0
    fi
fi

# If only some services are running, warn and continue
if [ "$backend_running" = true ] || [ "$frontend_running" = true ] || [ "$workers_running" = true ]; then
    echo -e "${YELLOW}⚠️  Some services already running${NC}"
    echo ""
    if [ "$backend_running" = true ]; then
        echo -e "  - Backend:   Already running"
    fi
    if [ "$frontend_running" = true ]; then
        echo -e "  - Frontend:  Already running"
    fi
    if [ "$workers_running" = true ]; then
        echo -e "  - Workers:   Already running"
    fi
    echo ""
    echo "Starting only the services that are not running..."
    echo ""
fi

# Start all services
ensure_redis
ensure_venv
ensure_frontend_deps
run_migrations

start_backend
start_workers
start_frontend

verify
