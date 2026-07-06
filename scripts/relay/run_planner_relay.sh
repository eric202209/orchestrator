#!/usr/bin/env bash
# run_planner_relay.sh — WF-B/WF-C/WF-D wrapper
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
CDP_URL="${CDP_URL:-http://localhost:9222}"
RELAY_EXPECTED_CONVERSATION_URL="${RELAY_EXPECTED_CONVERSATION_URL:-}"

HANDOFF="$WORKFLOW_DIR/HANDOFF_DRAFT.md"
NEXT_PROMPT="$WORKFLOW_DIR/NEXT_PROMPT.md"
INPUT="$RELAY_DIR/input.md"
OUTPUT="$RELAY_DIR/output.md"
REPLAY_DIR="$RELAY_DIR/replay"
RESUME=0
BUNDLE=0
VERBOSE=0

usage() {
    cat <<EOF
Usage: scripts/relay/run_planner_relay.sh [--resume] [--bundle] [--verbose]

  --resume   resume from relay/state.json; never sends automatically
  --bundle   create relay/replay/replay-YYYYMMDD-HHMMSS.zip and exit
  --verbose  include verbose relay diagnostics
EOF
}

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --resume)
            RESUME=1
            ;;
        --bundle)
            BUNDLE=1
            ;;
        --verbose)
            VERBOSE=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1"
            usage
            exit 2
            ;;
    esac
    shift
done

bundle_replay() {
    mkdir -p "$REPLAY_DIR"
    chmod a+rwx "$REPLAY_DIR"

    local timestamp
    timestamp="$(date -u +%Y%m%d-%H%M%S)"
    local archive="$REPLAY_DIR/replay-$timestamp.zip"
    local bundle_python="$REPO_ROOT/.relay-venv/bin/python"
    if [[ ! -x "$bundle_python" ]]; then
        bundle_python="python3"
    fi

    "$bundle_python" - "$RELAY_DIR" "$archive" <<'PYEOF'
import sys
import zipfile
from pathlib import Path

relay_dir = Path(sys.argv[1])
archive = Path(sys.argv[2])
files = [
    "input.md",
    "output.md",
    "relay.log",
    "metrics.jsonl",
    "session_snapshot.json",
    "state.json",
]

with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for name in files:
        path = relay_dir / name
        if path.exists():
            zf.write(path, arcname=name)
        else:
            zf.writestr(f"MISSING-{name}.txt", f"{name} was not present when bundled.\n")

print(archive)
PYEOF
    chmod a+rw "$archive"
    echo "[wrapper] Replay package created: $archive"
}

echo "=== WF-B/WF-C/WF-D Planner Relay ==="
echo ""

if [[ "$BUNDLE" == "1" ]]; then
    bundle_replay
    exit 0
fi

# Preflight (WF-C): browser-session up, CDP/noVNC reachable, expected
# conversation open, login valid, dirs/selectors/files in place.
echo "[wrapper] Running preflight..."
if ! "$SCRIPT_DIR/check_relay.sh"; then
    echo ""
    echo "ERROR: Preflight FAILED. See diagnostics above. Relay not started."
    exit 1
fi
echo ""

if [[ "$RESUME" == "1" ]]; then
    echo "[wrapper] Resume requested; keeping existing relay/input.md and relay/state.json"
else
    # Verify HANDOFF_DRAFT.md exists
    if [[ ! -f "$HANDOFF" ]]; then
        echo "ERROR: HANDOFF_DRAFT.md not found at $HANDOFF"
        exit 1
    fi

    # Step 1: copy HANDOFF → input
    echo "[wrapper] Copying HANDOFF_DRAFT.md → relay/input.md"
    cp "$HANDOFF" "$INPUT"
    chmod a+rw "$INPUT"
fi

# Step 2: run the stateless relay
echo "[wrapper] Running planner relay..."
RELAY_ARGS=()
if [[ "$RESUME" == "1" ]]; then
    RELAY_ARGS+=("--resume")
fi
if [[ "$VERBOSE" == "1" ]]; then
    RELAY_ARGS+=("--verbose")
fi
RELAY_DIR="$RELAY_DIR" CDP_URL="$CDP_URL" \
    RELAY_EXPECTED_CONVERSATION_URL="$RELAY_EXPECTED_CONVERSATION_URL" \
    "$REPO_ROOT/.relay-venv/bin/python" "$SCRIPT_DIR/planner_relay.py" \
    "${RELAY_ARGS[@]}"

# Step 3: check output was written
if [[ ! -f "$OUTPUT" ]]; then
    echo "ERROR: relay/output.md was not created. Check relay/relay.log"
    exit 1
fi

# Step 4: copy output → NEXT_PROMPT
echo "[wrapper] Copying relay/output.md → NEXT_PROMPT.md"
cp "$OUTPUT" "$NEXT_PROMPT"
chmod a+rw "$NEXT_PROMPT"

echo ""
echo "=== Done ==="
echo "Review NEXT_PROMPT.md, then run:"
echo "  scripts/developer_utilities/run_executor_subtask.sh"
