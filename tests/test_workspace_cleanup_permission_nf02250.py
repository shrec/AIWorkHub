"""NF-2026-02250: a held file must not fail finalize nor cost a live candidate.

External report (Mego, AIWorkHub 0.12.97): workspace cleanup met a Windows file
lock, raised ``workspace_cleanup_remove_failed:<name>:PermissionError``, the card
became ``finalize_failed`` and the janitor then deleted the reviewed candidate
worktree.  Finalize (purge and full-snapshot scratch), accept and the janitor all
reach ``worker_workspace.cleanup_workspace``; these tests drive that one shared
function and the real janitor sweep.  Only the OS remove call is replaced.
"""
from __future__ import annotations

import errno
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import (  # noqa: E402
    storage_retention,
    task_store,
    worker_workspace,
    workspace_cleanup_retry,
)

NOW = "2026-11-13T00:00:00+00:00"


def _layout(base: Path, request_id: str) -> tuple[Path, Path, Path]:
    root = base / request_id
    path = root / "worktree"
    home = root / "home"
    (path / "src").mkdir(parents=True)
    home.mkdir()
    locked = path / "src" / "candidate.py"
    locked.write_text("print('reviewed candidate')\n", encoding="utf-8")
    return path, home, locked


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(workspace_cleanup_retry, "_sleep", recorded.append)
    return recorded


def _hold_rmtree(monkeypatch, locked: Path, failures: int | None) -> list[str]:
    """Replace the remove call: ``failures`` PermissionErrors, then the real one."""
    real_rmtree = worker_workspace.shutil.rmtree
    calls: list[str] = []

    def held(target: Any, *args: Any, **kwargs: Any) -> None:
        calls.append(str(target))
        if failures is None or len(calls) <= failures:
            raise PermissionError(errno.EACCES, "Access is denied", str(locked))
        real_rmtree(target, *args, **kwargs)

    monkeypatch.setattr(worker_workspace.shutil, "rmtree", held)
    return calls


def test_transient_permission_error_is_retried_and_cleanup_completes(
    tmp_path, monkeypatch, sleeps
):
    repo = tmp_path / "repo"
    repo.mkdir()
    path, home, locked = _layout(tmp_path / "worktrees", "REQ_TRANSIENT")
    calls = _hold_rmtree(monkeypatch, locked, failures=2)

    worker_workspace.cleanup_workspace(repo, path, home)

    assert not path.parent.exists()
    assert len(calls) >= 3
    assert sleeps, "the lock was retried after a backoff, not raised"
    assert sleeps == sorted(sleeps)
    assert len(sleeps) <= len(workspace_cleanup_retry.PERMISSION_RETRY_DELAYS_SECONDS)


def test_persistent_permission_error_is_a_residue_not_a_finalize_failure(
    tmp_path, monkeypatch, sleeps, caplog
):
    repo = tmp_path / "repo"
    repo.mkdir()
    path, home, locked = _layout(tmp_path / "worktrees", "REQ_HELD")
    _hold_rmtree(monkeypatch, locked, failures=None)
    disposed: list[str] = []
    monkeypatch.setattr(
        worker_workspace, "dispose_worker_temp",
        lambda _repo, request_id: disposed.append(request_id),
    )

    # Before the fix this raised workspace_cleanup_remove_failed:...:PermissionError.
    with caplog.at_level("WARNING", logger=workspace_cleanup_retry.__name__):
        residue = worker_workspace.cleanup_workspace(repo, path, home)

    # The partial delete is surfaced to the caller, never reported as success.
    assert residue is not None
    assert residue == workspace_cleanup_retry.recent_residues()[-1]
    assert sleeps == list(workspace_cleanup_retry.PERMISSION_RETRY_DELAYS_SECONDS)
    assert residue["schema_id"] == workspace_cleanup_retry.CLEANUP_RESIDUE_SCHEMA_ID
    assert residue["event"] == workspace_cleanup_retry.CLEANUP_RESIDUE_EVENT
    assert residue["exception_type"] == "PermissionError"
    assert residue["path"] == str(locked)
    assert residue["root"] == str(path.parent)
    assert residue["stage"] == "workspace_tree"
    assert residue["errno"] == errno.EACCES
    assert residue["attempts"] == len(sleeps) + 1
    assert "workspace_cleanup_remove_failed" in residue["error"]
    assert locked.read_text(encoding="utf-8")  # the candidate is still on disk
    # NF430: the request-owned worker temp authority is still disposed.
    assert disposed == ["REQ_HELD"]
    events = [getattr(r, "cleanup_residue", None) for r in caplog.records]
    assert residue in events, "a structured cleanup_residue event was logged"


def _windows_permission_error(locked: Path, winerror: int) -> PermissionError:
    exc = PermissionError(errno.EACCES, "Access is denied", str(locked))
    exc.winerror = winerror  # type: ignore[misc]  # POSIX builds lack the slot
    return exc


def test_windows_sharing_violation_is_a_residue(tmp_path, monkeypatch, sleeps):
    repo = tmp_path / "repo"
    repo.mkdir()
    path, home, locked = _layout(tmp_path / "worktrees", "REQ_SHARING")

    def shared(target: Any, *args: Any, **kwargs: Any) -> None:
        raise _windows_permission_error(locked, 32)

    monkeypatch.setattr(worker_workspace.shutil, "rmtree", shared)

    residue = worker_workspace.cleanup_workspace(repo, path, home)

    assert residue is not None and residue["winerror"] == 32
    assert residue["path"] == str(locked)


def test_windows_access_denied_propagates_after_retries(tmp_path, monkeypatch, sleeps):
    repo = tmp_path / "repo"
    repo.mkdir()
    path, home, locked = _layout(tmp_path / "worktrees", "REQ_DENIED")
    before = len(workspace_cleanup_retry.recent_residues())

    def denied(target: Any, *args: Any, **kwargs: Any) -> None:
        raise _windows_permission_error(locked, 5)

    monkeypatch.setattr(worker_workspace.shutil, "rmtree", denied)

    with pytest.raises(worker_workspace.WorkspaceError, match="workspace_cleanup_remove_failed"):
        worker_workspace.cleanup_workspace(repo, path, home)

    assert sleeps == list(workspace_cleanup_retry.PERMISSION_RETRY_DELAYS_SECONDS)
    assert len(workspace_cleanup_retry.recent_residues()) == before
    assert locked.is_file()


def test_a_non_lock_failure_still_fails_closed(tmp_path, monkeypatch, sleeps):
    repo = tmp_path / "repo"
    repo.mkdir()
    path, home, locked = _layout(tmp_path / "worktrees", "REQ_IOERROR")

    def broken(target: Any, *args: Any, **kwargs: Any) -> None:
        raise OSError(errno.EIO, "I/O error", str(locked))

    monkeypatch.setattr(worker_workspace.shutil, "rmtree", broken)

    with pytest.raises(worker_workspace.WorkspaceError, match="workspace_cleanup_remove_failed"):
        worker_workspace.cleanup_workspace(repo, path, home)
    assert sleeps == []


# --------------------------------------------------------------------------
# A worktree holding a candidate of a non-terminal card is never deleted.
# --------------------------------------------------------------------------


def _insert(root: Path, task_id: str, *, status: str, card: dict[str, Any]) -> None:
    conn = sqlite3.connect(task_store.canonical_db_path(root))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id,runner,topic,mode,status,worker_status,priority,"
            "objective,card_json,created_at,updated_at,claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, "claude_coding", "coding", "solo", status, status, "normal",
             "obj", json.dumps(card), NOW, NOW, "claude_coding"),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    return root


def _worktrees(repo: Path) -> Path:
    return repo / ".aiworkhub" / "runtime" / "worktrees"


CARDS = [
    pytest.param(
        "review",
        lambda rid: {"launch_request_id": rid,
                     "terminal_review": {"substatus": "review_ready"}},
        "non_terminal_candidate:review:",
        id="review-ready",
    ),
    pytest.param(
        "blocked",
        lambda rid: {"launch_request_id": rid,
                     "terminal_failure": {"substatus": "finalize_failed", "request_id": rid}},
        "non_terminal_candidate:blocked:",
        id="blocked-finalize-failed",
    ),
    pytest.param(
        "processing",
        lambda rid: {"launch_request_id": rid, "worker_substatus": "finalizing"},
        "non_terminal_candidate:processing:",
        id="finalizing",
    ),
    pytest.param(
        "pending",
        lambda rid: {"predecessor_request_id": rid,
                     "retained_candidate": {"request_id": rid}},
        "non_terminal_candidate:pending:",
        id="pending-with-retained-candidate",
    ),
]


@pytest.mark.parametrize("status, card, reason", CARDS)
def test_cleanup_never_deletes_a_live_candidate(repo, status, card, reason):
    request_id = "REQ_LIVE"
    _insert(repo, "T_LIVE", status=status, card=card(request_id))
    path, home, locked = _layout(_worktrees(repo), request_id)

    with pytest.raises(worker_workspace.WorkspaceError, match=reason):
        worker_workspace.cleanup_workspace(repo, path, home)

    assert locked.is_file()
    assert home.is_dir()


@pytest.mark.parametrize("status, card, reason", CARDS)
def test_janitor_never_deletes_a_live_candidate(repo, status, card, reason):
    request_id = "REQ_JANITOR"
    _insert(repo, "T_JANITOR", status=status, card=card(request_id))
    path, _home, locked = _layout(_worktrees(repo), request_id)

    result = storage_retention._remove_unattributed_worktrees(
        repo, _worktrees(repo), time.time() + 10 * 86400
    )

    assert result["removed"] == []
    assert any(reason in str(error) for error in result["errors"]), result
    assert locked.is_file()


@pytest.mark.parametrize("status", ["finished", "superseded"])
def test_a_terminal_card_does_not_pin_its_worktree(repo, status):
    request_id = "REQ_DONE"
    _insert(repo, "T_DONE", status=status, card={"launch_request_id": request_id})
    path, home, _locked = _layout(_worktrees(repo), request_id)

    assert worker_workspace.cleanup_workspace(repo, path, home) is None

    assert not path.parent.exists()


def _store_not_ready(*_args: Any, **_kwargs: Any) -> Any:
    return task_store.StorageReadiness(False, "canonical_db_corrupt", "", "")


def _store_raises(*_args: Any, **_kwargs: Any) -> Any:
    raise sqlite3.DatabaseError("database disk image is malformed")


@pytest.mark.parametrize("readiness", [_store_not_ready, _store_raises],
                         ids=["not-ready", "unreadable"])
def test_an_unverifiable_task_store_fails_closed(repo, monkeypatch, readiness):
    request_id = "REQ_UNVERIFIED"
    path, home, locked = _layout(_worktrees(repo), request_id)
    monkeypatch.setattr(task_store, "storage_readiness", readiness)

    with pytest.raises(worker_workspace.WorkspaceError,
                       match="workspace_cleanup_live_candidate_retained:candidate_hold_unverified"):
        worker_workspace.cleanup_workspace(repo, path, home)

    assert locked.is_file()
    assert home.is_dir()
