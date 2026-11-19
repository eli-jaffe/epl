#!/bin/bash
# Pull the snapshots GitHub Actions has been landing in epl-data, then load any
# that aren't in the DuckDB file yet. Run from anywhere: ingestion/refresh_snapshots.sh
# (stop the backend first, or let the loader's lock retry wait it out).
set -euo pipefail
cd "$(dirname "$0")"
git -C "$HOME/epl-data" pull --ff-only
../venv/bin/python load_live_snapshot.py
