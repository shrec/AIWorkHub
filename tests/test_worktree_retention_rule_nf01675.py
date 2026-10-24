"""NF-2026-01675: a sweeper built on an older retention rule must not delete what a newer rule keeps.

Task rows, retained workspaces and import stubs are the b863 suite's own, so they are loaded from
that file instead of being copied.
"""

from __future__ import annotations

import importlib.util
import json
import os
import threading
from pathlib import Path

import pytest

_SCAFFOLD = Path(__file__).with_name("test_aiworkhub_finalizer_canonical_authority_b863.py")
_spec = importlib.util.spec_from_file_location("nf01675_b863_scaffold", _SCAFFOLD)
b863 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(b863)

from aiworkhub import platform_io, process_launcher, worker_workspace
from aiworkhub.worktree_retention_rule import (
    RULE_LOCK_NAME,
    RULE_MARKER_NAME,
    RULE_MARKER_SCHEMA_ID,
    WORKTREE_RETENTION_RULE_VERSION as MINE,
    read_recorded_rule_version,
    record_rule_version,
    superseded_reason,
)

RUNNER = "claude_worker_nf01675"
TOPIC = "task_mcp"


@pytest.fixture(autouse=True)
def _bridge_chmod_sandbox_restriction(monkeypatch: pytest.MonkeyPatch) -> None:
    if b863._chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None, raising=False)


def _manager(repo: Path, tmp_path: Path):
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        collision_guard=lambda **_: {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""},
        isolation_enabled=True,
    )


def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A ProcessManager bound to a repo whose task store is initialised."""
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    repo = tmp_path / "repo"
    repo.mkdir()
    b863._seed_task_row(
        repo, task_id="TASK_NF01675_STORE", runner=RUNNER, topic=TOPIC,
        status="finished", worker_status="done", claimed_by="",
    )
    return _manager(repo, tmp_path)


def _retain(
    manager, tmp_path: Path, name: str, *, status: str, worker_status: str, state: str = "",
):
    """Retain one request's worktree, then give its card the canonical ``status``."""
    task_id, request_id = f"TASK_NF01675_{name.upper()}", f"req-nf01675-{name}"
    card = {"task_id": task_id, "runner": RUNNER, "topic": TOPIC}
    b863._seed_task_row(
        manager.repo, **card, status="processing", worker_status="in_progress",
        claimed_by="a_different_runner",
    )
    path, home, metadata_path = b863._metadata_and_events(
        manager, tmp_path, request_id=request_id, task_id=task_id, runner=RUNNER, topic=TOPIC,
    )
    manager._finalize_isolated_request(request_id)
    assert path.exists() and home.exists()
    b863._seed_task_row(
        manager.repo, **card, status=status, worker_status=worker_status, claimed_by="",
    )
    # A finalize_failed request is kept as retryable_finalize_failed before its card status counts.
    if state:
        manager._append_event({
            **card, "request_id": request_id, "adapter_id": "claude_cli", "state": state,
            "workspace_retained": True, "metadata_path": str(metadata_path),
            "pid": 2_147_483_070, "pid_start_ticks": 999_999_930,
        })
    return request_id, path, home


def _rows(manager, request_id: str) -> list[dict]:
    return [row for row in manager._events() if row.get("request_id") == request_id]


def _superseded(recorded: int) -> str:
    return f"retention_rule_superseded:{MINE}<{recorded}"


# --- the marker ---------------------------------------------------------------


def test_record_rule_version_keeps_the_highest_value_it_was_given(tmp_path):
    assert read_recorded_rule_version(tmp_path) == 0
    assert record_rule_version(tmp_path, 3) == 3
    marker = tmp_path / RULE_MARKER_NAME
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "schema_id": RULE_MARKER_SCHEMA_ID, "version": 3,
    }

    assert record_rule_version(tmp_path, 2) == 3
    assert record_rule_version(tmp_path, 3) == 3
    assert read_recorded_rule_version(tmp_path) == 3
    assert record_rule_version(tmp_path, 4) == 4
    assert read_recorded_rule_version(tmp_path) == 4
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [RULE_MARKER_NAME, RULE_LOCK_NAME]


def test_record_rule_version_replaces_the_marker_atomically(tmp_path, monkeypatch):
    assert record_rule_version(tmp_path, 1) == 1
    marker = tmp_path / RULE_MARKER_NAME
    seen = []
    real_replace = os.replace

    def spy(source, destination):
        seen.append((
            Path(source), Path(destination),
            marker.read_text(encoding="utf-8"), Path(source).read_text(encoding="utf-8"),
        ))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", spy)
    assert record_rule_version(tmp_path, 2) == 2

    [(staged, destination, before, staged_text)] = seen
    assert destination == marker and staged != marker and staged.parent == tmp_path
    assert json.loads(before)["version"] == 1
    assert json.loads(staged_text) == {"schema_id": RULE_MARKER_SCHEMA_ID, "version": 2}
    assert not staged.exists()
    assert read_recorded_rule_version(tmp_path) == 2


def test_record_rule_version_swallows_marker_io_errors(tmp_path, monkeypatch):
    gone = tmp_path / "no" / "such" / "dir"
    assert record_rule_version(gone, 5) == 0
    assert not gone.exists()

    assert record_rule_version(tmp_path, 1) == 1

    def refuse(source, destination):
        raise OSError("replace refused")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", refuse)
        assert record_rule_version(tmp_path, 2) == 1

    assert read_recorded_rule_version(tmp_path) == 1
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [RULE_MARKER_NAME, RULE_LOCK_NAME]


def test_a_racing_lower_version_waits_for_the_lock_and_cannot_lower_the_marker(tmp_path):
    """Two builds newer than the marker race: the lower one must not commit last (security review)."""
    lock = platform_io.open_lock_file(tmp_path / RULE_LOCK_NAME)
    platform_io.lock_fd(lock, blocking=True)
    result: list[int] = []
    lower = threading.Thread(target=lambda: result.append(record_rule_version(tmp_path, 2)))
    try:
        lower.start()
        lower.join(0.5)
        assert lower.is_alive() and read_recorded_rule_version(tmp_path) == 0
        # The newer build commits while the lower one is parked on the lock.
        (tmp_path / RULE_MARKER_NAME).write_text(
            json.dumps({"schema_id": RULE_MARKER_SCHEMA_ID, "version": 3}), encoding="utf-8",
        )
    finally:
        platform_io.unlock_fd(lock)
        os.close(lock)
    lower.join(30)
    assert not lower.is_alive() and result == [3]
    assert read_recorded_rule_version(tmp_path) == 3


def test_read_recorded_rule_version_is_zero_when_the_marker_is_missing(tmp_path):
    assert read_recorded_rule_version(tmp_path) == 0
    assert read_recorded_rule_version(tmp_path / "no" / "such" / "dir") == 0
    (tmp_path / RULE_MARKER_NAME).mkdir()
    assert read_recorded_rule_version(tmp_path) == 0


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json",
        b"\xff\xfe\x00",
        b"[]",
        b"{}",
        b'{"version": "2"}',
        b'{"version": 2.5}',
        b'{"version": true}',
        b'{"version": -3}',
        b'{"version": null}',
        b"[" * 5000,
        b'{"version": 9, "pad": "' + b"x" * 8192 + b'"}',
    ],
)
def test_read_recorded_rule_version_is_zero_when_the_marker_is_corrupt(tmp_path, content):
    (tmp_path / RULE_MARKER_NAME).write_bytes(content)
    assert read_recorded_rule_version(tmp_path) == 0


def test_a_corrupt_marker_is_replaced_by_the_next_recorded_version(tmp_path):
    (tmp_path / RULE_MARKER_NAME).write_bytes(b"not json")
    assert record_rule_version(tmp_path, 2) == 2
    assert read_recorded_rule_version(tmp_path) == 2


@pytest.mark.parametrize(
    ("recorded", "expected"),
    [
        ((), None),
        ((None,), None),
        ((MINE,), None),
        ((MINE - 1,), None),
        ((MINE + 1,), _superseded(MINE + 1)),
        ((None, MINE + 2, MINE + 1), _superseded(MINE + 2)),
        ((str(MINE + 1), float(MINE + 1), True, [MINE + 1]), None),
    ],
)
def test_superseded_reason_only_honours_a_newer_integer_version(recorded, expected):
    assert superseded_reason(MINE, *recorded) == expected


# --- the rule itself ----------------------------------------------------------


def test_removal_statuses_are_pinned_to_the_rule_version():
    removable_by_version = {1: {"finished", "archived", "superseded"}}
    assert MINE in removable_by_version, "pin this rule version's removable statuses here"
    assert process_launcher.GC_DISPOSED_CANONICAL_STATUSES == removable_by_version[MINE], (
        "the statuses that allow worktree removal changed: bump WORKTREE_RETENTION_RULE_VERSION "
        "and pin the new set here, or an older sweeper deletes what this build keeps"
    )


@pytest.mark.parametrize(
    ("card", "expected"),
    [
        ({"status": "blocked", "worker_status": "blocked"}, (False, "task_not_disposed:blocked")),
        ({"status": "pending"}, (False, "task_not_disposed:pending")),
        ({"status": "finished", "worker_status": "done"}, (True, "disposed_task_status:finished")),
        ({"status": "archived"}, (True, "disposed_task_status:archived")),
        ({"status": "superseded"}, (True, "disposed_task_status:superseded")),
    ],
)
def test_blocked_and_pending_cards_keep_their_worktree_under_the_current_rule(card, expected):
    disposition = process_launcher.ProcessManager._gc_disposition(
        card, "req-nf01675-rule", process_state="exited",
    )
    assert disposition == expected


# --- the sweep ----------------------------------------------------------------


def test_process_manager_records_its_rule_version_and_never_lowers_a_newer_one(
    tmp_path, monkeypatch
):
    manager = _rig(tmp_path, monkeypatch)
    # Construction and a sweep over no ledger write nothing (the stdio smoke no-write gate, whose
    # startup reconciler sweeps); the first ledger write records it.
    manager._gc_finalized_workspaces()
    assert not (tmp_path / RULE_MARKER_NAME).exists()
    manager._append_event({"request_id": "req-nf01675-first", "state": "running"})
    assert json.loads((tmp_path / RULE_MARKER_NAME).read_text(encoding="utf-8")) == {
        "schema_id": RULE_MARKER_SCHEMA_ID, "version": MINE,
    }

    record_rule_version(tmp_path, MINE + 3)
    _manager(manager.repo, tmp_path)._append_event(
        {"request_id": "req-nf01675-second", "state": "running"},
    )

    assert read_recorded_rule_version(tmp_path) == MINE + 3


def test_sweep_removes_nothing_once_a_newer_rule_is_recorded(tmp_path, monkeypatch):
    manager = _rig(tmp_path, monkeypatch)
    _, path, home = _retain(manager, tmp_path, "fenced", status="finished", worker_status="done")
    assert record_rule_version(tmp_path, MINE + 1) == MINE + 1

    summary = manager._gc_finalized_workspaces()

    assert summary == {
        "gc_scanned": 0, "gc_cleaned": 0, "gc_skipped": 0, "refused": _superseded(MINE + 1),
    }
    assert path.exists() and home.exists()

    (tmp_path / RULE_MARKER_NAME).unlink()
    assert manager._gc_finalized_workspaces()["gc_cleaned"] == 1
    assert not path.exists() and not home.exists()
    # The sweep re-records its own version, so a lost marker is restored.
    assert read_recorded_rule_version(tmp_path) == MINE


def test_matching_rule_versions_collect_a_finished_card_and_keep_the_others(tmp_path, monkeypatch):
    manager = _rig(tmp_path, monkeypatch)
    _, done_path, done_home = _retain(
        manager, tmp_path, "done", status="finished", worker_status="done",
    )
    kept = {
        status: _retain(
            manager, tmp_path, status, status=status, worker_status=worker_status,
            state="exited",
        )
        for status, worker_status in (("blocked", "blocked"), ("pending", ""))
    }
    assert read_recorded_rule_version(tmp_path) == MINE

    summary = manager._gc_finalized_workspaces()

    assert summary == {"gc_scanned": 3, "gc_cleaned": 1, "gc_skipped": 2}
    assert not done_path.exists() and not done_home.exists()
    for status, (request_id, path, home) in kept.items():
        assert path.exists() and home.exists()
        result = manager._gc_finalized_workspace(
            request_id, manager._latest_by_request()[request_id],
        )
        assert result == {
            "request_id": request_id, "gc": False, "reason": f"task_not_disposed:{status}",
        }


def test_latest_row_from_a_newer_rule_keeps_its_worktree(tmp_path, monkeypatch):
    manager = _rig(tmp_path, monkeypatch)
    request_id, path, home = _retain(
        manager, tmp_path, "newer", status="finished", worker_status="done",
    )
    retained_row = manager._latest_by_request()[request_id]
    manager._append_event({**retained_row, "retention_rule_version": MINE + 1})

    result = manager._gc_finalized_workspace(request_id, manager._latest_by_request()[request_id])

    assert result == {"request_id": request_id, "gc": False, "reason": _superseded(MINE + 1)}
    assert path.exists() and home.exists()
    assert manager._gc_finalized_workspaces() == {"gc_scanned": 1, "gc_cleaned": 0, "gc_skipped": 1}
    assert path.exists() and home.exists()

    manager._append_event({**retained_row, "retention_rule_version": MINE})
    assert manager._gc_finalized_workspaces()["gc_cleaned"] == 1
    assert not path.exists() and not home.exists()


def test_a_prefilter_row_from_a_newer_rule_also_keeps_the_worktree(tmp_path, monkeypatch):
    manager = _rig(tmp_path, monkeypatch)
    request_id, path, home = _retain(
        manager, tmp_path, "snapshot", status="finished", worker_status="done",
    )
    snapshot = {**manager._latest_by_request()[request_id], "retention_rule_version": MINE + 1}

    result = manager._gc_finalized_workspace(request_id, snapshot)

    assert result == {"request_id": request_id, "gc": False, "reason": _superseded(MINE + 1)}
    assert path.exists() and home.exists()


def test_rows_written_by_the_sweep_carry_the_retention_rule_version(tmp_path, monkeypatch):
    manager = _rig(tmp_path, monkeypatch)
    refused_id, refused_path, _ = _retain(
        manager, tmp_path, "refused", status="finished", worker_status="done",
    )
    removed_id, removed_path, _ = _retain(
        manager, tmp_path, "removed", status="finished", worker_status="done",
    )

    def refuse_cleanup(repo, path, home):
        raise worker_workspace.WorkspaceError("cleanup refused")

    rows_before = len(_rows(manager, refused_id))
    with monkeypatch.context() as patch:
        patch.setattr(process_launcher, "cleanup_workspace", refuse_cleanup)
        refusal = manager._gc_finalized_workspace(
            refused_id, manager._latest_by_request()[refused_id],
        )
    assert refusal["gc"] is False and refusal["reason"].startswith("cleanup_failed:")
    rows = _rows(manager, refused_id)
    assert len(rows) == rows_before + 1
    assert rows[-1]["workspace_disposition"] == "retained_in_place"
    assert rows[-1]["retention_rule_version"] == MINE
    assert refused_path.exists()

    rows_before = len(_rows(manager, removed_id))
    removal = manager._gc_finalized_workspace(
        removed_id, manager._latest_by_request()[removed_id],
    )
    assert removal["gc"] is True
    rows = _rows(manager, removed_id)
    assert len(rows) == rows_before + 1
    assert rows[-1]["workspace_disposition"] == "removed"
    assert rows[-1]["retention_rule_version"] == MINE
    assert not removed_path.exists()
