#!/usr/bin/env bash
# Double-click to launch Notula (or run ./run.command from a terminal).
# Uses the app's own venv; that venv only needs pyobjc (see requirements.txt).
set -euo pipefail
cd "$(dirname "$0")"

PY="./.venv/bin/python3"
if [[ ! -x "$PY" ]]; then
  echo "First run: creating the app venv and installing pyobjc…"
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

exec "$PY" notula.py
