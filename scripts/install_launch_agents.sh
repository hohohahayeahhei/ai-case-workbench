#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
python_bin="${PYTHON_BIN:-$project_root/.venv/bin/python}"
exec "$python_bin" "$project_root/scripts/deploy_background.py" --install "$@"
