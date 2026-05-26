#!/usr/bin/env bash
set -euo pipefail

mkdir -p /config /recordings /data

if [[ "${MCP_TRANSPORT}" == "stdio" ]]; then
  exec sdr-mcp serve
fi

python -m http.server "${FRONTEND_PORT}" --bind 0.0.0.0 -d /opt/web_sota &
frontend_pid=$!

sdr-mcp serve --http --host "${MCP_HOST}" --port "${MCP_PORT}" &
mcp_pid=$!

cleanup() {
  kill "${mcp_pid}" "${frontend_pid}" 2>/dev/null || true
  wait "${mcp_pid}" "${frontend_pid}" 2>/dev/null || true
}

trap cleanup SIGINT SIGTERM

wait -n "${mcp_pid}" "${frontend_pid}"
status=$?
cleanup
exit "${status}"
