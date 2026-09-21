#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python_bin="${PYTHON_BIN:-.venv/bin/python}"
collection_status=0
"$python_bin" scripts/run_collection.py --source-mode live --catalog --max-results 12 --retry-pending --notify "$@" || collection_status=$?
"$python_bin" scripts/backup_catalog.py
exit "$collection_status"
