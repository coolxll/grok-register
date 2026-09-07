#!/usr/bin/env bash
# Forward resin proxy from rn-direct (127.0.0.11:37595) to local port 10800
# Usage: ./forward_resin.sh
# Proxy auth: Default.xai / resinproxy

set -euo pipefail

LOCAL_PORT=10800
REMOTE_HOST="rn-direct"
REMOTE_ADDR="127.0.0.1:11300"

echo "Forwarding resin proxy: localhost:${LOCAL_PORT} -> ${REMOTE_HOST}:${REMOTE_ADDR}"
echo "Proxy auth: Default.xai / resinproxy"
echo "Press Ctrl+C to stop"

exec ssh -N -L "${LOCAL_PORT}:${REMOTE_ADDR}" \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  "${REMOTE_HOST}"
