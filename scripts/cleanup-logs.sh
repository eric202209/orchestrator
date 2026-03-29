#!/bin/bash
# Orchestrator Logs Cleanup Script
# Purpose: Archive old logs and maintain disk space
# Usage: ./scripts/cleanup-logs.sh
# Schedule: Run weekly (e.g., every Monday morning)

set -e

PROJECT_LOGS="/root/.openclaw/workspace/projects/orchestrator/logs"
ARCHIVE_DIR="/root/.openclaw/workspace/projects/orchestrator/.archive"
MAX_LOG_SIZE_MB=100

echo "🧹 Starting logs cleanup..."

# Create archive directory if it doesn't exist
mkdir -p "$ARCHIVE_DIR"

# Archive logs older than 7 days
echo "📦 Archiving logs older than 7 days..."
find "$PROJECT_LOGS" -name "*.log" -mtime +7 -exec mv {} "$ARCHIVE_DIR/" \; 2>/dev/null || true

# Delete logs older than 30 days
echo "🗑️ Deleting logs older than 30 days..."
find "$ARCHIVE_DIR" -name "*.log" -mtime +30 -delete 2>/dev/null || true

# Compress old logs
echo "📦 Compressing archived logs..."
if [ "$(ls -A $ARCHIVE_DIR/*.log 2>/dev/null)" ]; then
    tar -czf "$ARCHIVE_DIR/logs-$(date +%Y%m%d).tar.gz" "$ARCHIVE_DIR"/*.log 2>/dev/null || true
    # Remove compressed logs after archiving
    rm -f "$ARCHIVE_DIR"/*.log
fi

# Check current log sizes
echo "📊 Current log sizes:"
du -sh "$PROJECT_LOGS"/*.log 2>/dev/null || echo "No logs found"

# Check for oversized logs
echo "⚠️ Checking for oversized logs (>${MAX_LOG_SIZE_MB}MB)..."
for log_file in "$PROJECT_LOGS"/*.log; do
    if [ -f "$log_file" ]; then
        size_mb=$(du -m "$log_file" | cut -f1)
        if [ "$size_mb" -gt "$MAX_LOG_SIZE_MB" ]; then
            echo "⚠️ WARNING: $log_file is ${size_mb}MB (exceeds ${MAX_LOG_SIZE_MB}MB limit)"
        fi
    fi
done

echo "✅ Logs cleanup completed!"
echo "📍 Archives are in: $ARCHIVE_DIR/"
