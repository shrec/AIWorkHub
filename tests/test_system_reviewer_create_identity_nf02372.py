"""NF-2026-02372: a reconciler-driven system reviewer create needs no live manager.

Measured: 28 review_orchestrator_lens_blocked events with
``reviewer_launch_failed:quality_review_task_create_failed:
manager_identity_required:task_create``. The background reconciler's ancestry
is not the manager's, so ``_live_manager_identity()`` is None there and
create_task refused before the NF-2026-01095 route inheritance was reached.

Run: python -m pytest -q -o pythonpath=src tests/test_system_reviewer_create_identity_nf02372.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import core, task_store  # noqa: E402

from test_quality_reviewer_target_route import _CLAUDE_SESSION, _insert_target  # noqa: E402

_TARGET = "TARGET_02372"
_REFUSED = "manager_identity_required:task_create"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_live_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda _runner: (True, "ok"))
    return root


def _create(task_id: str, target: str | None, **overrides) -> dict:
    fields = {
        "topic": "quality_review",
        "read_only": True,
        "allowed_writes": [],
    }
    fields.update(overrides)
    return core.create_task(
        task_id=task_id,
        title=f"Independent correctness review for {target}",
        runner="worker_02372",
        objective="Review the exact candidate packet.",
        acceptance=["Exactly one authenticated quality_review_submit receipt"],
        required_outputs=[],
        validation=[],
        priority="high",
        callback_required=True,
        task_type="research",
        review_target_task_id=target,
        **fields,
    )


def test_system_reviewer_is_created_without_a_live_manager_and_inherits_the_route(repo):
    _insert_target(repo, _TARGET)

    result = _create("QUALITY_REVIEW_02372", _TARGET)

    assert result["ok"] is True, result.get("stderr")
    card = task_store.get_task(repo, "QUALITY_REVIEW_02372")
    target = task_store.get_task(repo, _TARGET)
    assert card["origin_thread_id"] == _CLAUDE_SESSION == target["origin_thread_id"]
    assert card["coordinator_provider"] == "claude"
    assert str(card.get("manager_route_state") or "") == str(target.get("manager_route_state") or "")


def test_without_a_review_target_the_create_is_still_refused(repo):
    _insert_target(repo, _TARGET)

    result = _create("QUALITY_REVIEW_02372_NO_TARGET", None)

    assert result["ok"] is False
    assert result["stderr"] == _REFUSED


@pytest.mark.parametrize(
    ("target", "overrides"),
    [
        (_TARGET, {"topic": "task_mcp"}),
        (_TARGET, {"read_only": False}),
        (_TARGET, {"allowed_writes": ["src/aiworkhub/core.py"]}),
        ("UNKNOWN_TARGET_02372", {}),
    ],
    ids=["topic", "read_only_false", "allowed_writes", "unknown_target"],
)
def test_any_other_create_without_a_live_manager_is_refused(repo, target, overrides):
    _insert_target(repo, _TARGET)

    result = _create("QUALITY_REVIEW_02372_REFUSED", target, **overrides)

    assert result["ok"] is False
    assert result["stderr"] == _REFUSED
    assert task_store.get_task(repo, "QUALITY_REVIEW_02372_REFUSED") is None


def test_the_coordinator_capability_gate_still_refuses(repo, monkeypatch):
    _insert_target(repo, _TARGET)
    monkeypatch.setattr(
        core,
        "_verify_coordinator_capability",
        lambda _runner: (False, "coordinator_capability_denied:token_mismatch"),
    )

    result = _create("QUALITY_REVIEW_02372_CAPABILITY", _TARGET)

    assert result["ok"] is False
    assert result["stderr"] == "coordinator_capability_denied:token_mismatch"


def test_no_mcp_tool_can_pass_a_review_target():
    server = (SRC / "aiworkhub" / "server.py").read_text(encoding="utf-8")

    assert "review_target_task_id" not in server
