#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AIWORKHUB_REPO="${AIWORKHUB_REPO:-$(cd "$ROOT/../.." && pwd)}"

export PYTHONPATH="$ROOT/src"
export AIWORKHUB_REPO
export AIWORKHUB_ALLOW_WRITES=0

python3 -m aiworkhub.health >/tmp/aiworkhub_health.json
python3 - <<'PY'
import json
from pathlib import Path

data = json.loads(Path("/tmp/aiworkhub_health.json").read_text())
assert data["ok"] is True, data
assert data["writes_allowed"] is False, data
assert data["verify"]["returncode"] == 0, data
print("health ok")
PY

python3 - <<'PY'
from aiworkhub import core

review = core.review_queue()
assert "stdout" in review
blocked = core.auto_pickup("smoke_runner_no_write", "signal_atlas")
assert blocked["returncode"] == 126, blocked
assert "write command blocked" in blocked["stderr"], blocked
print("read tools ok; write gate ok")
PY

