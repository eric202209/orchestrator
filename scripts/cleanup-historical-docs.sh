#!/bin/bash
# Historical Documentation Management Script
# Purpose: Review and clean up temporary daily logs
# Usage: ./scripts/cleanup-historical-docs.sh

set -e

NOTES_DIR="/root/.openclaw/workspace/projects/orchestrator/.notes"
CORE_FILES=("CORS_FIXES.md" "DEBUG_HISTORY.md" "PHASES_PROGRESS.md" "TASK_TIMEOUT_FIX.md" "TEST_RECORDS.md")

echo "📚 Reviewing historical documentation..."
echo ""

# List temporary daily logs
echo "📋 Temporary daily logs found:"
DAILY_LOGS=$(find "$NOTES_DIR" -name "orchestrator-*.log" -type f 2>/dev/null)
if [ -n "$DAILY_LOGS" ]; then
    echo "$DAILY_LOGS" | while read -r file; do
        size=$(du -h "$file" | cut -f1)
        date=$(stat -c %y "$file" | cut -d' ' -f1)
        echo "   - $(basename "$file") ($size) - Created: $date"
    done
else
    echo "   None found ✅"
fi
echo ""

# List core files
echo "📚 Core documentation files (KEEP):"
for file in "${CORE_FILES[@]}"; do
    if [ -f "$NOTES_DIR/$file" ]; then
        size=$(du -h "$NOTES_DIR/$file" | cut -f1)
        echo "   ✅ $file ($size)"
    fi
done
echo ""

# Check for old daily logs (>7 days)
echo "⚠️  Checking for old daily logs (>7 days):"
OLD_LOGS=$(find "$NOTES_DIR" -name "orchestrator-*.log" -type f -mtime +7 2>/dev/null)
if [ -n "$OLD_LOGS" ]; then
    echo "$OLD_LOGS" | while read -r file; do
        echo "   ⚠️  $(basename "$file") - Older than 7 days"
        echo "      Action: Review and delete after merging content"
    done
else
    echo "   ✅ No old daily logs found"
fi
echo ""

# Summary
echo "📊 Summary:"
CORE_COUNT=$(ls -1 "$NOTES_DIR"/*.md 2>/dev/null | wc -l)
if [ -n "$DAILY_LOGS" ]; then
    DAILY_COUNT=$(echo "$DAILY_LOGS" | wc -l)
else
    DAILY_COUNT=0
fi

echo "   Core files: $CORE_COUNT"
echo "   Daily logs: $DAILY_COUNT"
echo ""

if [ "$DAILY_COUNT" -eq 0 ]; then
    echo "✅ Historical documentation is clean!"
else
    echo "⚠️  $DAILY_COUNT temporary daily logs found"
    echo "   Action: Review and merge content to core files, then delete"
fi
