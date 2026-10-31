from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import core, learning_commit_store, task_reconciler  # noqa: E402
from test_aiworkhub_canonical_task_engine_b852 import repo  # noqa: E402,F401

GATE_TASK_HEALTH = {
    "status": "not_evaluated",
    "reason": "route_gate_call:the_bootstrap_tool_reports_task_health",
}


@pytest.fixture
def manager_repo(repo, monkeypatch):
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(
        core,
        "_codex_manager_identity",
        lambda: {
            "provider": "codex",
            "session_id": "b8a3d1c4-0000-4000-8000-00000000c0de",
            "thread_id": "b8a3d1c4-0000-4000-8000-00000000c0de",
        },
    )
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_CONTRACT_DELIVERIES", {})
    monkeypatch.setattr(core, "_run_off_request_path", lambda target, *, name: None)
    calls = {"coverage": 0, "reconciler": 0}

    def counting_coverage(root):
        calls["coverage"] += 1
        return {"ok": True}

    def counting_reconciler(root):
        calls["reconciler"] += 1
        return {"ok": True}

    monkeypatch.setattr(learning_commit_store, "coverage", counting_coverage)
    monkeypatch.setattr(task_reconciler, "reconciler_health", counting_reconciler)
    return calls


def test_gate_call_skips_task_health_scans(manager_repo):
    gate = core.manager_bootstrap()
    assert manager_repo == {"coverage": 0, "reconciler": 0}
    assert gate["task_health"] == GATE_TASK_HEALTH
    assert gate["role"] == "manager"


def test_bootstrap_call_still_reports_task_health(manager_repo):
    bootstrap = core.manager_bootstrap(bootstrap_call=True)
    assert manager_repo["coverage"] == 1
    assert bootstrap["task_health"]["writes_allowed"] is True
    assert "learning_coverage" in bootstrap["task_health"]
