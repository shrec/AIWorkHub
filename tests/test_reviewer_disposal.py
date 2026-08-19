"""NF-2026-00265 / NF-WAVE-REVIEWER-LAUNCH-V4, PART TWO: the disposal guard.

A reviewer launch reserves an attempt -- minting a ``launch_request_id`` -- and
prepares for minutes before any process starts.  During that window the row
exists, the launch is committed and about to run, and ``started_at`` is still
NULL.  ``started_at`` is written at CLAIM time, not at reservation time, so a
guard that read ``started_at`` NULL as "blocked before start" would dispose of a
reviewer that is about to run.

``task_store.archive_task`` therefore keys disposal on the RESERVATION, not on
``started_at``: a held reservation is a commitment and is refused; a launch that
committed nothing (no reservation held and never started) is disposable; a live,
started reviewer keeps ``archive_processing_forbidden``.  No pid is read to
decide any of this -- the tasks table has no pid column.

These regressions test the WINDOW itself (reserved-but-not-started), not only the
before-creation and after-start endpoints where the defect does not live.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from aiworkhub import task_store

RUNNER = "claude_opus-5"
TOPIC = "quality_review"
NOW = "2026-08-19T00:00:00+00:00"


def _repo(tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    return repo


def _insert(
    repo: Path,
    task_id: str,
    *,
    status: str,
    worker_status: str,
    launch_request_id: str = "",
    started_at: str | None = None,
) -> None:
    readiness = task_store.storage_readiness(repo)
    card = {
        "task_id": task_id,
        "runner": RUNNER,
        "topic": TOPIC,
        "status": status,
        "worker_status": worker_status,
        "launch_request_id": launch_request_id,
        "read_only": True,
        "allowed_writes": [],
        "required_outputs": [],
        "project_context": {"task_type": "research"},
    }
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at, started_at) "
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
                started_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_reservation(repo: Path, task_id: str) -> None:
    """Release the reservation by detaching its id from the card (still pending)."""

    card = task_store.get_task(repo, task_id) or {}
    card["launch_request_id"] = ""
    conn = sqlite3.connect(task_store.storage_readiness(repo).canonical_db)
    try:
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card, ensure_ascii=False, sort_keys=True), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_guard_refuses_while_reservation_held_then_permits_once_released(
    tmp_path, monkeypatch
):
    """The window: reserved-but-not-started refuses; released permits.

    The row is ``pending`` (not yet ``processing``) with a minted reservation id
    and ``started_at`` NULL -- exactly the reserved-and-preparing window.  The
    old guard, which keyed only on canonical ``processing``, would happily
    dispose of this reviewer about to run.
    """

    repo = _repo(tmp_path, monkeypatch)
    _insert(
        repo,
        "QR_HELD_LENS",
        status="pending",
        worker_status="unclaimed",
        launch_request_id="req-held",
        started_at=None,
    )

    # While the reservation is held, disposal is refused.
    ok, reason = task_store.archive_task(repo, "QR_HELD_LENS", reason="stale?")
    assert ok is False
    assert reason == "archive_processing_forbidden"

    # Releasing the reservation -- and nothing else, the row is still pending and
    # started_at is still NULL -- makes it disposable.  Only the reservation
    # signal changed, proving the guard keys on the reservation, not started_at.
    _clear_reservation(repo, "QR_HELD_LENS")
    ok, reason = task_store.archive_task(
        repo, "QR_HELD_LENS", reason="reservation released"
    )
    assert ok is True, reason
    card = task_store.get_task(repo, "QR_HELD_LENS") or {}
    assert card["archived_at"]
    assert card["archive_reason"] == "reservation released"


def test_stale_pending_without_reservation_is_disposable(tmp_path, monkeypatch):
    """started_at NULL alone never blocks: a pending card with no reservation and
    no start committed nothing and is disposable.  This is the control that shows
    the refusal above is the reservation, not the (identical) NULL started_at."""

    repo = _repo(tmp_path, monkeypatch)
    _insert(
        repo,
        "QR_STALE_PENDING",
        status="pending",
        worker_status="unclaimed",
        launch_request_id="",
        started_at=None,
    )
    ok, _reason = task_store.archive_task(repo, "QR_STALE_PENDING", reason="stale")
    assert ok is True


def test_reviewer_blocked_before_spawn_is_disposable_with_reason_recorded(
    tmp_path, monkeypatch
):
    """A reviewer terminalized before it ever spawned is disposable.

    Its reservation was released (the attempt is ``blocked``) and it never
    started, so nothing is committed on its behalf even though a launch_request_id
    still lingers on the card.  Disposal succeeds and records the reason.
    """

    repo = _repo(tmp_path, monkeypatch)
    _insert(
        repo,
        "QR_BLOCKED_NOSPAWN",
        status="blocked",
        worker_status="launch_failed",
        launch_request_id="req-blocked",
        started_at=None,
    )
    ok, state = task_store.archive_task(
        repo, "QR_BLOCKED_NOSPAWN", reason="reservation expired, never spawned"
    )
    assert ok is True, state
    card = task_store.get_task(repo, "QR_BLOCKED_NOSPAWN") or {}
    assert card["archived_at"]
    assert card["archive_reason"] == "reservation expired, never spawned"


def test_reviewer_with_live_pid_still_gets_processing_forbidden(tmp_path, monkeypatch):
    """A reviewer that actually started (claimed + started_at set) is a live
    process and keeps ``archive_processing_forbidden`` -- decided without ever
    reading a pid, which the tasks table does not store."""

    repo = _repo(tmp_path, monkeypatch)
    _insert(
        repo,
        "QR_LIVE",
        status="processing",
        worker_status="claimed",
        launch_request_id="req-live",
        started_at="2026-08-19T00:05:00+00:00",
    )
    ok, reason = task_store.archive_task(repo, "QR_LIVE", reason="try to dispose")
    assert ok is False
    assert reason == "archive_processing_forbidden"


def test_supersede_path_may_dispose_a_held_reservation(tmp_path, monkeypatch):
    """The operator supersede path (``allow_processing=True``) is unchanged: it
    can still displace a held/orphaned reservation for a manual reclaim."""

    repo = _repo(tmp_path, monkeypatch)
    _insert(
        repo,
        "QR_HELD_SUPERSEDE",
        status="pending",
        worker_status="unclaimed",
        launch_request_id="req-held",
        started_at=None,
    )
    ok, state = task_store.archive_task(
        repo,
        "QR_HELD_SUPERSEDE",
        reason="operator reclaim",
        allow_processing=True,
        operation="superseded",
        superseded_by="QR_REPLACEMENT",
    )
    assert ok is True, state
    card = task_store.get_task(repo, "QR_HELD_SUPERSEDE") or {}
    assert card["archive_operation"] == "superseded"
