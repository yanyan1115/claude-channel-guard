#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [ ! -x "$DEFAULT_PYTHON" ]; then
    DEFAULT_PYTHON="python3"
fi
PYTHON_BIN="${PYTHON_BIN:-$DEFAULT_PYTHON}"
export CHANNEL_GUARD_DIR="${CHANNEL_GUARD_DIR:-$HOME/.claude/channel-guard}"
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

"$PYTHON_BIN" -m channel_guard.memory_write_guard "$@"
