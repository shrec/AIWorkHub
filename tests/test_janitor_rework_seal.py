"""S1 janitor C1: pinned rework predecessors are sealed, then released.

A worktree pinned as a card's ``rework_predecessor`` used to be retained for
as long as the card could still be recovered -- in practice forever. The sweep
now seals the predecessor's exact hash-pinned bytes into a verified delta,
attaches that delta to the card with a preimage-guarded write, and only then
releases the worktree through the ordinary delete path. Any seal failure keeps
the worktree and is reported under ``failures``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    process_launcher,
    successful_rework_recovery,
    task_store,
    worker_workspace,
)

TASK_ID = "TASK_JANITOR_C1"
RUNNER = "claude_worker_janitor"
REQUEST_ID = "0123456789abcdef0123456789abcdef"
CLAIM_EPOCH = 3
CANDIDATE = b"sealed candidate bytes\n"


def _chmod_blocked_by_sandbox() -> bool:
    import tempfile

    with tempfile.TemporaryDirectory() as name:
        try:
            os.chmod(name, 0o700)
        except PermissionError:
            return True
    return False


@pytest.fixture(autouse=True)
def _bridge_chmod_sandbox_restriction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize chmod only where this exact sandbox rejects the bare syscall."""
    if _chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _regular_descriptor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open candidates plainly: some sandboxes cannot open the drive root."""

    @contextmanager
    def _plain_descriptor(path):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            yield fd
        finally:
            os.close(fd)

    monkeypatch.setattr(successful_rework_recovery, "_regular_descriptor", _plain_descriptor)


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    monkeypatch.delenv(worker_workspace.RUNTIME_ROOT_ENV, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _insert_card(
    repo: Path,
    task_id: str,
    card: dict,
    *,
    status: str = "blocked",
    worker_status: str = "blocked",
    archived_at: str = "",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(task_store.canonical_db_path(repo)))
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, RUNNER, "task_mcp", status, worker_status,
             json.dumps(card), now, now, archived_at),
        )
        conn.commit()
    finally:
        conn.close()


def _show(repo: Path):
    def show(task_id: str) -> dict:
        card = task_store.get_task(repo, task_id)
        if card is None:
            return {"returncode": 1, "stdout": "", "stderr": "missing"}
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    return show


def _manager(tmp_path: Path, repo: Path) -> process_launcher.ProcessManager:
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(repo),
        collision_guard=lambda **_k: {
            "returncode": 0, "stdout": '{"collision_free":true}', "stderr": "",
        },
        adapter_builder=lambda **_k: SimpleNamespace(
            argv=[], cwd=str(tmp_path), launchable=True, reason=""
        ),
        isolation_enabled=True,
    )


def _seed(
    manager: process_launcher.ProcessManager,
    tmp_path: Path,
    repo: Path,
    *,
    state: str,
    pid: int,
) -> tuple[Path, Path, dict]:
    """Seed one retained, proven-dead attempt and return its workspace payload."""
    path = tmp_path / "wtroot" / REQUEST_ID / "worktree"
    home = tmp_path / "wtroot" / REQUEST_ID / "home"
    (path / "out").mkdir(parents=True)
    (path / "out" / "result.txt").write_bytes(CANDIDATE)
    home.mkdir(parents=True)
    workspace = {
        "request_id": REQUEST_ID,
        "repo": str(repo),
        "path": str(path),
        "home": str(home),
        "allowed_writes": ["out/result.txt"],
        "parent_baseline": {},
        "workspace_baseline": {},
    }
    process_dir = tmp_path / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = process_dir / f"{REQUEST_ID}.request.json"
    worker_workspace.write_json_0600(metadata_path, {
        "request_id": REQUEST_ID,
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "metadata_path": str(metadata_path),
        "workspace": workspace,
    })
    manager._append_event({
        "request_id": REQUEST_ID,
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "state": state,
        "pid": pid,
        "pid_start_ticks": 999_999_000,
        "metadata_path": str(metadata_path),
        "workspace_retained": True,
    })
    return path, home, workspace


def _pinned_card(workspace: dict, hashes: dict) -> dict:
    return {
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": "task_mcp",
        "rework_predecessor": {
            "schema_id": "aiworkhub.rework_predecessor.v1",
            "task_id": TASK_ID,
            "request_id": REQUEST_ID,
            "claim_epoch": CLAIM_EPOCH,
            "workspace": workspace,
            "changed_path_hashes": hashes,
        },
    }


def _descriptor(repo: Path, digest: str = "d" * 64) -> dict:
    return {
        "schema_id": "aiworkhub.rework_delta_descriptor.v1",
        "sealed": True,
        "authority_repo": str(repo.resolve()),
        "task_id": TASK_ID,
        "request_id": REQUEST_ID,
        "claim_epoch": CLAIM_EPOCH,
        "artifact_path": str(repo / "delta.json"),
        "artifact_sha256": digest,
    }


# --- the sweep seals a pinned predecessor, then releases it --------------------


def test_pinned_rework_predecessor_is_sealed_then_released(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    manager = _manager(tmp_path, repo)
    path, home, workspace = _seed(
        manager, tmp_path, repo, state="validation_failed", pid=2_147_483_101,
    )
    hashes = {"out/result.txt": hashlib.sha256(CANDIDATE).hexdigest()}
    _insert_card(repo, TASK_ID, _pinned_card(workspace, hashes))

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 1, "gc_skipped": 0}
    assert not path.exists() and not home.exists()
    card = task_store.get_task(repo, TASK_ID)
    predecessor = card["rework_predecessor"]
    descriptor = predecessor["rework_delta"]
    artifact = Path(descriptor["artifact_path"])
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == descriptor["artifact_sha256"]
    assert predecessor["delta_artifact"] == {
        "path": descriptor["artifact_path"], "digest": descriptor["artifact_sha256"],
    }
    assert manager._gc_disposition(card, REQUEST_ID, repo=manager.repo) == (
        True, "sealed_rework_delta",
    )
    latest = manager._request_events(REQUEST_ID)[-1]
    assert latest["workspace_gc"] is True
    assert latest["workspace_gc_reason"] == "sealed_rework_delta"
    assert task_store.get_task_events(repo, TASK_ID)[0]["event"] == "rework_delta_sealed"


def test_hash_mismatch_keeps_the_worktree_and_reports_the_failure(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    manager = _manager(tmp_path, repo)
    path, home, workspace = _seed(
        manager, tmp_path, repo, state="validation_failed", pid=2_147_483_102,
    )
    hashes = {"out/result.txt": hashlib.sha256(b"what the reviewer saw\n").hexdigest()}
    _insert_card(repo, TASK_ID, _pinned_card(workspace, hashes))
    reason = "rework_seal_failed:successful_rework_hash_mismatch"

    single = manager._gc_finalized_workspace(
        REQUEST_ID, manager._latest_by_request()[REQUEST_ID]
    )
    result = manager._gc_finalized_workspaces()

    assert single == {"request_id": REQUEST_ID, "gc": False, "reason": reason}
    assert result["failures"] == [
        {"request_id": REQUEST_ID, "task_id": TASK_ID, "reason": reason}
    ]
    assert (result["gc_cleaned"], result["gc_skipped"]) == (0, 1)
    assert path.exists() and home.exists()
    predecessor = task_store.get_task(repo, TASK_ID)["rework_predecessor"]
    assert "rework_delta" not in predecessor


# --- every terminal process state reaches GC -----------------------------------


def test_gc_candidates_are_every_terminal_process_state():
    assert set(process_launcher.GC_CANDIDATE_PROCESS_STATES) == set(
        process_launcher.TERMINAL_PROCESS_STATES
    )
    assert "superseded" in process_launcher.GC_DISPOSED_CANONICAL_STATUSES


def test_finalize_failed_attempt_of_superseded_task_is_removed(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    manager = _manager(tmp_path, repo)
    path, home, _ = _seed(
        manager, tmp_path, repo, state="finalize_failed", pid=2_147_483_103,
    )
    _insert_card(
        repo, TASK_ID, {"task_id": TASK_ID},
        status="superseded", worker_status="superseded",
    )

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 1, "gc_skipped": 0}
    assert not path.exists() and not home.exists()
    assert manager._request_events(REQUEST_ID)[-1]["workspace_gc_reason"] == (
        "disposed_task_status:superseded"
    )


# --- task_store.attach_rework_delta ---------------------------------------------


def test_attach_rework_delta_is_exact_and_single_shot(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _insert_card(repo, TASK_ID, _pinned_card({}, {"out/result.txt": "a" * 64}))
    descriptor = _descriptor(repo)

    wrong = task_store.attach_rework_delta(
        repo, TASK_ID, predecessor_request_id="f" * 32,
        claim_epoch=CLAIM_EPOCH, descriptor={**descriptor, "request_id": "f" * 32},
    )
    first = task_store.attach_rework_delta(
        repo, TASK_ID, predecessor_request_id=REQUEST_ID,
        claim_epoch=CLAIM_EPOCH, descriptor=descriptor,
    )
    second = task_store.attach_rework_delta(
        repo, TASK_ID, predecessor_request_id=REQUEST_ID,
        claim_epoch=CLAIM_EPOCH, descriptor=descriptor,
    )

    assert wrong == (False, "rework_predecessor_mismatch")
    assert first == (True, "attached")
    assert second == (False, "rework_delta_present")
    assert task_store.get_task_events(repo, TASK_ID)[0]["event"] == "rework_delta_sealed"
    predecessor = task_store.get_task(repo, TASK_ID)["rework_predecessor"]
    assert predecessor["rework_delta"] == descriptor
    assert predecessor["delta_artifact"] == {
        "path": descriptor["artifact_path"], "digest": descriptor["artifact_sha256"],
    }


def test_attach_rework_delta_reports_a_missing_task(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)

    assert task_store.attach_rework_delta(
        repo, "NO_SUCH_TASK", predecessor_request_id=REQUEST_ID,
        claim_epoch=CLAIM_EPOCH, descriptor=_descriptor(repo),
    ) == (False, "task_missing")


def test_referenced_rework_delta_digests_counts_undecided_tasks_only(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)

    def sealed(digest: str) -> dict:
        return {"rework_predecessor": {
            "rework_delta": {"artifact_sha256": digest},
            "delta_artifact": {"digest": digest},
        }}

    _insert_card(repo, "BLOCKED", sealed("1" * 64))
    _insert_card(repo, "PENDING", sealed("2" * 64), status="pending", worker_status="unclaimed")
    _insert_card(repo, "FINISHED", sealed("3" * 64), status="finished", worker_status="done")
    _insert_card(
        repo, "SUPERSEDED", sealed("4" * 64),
        status="superseded", worker_status="superseded",
    )
    _insert_card(
        repo, "ARCHIVED", sealed("5" * 64),
        archived_at=datetime.now(timezone.utc).isoformat(),
    )
    _insert_card(repo, "NOT_A_DIGEST", {"digest": "not-hex"})

    assert task_store.referenced_rework_delta_digests(repo) == {"1" * 64, "2" * 64}
