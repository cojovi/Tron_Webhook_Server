#!/usr/bin/env bash
set -euo pipefail
PORT="${WEBHOOK_PORT:-9876}"
if command -v fuser >/dev/null 2>&1; then
  fuser -k "${PORT}/tcp" 2>/dev/null || true
fi
pids=$(ss -tlnp 2>/dev/null | grep ":${PORT}" | grep -oP 'pid=\K[0-9]+' || true)
for p in $pids; do kill "$p" 2>/dev/null || true; done
sleep 0.5
echo "port ${PORT} listeners cleared"
