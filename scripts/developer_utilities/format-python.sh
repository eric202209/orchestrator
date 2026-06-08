#!/usr/bin/env bash
# Format Python files with Black

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PYTHON="${PROJECT_ROOT}/venv/bin/python"

echo "Formatting Python files with Black..."
cd "${PROJECT_ROOT}"

if [ -x "${VENV_PYTHON}" ]; then
    PYTHON="${VENV_PYTHON}"
else
    PYTHON="${PYTHON:-python3}"
fi

"${PYTHON}" -m black app/

echo "✅ Formatting complete!"
