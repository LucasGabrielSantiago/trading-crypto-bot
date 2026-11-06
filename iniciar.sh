#!/bin/sh
cd "$(dirname "$0")" || exit 1
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt
exec .venv/bin/python main.py ui
