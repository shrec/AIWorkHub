"""NF-02395: MCP-layer fold of the aiworkhub_completion_inbox reply.

Synthetic reply: 150 terminal + 3 live agent processes, 40 blocked + 2 review
validation facts. The fold must keep only live rows, drop blocked facts, keep
counts consistent, leave the input untouched and stay well under 16000 bytes.
"""

from __future__ import annotations

import ast
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from aiworkhub import completion_inbox

SERVER_PATH = Path(__file__).resolve().parents[1] / "src" / "aiworkhub" / "server.py"
TERMINAL_STATES = ("completed", "failed")
LIVE_STATES = ("launching", "running", "finalizing")


def _process_row(index: int, state: str, task_id: str | None = None) -> dict[str, Any]:
    return {
        "request_id": f"req-{index:04d}",
        "task_id": task_id or f"T{index:04d}",
        "runner": "codex_cli_b01",
        "topic": "demo",
        "adapter_id": "demo_adapter",
        "model": "demo-model",
        "state": state,
        "exit_code": 0,
        "timestamp": "2026-11-24T00:00:00+00:00",
        "finished_at": "2026-11-24T00:01:00+00:00",
        "accepted": False,
        "review_ready": False,
        "liveness_lost": False,
        "workspace_retained": False,
    }


def _fact(index: int, lifecycle_state: str) -> dict[str, Any]:
    return {
        "task_id": f"B{index:04d}",
        "runner": "runner_b01",
        "topic": "demo",
        "lifecycle_state": lifecycle_state,
        "validation_status": "failed",
        "validation_error": "scratch validation failed",
        "blocker_reason": None,
        "last_activity_at": "2026-11-24T00:00:00+00:00",
    }


def _synthetic_reply() -> dict[str, Any]:
    rows = [_process_row(i, TERMINAL_STATES[i % 2]) for i in range(150)]
    rows += [_process_row(150 + i, state) for i, state in enumerate(LIVE_STATES)]
    rows += [
        _process_row(153, "review_ready", task_id="R-KEEP"),
        _process_row(154, "review_ready", task_id="R-DONE"),
    ]
    review_facts = [_fact(900 + i, "review") for i in range(2)]
    blocked_facts = [_fact(i, "blocked") for i in range(40)]
    facts = review_facts + blocked_facts
    return {
        "tool": "aiworkhub_completion_inbox",
        "review_queue": [{"task_id": "R-KEEP"}],
        "operational_failures": [],
        "latest_validation_facts": facts,
        "agent_processes": {
            "schema_id": "aiworkhub.completion_process_summary.v1",
            "processes": rows,
            "detail_fields_omitted": True,
            "detail_tool": "aiworkhub_agent_task_status",
        },
        "counts": {
            "review_queue": 1,
            "latest_validation_facts": len(facts),
        },
    }


def test_fold_keeps_only_live_process_rows_and_counts_terminal_rows() -> None:
    reply = _synthetic_reply()
    folded = completion_inbox.fold_completion_inbox_reply(reply)
    processes = folded["agent_processes"]

    assert processes["schema_id"] == "aiworkhub.completion_process_summary.v2"
    assert {row["state"] for row in processes["processes"]} == set(LIVE_STATES) | {"review_ready"}
    assert len(processes["processes"]) == 4
    assert [
        row["task_id"] for row in processes["processes"] if row["state"] == "review_ready"
    ] == ["R-KEEP"]
    assert processes["processes_omitted"] == 151
    assert processes["detail_fields_omitted"] is True
    assert processes["detail_tool"] == "aiworkhub_agent_task_status"
    assert processes["processes_by_state"] == {
        "completed": 75,
        "failed": 75,
        "launching": 1,
        "running": 1,
        "finalizing": 1,
        "review_ready": 2,
    }
    assert sum(processes["processes_by_state"].values()) == 155


def test_fold_drops_blocked_facts_and_keeps_review_facts_identical() -> None:
    reply = _synthetic_reply()
    folded = completion_inbox.fold_completion_inbox_reply(reply)

    assert folded["latest_validation_facts"] == reply["latest_validation_facts"][:2]
    assert json.dumps(folded["latest_validation_facts"], sort_keys=True) == json.dumps(
        reply["latest_validation_facts"][:2], sort_keys=True
    )
    assert folded["counts"]["latest_validation_facts_blocked_omitted"] == 40
    assert folded["counts"]["latest_validation_facts"] == 42
    assert folded["blocked_validation_facts_detail_tool"] == "aiworkhub_task_show"


def test_fold_does_not_mutate_input_and_is_deterministic() -> None:
    reply = _synthetic_reply()
    snapshot = deepcopy(reply)
    first = completion_inbox.fold_completion_inbox_reply(reply)
    second = completion_inbox.fold_completion_inbox_reply(reply)

    assert reply == snapshot
    assert first == second


def test_folded_reply_is_under_16000_bytes_while_unfolded_is_larger() -> None:
    reply = _synthetic_reply()
    folded = completion_inbox.fold_completion_inbox_reply(reply)

    assert len(json.dumps(reply).encode("utf-8")) > 16000
    assert len(json.dumps(folded).encode("utf-8")) < 16000


def test_server_returns_fold_as_last_call() -> None:
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "aiworkhub_completion_inbox"
    )
    last = handler.body[-1]
    assert isinstance(last, ast.Return)
    call = last.value
    assert isinstance(call, ast.Call)
    assert ast.unparse(call.func) == "completion_inbox.fold_completion_inbox_reply"
    assert [ast.unparse(arg) for arg in call.args] == ["result"]
