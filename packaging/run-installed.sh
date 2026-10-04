#!/bin/sh
# Debian dependencies are installed by apt; never run pip from an installed app.
set -eu
app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$app_dir${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
exec /usr/bin/python3 -m typevoice "$@"
