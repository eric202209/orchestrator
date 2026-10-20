#!/usr/bin/bash
set -euf

if [[ "$#" -lt 1 ]]; then
    exit 64
fi

case "$1" in
    install|refresh|gate-a) ;;
    *) exit 64 ;;
esac

SCRIPT_PATH="${BASH_SOURCE[0]}"
if [[ "${SCRIPT_PATH}" != /* || -L "${SCRIPT_PATH}" ]]; then
    exit 78
fi
SCRIPT_DIR="${SCRIPT_PATH%/*}"
cd /
exec /usr/bin/env -i \
    HOME=/root \
    LANG=C.UTF-8 \
    LC_ALL=C \
    PATH=/usr/bin:/bin \
    /usr/bin/python3.12 -I -S "${SCRIPT_DIR}/release_entrypoint.py" "$@"
