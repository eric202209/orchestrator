#!/bin/bash

# Compatibility wrapper: keep a single reliable startup path.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/start.sh"
