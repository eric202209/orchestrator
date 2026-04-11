#!/bin/bash

# Force kill all orchestrator processes
echo "🛑 Killing all orchestrator processes..."

# Kill by port (most reliable)
for port in 8080 8081 9000 3000 3001; do
    fuser -k ${port}/tcp 2>/dev/null && echo "  Killed port ${port}"
done

# Kill by process name (extra safety)
pkill -9 -f "uvicorn app.main" 2>/dev/null
pkill -9 -f "celery.*worker" 2>/dev/null
pkill -9 -f "vite" 2>/dev/null
pkill -9 -f "pnpm dev" 2>/dev/null

sleep 2

# Verify they're gone
echo "🔍 Verifying processes are dead..."
if ps aux | grep -E "uvicorn.*app.main|celery.*worker|vite" | grep -v grep | grep -q .; then
    echo "❌ Some processes still running:"
    ps aux | grep -E "uvicorn.*app.main|celery.*worker|vite" | grep -v grep
    exit 1
fi

echo "✅ All orchestrator processes killed!"
echo ""
