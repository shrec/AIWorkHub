#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${PYTHONPATH:-$ROOT/src}"
export AIWORKHUB_REPO="${AIWORKHUB_REPO:-$(cd "$ROOT/../.." && pwd)}"
export AIWORKHUB_ALLOW_WRITES="${AIWORKHUB_ALLOW_WRITES:-0}"

exec python3 -m aiworkhub.server

