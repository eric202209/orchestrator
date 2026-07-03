#!/usr/bin/env bash
# run_planner_relay.sh — WF-B wrapper
#
# Bridges the Orchestrator workflow file contract to the stateless relay.
#
#   HANDOFF_DRAFT.md → relay/input.md
#   [relay runs]
#   relay/output.md  → NEXT_PROMPT.md
#
# The relay script knows nothing about HANDOFF_DRAFT or NEXT_PROMPT.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

WORKFLOW_DIR="${WORKFLOW_DIR:-$REPO_ROOT/docs/roadmap/workflow}"
RELAY_DIR="${RELAY_DIR:-$REPO_ROOT/relay}"

HANDOFF="$WORKFLOW_DIR/HANDOFF_DRAFT.md"
NEXT_PROMPT="$WORKFLOW_DIR/NEXT_PROMPT.md"
INPUT="$RELAY_DIR/input.md"
OUTPUT="$RELAY_DIR/output.md"

echo "=== WF-B Planner Relay ==="
echo ""

# Verify browser-session container is running
if ! docker inspect orchestrator-browser-session &>/dev/null; then
    echo "ERROR: browser-session container is not running."
    echo "Start it with:"
    echo "  docker compose -f docker-compose.browser-session.yml up -d"
    exit 1
fi

# Verify HANDOFF_DRAFT.md exists
if [[ ! -f "$HANDOFF" ]]; then
    echo "ERROR: HANDOFF_DRAFT.md not found at $HANDOFF"
    exit 1
fi

# Step 1: copy HANDOFF → input
echo "[wrapper] Copying HANDOFF_DRAFT.md → relay/input.md"
cp "$HANDOFF" "$INPUT"

# Step 2: run the stateless relay
echo "[wrapper] Running planner relay..."
RELAY_DIR="$RELAY_DIR" CDP_URL="http://localhost:9222" \
    python3 "$SCRIPT_DIR/planner_relay.py"

# Step 3: check output was written
if [[ ! -f "$OUTPUT" ]]; then
    echo "ERROR: relay/output.md was not created. Check relay/relay.log"
    exit 1
fi

# Step 4: copy output → NEXT_PROMPT
echo "[wrapper] Copying relay/output.md → NEXT_PROMPT.md"
cp "$OUTPUT" "$NEXT_PROMPT"

echo ""
echo "=== Done ==="
echo "Review NEXT_PROMPT.md, then run:"
echo "  scripts/developer_utilities/run_executor_subtask.sh"
