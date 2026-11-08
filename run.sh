#!/usr/bin/env bash
# Contract: ./run.sh <SIM_URL> starts the controller and runs until the scenario finishes. No Docker.
#
# State lives in one SQLite file (CONTROLLER_DB, default state/controller.db). Re-running against the same
# simulator world resumes from it (crash recovery). The store is checked against the simulator's own records
# first; a different world, including a fresh restart of the same scenario, gets a new store and the old file
# is archived next to it.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
PY="${PYTHON:-$(command -v python3 || command -v python)}"
exec "$PY" -m controller run --url "$SIM_URL" --db "${CONTROLLER_DB:-state/controller.db}"
