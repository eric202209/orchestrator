#!/bin/bash
# Orchestrator Logs Management Script
# Purpose: Sync logs from /tmp/ to project logs directory
# Usage: ./scripts/sync-tmp-logs.sh

set -e

PROJECT_LOGS="/root/.openclaw/workspace/projects/orchestrator/logs"
TMP_LOGS="/tmp"

echo "🔄 Syncing logs from /tmp/ to project logs directory..."

# Copy backend logs
if [ -f "$TMP_LOGS/backend.log" ]; then
    cp "$TMP_LOGS/backend.log" "$PROJECT_LOGS/backend.log"
    echo "✅ Synced backend.log"
fi

# Copy celery logs
if [ -f "$TMP_LOGS/celery.log" ]; then
    cp "$TMP_LOGS/celery.log" "$PROJECT_LOGS/celery.log"
    echo "✅ Synced celery.log"
fi

# Copy frontend logs
if [ -f "$TMP_LOGS/frontend.log" ]; then
    cp "$TMP_LOGS/frontend.log" "$PROJECT_LOGS/frontend.log"
    echo "✅ Synced frontend.log"
fi

echo "✅ Logs synchronized successfully!"
echo "📍 All logs are now in: $PROJECT_LOGS/"
