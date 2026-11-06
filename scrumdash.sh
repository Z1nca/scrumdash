#!/bin/sh
cd "$(dirname "$0")/app"
PY=$(command -v python3 || command -v python) || { echo "Install Python 3.10 or newer, then start this again."; exit 1; }
"$PY" app.py
