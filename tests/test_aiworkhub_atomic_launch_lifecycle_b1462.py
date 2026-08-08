from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from aiworkhub import core, process_launcher, task_engine, task_plan, task_store


RUNNER = "deepseek_tasking"
TOPIC = "tasking_system"
NOW = "2026-08-03T00:00:00+00:00"


def _repo(tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    return repo


def _insert(repo: Path, task_id: str, *, status: str = "pending", worker_status: str = "unclaimed", claimed_by: str = "", launch_request_id: str = "") -> None:
    readiness = task_store.storage_readiness(repo)
    card = {
        "task_id": task_id,
        "runner": RUNNER,
        "topic": TOPIC,
        "status": status,
        "worker_status": worker_status,
        "claimed_by": claimed_by,
        "launch_request_id": launch_request_id,
        "claim_epoch": 1 if status == "processing" else 0,
        "read_only": True,
        "allowed_writes": [],
        "required_outputs": [],
        "project_context": {"task_type": "research"},
    }
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at, claimed_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                RUNNER,
                TOPIC,
                "solo",
                status,
                worker_status,
                "normal",
                "objective",
                json.dumps(card, ensure_ascii=False, sort_keys=True),
                NOW,
                NOW,
                claimed_by,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_auto_pickup_claim_can_atomically_attach_exact_launch(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _insert(repo, "TASK_ATTACH")
    picked = core.auto_pickup(RUNNER, TOPIC)
    assert picked["ok"] is True, picked

    attached = task_engine.claim_start_exact(
        repo, "TASK_ATTACH", RUNNER, TOPIC, request_id="request-1"
    )
    assert attached["ok"] is True, attached
    card = task_store.get_task(repo, "TASK_ATTACH") or {}
    assert card["status"] == "processing"
    assert card["claimed_by"] == RUNNER
    assert card["launch_request_id"] == "request-1"
    assert card["claim_epoch"] == 1

    conflicting = task_engine.claim_start_exact(
        repo, "TASK_ATTACH", RUNNER, TOPIC, request_id="request-2"
    )
    assert conflicting["ok"] is False
    assert "launch_request_conflict" in conflicting["stderr"]


def test_launcher_preflight_accepts_only_unattached_owned_claim(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _insert(
        repo,
        "TASK_PREFLIGHT",
        status="processing",
        worker_status="claimed",
        claimed_by=RUNNER,
    )
    monkeypatch.setattr(process_launcher.repo_policy, "validate_launch", lambda *_args: {"ok": True})
    manager = process_launcher.ProcessManager(
        repo=repo,
        isolation_enabled=False,
        collision_guard=lambda **_kwargs: {"returncode": 0},
    )
    card = manager._preflight_card("TASK_PREFLIGHT", RUNNER, TOPIC, "vscode_lm")
    assert card["status"] == "processing"

    assert task_engine.claim_start_exact(
        repo, "TASK_PREFLIGHT", RUNNER, TOPIC, request_id="request-1"
    )["ok"]
    try:
        manager._preflight_card("TASK_PREFLIGHT", RUNNER, TOPIC, "vscode_lm")
    except process_launcher.LaunchRejected as exc:
        assert "task_launch_already_attached" in str(exc)
    else:
        raise AssertionError("an attached launch must not pass preflight twice")


def test_blocked_predecessor_does_not_retain_collision_scope(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _insert(repo, "BLOCKED_PREDECESSOR", status="blocked", worker_status="blocked")
    _insert(repo, "READY_REPLACEMENT")

    blocked = task_store.get_task(repo, "BLOCKED_PREDECESSOR") or {}
    ready = task_store.get_task(repo, "READY_REPLACEMENT") or {}
    for card in (blocked, ready):
        card["allowed_writes"] = ["src/aiworkhub/token_budget.py"]
        conn = sqlite3.connect(task_store.storage_readiness(repo).canonical_db)
        try:
            conn.execute(
                "UPDATE tasks SET card_json=? WHERE task_id=?",
                (json.dumps(card, ensure_ascii=False, sort_keys=True), card["task_id"]),
            )
            conn.commit()
        finally:
            conn.close()

    active = core._active_cards_for_collision_guard()
    assert {card["task_id"] for card in active} == {"READY_REPLACEMENT"}
    assert core.collision_guard(print_json=True)["ok"] is True


def test_launch_failure_is_blocked_not_review_and_is_request_scoped(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _insert(
        repo,
        "TASK_FAILED",
        status="processing",
        worker_status="claimed",
        claimed_by=RUNNER,
        launch_request_id="request-1",
    )
    wrong = task_engine.mark_launch_failed(
        repo,
        "TASK_FAILED",
        RUNNER,
        reason="sandbox failed",
        request_id="request-2",
    )
    assert wrong["ok"] is False
    assert (task_store.get_task(repo, "TASK_FAILED") or {})["status"] == "processing"

    result = task_engine.mark_launch_failed(
        repo,
        "TASK_FAILED",
        RUNNER,
        reason="sandbox failed",
        request_id="request-1",
    )
    assert result["ok"] is True, result
    card = task_store.get_task(repo, "TASK_FAILED") or {}
    assert card["status"] == "blocked"
    assert card["worker_status"] == "launch_failed"
    assert card["terminal_substatus"] == "launch_failed"
    assert card["status"] != "review"


def test_timed_out_worker_is_blocked_with_original_gate_denominator(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _insert(
        repo,
        "TASK_TIMED_OUT",
        status="processing",
        worker_status="claimed",
        claimed_by=RUNNER,
        launch_request_id="request-timeout",
    )
    card = task_store.get_task(repo, "TASK_TIMED_OUT") or {}
    card["required_outputs"] = ["out/result.json"]
    card["validation"] = ["python3 -m pytest -q tests/test_result.py"]
    conn = sqlite3.connect(task_store.storage_readiness(repo).canonical_db)
    try:
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card, ensure_ascii=False, sort_keys=True), "TASK_TIMED_OUT"),
        )
        conn.commit()
    finally:
        conn.close()

    evidence = {
        "error": "worker exceeded deadline",
        **process_launcher._declared_failure_denominators(card),
    }
    result = task_engine.mark_terminal_failure(
        repo,
        "TASK_TIMED_OUT",
        RUNNER,
        "timed_out",
        evidence=evidence,
        request_id="request-timeout",
    )
    assert result["ok"] is True, result
    failed = task_store.get_task(repo, "TASK_TIMED_OUT") or {}
    assert failed["status"] == "blocked"
    assert failed["worker_status"] == "timed_out"
    assert failed["terminal_substatus"] == "timed_out"
    verdict = failed["deterministic_verification"]["evidence_verdict"]
    assert verdict["validation_count"] == 1
    assert verdict["failed_validation_count"] == 1
    assert verdict["required_output_count"] == 1
    assert verdict["missing_required_output_count"] == 1
    assert failed["terminal_failure"]["evidence"]["validation"][0]["not_run"] is True
    assert failed["terminal_failure"]["evidence"]["required_outputs"][0]["missing"] is True

    review = core.review_queue()
    assert review["ok"] is True
    assert "TASK_TIMED_OUT" not in review["stdout"]


def test_plan_counts_unattached_processing_as_operational_blocker():
    snapshot = task_plan.build_snapshot(
        [
            {
                "task_id": "TASK_ORPHAN",
                "runner": RUNNER,
                "topic": TOPIC,
                "status": "processing",
                "worker_status": "claimed",
                "claimed_by": RUNNER,
                "launch_request_id": "",
                "allowed_writes": [],
                "depends_on": [],
                "created_at": NOW,
            }
        ]
    )
    assert snapshot["orphaned_processing"] == ["TASK_ORPHAN"]
    assert snapshot["operational_blockers"] == {
        "TASK_ORPHAN": "processing_without_launch_request"
    }
    assert snapshot["blocked_task_ids"] == ["TASK_ORPHAN"]
    assert snapshot["blocked_count"] == 1


def test_plan_surfaces_pending_preclaim_launch_blocker():
    snapshot = task_plan.build_snapshot(
        [
            {
                "task_id": "TASK_PRECLAIM_BLOCKED",
                "runner": RUNNER,
                "topic": TOPIC,
                "status": "pending",
                "worker_status": "unclaimed",
                "allowed_writes": [],
                "depends_on": [],
                "created_at": NOW,
                "operational_blocker": {
                    "kind": "launch_blocked",
                    "adapter_id": "vscode_lm",
                    "reason": "model consent required",
                },
            }
        ]
    )

    assert snapshot["ready"] == []
    assert snapshot["operational_blockers"] == {
        "TASK_PRECLAIM_BLOCKED": "model consent required"
    }
    assert snapshot["operational_blocked_count"] == 1
    assert snapshot["blocked_task_ids"] == ["TASK_PRECLAIM_BLOCKED"]
    assert snapshot["explicit_retry_task_ids"] == ["TASK_PRECLAIM_BLOCKED"]
    assert snapshot["explicit_retry_count"] == 1


def test_preclaim_launch_blocker_is_persisted_and_cleared_by_exact_claim(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _insert(repo, "TASK_RETRYABLE_BLOCKER")

    recorded = task_engine.record_launch_blocker(
        repo,
        "TASK_RETRYABLE_BLOCKER",
        RUNNER,
        TOPIC,
        adapter_id="vscode_lm",
        reason="model consent required",
    )
    assert recorded["ok"] is True, recorded
    blocked_card = task_store.get_task(repo, "TASK_RETRYABLE_BLOCKER") or {}
    assert blocked_card["status"] == "pending"
    assert blocked_card["worker_status"] == "unclaimed"
    assert blocked_card["operational_blocker"]["reason"] == "model consent required"

    claimed = task_engine.claim_start_exact(
        repo,
        "TASK_RETRYABLE_BLOCKER",
        RUNNER,
        TOPIC,
        request_id="request-retry",
    )
    assert claimed["ok"] is True, claimed
    retried_card = task_store.get_task(repo, "TASK_RETRYABLE_BLOCKER") or {}
    assert retried_card["status"] == "processing"
    assert "operational_blocker" not in retried_card


def test_preclaim_blocker_uses_coordinator_authority_for_legacy_codex_card(
    tmp_path, monkeypatch,
):
    repo = _repo(tmp_path, monkeypatch)
    _insert(repo, "TASK_LEGACY_CODEX_BLOCKER")
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        row = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?",
            ("TASK_LEGACY_CODEX_BLOCKER",),
        ).fetchone()
        card = json.loads(row[0])
        card["runner"] = "codex"
        conn.execute(
            "UPDATE tasks SET runner=?, card_json=? WHERE task_id=?",
            ("codex", json.dumps(card, ensure_ascii=False, sort_keys=True), "TASK_LEGACY_CODEX_BLOCKER"),
        )
        conn.commit()
    finally:
        conn.close()

    denied_claim = task_engine.claim_start_exact(
        repo,
        "TASK_LEGACY_CODEX_BLOCKER",
        "codex",
        TOPIC,
        request_id="must-not-claim",
    )
    assert denied_claim["ok"] is False
    assert "card_scoped_codex_forbidden" in denied_claim["stderr"]

    recorded = task_engine.record_launch_blocker(
        repo,
        "TASK_LEGACY_CODEX_BLOCKER",
        "codex",
        TOPIC,
        adapter_id="glm_vscode_lm",
        reason="coordinator_runner_cannot_launch_worker",
    )
    assert recorded["ok"] is True, recorded
    card = task_store.get_task(repo, "TASK_LEGACY_CODEX_BLOCKER") or {}
    assert card["status"] == "pending"
    assert card["worker_status"] == "unclaimed"
    assert card["operational_blocker"]["reason"] == "coordinator_runner_cannot_launch_worker"


def test_preclaim_launch_blocker_gate_denial_reports_exact_operation(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _insert(repo, "TASK_BLOCKER_GATE")
    monkeypatch.setattr(
        task_engine.core,
        "_canonical_write_gate",
        lambda *args, **kwargs: {
            "ok": False,
            "returncode": 126,
            "command": ["claim-start"],
            "stdout": "",
            "stderr": "write_gate_closed",
        },
    )

    result = task_engine.record_launch_blocker(
        repo,
        "TASK_BLOCKER_GATE",
        RUNNER,
        TOPIC,
        adapter_id="vscode_lm",
        reason="model consent required",
    )

    assert result["ok"] is False
    assert result["command"] == ["launch-blocked", "TASK_BLOCKER_GATE", "--runner", RUNNER]
    assert result["stderr"] == "write_gate_closed"


def test_readonly_scope_is_valid_but_required_output_without_scope_is_not(tmp_path):
    process_launcher._validate_scope(
        tmp_path,
        {"read_only": True, "allowed_writes": [], "required_outputs": []},
    )
    try:
        process_launcher._validate_scope(
            tmp_path,
            {
                "read_only": True,
                "allowed_writes": [],
                "required_outputs": ["out/result.json"],
            },
        )
    except process_launcher.LaunchRejected as exc:
        assert "allowed_writes_empty" in str(exc)
    else:
        raise AssertionError("required output without write authority must fail")


def test_launch_failure_storage_error_is_a_bounded_result(tmp_path):
    repo = tmp_path / "uninitialised"
    repo.mkdir()
    result = task_engine.mark_launch_failed(
        repo, "TASK_NONE", RUNNER, reason="failed", request_id="request-1"
    )
    assert result["ok"] is False
    assert result["returncode"] == 1
