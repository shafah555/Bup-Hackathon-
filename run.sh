#!/usr/bin/env bash
# Convenience launcher for the GridWise service.
set -euo pipefail

HOST="${APP_HOST:-0.0.0.0}"
PORT="${APP_PORT:-8000}"

exec python -m uvicorn app.main:app --host "$HOST" --port "$PORT" --log-level info
