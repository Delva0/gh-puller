#!/bin/sh
set -eu
exec uvicorn agent_chat.app:application --factory --host 0.0.0.0 --port "${PORT:-10000}" \
  --workers 1 --no-access-log --timeout-graceful-shutdown 20
