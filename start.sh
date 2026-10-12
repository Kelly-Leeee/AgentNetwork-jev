#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi

if [[ ! -x .venv/bin/agentrouter ]]; then
  .venv/bin/python -m pip install -e .
fi
exec .venv/bin/agentrouter --canvas "$@"
