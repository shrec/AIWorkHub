#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${PYTHONPATH:-$ROOT/src}"
export GEOAI_REPO="${GEOAI_REPO:-$(cd "$ROOT/../.." && pwd)}"
export GEOAI_TASK_MCP_ALLOW_WRITES="${GEOAI_TASK_MCP_ALLOW_WRITES:-0}"

exec python3 -m geoai_task_mcp.server

