#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROCESS_HELPER="${SCRIPT_DIR}/scripts/maintenance/project_process.py"

echo "Stopping verified processes owned by ${SCRIPT_DIR}..."
/usr/bin/python3 "${PROCESS_HELPER}" stop-all \
    --root "${SCRIPT_DIR}" \
    --timeout "${PROJECT_PROCESS_STOP_TIMEOUT:-30}"
echo "Project process shutdown complete."
