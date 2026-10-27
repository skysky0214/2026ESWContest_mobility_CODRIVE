#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
task_python="${CODRIVE_TEST_PYTHON:-python3}"
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
PYTHONPATH="$PWD/src" "$task_python" -m pytest tests -ra "$@"
PYTHONPATH="$PWD/board_runtime/src" "$task_python" -m pytest -c board_runtime/pyproject.toml board_runtime/tests -ra "$@"
