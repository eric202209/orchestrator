#!/usr/bin/env bash
# Public compact WSL2 Docker/Ollama startup entrypoint.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export ORCHESTRATOR_DIR="${ORCHESTRATOR_DIR:-"$(cd "$SCRIPT_DIR/.." && pwd)"}"

exec "$SCRIPT_DIR/developer_utilities/wsl-ollama-start.sh" "$@"
