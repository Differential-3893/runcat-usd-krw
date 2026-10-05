#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BOOTSTRAP=$(command -v python3) || { echo 'Python 3.10+ is required.' >&2; exit 1; }
exec "$BOOTSTRAP" -B "$SCRIPT_DIR/scripts/manage_install.py" install "$@"
