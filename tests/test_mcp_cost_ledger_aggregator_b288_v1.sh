#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO"

export PYTHONPATH="$REPO/tools/geoai-task-mcp/src"
export GEOAI_REPO="$REPO"

python3 - <<'PY'
import json
from pathlib import Path

from geoai_task_mcp import core, cost_ledger, launch_queue_persist

repo = Path(__import__("os").environ["GEOAI_REPO"])
eval_path = repo / "tools/geoai-task-mcp/eval/mcp_cost_ledger_aggregator_b288_v1.json"

def fake_usage_report(runner=None, topic=None, status=None):
    return {
        "ok": True,
        "stdout": "\n".join([
            "TASK_A | runner=runner_a | topic=lexicon | records=1 | tokens=30 | in=10 out=20 | cost=$0.3000",
            "TASK_B | runner=runner_b | topic=signal_atlas | records=2 | tokens=70 | in=30 out=40 | cost=$0.7000",
        ]),
    }

def fake_log(max_entries=10000):
    return {
        "ok": True,
        "last_entries": [
            {
                "task_id": "TASK_C",
                "runner": "runner_a",
                "topic": "lexicon",
                "model": "model_c",
                "requested_at": "2026-07-09T00:00:00+00:00",
                "usage_input_tokens": 5,
                "usage_output_tokens": 7,
                "usage_total_tokens": 12,
                "cost_usd": 0.12,
            },
            {
                "task_id": "TASK_A",
                "runner": "runner_a",
                "topic": "lexicon",
                "model": "",
                "requested_at": "2026-07-09T00:00:01+00:00",
                "usage_input_tokens": 999,
                "usage_output_tokens": 999,
                "usage_total_tokens": 1998,
                "cost_usd": 99,
            },
        ],
    }

core.usage_report = fake_usage_report
launch_queue_persist.read_persisted_log = fake_log

result = cost_ledger.build_cost_ledger(include_tasks=True)
assert result["readonly"] is True
assert result["counts"]["usage_rows"] == 2
assert result["counts"]["launch_rows"] == 2
assert result["counts"]["union_rows"] == 3
assert round(result["aggregates"]["by_topic"]["lexicon"]["cost_usd"], 2) == 0.42
assert round(result["aggregates"]["by_topic"]["signal_atlas"]["cost_usd"], 2) == 0.70
assert result["aggregates"]["by_runner"]["runner_a"]["total_tokens"] == 42
assert all(v is False for v in result["authority_flags"].values())

doc = {
    "schema_id": "geoai.mcp_cost_ledger_aggregator.b288.v1",
    "task_id": "CLAUDE_TASK_MCP_COST_LEDGER_AGGREGATOR_B288_V1",
    "runner": "claude_task_mcp_cost_ledger_b288",
    "topic": "task_mcp",
    "verdict": "PASS",
    "metrics": result["counts"],
    "authority_flags": result["authority_flags"],
    "aggregates": result["aggregates"],
}
eval_path.parent.mkdir(parents=True, exist_ok=True)
eval_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
print("B288_COST_LEDGER_VERDICT=PASS")
PY

grep -q '"verdict": "PASS"' tools/geoai-task-mcp/eval/mcp_cost_ledger_aggregator_b288_v1.json
