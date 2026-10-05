#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"
python3 -B publish_github.py --check-package
python3 -B -m unittest discover -s tests -v
exec sh install.sh "$@"
