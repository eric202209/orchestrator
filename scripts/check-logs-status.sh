#!/bin/bash
# Orchestrator Logs Status Check
# Purpose: Verify all logs are in the correct location
# Usage: ./scripts/check-logs-status.sh

set -e

PROJECT_LOGS="/root/.openclaw/workspace/projects/orchestrator/logs"
PROJECT_ROOT="/root/.openclaw/workspace/projects/orchestrator"
TMP_LOGS="/tmp"

echo "🔍 Checking logs status..."
echo ""

# Check 1: Project root directory
echo "📁 Checking project root directory..."
ROOT_LOGS=$(find "$PROJECT_ROOT" -maxdepth 1 -name "*.log" -type f 2>/dev/null | wc -l)
if [ "$ROOT_LOGS" -gt 0 ]; then
    echo "❌ ERROR: Found ${ROOT_LOGS} .log files in project root!"
    find "$PROJECT_ROOT" -maxdepth 1 -name "*.log" -type f
    echo "   Action: Move to $PROJECT_LOGS/ or delete"
else
    echo "✅ Project root is clean (no .log files)"
fi
echo ""

# Check 2: Logs directory
echo "📁 Checking logs directory..."
if [ -d "$PROJECT_LOGS" ]; then
    echo "✅ Logs directory exists: $PROJECT_LOGS"
    echo "   Files:"
    ls -lh "$PROJECT_LOGS"/*.log 2>/dev/null | awk '{print "   -", $9, "("$5")"}' || echo "   No logs found"
else
    echo "❌ Logs directory does not exist!"
fi
echo ""

# Check 3: /tmp/ directory
echo "📁 Checking /tmp/ for logs..."
TMP_LOG_COUNT=$(ls -1 $TMP_LOGS/*.log 2>/dev/null | wc -l)
if [ "$TMP_LOG_COUNT" -gt 0 ]; then
    echo "⚠️ WARNING: Found ${TMP_LOG_COUNT} logs in /tmp/ (may be lost on reboot)!"
    ls -lh $TMP_LOGS/*.log 2>/dev/null | awk '{print "   -", $9, "("$5")"}'
    echo "   Action: Run ./scripts/sync-tmp-logs.sh"
else
    echo "✅ /tmp/ is clean (no logs)"
fi
echo ""

# Summary
echo "📊 Summary:"
if [ "$ROOT_LOGS" -eq 0 ] && [ "$TMP_LOG_COUNT" -eq 0 ]; then
    echo "✅ All logs are in the correct location!"
    echo "   Primary location: $PROJECT_LOGS/"
else
    echo "⚠️ Some logs may be in wrong locations"
    echo "   Action: Review above warnings"
fi
