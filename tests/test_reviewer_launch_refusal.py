"""NF-2026-00265 / NF-WAVE-REVIEWER-LAUNCH-V4, PART ONE.

``launch_quality_reviewer`` returns a bounded ``starting``/``deferred`` receipt
and finishes preparation on a background thread -- that deferral is correct.
What was wrong is that the deferred receipt said ``ok:true`` before anything that
could refuse the launch had run, so a launch whose target can never be reviewed
still handed the caller a success receipt for a reviewer that would resolve its
target on the thread, refuse and die -- a ghost card.

``quality_review.assess_reviewer_launch_target`` is the decidable part of the
launch and must now run INSIDE ``launch_quality_reviewer`` BEFORE the deferred
return.  These regressions drive the production method itself (never the assessor
in isolation -- that shape proves nothing while the gate is dead) and assert the
returned dict is ``ok:false`` with the named reason, with no reservation minted.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from aiworkhub import process_launcher, quality_evidence, task_store

RUNNER = "claude_opus-5"
ADAPTER = "vscode_lm"
NOW = "2026-08-19T00:00:00+00:00"

_LENS = sorted(quality_evidence.JUDGMENT_LENSES)[0]


def _repo(tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    return repo


def _insert_target(repo: Path, task_id: str, *, substatus: str) -> None:
    """Insert a target card whose terminal_review substatus is ``substatus``."""

    readiness = task_store.storage_readiness(repo)
    card = {
        "task_id": task_id,
        "runner": RUNNER,
        "topic": "quality_gate",
        "status": "blocked",
        "worker_status": substatus,
        "terminal_review": {"substatus": substatus},
        "read_only": False,
        "allowed_writes": [],
        "required_outputs": [],
        "project_context": {"task_type": "code"},
    }
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                RUNNER,
                "quality_gate",
                "solo",
                "blocked",
                substatus,
                "normal",
                "objective",
                json.dumps(card, ensure_ascii=False, sort_keys=True),
                NOW,
                NOW,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _manager(repo: Path) -> process_launcher.ProcessManager:
    return process_launcher.ProcessManager(
        repo=repo,
        isolation_enabled=False,
        collision_guard=lambda **_kwargs: {"returncode": 0},
    )


def test_launch_against_validation_failed_target_returns_ok_false(tmp_path, monkeypatch):
    """Driven from the production path: a validation_failed target fails at launch.

    A test that called ``assess_reviewer_launch_target`` directly would pass even
    while the gate had zero production callers, so this drives
    ``launch_quality_reviewer`` itself and asserts the returned dict.
    """

    repo = _repo(tmp_path, monkeypatch)
    _insert_target(repo, "TARGET_VF", substatus="validation_failed")
    manager = _manager(repo)

    receipt = manager.launch_quality_reviewer(
        target_request_id="req-target-vf",
        target_task_id="TARGET_VF",
        reviewer_task_id="QR_TARGET_VF_LENS",
        runner=RUNNER,
        adapter_id=ADAPTER,
        lens=_LENS,
    )

    # ok:false, carrying the assessor's named reason -- never ok:true.
    assert receipt["ok"] is False, receipt
    assert receipt.get("fails_at_launch") is True
    assert receipt["reason"] == "reviewer_target_not_review_ready:validation_failed"
    assert receipt.get("target_substatus") == "validation_failed"
    # The refusal happened BEFORE the deferred return: no attempt was reserved,
    # so no thread/process was started and no request_id was minted.
    assert receipt.get("deferred") is not True
    assert not receipt.get("request_id")
    # And no reviewer reservation event exists for this reviewer task.
    assert manager._live_reviewer_receipt("QR_TARGET_VF_LENS") is None


def test_launch_against_missing_target_returns_ok_false(tmp_path, monkeypatch):
    """An unresolvable target cannot be reviewed and is refused, not deferred."""

    repo = _repo(tmp_path, monkeypatch)
    manager = _manager(repo)

    receipt = manager.launch_quality_reviewer(
        target_request_id="req-missing",
        target_task_id="TARGET_MISSING",
        reviewer_task_id="QR_TARGET_MISSING_LENS",
        runner=RUNNER,
        adapter_id=ADAPTER,
        lens=_LENS,
    )

    assert receipt["ok"] is False, receipt
    assert receipt["reason"] == "reviewer_target_not_review_ready:<none>"
    assert receipt.get("deferred") is not True
    assert not receipt.get("request_id")


def test_invalid_lens_is_still_refused(tmp_path, monkeypatch):
    """The pre-existing lens gate is unchanged and still fails closed."""

    repo = _repo(tmp_path, monkeypatch)
    _insert_target(repo, "TARGET_RR", substatus="review_ready")
    manager = _manager(repo)

    receipt = manager.launch_quality_reviewer(
        target_request_id="req-target-rr",
        target_task_id="TARGET_RR",
        reviewer_task_id="QR_TARGET_RR_LENS",
        runner=RUNNER,
        adapter_id=ADAPTER,
        lens="not-a-real-lens",
    )

    assert receipt["ok"] is False
    assert receipt["error"] == "quality_review_lens_invalid"
