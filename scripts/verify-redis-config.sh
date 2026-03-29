#!/bin/bash
# Redis dump.rdb Prevention Verification Script
# Purpose: Verify Redis is configured to prevent dump.rdb in workspace
# Usage: ./scripts/verify-redis-config.sh

set -e

echo "🔍 Verifying Redis configuration..."
echo ""

# Check 1: Redis working directory
echo "1. Redis working directory:"
REDIS_DIR=$(redis-cli CONFIG GET dir | tail -1)
if [ "$REDIS_DIR" == "/tmp" ]; then
    echo "   ✅ Correct: $REDIS_DIR"
else
    echo "   ❌ WRONG: $REDIS_DIR (should be /tmp)"
    echo "   Action: Restart Redis with 'redis-server --daemonize yes --dir /tmp'"
fi
echo ""

# Check 2: dump.rdb in workspace root
echo "2. dump.rdb in workspace root:"
if [ -f /root/.openclaw/workspace/dump.rdb ]; then
    echo "   ❌ FOUND: /root/.openclaw/workspace/dump.rdb"
    echo "   Action: Delete and fix Redis config"
    rm -f /root/.openclaw/workspace/dump.rdb
    echo "   ✅ Deleted"
else
    echo "   ✅ Clean"
fi
echo ""

# Check 3: dump.rdb in projects directory
echo "3. dump.rdb in projects directory:"
if [ -f /root/.openclaw/workspace/projects/orchestrator/dump.rdb ]; then
    echo "   ❌ FOUND: /root/.openclaw/workspace/projects/orchestrator/dump.rdb"
    echo "   Action: Delete and fix Redis config"
    rm -f /root/.openclaw/workspace/projects/orchestrator/dump.rdb
    echo "   ✅ Deleted"
else
    echo "   ✅ Clean"
fi
echo ""

# Summary
echo "📊 Summary:"
if [ "$REDIS_DIR" == "/tmp" ] && \
   [ ! -f /root/.openclaw/workspace/dump.rdb ] && \
   [ ! -f /root/.openclaw/workspace/projects/orchestrator/dump.rdb ]; then
    echo "✅ Redis configuration is correct!"
    echo "   - Working directory: /tmp"
    echo "   - No dump.rdb in workspace"
else
    echo "⚠️  Redis configuration needs attention"
    echo "   Action: Run 'redis-server --daemonize yes --dir /tmp'"
fi
