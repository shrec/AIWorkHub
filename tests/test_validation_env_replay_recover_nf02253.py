"""NF-2026-02253 / NF-2026-02254: validation_environment replay loop and recovery.

Measured on AIWorkHub 0.12.97 (external project report):

* every ``aiworkhub_task_reject_review(to=blocked,
  failure_category=validation_environment)`` started a fresh
  ``automatic_validation_replay`` of the same candidate, each replay hit the
  same sandbox launch error and woke the callback queue again;
* ``aiworkhub_task_recover_blocked_rework(validation_only_replay=true)`` refused
  with ``no_residual_feedback`` although the retained predecessor carried its
  request and changed-path hashes, so the manager relaunched a full worker;
* after an automatic replay the reject receipt said ``blocked`` while
  ``aiworkhub_task_show`` already reported the replay's state.

The recovery transaction runs against a real task store and a retained
predecessor worktree (the NF-2026-01655 fixtures).  The reject handler's
post-commit steps that are not under test are stubbed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import core, process_launcher, server, task_store, validation_replay_policy
from test_validation_replay_sealed_delta_nf01655 import (  # noqa: F401  (fixture used by name)
    CLAIM_EPOCH,
    FEEDBACK,
    NOW,
    PREDECESSOR,
    RUNNER,
    TASK_ID,
    _card,
    _insert_blocked,
    _predecessor,
    repo,
)

_CATEGORY = "validation_environment"
_LAUNCH_ERROR = (
    "validation_sandbox_launch_failed: CreateProcessAsUser failed for request "
    "{request}: [WinError 5] Access is denied"
)


def _terminal(request_id: str, *, failure_class: str = "sandbox_launch_failed") -> dict[str, Any]:
    """The predecessor's terminal review: its validation never started."""
    return {
        "substatus": "validation_failed",
        "runner": RUNNER,
        "request_id": PREDECESSOR,
        "task_id": TASK_ID,
        "claim_epoch": CLAIM_EPOCH,
        "recorded_at": NOW,
        "evidence": {
            "request_id": PREDECESSOR,
            "error": _LAUNCH_ERROR.format(request=request_id),
            "validation": [
                {
                    "command": "python -m pytest -q tests/test_target.py",
                    "returncode": 1,
                    "failure_class": failure_class,
                }
            ],
        },
    }


def _with_identity(
    repo_root: Path, terminal: dict[str, Any], predecessor: dict[str, Any]
) -> dict[str, Any]:
    identity = {"request_id": PREDECESSOR, "repo": str(repo_root)}
    terminal["evidence"].update(
        changed_path_hashes=dict(predecessor["changed_path_hashes"]),
        request_identity={**identity, "task_id": TASK_ID},
        workspace={**identity, "path": predecessor["workspace"]["path"]},
    )
    return terminal


def _connect(repo_root: Path) -> sqlite3.Connection:
    _readiness, db_path = task_store._require_ready(repo_root)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _events(repo_root: Path, event: str) -> list[dict[str, Any]]:
    conn = _connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM task_events WHERE task_id=? AND event=? ORDER BY rowid",
            (TASK_ID, event),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row["payload_json"]) for row in rows]


def _insert_event(repo_root: Path, event: str, payload: dict[str, Any]) -> None:
    conn = _connect(repo_root)
    try:
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (TASK_ID, event, RUNNER, json.dumps(payload, sort_keys=True), NOW),
        )
        conn.commit()
    finally:
        conn.close()


def _reblock(repo_root: Path, card: dict[str, Any]) -> None:
    """Put the card back as the next infrastructure reject leaves it; history stays."""
    conn = _connect(repo_root)
    try:
        conn.execute(
            "UPDATE tasks SET status='blocked', worker_status='blocked', claimed_by=NULL, "
            "card_json=? WHERE task_id=?",
            (json.dumps(card, sort_keys=True), TASK_ID),
        )
        conn.commit()
    finally:
        conn.close()


def _env_rejected_card(repo_root: Path, predecessor: dict[str, Any]) -> dict[str, Any]:
    card = _card(repo_root, predecessor, claim_epoch=CLAIM_EPOCH)
    card["rejection_disposition"] = {
        "schema_id": "aiworkhub.rejection_disposition.v1",
        "to": "blocked",
        "failure_category": _CATEGORY,
        "request_id": PREDECESSOR,
    }
    return card


def _infra_replay(repo_root: Path) -> tuple[bool, str]:
    return task_store.recover_blocked_rework(
        repo_root,
        TASK_ID,
        actor=core.CODEX_RUNNER,
        feedback_reason=FEEDBACK,
        validation_only_replay=True,
        infra_reject_replay=True,
    )


# --- NF-2026-02254: validation_only_replay without residual reviewer feedback ---


def test_validation_only_replay_recovers_a_retained_predecessor_without_feedback(
    repo: Path,
) -> None:
    predecessor = _predecessor(repo, collected=False)
    card = _card(repo, predecessor, claim_epoch=CLAIM_EPOCH)
    card.pop("reject_review")  # a to=blocked reject leaves no reviewer residual
    _insert_blocked(
        repo, card, "terminal_review", _with_identity(repo, _terminal("a" * 32), predecessor)
    )

    assert task_store.recover_blocked_rework(
        repo, TASK_ID, actor=core.CODEX_RUNNER, feedback_reason="", validation_only_replay=True
    ) == (True, "recovered")

    recovered = task_store.get_task(repo, TASK_ID)
    assert recovered is not None and recovered["status"] == "pending"
    assert recovered["recovery_feedback"] == validation_replay_policy.replay_feedback(PREDECESSOR)
    authorization = recovered["validation_only_replay_authorization"]
    assert authorization["predecessor_request_id"] == PREDECESSOR
    assert authorization["changed_path_hashes"] == predecessor["changed_path_hashes"]
    (recovery,) = _events(repo, "blocked_rework_recovery")
    assert recovery["validation_only_replay"] is True
    assert PREDECESSOR in recovery["feedback"]


# --- NF-2026-02253: one automatic replay per (task, predecessor, failure) ---


def test_failure_signature_folds_volatile_identifiers_only() -> None:
    first = validation_replay_policy.failure_signature(_terminal("1" * 32))
    assert first == validation_replay_policy.failure_signature(_terminal("f" * 32))
    assert first != validation_replay_policy.failure_signature(
        _terminal("1" * 32, failure_class="assertion_failed")
    )
    # Unknown terminal evidence is one constant signature, never a fresh one.
    assert validation_replay_policy.failure_signature(None) == (
        validation_replay_policy.failure_signature({})
    )


def test_repeated_environment_failure_is_not_replayed_again(repo: Path) -> None:
    predecessor = _predecessor(repo, collected=False)
    card = _env_rejected_card(repo, predecessor)
    _insert_blocked(
        repo, card, "terminal_review", _with_identity(repo, _terminal("1" * 32), predecessor)
    )

    assert _infra_replay(repo) == (True, "recovered")
    (first,) = _events(repo, "blocked_rework_recovery")
    key = first["automatic_replay_key"]
    assert key["predecessor_request_id"] == PREDECESSOR
    assert key["failure_category"] == _CATEGORY
    assert key["candidate_sha256"] == validation_replay_policy.candidate_digest(
        predecessor["changed_path_hashes"]
    )

    # The replay hit the same sandbox launch error and the manager rejected it
    # again with validation_environment: the second replay is refused.
    _insert_event(
        repo, "terminal_review", _with_identity(repo, _terminal("2" * 32), predecessor)
    )
    _reblock(repo, card)
    assert _infra_replay(repo) == (False, validation_replay_policy.REPEATED_REFUSAL)
    assert len(_events(repo, "blocked_rework_recovery")) == 1
    row = task_store.get_task(repo, TASK_ID)
    assert row is not None and row["status"] == "blocked"

    # A different failure is a new signature and earns its own single replay.
    _insert_event(
        repo,
        "terminal_review",
        _with_identity(repo, _terminal("3" * 32, failure_class="worker_timeout"), predecessor),
    )
    assert _infra_replay(repo) == (True, "recovered")
    assert len(_events(repo, "blocked_rework_recovery")) == 2


class _NoLaunch:
    def launch(self, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a capped replay must not launch")


def test_reject_receipt_names_why_the_replay_was_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refusal = f"recover_blocked_rework_failed:{validation_replay_policy.REPEATED_REFUSAL}"
    monkeypatch.setattr(
        core, "recover_blocked_rework", lambda *_a, **_k: {"ok": False, "stderr": refusal}
    )
    monkeypatch.setattr(process_launcher, "default_manager", lambda: _NoLaunch())

    assert server._automatic_validation_replay(TASK_ID, _CATEGORY, {}) == {
        "attempted": False,
        "succeeded": False,
        "reason": f"{validation_replay_policy.SKIP_REASON}:{_CATEGORY}",
        "request_id": None,
    }


# --- NF-2026-02254: one status across reject receipt, task_show and task_status ---


def test_reject_receipt_reports_the_card_status_after_the_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = {
        "task_id": TASK_ID,
        "status": "blocked",
        "worker_status": "blocked",
        "rejection_disposition": {"failure_category": _CATEGORY, "request_id": PREDECESSOR},
    }
    after_replay = {**rejected, "status": "review", "worker_status": "completed"}
    started = {"attempted": True, "succeeded": True, "reason": "started", "request_id": "e" * 32}
    monkeypatch.setattr(
        core,
        "reject_review",
        lambda **_k: {"ok": True, "returncode": 0, "stdout": json.dumps(rejected), "stderr": ""},
    )
    monkeypatch.setattr(
        core,
        "show_task",
        lambda _task_id: {"ok": True, "returncode": 0, "stdout": json.dumps(after_replay)},
    )
    monkeypatch.setattr(
        server,
        "_reject_review_receipt_fields",
        lambda *_a, **_k: {
            "to": "blocked",
            "failure_category": _CATEGORY,
            "request_id": PREDECESSOR,
        },
    )
    monkeypatch.setattr(server, "_with_learning_closeout", lambda receipt, **_k: receipt)
    monkeypatch.setattr(server, "_attach_reviewer_process_cancellation", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_automatic_validation_replay", lambda *_a: dict(started))

    reply = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason="sandbox launch failed", to="blocked", failure_category=_CATEGORY
    )

    shown, error = core._live_card(TASK_ID)
    assert error is None and shown is not None
    assert reply["automatic_validation_replay"] == started
    assert reply["to"] == "blocked"
    assert reply["status_at_reject"] == "blocked"
    assert (reply["status"], reply["worker_status"]) == (shown["status"], shown["worker_status"])
    assert reply["card_sha256"] == server._card_identity(shown)["card_sha256"]
