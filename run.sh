#!/usr/bin/env bash
# Start Algorithm Arena.
#
# Creates a virtual environment on first run, installs the dependencies and
# serves the app.  Binds to 0.0.0.0 so it is reachable from outside the
# machine/container.
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
PYTHON="${PYTHON:-python3}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

if [ ! -d "$VENV" ]; then
  echo "Creating virtual environment in $VENV ..."
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r requirements.txt
  echo "Dependencies installed."
fi

echo "Algorithm Arena -> http://$HOST:$PORT"
exec "$VENV/bin/python" -m uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"
