#!/bin/bash

# Orchestrator Network - Full Startup Script
# This script starts all components in the correct order

set -e

echo "🚀 Starting Orchestrator Network..."
echo ""

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Localhost alias (from .env, default to 172.17.0.2)
LOCALHOST=${LOCALHOST:-172.17.0.2}

# Function to check if a process is running
check_process() {
    if pgrep -f "$1" > /dev/null; then
        return 0
    else
        return 1
    fi
}

# Function to stop existing processes
stop_existing() {
    echo -e "${YELLOW}⚠️  Stopping existing processes...${NC}"
    
    # Stop backend
    if check_process "uvicorn app.main:app"; then
        pkill -f "uvicorn app.main:app"
        echo -e "${GREEN}✅ Backend stopped${NC}"
    fi
    
    # Stop workers
    if check_process "celery -A app.tasks worker"; then
        pkill -f "celery -A app.tasks worker"
        echo -e "${GREEN}✅ Workers stopped${NC}"
    fi
    
    # Stop frontend
    if check_process "vite"; then
        pkill -f "vite"
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
    
    cd /root/.openclaw/workspace/projects/orchestrator
    
    # Create log directory if it doesn't exist
    mkdir -p /tmp
    
    # Kill any existing backend
    if check_process "uvicorn app.main:app"; then
        pkill -f "uvicorn app.main:app"
        sleep 1
    fi
    
    # Start backend in background
    nohup /root/.openclaw/workspace/projects/orchestrator/venv/bin/uvicorn app.main:app \
        --host 0.0.0.0 \
        --port 8080 \
        > /tmp/backend.log 2>&1 &
    
    sleep 3
    
    if check_process "uvicorn app.main:app"; then
        echo -e "${GREEN}✅ Backend started on port 8080${NC}"
        echo -e "${GREEN}📝 Backend logs: tail -f /tmp/backend.log${NC}"
    else
        echo -e "${RED}❌ Backend failed to start!${NC}"
        echo -e "${YELLOW}Check logs: cat /tmp/backend.log${NC}"
        return 1
    fi
    echo ""
}

# Function to start workers
start_workers() {
    echo -e "${BLUE}👷 Starting Celery Workers...${NC}"
    
    cd /root/.openclaw/workspace/projects/orchestrator
    
    # Kill any existing workers
    if check_process "celery -A app.celery_app worker"; then
        pkill -f "celery -A app.celery_app worker"
        sleep 1
    fi
    
    # Start worker in background
    nohup /root/.openclaw/workspace/projects/orchestrator/venv/bin/celery \
        -A app.celery_app worker \
        --loglevel=info \
        > /tmp/worker.log 2>&1 &
    
    sleep 5
    
    if check_process "celery -A app.celery_app worker"; then
        echo -e "${GREEN}✅ Celery worker started${NC}"
        echo -e "${GREEN}📝 Worker logs: tail -f /tmp/worker.log${NC}"
    else
        echo -e "${RED}❌ Worker failed to start!${NC}"
        echo -e "${YELLOW}Check logs: cat /tmp/worker.log${NC}"
        return 1
    fi
    echo ""
}

# Function to start frontend
start_frontend() {
    echo -e "${BLUE}🎨 Starting Frontend (Vite)...${NC}"
    
    cd /root/.openclaw/workspace/projects/orchestrator/frontend
    
    # Kill any existing frontend
    if check_process "vite"; then
        pkill -f "vite"
        sleep 1
    fi
    
    # Start frontend in background
    nohup node /usr/bin/pnpm dev > /tmp/frontend.log 2>&1 &
    
    sleep 5
    
    if check_process "vite"; then
        echo -e "${GREEN}✅ Frontend started on port 3000${NC}"
        echo -e "${GREEN}📝 Frontend logs: tail -f /tmp/frontend.log${NC}"
    else
        echo -e "${RED}❌ Frontend failed to start!${NC}"
        echo -e "${YELLOW}Check logs: cat /tmp/frontend.log${NC}"
        return 1
    fi
    echo ""
}

# Function to check health
check_health() {
    echo -e "${BLUE}🏥 Checking service health...${NC}"
    
    sleep 2
    
    # Check backend
    if curl -s http://127.0.0.1:8080/health > /dev/null 2>&1; then
        echo -e "${GREEN}✅ Backend is healthy${NC}"
    else
        echo -e "${RED}❌ Backend is not responding${NC}"
    fi
    
    # Check frontend
    if curl -s http://127.0.0.1:3000 > /dev/null 2>&1; then
        echo -e "${GREEN}✅ Frontend is healthy${NC}"
    else
        echo -e "${RED}❌ Frontend is not responding${NC}"
    fi
    
    echo ""
}

# Main execution
main() {
    echo "========================================"
    echo "  Orchestrator Network Startup Script"
    echo "========================================"
    echo ""
    
    # Ask if user wants to stop existing processes
    if check_process "uvicorn app.main:app" || check_process "vite" || check_process "celery"; then
        read -p "Existing processes detected. Stop them and restart? (y/n): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            stop_existing
        fi
    fi
    
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
    echo "📱 Frontend Dashboard: http://172.17.0.2:3000"
    echo "🔧 Backend API: http://127.0.0.1:8080"
    echo "📚 API Docs: http://127.0.0.1:8080/docs"
    echo "🐘 Redis: localhost:6379"
    echo ""
    echo "📝 View logs:"
    echo "  Backend:    tail -f /tmp/backend.log"
    echo "  Worker:     tail -f /tmp/worker.log"
    echo "  Frontend:   tail -f /tmp/frontend.log"
    echo ""
    echo "🛑 To stop all services:"
    echo "  pkill -f 'uvicorn app.main:app'"
    echo "  pkill -f 'celery -A app.tasks worker'"
    echo "  pkill -f 'vite'"
    echo ""
}

# Run main function
main
