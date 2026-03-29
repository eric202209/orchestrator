#!/bin/bash
#
# Sync logs from /tmp/ to project logs directory
# This script ensures logs are not lost when /tmp/ is cleared
#

PROJECT_DIR="/root/.openclaw/workspace/projects/orchestrator"
LOGS_DIR="$PROJECT_DIR/logs"
TMP_LOGS="/tmp"

# Create logs directory if it doesn't exist
mkdir -p "$LOGS_DIR"

echo "📋 Syncing logs from $TMP_LOGS to $LOGS_DIR..."

# Sync important logs
declare -A LOG_FILES=(
    ["backend.log"]="Backend API logs"
    ["celery.log"]="Celery worker startup logs"
    ["frontend.log"]="Frontend build/dev logs"
    ["orchestrator-backend.log"]="Orchestrator backend logs"
)

for log_file in "${!LOG_FILES[@]}"; do
    if [ -f "$TMP_LOGS/$log_file" ]; then
        # Add timestamp to avoid overwriting
        timestamp=$(date +%Y%m%d-%H%M%S)
        backup_file="$LOGS_DIR/${log_file%.log}-${timestamp}.log"
        
        # Copy the log
        cp "$TMP_LOGS/$log_file" "$backup_file"
        
        echo "✅ Synced: $log_file → $backup_file"
    fi
done

# Check for any new logs
echo ""
echo "📊 Current logs in $LOGS_DIR:"
ls -lh "$LOGS_DIR"/*.log 2>/dev/null | awk '{print $9, "("$5")"}' || echo "No log files found"

echo ""
echo "✅ Sync complete!"
