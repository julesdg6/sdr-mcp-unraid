#!/usr/bin/env bash
set -euo pipefail

mkdir -p /config /recordings /data

if [[ "${MCP_TRANSPORT}" == "stdio" ]]; then
  exec sdr-mcp serve
fi

exec sdr-mcp serve --http --host "${MCP_HOST}" --port "${MCP_PORT}"
