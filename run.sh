#!/usr/bin/env bash
# One-command local run: creates a venv, installs deps, loads .env if present, starts the app.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv >/dev/null 2>&1 || true
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements-dev.txt
if [ -f .env ]; then set -a; source .env; set +a; fi
if [ "${1:-}" = "test" ]; then exec python -m pytest -q; fi
exec uvicorn countersign.main:app --host 0.0.0.0 --port "${PORT:-8000}"
