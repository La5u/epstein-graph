#!/usr/bin/env bash
# Bring every derived output up to date (downloads not included). Incremental:
# only steps whose inputs changed rerun; see scripts/build.py. Arguments pass
# through: --force rebuilds everything, -n explains what would run.
# Environment: VIEW_EDGES, COMENTION_EDGES, COMPANY_EDGES, VICTIM_EDGES, VIEWER=public (skip the encrypted real names; default includes them).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p output
.venv/bin/python -B scripts/build.py "$@"
echo "Open output/graph.html (enter the viewer password to show real names); ranked leads in output/new_connections.csv"
