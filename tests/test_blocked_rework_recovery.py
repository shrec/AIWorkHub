from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import task_store


_TERMINAL_REVIEW_EVIDENCE = {
    "validation": [
        {"command": "pytest -q", "returncode": 1, "stdout": "1 failed", "stderr": ""}
    ],
    "required_outputs": [],
    "changed_path_hashes": {
        "src/aiworkhub/task_store.py": "abc123hash",
    },
}


def _retained_terminal_failure_fixture(
    tmp_path: Path, *, required_outputs: bool = False,
) -> tuple[Path, Path, dict[str, str]]:
    repo = _setup_repo(tmp_path)
    request_id = "a" * 32
    workspace = repo / ".aiworkhub" / "runtime" / "worktrees" / request_id / "worktree"
    baseline = repo / "src" / "aiworkhub" / "task_store.py"
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes(b"baseline\n")
    baseline_digest = hashlib.sha256(baseline.read_bytes()).hexdigest()
    baseline_token = f"file:664:{baseline_digest}"
    candidate = workspace / "src" / "aiworkhub" / "task_store.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"retained candidate\n")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    authority = {
        "schema_id": "aiworkhub.python_candidate_authority.v1",
        "sources": [
            {
                "path": "src/aiworkhub/task_store.py",
                "state": "modified",
                "bytes_sha256": digest,
            }
        ],
    }
    workspace_metadata = {
        "request_id": request_id,
        "repo": str(repo),
        "path": str(workspace),
        "allowed_writes": ["src/aiworkhub/task_store.py"],
        "parent_baseline": {"src/aiworkhub/task_store.py": baseline_token},
        "base_oid": "b" * 40,
        "python_candidate_authority": authority,
    }
    evidence = {
        "required_outputs": [],
        "changed_paths": ["src/aiworkhub/task_store.py"],
        "changed_path_hashes": {"src/aiworkhub/task_store.py": digest},
        "request_identity": {
            "request_id": request_id,
            "task_id": "BLOCKED_RETAINED_EMPTY_OUTPUTS",
            "runner": "codex_worker_test",
            "topic": "aiworkhub_blocked_rework_recovery",
            "repo": str(repo),
            "claim_epoch": 1,
            "allowed_writes": ["src/aiworkhub/task_store.py"],
            "parent_baseline": {"src/aiworkhub/task_store.py": baseline_token},
            "base_oid": "b" * 40,
        },
        "workspace": workspace_metadata,
        "python_candidate_authority": authority,
    }
    if required_outputs:
        test_path = "tests/test_retained_candidate.py"
        test_baseline = repo / test_path
        test_baseline.parent.mkdir(parents=True)
        test_baseline.write_bytes(b"baseline test\n")
        test_candidate = workspace / test_path
        test_candidate.parent.mkdir(parents=True)
        test_candidate.write_bytes(b"retained test\n")
        test_digest = hashlib.sha256(test_candidate.read_bytes()).hexdigest()
        workspace_metadata["allowed_writes"].append(test_path)
        workspace_metadata["parent_baseline"][test_path] = (
            "file:664:" + hashlib.sha256(test_baseline.read_bytes()).hexdigest()
        )
        evidence["request_identity"]["allowed_writes"].append(test_path)
        evidence["request_identity"]["parent_baseline"].update(
            workspace_metadata["parent_baseline"]
        )
        evidence["changed_paths"].append(test_path)
        evidence["changed_path_hashes"][test_path] = test_digest
        evidence["required_outputs"] = [
            {"path": path, "sha256": "file:664:" + digest}
            for path, digest in evidence["changed_path_hashes"].items()
        ]
        authority["sources"].append({
            "path": test_path, "state": "modified", "bytes_sha256": test_digest,
        })
    _insert_processing_task(
        repo, "BLOCKED_RETAINED_EMPTY_OUTPUTS", request_id=request_id,
        allowed_writes=workspace_metadata["allowed_writes"],
        required_outputs=(workspace_metadata["allowed_writes"] if required_outputs else []),
    )
    assert task_store.mark_terminal_failure(
        repo,
        "BLOCKED_RETAINED_EMPTY_OUTPUTS",
        runner="codex_worker_test",
        substatus="finalize_failed",
        evidence=evidence,
        request_id=request_id,
        claim_epoch=1,
    ) == (True, "blocked")
    return repo, candidate, evidence["changed_path_hashes"]


@pytest.mark.parametrize("validation_only_replay", [False, True])
@pytest.mark.parametrize("required_outputs", [False, True])
def test_recover_blocked_terminal_failure_retained_delta_without_required_outputs(
    tmp_path: Path, validation_only_replay: bool, required_outputs: bool,
) -> None:
    repo, candidate, hashes = _retained_terminal_failure_fixture(
        tmp_path, required_outputs=required_outputs,
    )

    candidate.write_bytes(b"tampered retained candidate\n")
    assert task_store.recover_blocked_rework(
        repo,
        "BLOCKED_RETAINED_EMPTY_OUTPUTS",
        actor="coordinator",
        feedback_reason="NeedFix: replay retained candidate",
        validation_only_replay=validation_only_replay,
    ) == (False, "retained_terminal_candidate_hash_mismatch")
    blocked_card = _get_card(repo, "BLOCKED_RETAINED_EMPTY_OUTPUTS")
    assert blocked_card["status"] == "blocked"
    assert "rework_predecessor" not in blocked_card

    candidate.write_bytes(b"retained candidate\n")
    assert task_store.recover_blocked_rework(
        repo,
        "BLOCKED_RETAINED_EMPTY_OUTPUTS",
        actor="coordinator",
        feedback_reason="NeedFix: replay retained candidate",
        validation_only_replay=validation_only_replay,
    ) == (True, "recovered")
    card = _get_card(repo, "BLOCKED_RETAINED_EMPTY_OUTPUTS")
    assert card["rework_predecessor"]["request_id"] == "a" * 32
    assert card["rework_predecessor"]["changed_path_hashes"] == hashes
    assert card["recovery_feedback"] == "NeedFix: replay retained candidate"
    if validation_only_replay:
        assert card["validation_only_replay_authorization"]["next_claim_epoch"] == 2
    else:
        assert "validation_only_replay_authorization" not in card
        assert task_store.recover_blocked_rework(
            repo, "BLOCKED_RETAINED_EMPTY_OUTPUTS", actor="coordinator",
            feedback_reason="NeedFix: replay retained candidate",
        ) == (True, "already_recovered")


@pytest.mark.parametrize(("mutation", "expected"), [
    ("missing_event", "no_retained_predecessor_evidence"),
    ("event_mismatch", "retained_terminal_candidate_event_mismatch"),
    ("claim_epoch", "retained_terminal_candidate_claim_epoch_invalid"),
    ("boolean_claim_epoch", "retained_terminal_candidate_claim_epoch_invalid"),
    ("identity_epoch", "retained_terminal_candidate_identity_invalid"),
    ("request_id", "retained_terminal_candidate_identity_invalid"),
    ("launch_request_id", "retained_terminal_candidate_identity_invalid"),
    ("task_id", "retained_terminal_candidate_identity_invalid"),
    ("repo", "retained_terminal_candidate_identity_invalid"),
    ("baseline", "retained_terminal_candidate_baseline_mismatch"),
    ("live_pid", "retained_terminal_candidate_process_live"),
    ("symlink", "retained_terminal_candidate_bytes_invalid"),
    ("hardlink", "retained_terminal_candidate_bytes_invalid"),
    ("pinned_predecessor", "no_retained_predecessor_evidence"),
    ("terminal_review", "hard_blocker:scope_rejected"),
])
def test_normal_rework_terminal_failure_fallback_fails_closed(
    tmp_path: Path, mutation: str, expected: str,
) -> None:
    repo, candidate, _hashes = _retained_terminal_failure_fixture(
        tmp_path, required_outputs=True,
    )
    task_id = "BLOCKED_RETAINED_EMPTY_OUTPUTS"
    card = _get_card(repo, task_id)
    failure = card["terminal_failure"]
    evidence = failure["evidence"]
    if mutation == "event_mismatch":
        failure["recorded_at"] = "not-the-recorded-event"
    elif mutation == "claim_epoch":
        card["claim_epoch"] = 2
    elif mutation == "boolean_claim_epoch":
        card["claim_epoch"] = True
    elif mutation == "identity_epoch":
        evidence["request_identity"]["claim_epoch"] = True
    elif mutation == "request_id":
        failure["request_id"] = "b" * 32
    elif mutation == "launch_request_id":
        card["launch_request_id"] = "b" * 32
    elif mutation == "task_id":
        evidence["request_identity"]["task_id"] = "OTHER_TASK"
    elif mutation == "repo":
        evidence["request_identity"]["repo"] = str(tmp_path)
    elif mutation == "baseline":
        (repo / "src/aiworkhub/task_store.py").write_bytes(b"new baseline\n")
    elif mutation == "live_pid":
        evidence["pid"] = os.getpid()
    elif mutation == "symlink":
        target = candidate.with_suffix(".retained")
        candidate.rename(target)
        candidate.symlink_to(target)
    elif mutation == "hardlink":
        os.link(candidate, candidate.with_suffix(".retained"))
    elif mutation == "pinned_predecessor":
        card["rework_predecessor"] = {
            "request_id": "reviewer-pinned-request", "workspace": {},
        }

    _ready, db_path = task_store._require_ready(repo)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card), task_id),
        )
        if mutation == "missing_event":
            conn.execute("DELETE FROM task_events WHERE task_id=?", (task_id,))
        elif mutation == "terminal_review":
            conn.execute(
                "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) "
                "VALUES (?, 'terminal_review', ?, ?, ?)",
                (task_id, card["runner"], json.dumps({"substatus": "scope_rejected"}),
                 "2026-08-06T00:00:00+00:00"),
            )
        elif mutation != "event_mismatch":
            conn.execute(
                "UPDATE task_events SET payload_json=? "
                "WHERE task_id=? AND event='terminal_failure'",
                (json.dumps(failure), task_id),
            )
        before_card = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", (task_id,),
        ).fetchone()
        before_events = conn.execute(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY rowid", (task_id,),
        ).fetchall()

    assert task_store.recover_blocked_rework(
        repo, task_id, actor="coordinator", feedback_reason="Fix retained test",
    ) == (False, expected)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", (task_id,),
        ).fetchone() == before_card
        assert conn.execute(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY rowid", (task_id,),
        ).fetchall() == before_events


def _setup_repo(tmp_path: Path) -> Path:
    """Create and initialize a minimal repo for task_store tests."""
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    return repo


def _insert_processing_task(
    repo: Path, task_id: str, *, request_id: str,
    allowed_writes: list[str] | None = None,
    required_outputs: list[str] | None = None,
) -> None:
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-08-06T00:00:00+00:00"
    card = {
        "task_id": task_id,
        "runner": "codex_worker_test",
        "topic": "aiworkhub_blocked_rework_recovery",
        "allowed_writes": allowed_writes or ["src/aiworkhub/task_store.py"],
        "required_outputs": required_outputs or [],
        "claim_epoch": 1,
        "launch_request_id": request_id,
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, "
            "started_at) VALUES (?, ?, ?, 'processing', 'in_progress', '', '', ?, ?, ?, ?, ?, ?)",
            (task_id, "codex_worker_test", "aiworkhub_blocked_rework_recovery", json.dumps(card),
             now, now, "codex_worker_test", now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_blocked_task(
    repo: Path,
    task_id: str,
    *,
    runner: str = "codex_worker_test",
    terminal_substatus: str = "validation_failed",
    terminal_evidence: dict | None = None,
    reject_review_reason: str = "",
    extra_card: dict | None = None,
    worker_status: str = "validation_failed",
    claimed_by: str = "",
    topic: str = "aiworkhub_blocked_rework_recovery",
) -> None:
    """Insert a blocked task with terminal-review predecessor evidence."""
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-08-06T00:00:00+00:00"

    evidence = dict(terminal_evidence or _TERMINAL_REVIEW_EVIDENCE)
    card = {
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "mode": "",
        "allowed_writes": ["src/aiworkhub/task_store.py"],
        "objective": "Implement blocked rework recovery",
        "terminal_review": {
            "substatus": terminal_substatus,
            "evidence": evidence,
            "deterministic_verification": {
                "applicable": True,
                "pass": False,
                "substatus": terminal_substatus,
                "reason": "evidence_verdict_failed",
                "claim_epoch": 0,
                "evidence_verdict": {
                    "passed": False,
                    "validation_count": 1,
                    "failed_validation_count": 1,
                    "required_output_count": 0,
                    "missing_required_output_count": 0,
                },
            },
            "recorded_at": now,
            "runner": runner,
            "claim_epoch": 0,
        },
        "terminal_substatus": terminal_substatus,
        "deterministic_verification": {
            "applicable": True,
            "pass": False,
            "substatus": terminal_substatus,
            "reason": "evidence_verdict_failed",
            "claim_epoch": 0,
            "evidence_verdict": {"passed": False, "validation_count": 1, "failed_validation_count": 1},
        },
        "blocker_reason": f"{terminal_substatus}: tests failed",
        "blocked_at": now,
        "blocked_by": runner,
        "claim_epoch": 0,
    }
    if reject_review_reason:
        card["reject_review"] = {
            "to": "pending",
            "reason": reject_review_reason,
            "recorded_at": now,
        }
    if extra_card:
        card.update(extra_card)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, started_at, "
            "completed_at) "
            "VALUES (?, ?, ?, 'blocked', ?, '', '', "
            "?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                runner,
                topic,
                worker_status,
                json.dumps(card),
                now,
                now,
                claimed_by,
                now if claimed_by else "",
                now if claimed_by else "",
                now,
            ),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, 'terminal_review', ?, ?, ?)",
            (
                task_id,
                runner,
                json.dumps({"substatus": terminal_substatus, "evidence": evidence}),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _get_card(repo: Path, task_id: str) -> dict:
    task = task_store.get_task(repo, task_id)
    assert task is not None, f"task {task_id} not found"
    return task


# ---------------------------------------------------------------------------
# Successful recovery
# ---------------------------------------------------------------------------


def test_recover_blocked_validation_failed_with_feedback_requeues_to_pending(
    tmp_path: Path,
) -> None:
    """A blocked task with retained predecessor evidence and residual feedback
    is safely requeued to pending, preserving history."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "BLOCKED_NEEDFIX_01",
        terminal_substatus="validation_failed",
        reject_review_reason="NeedFix: handle edge case when input is empty",
    )

    ok, state = task_store.recover_blocked_rework(
        repo,
        "BLOCKED_NEEDFIX_01",
        actor="coordinator",
        feedback_reason="NeedFix: handle edge case when input is empty",
    )
    assert (ok, state) == (True, "recovered")

    task = _get_card(repo, "BLOCKED_NEEDFIX_01")
    assert task["status"] == "pending"
    assert task["worker_status"] == "unclaimed"
    assert task.get("claimed_by") in (None, "")
    assert task.get("claimed_at") in (None, "")
    assert task.get("claim_epoch") == 1
    assert task.get("recovery_epoch") == 1
    assert task.get("recovered_by") == "coordinator"
    assert task.get("recovery_feedback") == "NeedFix: handle edge case when input is empty"

    # Predecessor evidence is preserved.
    pred = task.get("recovery_predecessor")
    assert isinstance(pred, dict)
    assert pred["terminal_substatus"] == "validation_failed"
    assert pred["changed_path_hashes"] == {
        "src/aiworkhub/task_store.py": "abc123hash",
    }

    # Audit event is recorded.
    events = task_store.get_task_events(repo, "BLOCKED_NEEDFIX_01")
    event_names = [e["event"] for e in events]
    assert "blocked_rework_recovery" in event_names
    # Terminal review event is preserved.
    assert "terminal_review" in event_names


def test_recover_blocked_worker_failed_with_feedback_requeues_to_pending(
    tmp_path: Path,
) -> None:
    """A worker_failed blocked task with feedback is recoverable."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "BLOCKED_WORKER_FAIL",
        terminal_substatus="worker_failed",
        reject_review_reason="NeedFix: worker crashed due to OOM",
    )

    ok, state = task_store.recover_blocked_rework(
        repo,
        "BLOCKED_WORKER_FAIL",
        actor="coordinator",
        feedback_reason="NeedFix: worker crashed due to OOM",
    )
    assert (ok, state) == (True, "recovered")
    assert _get_card(repo, "BLOCKED_WORKER_FAIL")["status"] == "pending"


def test_quality_review_recovery_requires_packet_bound_relaunch(
    tmp_path: Path,
) -> None:
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "BLOCKED_REVIEWER",
        topic="quality_review",
        terminal_substatus="worker_failed",
        reject_review_reason="Retry exact reviewer packet",
        extra_card={
            "quality_review": {
                "target_request_id": "a" * 32,
                "target_task_id": "TARGET_TASK",
                "packet_sha256": "b" * 64,
            }
        },
    )

    ok, state = task_store.recover_blocked_rework(
        repo,
        "BLOCKED_REVIEWER",
        actor="coordinator",
        feedback_reason="Retry exact reviewer packet",
    )

    assert (ok, state) == (
        False,
        "quality_review_recovery_requires_bound_relaunch",
    )
    assert _get_card(repo, "BLOCKED_REVIEWER")["status"] == "blocked"
    events = task_store.get_task_events(repo, "BLOCKED_REVIEWER")
    assert "blocked_rework_recovery" not in {event["event"] for event in events}


def _missing_rework_predecessor(repo: Path, tmp_path: Path) -> dict:
    request_id = "a" * 32
    return {
        "request_id": request_id,
        "changed_path_hashes": {"src/aiworkhub/task_store.py": "b" * 64},
        "workspace": {
            "request_id": request_id,
            "repo": str(repo),
            "path": str((tmp_path / "removed-worktree").resolve()),
            "home": str((tmp_path / "removed-home").resolve()),
            "allowed_writes": ["src/aiworkhub/task_store.py"],
        },
    }


def test_recover_missing_predecessor_can_explicitly_use_clean_root(
    tmp_path: Path,
) -> None:
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "CLEAN_ROOT_MISSING_PREDECESSOR",
        terminal_substatus="validation_failed",
        reject_review_reason="Reconstruct the bounded candidate on current HEAD",
        extra_card={
            "rework_predecessor": _missing_rework_predecessor(repo, tmp_path),
        },
    )

    ok, state = task_store.recover_blocked_rework(
        repo,
        "CLEAN_ROOT_MISSING_PREDECESSOR",
        actor="coordinator",
        feedback_reason="Reconstruct the bounded candidate on current HEAD",
        clean_root_if_predecessor_missing=True,
    )

    assert (ok, state) == (True, "recovered")
    task = _get_card(repo, "CLEAN_ROOT_MISSING_PREDECESSOR")
    assert "rework_predecessor" not in task
    assert task["recovery_mode"] == "clean_root_missing_predecessor"
    authorization = task["clean_root_recovery_authorization"]
    assert authorization["predecessor_request_id"] == "a" * 32
    assert authorization["changed_path_hashes"] == {
        "src/aiworkhub/task_store.py": "b" * 64
    }
    events = task_store.get_task_events(repo, "CLEAN_ROOT_MISSING_PREDECESSOR")
    assert "blocked_rework_recovery" in {event["event"] for event in events}


def test_already_recovered_missing_predecessor_can_be_clean_root_authorized(
    tmp_path: Path,
) -> None:
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "CLEAN_ROOT_AFTER_RECOVERY",
        terminal_substatus="validation_failed",
        reject_review_reason="Reconstruct on current HEAD",
        extra_card={
            "rework_predecessor": _missing_rework_predecessor(repo, tmp_path),
        },
    )
    assert task_store.recover_blocked_rework(
        repo,
        "CLEAN_ROOT_AFTER_RECOVERY",
        actor="coordinator",
        feedback_reason="Reconstruct on current HEAD",
    ) == (True, "recovered")

    ok, state = task_store.recover_blocked_rework(
        repo,
        "CLEAN_ROOT_AFTER_RECOVERY",
        actor="coordinator",
        feedback_reason="Reconstruct on current HEAD",
        clean_root_if_predecessor_missing=True,
    )

    assert (ok, state) == (True, "recovered_clean_root")
    task = _get_card(repo, "CLEAN_ROOT_AFTER_RECOVERY")
    assert "rework_predecessor" not in task
    assert task["recovery_mode"] == "clean_root_missing_predecessor"
    events = task_store.get_task_events(repo, "CLEAN_ROOT_AFTER_RECOVERY")
    assert "blocked_rework_clean_root_authorized" in {
        event["event"] for event in events
    }


def test_clean_root_recovery_is_incompatible_with_validation_only_replay(
    tmp_path: Path,
) -> None:
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "CLEAN_ROOT_REPLAY_FORBIDDEN",
        terminal_substatus="validation_failed",
        reject_review_reason="Replay exact retained bytes",
        extra_card={
            "rework_predecessor": _missing_rework_predecessor(repo, tmp_path),
        },
    )

    ok, state = task_store.recover_blocked_rework(
        repo,
        "CLEAN_ROOT_REPLAY_FORBIDDEN",
        actor="coordinator",
        feedback_reason="Replay exact retained bytes",
        validation_only_replay=True,
        clean_root_if_predecessor_missing=True,
    )

    assert (ok, state) == (
        False,
        "clean_root_incompatible_with_validation_only_replay",
    )


# ---------------------------------------------------------------------------
# Idempotent retry
# ---------------------------------------------------------------------------


def test_recover_already_recovered_task_returns_idempotent_already_recovered(
    tmp_path: Path,
) -> None:
    """Calling recover on an already-recovered (pending) task returns
    already_recovered without duplicating audit history."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "IDEMPOTENT_01",
        terminal_substatus="validation_failed",
        reject_review_reason="NeedFix: rework needed",
    )

    # First recovery.
    ok, state = task_store.recover_blocked_rework(
        repo, "IDEMPOTENT_01", actor="coordinator",
        feedback_reason="NeedFix: rework needed",
    )
    assert (ok, state) == (True, "recovered")
    events_after_first = len(task_store.get_task_events(repo, "IDEMPOTENT_01"))

    # Second call -- idempotent.
    ok, state = task_store.recover_blocked_rework(
        repo, "IDEMPOTENT_01", actor="coordinator",
        feedback_reason="NeedFix: rework needed",
    )
    assert (ok, state) == (True, "already_recovered")

    # No duplicate audit event.
    events_after_second = len(task_store.get_task_events(repo, "IDEMPOTENT_01"))
    assert events_after_second == events_after_first

    # Task is still pending.
    assert _get_card(repo, "IDEMPOTENT_01")["status"] == "pending"


def test_recover_allows_second_cycle_after_reblock(
    tmp_path: Path,
) -> None:
    """A task that was recovered, re-claimed, and blocked again can be
    recovered a second time (new recovery cycle)."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "CYCLE_02",
        terminal_substatus="validation_failed",
        reject_review_reason="NeedFix: first attempt",
    )

    # First recovery.
    ok, _ = task_store.recover_blocked_rework(
        repo, "CYCLE_02", actor="coordinator",
        feedback_reason="NeedFix: first attempt",
    )
    assert ok

    # Simulate re-block (worker picked up, failed again).
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET status='blocked', worker_status='validation_failed' "
            "WHERE task_id='CYCLE_02'"
        )
        conn.commit()
    finally:
        conn.close()

    # Second recovery should succeed (new cycle).
    ok, state = task_store.recover_blocked_rework(
        repo, "CYCLE_02", actor="coordinator",
        feedback_reason="NeedFix: second attempt",
    )
    assert (ok, state) == (True, "recovered")
    assert _get_card(repo, "CYCLE_02")["status"] == "pending"


# ---------------------------------------------------------------------------
# Fail closed: hard blockers
# ---------------------------------------------------------------------------


def test_recover_dependency_blocked_task_fails_closed(
    tmp_path: Path,
) -> None:
    """A dependency-blocked task must not be silently recovered."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "DEP_BLOCKED_01",
        terminal_substatus="dependency_blocked",
        reject_review_reason="waiting for prerequisite",
    )

    ok, state = task_store.recover_blocked_rework(
        repo, "DEP_BLOCKED_01", actor="coordinator",
        feedback_reason="waiting for prerequisite",
    )
    assert (ok, state) == (False, "hard_blocker:dependency_blocked")
    assert _get_card(repo, "DEP_BLOCKED_01")["status"] == "blocked"


def test_recover_scope_rejected_task_fails_closed(
    tmp_path: Path,
) -> None:
    """A scope-rejected task must not be silently recovered."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "SCOPE_REJECTED_01",
        terminal_substatus="scope_rejected",
        reject_review_reason="out of scope",
    )

    ok, state = task_store.recover_blocked_rework(
        repo, "SCOPE_REJECTED_01", actor="coordinator",
        feedback_reason="out of scope",
    )
    assert (ok, state) == (False, "hard_blocker:scope_rejected")
    assert _get_card(repo, "SCOPE_REJECTED_01")["status"] == "blocked"


def test_recover_generic_blocked_substatus_fails_closed(
    tmp_path: Path,
) -> None:
    """A task with the generic 'blocked' terminal substatus fails closed
    since its blocker nature is unknown."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "GENERIC_BLOCKED_01",
        terminal_substatus="blocked",
        reject_review_reason="unspecified blocker",
    )

    ok, state = task_store.recover_blocked_rework(
        repo, "GENERIC_BLOCKED_01", actor="coordinator",
        feedback_reason="unspecified blocker",
    )
    assert (ok, state) == (False, "hard_blocker:blocked")
    assert _get_card(repo, "GENERIC_BLOCKED_01")["status"] == "blocked"


# ---------------------------------------------------------------------------
# Fail closed: missing evidence
# ---------------------------------------------------------------------------


def test_recover_without_terminal_review_evidence_fails_closed(
    tmp_path: Path,
) -> None:
    """A blocked task without retained terminal_review predecessor fails."""
    repo = _setup_repo(tmp_path)
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-08-06T00:00:00+00:00"
    card = {"task_id": "NO_EVIDENCE_01", "runner": "test", "topic": "test"}
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at, completed_at) "
            "VALUES ('NO_EVIDENCE_01', 'test', 'test', 'blocked', 'blocked', "
            "?, ?, ?, ?)",
            (json.dumps(card), now, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    ok, state = task_store.recover_blocked_rework(
        repo, "NO_EVIDENCE_01", actor="coordinator",
        feedback_reason="some feedback",
    )
    assert (ok, state) == (False, "no_retained_predecessor_evidence")
    assert _get_card(repo, "NO_EVIDENCE_01")["status"] == "blocked"


def test_recover_without_feedback_reason_fails_closed(
    tmp_path: Path,
) -> None:
    """A blocked task with predecessor evidence but no residual feedback fails."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "NO_FEEDBACK_01",
        terminal_substatus="validation_failed",
    )

    ok, state = task_store.recover_blocked_rework(
        repo, "NO_FEEDBACK_01", actor="coordinator",
        feedback_reason="",
    )
    assert (ok, state) == (False, "no_residual_feedback")
    assert _get_card(repo, "NO_FEEDBACK_01")["status"] == "blocked"


def test_recover_with_reject_review_feedback_from_card_succeeds(
    tmp_path: Path,
) -> None:
    """When feedback_reason is empty but card contains reject_review reason,
    recovery succeeds using the card-level feedback."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "CARD_FEEDBACK_01",
        terminal_substatus="validation_failed",
        reject_review_reason="NeedFix: card-level feedback present",
    )

    # feedback_reason param is empty, but card has reject_review.reason.
    ok, state = task_store.recover_blocked_rework(
        repo, "CARD_FEEDBACK_01", actor="coordinator",
        feedback_reason="",
    )
    assert (ok, state) == (True, "recovered")
    assert _get_card(repo, "CARD_FEEDBACK_01")["status"] == "pending"


# ---------------------------------------------------------------------------
# Fail closed: live claim
# ---------------------------------------------------------------------------


def test_recover_live_claimed_task_fails_closed(
    tmp_path: Path,
) -> None:
    """A task with an active claim must not be silently recovered."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "LIVE_CLAIM_01",
        terminal_substatus="validation_failed",
        reject_review_reason="NeedFix: rework",
        worker_status="claimed",
        claimed_by="some_worker",
    )

    ok, state = task_store.recover_blocked_rework(
        repo, "LIVE_CLAIM_01", actor="coordinator",
        feedback_reason="NeedFix: rework",
    )
    assert (ok, state) == (False, "live_claim_detected")


# ---------------------------------------------------------------------------
# Fail closed: non-blocked
# ---------------------------------------------------------------------------


def test_recover_pending_task_fails_closed(tmp_path: Path) -> None:
    """A pending (never-blocked) task must not be recovered."""
    repo = _setup_repo(tmp_path)
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-08-06T00:00:00+00:00"
    card = {"task_id": "FRESH_TASK", "runner": "test", "topic": "test"}
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at) "
            "VALUES ('FRESH_TASK', 'test', 'test', 'pending', 'unclaimed', ?, ?, ?)",
            (json.dumps(card), now, now),
        )
        conn.commit()
    finally:
        conn.close()

    ok, state = task_store.recover_blocked_rework(
        repo, "FRESH_TASK", actor="coordinator",
        feedback_reason="some feedback",
    )
    assert (ok, state) == (False, "not_blocked:current=pending")


def test_recover_processing_task_fails_closed(tmp_path: Path) -> None:
    """A processing task must not be recovered."""
    repo = _setup_repo(tmp_path)
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-08-06T00:00:00+00:00"
    card = {"task_id": "PROCESSING_TASK", "runner": "test", "topic": "test"}
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at, claimed_by, claimed_at) "
            "VALUES ('PROCESSING_TASK', 'test', 'test', 'processing', 'claimed', "
            "?, ?, ?, 'worker_1', ?)",
            (json.dumps(card), now, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    ok, state = task_store.recover_blocked_rework(
        repo, "PROCESSING_TASK", actor="coordinator",
        feedback_reason="some feedback",
    )
    assert ok is False
    assert "not_blocked" in state


# ---------------------------------------------------------------------------
# Fail closed: task not found
# ---------------------------------------------------------------------------


def test_recover_unknown_task_fails_closed(tmp_path: Path) -> None:
    """Recovering a non-existent task returns task_not_found."""
    repo = _setup_repo(tmp_path)
    ok, state = task_store.recover_blocked_rework(
        repo, "NONEXISTENT_TASK", actor="coordinator",
        feedback_reason="some feedback",
    )
    assert (ok, state) == (False, "task_not_found")


# ---------------------------------------------------------------------------
# Regression: NeedFix foundation scenario
# ---------------------------------------------------------------------------


def test_regression_blocked_needfix_foundation_scenario_preserves_lineage(
    tmp_path: Path,
) -> None:
    """Models the blocked NeedFix foundation scenario: a task undergoes
    terminal_review -> reject_review with NeedFix disposition, then is
    recovered for rework without creating a replacement task."""
    repo = _setup_repo(tmp_path)
    task_id = "NEEDFIX_FOUNDATION"

    # Step 1: Insert a blocked task with full NeedFix evidence.
    _insert_blocked_task(
        repo,
        task_id,
        terminal_substatus="validation_failed",
        reject_review_reason="NeedFix: fix the edge case in validation logic",
        extra_card={
            "predecessor_task_id": task_id,  # same ID, rework cycle
            "predecessor_sha256": "deadbeefcafe",
            "residual_feedback": "The validation should handle empty string input",
            "terminal_outcome": "validation_failed",
        },
    )

    # Step 2: Recover.
    ok, state = task_store.recover_blocked_rework(
        repo, task_id, actor="coordinator",
        feedback_reason="NeedFix: fix the edge case in validation logic",
    )
    assert (ok, state) == (True, "recovered")

    task = _get_card(repo, task_id)

    # Step 3: Task identity is preserved (no replacement).
    assert task["task_id"] == task_id
    assert task["runner"] == "codex_worker_test"
    assert task["topic"] == "aiworkhub_blocked_rework_recovery"

    # Step 4: Task is pending for rework.
    assert task["status"] == "pending"
    assert task["worker_status"] == "unclaimed"

    # Step 5: Lineage is preserved.
    assert task["claim_epoch"] == 1
    assert task["recovery_epoch"] == 1

    # Step 6: Predecessor evidence is pinned.
    pred = task["recovery_predecessor"]
    assert pred["terminal_substatus"] == "validation_failed"
    assert pred["changed_path_hashes"] == {
        "src/aiworkhub/task_store.py": "abc123hash",
    }

    # Step 7: Durable extra fields are preserved while current-episode truth
    # is cleared from card_json.
    assert task.get("predecessor_sha256") == "deadbeefcafe"
    assert task.get("residual_feedback") == "The validation should handle empty string input"
    assert task.get("terminal_outcome") in (None, "")

    # Step 8: Episode fields are cleared.
    assert task.get("terminal_substatus") in (None, "")
    assert task.get("blocker_reason") in (None, "")
    assert task.get("launch_error") in (None, "")
    assert task.get("launch_failed") in (None, "")

    # Step 9: Audit trail is intact.
    events = task_store.get_task_events(repo, task_id)
    event_names = [e["event"] for e in events]
    assert "terminal_review" in event_names
    assert "blocked_rework_recovery" in event_names

    # Step 10: Idempotent retry.
    ok2, state2 = task_store.recover_blocked_rework(
        repo, task_id, actor="coordinator",
        feedback_reason="NeedFix: fix the edge case in validation logic",
    )
    assert (ok2, state2) == (True, "already_recovered")


# ---------------------------------------------------------------------------
# History / evidence preservation
# ---------------------------------------------------------------------------


def test_recovery_preserves_original_terminal_review_in_events(
    tmp_path: Path,
) -> None:
    """Terminal evidence remains durable without contaminating a new episode."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "PRESERVE_EVIDENCE",
        terminal_substatus="timed_out",
        reject_review_reason="NeedFix: increase timeout",
    )

    task_store.recover_blocked_rework(
        repo, "PRESERVE_EVIDENCE", actor="coordinator",
        feedback_reason="NeedFix: increase timeout",
    )

    task = _get_card(repo, "PRESERVE_EVIDENCE")
    assert task.get("terminal_review") is None
    assert task.get("deterministic_verification") is None
    assert task["recovery_predecessor"]["terminal_substatus"] == "timed_out"

    events = task_store.get_task_events(repo, "PRESERVE_EVIDENCE")
    terminal_events = [event for event in events if event["event"] == "terminal_review"]
    assert len(terminal_events) == 1


def test_recovery_bumps_claim_epoch_for_fresh_lineage(
    tmp_path: Path,
) -> None:
    """Each recovery cycle produces a strictly increasing claim_epoch."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "EPOCH_BUMP",
        terminal_substatus="validation_failed",
        reject_review_reason="NeedFix: fix bug",
        extra_card={"claim_epoch": 2},
    )

    task_store.recover_blocked_rework(
        repo, "EPOCH_BUMP", actor="coordinator",
        feedback_reason="NeedFix: fix bug",
    )

    task = _get_card(repo, "EPOCH_BUMP")
    assert task["claim_epoch"] == 3
    assert task["recovery_epoch"] == 3


def test_validation_replay_binds_exact_reviewer_transport_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _setup_repo(tmp_path)
    task_id = "REVIEWER_TRANSPORT_EXACT"
    request_id = "1" * 32
    workspace = repo / ".aiworkhub" / "runtime" / "worktrees" / request_id / "worktree"
    candidate = workspace / "src" / "result.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"retained\n")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    predecessor = {
        "request_id": request_id,
        "task_id": task_id,
        "changed_path_hashes": {"src/result.py": digest},
        "workspace": {
            "request_id": request_id,
            "repo": str(repo),
            "path": str(workspace),
        },
    }
    _insert_blocked_task(
        repo,
        task_id,
        reject_review_reason="rerun retained validation",
        extra_card={"rework_predecessor": predecessor},
    )
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        for event_request_id in (request_id, "2" * 32):
            payload = {
                "substatus": "validation_failed",
                "evidence": {
                    "changed_path_hashes": {"src/result.py": digest},
                    "request_identity": {
                        "request_id": event_request_id,
                        "task_id": task_id,
                        "repo": str(repo),
                    },
                },
            }
            conn.execute(
                "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) "
                "VALUES (?, 'terminal_review', 'codex_worker_test', ?, ?)",
                (task_id, json.dumps(payload), "2026-08-06T00:00:01+00:00"),
            )
        conn.commit()
    finally:
        conn.close()

    original_read_bytes = Path.read_bytes

    def fail_candidate_read(path: Path) -> bytes:
        if path == candidate:
            raise OSError("candidate became unreadable")
        return original_read_bytes(path)

    with monkeypatch.context() as context:
        context.setattr(Path, "read_bytes", fail_candidate_read)
        assert task_store.recover_blocked_rework(
            repo,
            task_id,
            feedback_reason="rerun retained validation",
            validation_only_replay=True,
        ) == (False, "validation_only_replay_candidate_invalid")
        blocked_card = _get_card(repo, task_id)
        assert blocked_card["status"] == "blocked"
        assert "validation_only_replay_authorization" not in blocked_card

    assert task_store.recover_blocked_rework(
        repo,
        task_id,
        feedback_reason="rerun retained validation",
        validation_only_replay=True,
    ) == (True, "recovered")
    card = _get_card(repo, task_id)
    assert card["status"] == "pending"
    assert card["validation_only_replay_authorization"][
        "predecessor_request_id"
    ] == request_id


def test_recovery_accepts_launch_failed_and_timed_out_substatuses(
    tmp_path: Path,
) -> None:
    """Launch failures and timeouts are rework-eligible."""
    for substatus in ("launch_failed", "timed_out", "token_budget_exceeded"):
        parent = tmp_path / substatus
        parent.mkdir()
        repo = _setup_repo(parent)
        task_id = f"RECOVER_{substatus.upper()}"
        _insert_blocked_task(
            repo,
            task_id,
            terminal_substatus=substatus,
            reject_review_reason=f"NeedFix: {substatus} recovery",
        )
        ok, state = task_store.recover_blocked_rework(
            repo, task_id, actor="coordinator",
            feedback_reason=f"NeedFix: {substatus} recovery",
        )
        assert (ok, state) == (True, "recovered"), f"failed for {substatus}"
        assert _get_card(repo, task_id)["status"] == "pending"


def test_validation_only_replay_falls_back_to_authenticated_validation_failed_terminal_failure(
    tmp_path: Path,
) -> None:
    repo = _setup_repo(tmp_path)
    task_id = "C157_RETAINED_VALIDATION_FAILURE"
    request_id = "c" * 32
    claim_epoch = 2
    workspace = repo / ".aiworkhub" / "runtime" / "worktrees" / request_id / "worktree"
    candidate = workspace / "src" / "result.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"retained candidate\n")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    workspace_evidence = {
        "request_id": request_id,
        "repo": str(repo),
        "path": str(workspace),
    }
    predecessor = {
        "request_id": request_id,
        "task_id": task_id,
        "changed_path_hashes": {"src/result.py": digest},
        "workspace": workspace_evidence,
    }
    _insert_blocked_task(
        repo,
        task_id,
        reject_review_reason="rerun retained validation",
        extra_card={"claim_epoch": claim_epoch, "rework_predecessor": predecessor},
    )
    # mark_terminal_failure persists identity under evidence; it does not copy
    # request_id or task_id into the terminal event's top-level payload.
    failure = {
        "substatus": "validation_failed",
        "claim_epoch": claim_epoch,
        "evidence": {
            "request_id": request_id,
            "changed_path_hashes": {"src/result.py": digest},
            "request_identity": {
                "request_id": request_id,
                "task_id": task_id,
                "repo": str(repo),
                "claim_epoch": claim_epoch,
            },
            "workspace": workspace_evidence,
        },
    }
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM task_events WHERE task_id=? AND event='terminal_review'",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) "
            "VALUES (?, 'terminal_failure', 'codex_worker_test', ?, ?)",
            (task_id, json.dumps(failure), "2026-08-06T00:00:01+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    before = candidate.read_bytes()
    assert task_store.recover_blocked_rework(
        repo,
        task_id,
        feedback_reason="rerun retained validation",
        validation_only_replay=True,
    ) == (True, "recovered")
    card = _get_card(repo, task_id)
    assert card["status"] == "pending"
    assert card["claim_epoch"] == claim_epoch + 1
    assert card["validation_only_replay_authorization"]["next_claim_epoch"] == claim_epoch + 1
    assert candidate.read_bytes() == before


@pytest.mark.parametrize(
    ("event_epoch", "identity_epoch", "predecessor_epoch", "identity_mutation"),
    [
        (8, 8, None, None),
        (7, 8, None, None),
        (7, 7, 8, None),
        (7, 7, None, "conflicting_nested_request_id"),
        (7, 7, None, "conflicting_nested_task_id"),
        (7, 7, None, "conflicting_top_level_request_id"),
        (7, 7, None, "conflicting_top_level_task_id"),
        (7, 7, None, "conflicting_evidence_request_id"),
        (7, 7, None, "conflicting_workspace_request_id"),
        (7, 7, None, "conflicting_workspace_task_id"),
        (7, 7, None, "missing_request_identity_claim_epoch"),
    ],
    ids=[
        "terminal-event-vs-card",
        "conflicting-event-identities",
        "optional-predecessor-mismatch",
        "conflicting-nested-request-id",
        "conflicting-nested-task-id",
        "conflicting-top-level-request-id",
        "conflicting-top-level-task-id",
        "conflicting-evidence-request-id",
        "conflicting-workspace-request-id",
        "conflicting-workspace-task-id",
        "missing-request-identity-claim-epoch",
    ],
)
def test_validation_only_replay_rejects_terminal_failure_claim_epoch_mismatch(
    tmp_path: Path,
    event_epoch: int,
    identity_epoch: int,
    predecessor_epoch: int | None,
    identity_mutation: str | None,
) -> None:
    repo = _setup_repo(tmp_path)
    task_id = "RETAINED_VALIDATION_FAILURE_EPOCH_MISMATCH"
    request_id = "d" * 32
    claim_epoch = 7
    workspace = repo / ".aiworkhub" / "runtime" / "worktrees" / request_id / "worktree"
    candidate = workspace / "src" / "result.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"retained candidate\n")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    workspace_evidence = {
        "request_id": request_id,
        "repo": str(repo),
        "path": str(workspace),
    }
    predecessor = {
        "request_id": request_id,
        "task_id": task_id,
        "changed_path_hashes": {"src/result.py": digest},
        "workspace": workspace_evidence,
    }
    if predecessor_epoch is not None:
        predecessor["claim_epoch"] = predecessor_epoch
    _insert_blocked_task(
        repo,
        task_id,
        reject_review_reason="rerun retained validation",
        extra_card={"claim_epoch": claim_epoch, "rework_predecessor": predecessor},
    )
    failure = {
        "task_id": task_id,
        "request_id": request_id,
        "substatus": "validation_failed",
        "claim_epoch": event_epoch,
        "evidence": {
            "request_id": request_id,
            "changed_path_hashes": {"src/result.py": digest},
            "request_identity": {
                "request_id": request_id,
                "task_id": task_id,
                "repo": str(repo),
                "claim_epoch": identity_epoch,
            },
            "workspace": workspace_evidence,
        },
    }
    request_identity = failure["evidence"]["request_identity"]
    if identity_mutation == "conflicting_nested_request_id":
        request_identity["request_id"] = "wrong-request"
    elif identity_mutation == "conflicting_nested_task_id":
        request_identity["task_id"] = "WRONG_TASK"
    elif identity_mutation == "conflicting_top_level_request_id":
        failure["request_id"] = "wrong-request"
    elif identity_mutation == "conflicting_top_level_task_id":
        failure["task_id"] = "WRONG_TASK"
    elif identity_mutation == "conflicting_evidence_request_id":
        failure["evidence"]["request_id"] = "wrong-request"
    elif identity_mutation == "conflicting_workspace_request_id":
        failure["evidence"]["workspace"]["request_id"] = "wrong-request"
    elif identity_mutation == "conflicting_workspace_task_id":
        failure["evidence"]["workspace"]["task_id"] = "WRONG_TASK"
    elif identity_mutation == "missing_request_identity_claim_epoch":
        request_identity.pop("claim_epoch")
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM task_events WHERE task_id=? AND event='terminal_review'",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) "
            "VALUES (?, 'terminal_failure', 'codex_worker_test', ?, ?)",
            (task_id, json.dumps(failure), "2026-08-06T00:00:01+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    assert task_store.recover_blocked_rework(
        repo,
        task_id,
        feedback_reason="rerun retained validation",
        validation_only_replay=True,
    ) == (False, "validation_only_replay_terminal_review_mismatch")
    card = _get_card(repo, task_id)
    assert card["status"] == "blocked"
    assert "validation_only_replay_authorization" not in card
