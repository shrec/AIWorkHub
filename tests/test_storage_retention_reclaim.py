"""Regression coverage for the two dead-reclaim defects in storage_retention.

(1) Worktree eligibility never produced a candidate because a worktree with
    local, deliberately-unpushed commits (exactly what a rejected/superseded
    rework attempt looks like) was classified ``retained_unsaved`` and
    protected forever, regardless of whether any live task attempt still
    needed it.
(2) The quarantine executor could silently move nothing: its per-item safety
    re-check required ``CLASS_REMOVABLE_SAFE`` at execution time, so once
    preview started reporting superseded-but-unpushed worktrees as
    candidates, the executor would skip every single one of them.

Age is exercised via an explicit ``now`` injected into ``preview()`` /
``quarantine()`` rather than by mutating filesystem mtimes: workers run
under a landlock sandbox that forbids ``os.utime``, and a retention policy
whose age logic can only be tested by mutating the real filesystem clock
would be untestable there (and in CI) regardless of correctness.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import storage_retention, task_store, worktree_storage

# terminal_runs_days defaults to 30; 31 real days pushes a worktree past the
# default policy threshold without ever touching its on-disk mtime.
_AGED_NOW_OFFSET_DAYS = 31


def _aged_now() -> float:
    return time.time() + _AGED_NOW_OFFSET_DAYS * 86400


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _insert_task_card(
    repo_root: Path,
    task_id: str,
    *,
    status: str,
    accepted_request_id: str = "",
    launch_request_id: str = "",
    updated_at: str | None = None,
) -> None:
    """Insert one bounded canonical task row directly for test purposes.

    ``status`` is written verbatim into the ``tasks.status`` column; it must
    be a value ``task_store.canonical_status`` maps back to itself (e.g.
    ``processing``, ``review``, ``finished``, ``pending``). ``updated_at``
    defaults to now but may be overridden to simulate a card that is not
    among the most-recently-updated rows in a large repository.

    ``launch_request_id`` mirrors the field the real claim path writes into
    ``card_json`` while a task is actively ``processing``/``review`` -- the
    field ``_protected_attempt_ids`` keys live-worktree protection on.
    ``accepted_request_id`` is only ever populated once a review has been
    ACCEPTED and the card has already flipped to ``finished``; pass it only
    to model a finished card whose accepted attempt superseded an earlier
    one, never to model a still-live card.
    """
    db_path = task_store.canonical_db_path(repo_root)
    now = updated_at or datetime.now(timezone.utc).isoformat()
    card_payload: dict[str, str] = {}
    if accepted_request_id:
        card_payload["accepted_request_id"] = accepted_request_id
    if launch_request_id:
        card_payload["launch_request_id"] = launch_request_id
    card_json = json.dumps(card_payload)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "claude", "storage", status, "unclaimed", card_json, now, now),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def repo_with_worktrees(tmp_path: Path) -> dict[str, Path]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    base = tmp_path / "worktrees"
    base.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(tmp_path, "clone", str(remote), str(repo))
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "origin", "HEAD:refs/heads/main")
    _git(repo, "fetch", "origin")
    assert task_store.initialize_repository(repo)["ok"]
    return {"repo": repo, "base": base}


def _add_worktree(
    repo: Path,
    base: Path,
    entry_id: str,
    *,
    unpushed: bool,
) -> Path:
    entry = base / entry_id
    worktree = entry / "worktree"
    entry.mkdir()
    _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    if unpushed:
        # A local commit never pushed to any remote-tracking ref: exactly what
        # a rejected/superseded rework attempt looks like on disk.
        (worktree / "note.txt").write_text("rework\n", encoding="utf-8")
        _git(worktree, "add", "note.txt")
        _git(worktree, "commit", "-m", "unpushed rework attempt")
    return entry


def test_superseded_unpushed_worktree_becomes_candidate(repo_with_worktrees) -> None:
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    entry = _add_worktree(repo, base, "attempt-1", unpushed=True)
    # The card this worktree belonged to has already sealed a later attempt;
    # nothing is processing or reviewing "attempt-1" anymore.
    _insert_task_card(repo, "card-1", status="finished", accepted_request_id="attempt-2")

    preview = storage_retention.preview(repo, base=base, now=_aged_now())

    assert preview["candidate_count"] == 1
    assert preview["candidates"][0]["id"] == "attempt-1"
    assert preview["candidate_bytes"] > 0
    assert entry.is_dir()  # preview never mutates anything


def test_processing_and_newest_review_attempts_never_candidates(repo_with_worktrees) -> None:
    """Mirrors the production claim path: live cards carry ``launch_request_id``
    in card_json, not ``accepted_request_id`` (that field is only ever set once
    a review is ACCEPTED and the card has flipped to ``finished``)."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    _add_worktree(repo, base, "live-processing", unpushed=True)
    _add_worktree(repo, base, "live-review", unpushed=True)
    _insert_task_card(repo, "card-processing", status="processing", launch_request_id="live-processing")
    _insert_task_card(repo, "card-review", status="review", launch_request_id="live-review")

    preview = storage_retention.preview(repo, base=base, now=_aged_now())

    candidate_ids = {item["id"] for item in preview["candidates"]}
    assert candidate_ids == set()
    assert preview["candidate_count"] == 0


def test_over_cap_forces_oldest_superseded_lineage_even_under_age(
    repo_with_worktrees, monkeypatch
) -> None:
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    # Both worktrees are superseded (no live task holds them) but neither has
    # aged past the retention policy yet: no injected ``now`` is passed, so
    # each keeps its real (just-created) modified_at_epoch, well under any
    # age threshold. Creating them sequentially gives "attempt-old" a strictly
    # earlier real mtime than "attempt-new".
    _add_worktree(repo, base, "attempt-old", unpushed=True)
    time.sleep(0.05)
    _add_worktree(repo, base, "attempt-new", unpushed=True)

    # Force the "over cap" branch without needing megabytes of fixture data:
    # the repo policy's own worktree_max_bytes floor (64 MiB) is impractical
    # for a lightweight unit test, so bypass the JSON-validated policy here.
    monkeypatch.setattr(storage_retention, "_policy", lambda _root: (30, 1))

    preview = storage_retention.preview(repo, base=base)

    assert preview["candidate_count"] >= 1
    assert preview["candidate_bytes"] > 0
    candidate_ids = {item["id"] for item in preview["candidates"]}
    assert "attempt-old" in candidate_ids


def test_preview_and_executor_agree_on_superseded_worktree(repo_with_worktrees) -> None:
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    entry = _add_worktree(repo, base, "attempt-1", unpushed=True)
    _insert_task_card(repo, "card-1", status="finished", accepted_request_id="attempt-2")

    aged_now = _aged_now()
    preview = storage_retention.preview(repo, base=base, now=aged_now)
    assert preview["candidate_count"] == 1
    assert preview["candidate_bytes"] > 0

    result = storage_retention.quarantine(
        repo,
        preview_digest=preview["preview_digest"],
        confirm=True,
        base=base,
        now=aged_now,
    )

    assert result["ok"] is True
    assert result["no_op"] is False
    assert result["quarantined"] == preview["candidate_count"]
    assert result["bytes"] == preview["candidate_bytes"]
    assert not entry.exists()

    batches = storage_retention.list_batches(repo, base=base)
    assert batches["count"] == 1
    assert batches["batches"][0]["quarantined_count"] == 1


def test_live_worktree_protected_regardless_of_table_size(repo_with_worktrees) -> None:
    """Protection must never be resolved through a row-limited reader.

    ``task_store.list_tasks``'s SQL ``LIMIT`` is capped at 5000 rows no
    matter what value is passed to it, so any lineage check built on top of
    it -- at any row count -- could silently drop a live card once a
    repository's ``tasks`` table grows past that ceiling. Protection instead
    reads every row of the canonical table directly (see
    ``_protected_attempt_ids``), so it must still hold a ``processing``
    card's worktree even amid hundreds of newer, unrelated rows.
    """
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    _add_worktree(repo, base, "live-processing", unpushed=True)

    old_updated_at = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
    _insert_task_card(
        repo,
        "card-processing",
        status="processing",
        launch_request_id="live-processing",
        updated_at=old_updated_at,
    )
    # 600 more-recently-updated cards would push "card-processing" outside
    # any fixed-size "ORDER BY updated_at DESC LIMIT N" window.
    fresh_updated_at = datetime.now(timezone.utc).isoformat()
    for index in range(600):
        _insert_task_card(
            repo,
            f"card-filler-{index}",
            status="finished",
            updated_at=fresh_updated_at,
        )

    preview = storage_retention.preview(repo, base=base, now=_aged_now())

    candidate_ids = {item["id"] for item in preview["candidates"]}
    assert "live-processing" not in candidate_ids


def _write_idle_process_identity(repo: Path, request_id: str) -> None:
    from aiworkhub import process_event_ledger, process_launcher

    log_path = repo / process_launcher.PROCESS_LOG_DEFAULT_REL
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process_event_ledger.append_event(
        log_path,
        {
            "pid": 1_000_000_001,
            "pid_start_ticks": 1,
            "request_id": request_id,
            "timestamp": "2026-01-01T00:00:00+00:00",
        },
    )


def _accepted_cleanup_evidence(
    task_id: str, request_id: str, predecessor_request_id: str = ""
) -> dict[str, str]:
    from aiworkhub import task_retention

    predecessor = str(predecessor_request_id or "").strip()
    return {
        "schema_id": task_retention.ACCEPTED_CLEANUP_EVIDENCE_SCHEMA,
        "task_id": task_id,
        "request_id": request_id,
        "predecessor_request_id": predecessor,
        "canonical_digest": task_retention.canonical_acceptance_digest(
            accept_evidence={},
            accepted_request_id=request_id,
            predecessor_request_id=predecessor,
            request_id=request_id,
            status="finished",
            task_id=task_id,
        ),
    }


def test_cleanup_unpins_predecessor_only_when_successor_accepted(repo_with_worktrees) -> None:
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    pred = _add_worktree(repo, base, "pred-landed", unpushed=True)
    succ = _add_worktree(repo, base, "succ-accepted", unpushed=True)
    (pred / "manifest.json").write_text("{}", encoding="utf-8")
    (succ / "manifest.json").write_text("{}", encoding="utf-8")
    db_path = task_store.canonical_db_path(repo)
    now = datetime.now(timezone.utc).isoformat()
    card = {
        "accepted_request_id": "succ-accepted",
        "rework_predecessor": {"request_id": "pred-landed"},
    }
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("TASK-SUCC", "claude", "storage", "finished", "unclaimed", json.dumps(card), now, now),
        )
        connection.commit()
    finally:
        connection.close()
    _write_idle_process_identity(repo, "succ-accepted")
    _write_idle_process_identity(repo, "pred-landed")

    result = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_cleanup_evidence("TASK-SUCC", "succ-accepted", "pred-landed"),
        base=base,
    )

    assert result["ok"] is True
    assert result["predecessor_unpinned"] == "pred-landed"
    assert not (succ / "worktree").exists()
    assert not (pred / "worktree").exists()
    assert (succ / "manifest.json").is_file()
    assert (pred / "manifest.json").is_file()
    connection = sqlite3.connect(str(db_path))
    try:
        stored = json.loads(
            connection.execute(
                "SELECT card_json FROM tasks WHERE task_id=?",
                ("TASK-SUCC",),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    assert "rework_predecessor" not in stored


def test_cleanup_does_not_prune_unrelated_stale_registration(repo_with_worktrees) -> None:
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    succ = _add_worktree(repo, base, "succ-keep-stale", unpushed=True)
    stale = _add_worktree(repo, base, "stale-unrelated", unpushed=False)
    (succ / "manifest.json").write_text("{}", encoding="utf-8")
    shutil.rmtree(stale / "worktree")
    before = worktree_storage.scan_worktree_registrations(repo, base)
    assert before["ok"] is True
    assert "stale-unrelated" in {item["id"] for item in before["stale_candidates"]}
    now = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(str(task_store.canonical_db_path(repo)))
    try:
        connection.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "TASK-KEEP-STALE",
                "claude",
                "storage",
                "finished",
                "unclaimed",
                json.dumps({"accepted_request_id": "succ-keep-stale"}),
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    _write_idle_process_identity(repo, "succ-keep-stale")

    result = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_cleanup_evidence("TASK-KEEP-STALE", "succ-keep-stale"),
        base=base,
    )

    assert result["ok"] is True
    assert not (succ / "worktree").exists()
    after = worktree_storage.scan_worktree_registrations(repo, base)
    assert after["ok"] is True
    assert "stale-unrelated" in {item["id"] for item in after["stale_candidates"]}
    assert after["stale_candidate_count"] >= 1
