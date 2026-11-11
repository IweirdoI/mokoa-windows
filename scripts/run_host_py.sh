#!/usr/bin/env bash
# Run project Python on Kylin V11 host glibc from inside Cursor/KARE.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST_LD=/run/host/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2
HOST_PY=/run/host/usr/bin/python3.12
LIB=/run/host/usr/lib/x86_64-linux-gnu:/run/host/lib/x86_64-linux-gnu
VENV="$ROOT/.venv"
export PYTHONPATH="$ROOT:$VENV/lib/python3.12/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export VIRTUAL_ENV="$VENV"
export PATH="$VENV/bin:$PATH"
cd "$ROOT"
exec "$HOST_LD" --library-path "$LIB" "$HOST_PY" "$@"
