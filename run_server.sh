#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

PORT="${PORT:-8000}"

echo "Starting LeadMeLeads on 0.0.0.0:${PORT}"

python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
