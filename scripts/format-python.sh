#!/bin/bash
# Format Python files with Black
# This script should be run on a machine with Black installed

set -e

echo "Formatting Python files with Black..."
cd "$(dirname "$0")/.."

# Install black if not present
if ! command -v black &> /dev/null; then
    echo "Installing Black..."
    pip install black
fi

# Run Black on the app directory
black app/

echo "✅ Formatting complete!"
