#!/usr/bin/env bash
# TypeVoice 启动脚本：首次运行自动把依赖装到 .deps/（不需要 root / venv），
# 然后启动 App。可用参数见 README（run / record-once / test-asr / test-polish / autostart…）。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPS="$DIR/.deps"

if [ ! -d "$DEPS/numpy" ]; then
    echo "首次运行：安装 Python 依赖到 $DEPS …"
    python3 -m pip install --quiet --disable-pip-version-check \
        --target "$DEPS" -r "$DIR/requirements.txt"
fi

export PYTHONPATH="$DEPS:$DIR${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m typevoice "$@"
