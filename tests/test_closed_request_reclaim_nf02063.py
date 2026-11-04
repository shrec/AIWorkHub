"""NF-2026-02063: a closed request's worktree and home are reclaimed.

Three root causes kept disposed requests' workspaces forever:

A. ``gc_husk_without_metadata`` reclaimed only an exact husk, so a home holding
   a Source Graph base-pin file (or any worker state) kept a disposed card's
   request indefinitely once its ``.request.json`` was gone.
B. ``terminal_log_retention`` deleted a decided request's ``.request.json``
   while its worktree/home still existed -- the very metadata path A needs.
C. ``ProcessManager._latest_by_request`` let a stateless
   ``review_orchestrator_*`` row replace the request's lifecycle row, so the
   request read as state ``None`` and was never a GC candidate.
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import (
    process_launcher,
    review_orchestrator,
    terminal_log_retention,
    worker_workspace,
)

_HERE = Path(__file__).resolve().parent


def _load_sibling(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, _HERE / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_B512 = _load_sibling("test_finalized_workspace_gc_b512_v1.py", "nf02063_b512_fixtures")
_TLR = _load_sibling("test_terminal_log_retention.py", "nf02063_tlr_fixtures")

GC_RID = "req-closed-nf02063"
WAIT = "review_orchestrator_wait"
REWORK = "review_orchestrator_mechanical_rework"


@pytest.fixture(autouse=True)
def _bridge_chmod_sandbox_restriction(monkeypatch):
    if _B512._chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


# --- A: metadata-less, non-husk workspace of a disposed card --------------------


def _manager(tmp_path, monkeypatch, **status):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _B512._card()
    card.update(status)
    return _B512._build_manager(tmp_path, card), card


def _seed(manager, tmp_path, card, *, state="worker_failed", base_pin=True):
    """A retained request whose ``.request.json`` is gone and whose home holds a pin."""
    entry = tmp_path / "wtroot" / GC_RID
    (entry / "worktree").mkdir(parents=True)
    home = entry / "home"
    home.mkdir()
    if base_pin:
        (home / "source-graph-base.pin").write_text("pinned base\n")
    manager._append_event({
        "request_id": GC_RID,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "state": state,
        "pid": 2_147_483_090,
        "pid_start_ticks": 999_999_940,
        "workspace_retained": True,
        "error": "",
        "metadata_path": str(tmp_path / "processes" / f"{GC_RID}.request.json"),
    })
    return entry


def _audit_rows(manager) -> list[dict]:
    path = process_launcher.review_workspace_retention_audit_path(manager.process_log_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.mark.parametrize(
    "status",
    [
        {"status": "superseded", "worker_status": "superseded", "claimed_by": ""},
        {"status": "finished", "worker_status": "done", "claimed_by": "",
         "archived_at": "2026-10-01T00:00:00Z"},
    ],
    ids=["superseded", "archived"],
)
def test_disposed_card_with_base_pin_home_is_reclaimed_and_audited(
    tmp_path, monkeypatch, status
):
    manager, card = _manager(tmp_path, monkeypatch, **status)
    entry = _seed(manager, tmp_path, card)
    assert not worker_workspace.is_workspace_husk(entry / "worktree", entry / "home")

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 1, "gc_skipped": 0}
    assert not (entry / "worktree").exists()
    assert not (entry / "home").exists()
    latest = manager._request_events(GC_RID)[-1]
    assert latest["workspace_gc"] is True
    assert latest["workspace_disposition"] == "removed"
    assert latest["workspace_gc_reason"] == "disposed_workspace_reclaimed"
    audit = [row for row in _audit_rows(manager) if row.get("request_id") == GC_RID]
    assert audit, "reclaim must write a retention audit row"
    assert audit[-1]["reason"] == "disposed_workspace_reclaimed"
    assert audit[-1]["reason"] != "husk_reclaimed"
    assert audit[-1]["action"] == "purge"


def _detail(manager):
    return manager._gc_finalized_workspace(GC_RID, manager._latest_by_request()[GC_RID])


def test_pinned_rework_predecessor_is_kept(tmp_path, monkeypatch):
    manager, card = _manager(
        tmp_path, monkeypatch, status="blocked", worker_status="blocked", claimed_by="",
        rework_predecessor={"request_id": GC_RID, "task_id": "TASK_B512"},
    )
    entry = _seed(manager, tmp_path, card)
    monkeypatch.setattr(
        process_launcher._worker_workspace, "has_verified_rework_delta",
        lambda *a, **k: False,
    )

    result = _detail(manager)

    assert result == {"request_id": GC_RID, "gc": False, "reason": "pinned_rework_predecessor"}
    assert (entry / "home" / "source-graph-base.pin").exists()


def test_live_process_is_kept(tmp_path, monkeypatch):
    manager, card = _manager(
        tmp_path, monkeypatch, status="superseded", worker_status="superseded", claimed_by="",
    )
    entry = _seed(manager, tmp_path, card)
    monkeypatch.setattr(process_launcher, "_process_proven_dead", lambda *a, **k: False)

    result = _detail(manager)

    assert result == {"request_id": GC_RID, "gc": False, "reason": "process_not_proven_dead"}
    assert (entry / "home" / "source-graph-base.pin").exists()


def test_current_review_request_is_kept(tmp_path, monkeypatch):
    manager, card = _manager(
        tmp_path, monkeypatch, status="review", worker_status="review", claimed_by="",
        terminal_review={"evidence": {"request_identity": {"request_id": GC_RID}}},
    )
    entry = _seed(manager, tmp_path, card, state="review_ready")

    result = _detail(manager)

    assert result == {"request_id": GC_RID, "gc": False, "reason": "current_review_request"}
    assert (entry / "home" / "source-graph-base.pin").exists()


@pytest.mark.parametrize(
    "status,worker_status",
    [("pending", "unclaimed"), ("processing", "in_progress")],
)
def test_not_disposed_card_is_kept(tmp_path, monkeypatch, status, worker_status):
    manager, card = _manager(
        tmp_path, monkeypatch, status=status, worker_status=worker_status,
    )
    entry = _seed(manager, tmp_path, card)

    result = _detail(manager)

    assert result["gc"] is False
    assert result["reason"].startswith("task_not_disposed:")
    assert (entry / "home" / "source-graph-base.pin").exists()
    assert _audit_rows(manager) == []


# --- B: owned files are kept while the workspace exists --------------------------


@pytest.fixture
def _retention_clock(monkeypatch):
    monkeypatch.setattr(
        terminal_log_retention, "now_utc", lambda: datetime(2035, 1, 1, tzinfo=timezone.utc)
    )


def _owned_names(repo: Path, request_id: str) -> set[str]:
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH
    return {p.name for p in process_root.iterdir() if p.name.startswith(request_id)}


def test_owned_files_kept_while_workspace_exists_then_deleted(
    tmp_path, monkeypatch, _retention_clock
):
    root = tmp_path / "wtroot"
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(root))
    repo = _TLR._repo(tmp_path)
    done_id = "1" * 32
    _TLR._run(repo, done_id, "TASK_DONE")
    for part in ("worktree", "home"):
        (root / done_id / part).mkdir(parents=True)

    preview = terminal_log_retention.preview(repo)
    assert preview["candidate_count"] == 0
    held = [row for row in preview["protected"] if row["request_id"] == done_id]
    assert [row["reason"] for row in held] == ["request_workspace_present"]
    terminal_log_retention.enforce(repo)
    assert f"{done_id}.request.json" in _owned_names(repo, done_id)

    # Only the home left: still held.
    (root / done_id / "worktree").rmdir()
    terminal_log_retention.enforce(repo)
    assert f"{done_id}.request.json" in _owned_names(repo, done_id)

    # Workspace GC reclaimed it: the next pass deletes the files as before.
    (root / done_id / "home").rmdir()
    result = terminal_log_retention.enforce(repo)
    assert result["deleted"] == 1
    assert _owned_names(repo, done_id) == set()


def test_delete_reproves_workspace_absence(tmp_path, monkeypatch, _retention_clock):
    root = tmp_path / "wtroot"
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(root))
    repo = _TLR._repo(tmp_path)
    ids = ["1" * 32, "3" * 32]
    for request_id in ids:
        _TLR._run(repo, request_id, "TASK_DONE")
    preview = terminal_log_retention.preview(repo)
    assert {row["request_id"] for row in preview["candidates"]} == set(ids)
    # A workspace appears for one request after classification.
    (root / ids[0] / "home").mkdir(parents=True)

    for workers in (1, 4):
        deletion = terminal_log_retention._delete_candidates(
            repo, [row for row in preview["candidates"] if row["request_id"] == ids[0]],
            workers=workers,
        )
        assert deletion["deleted"] == 0
        assert f"{ids[0]}.request.json" in _owned_names(repo, ids[0])

    deletion = terminal_log_retention._delete_candidates(repo, preview["candidates"], workers=4)
    assert deletion["deleted"] == 1
    assert f"{ids[0]}.request.json" in _owned_names(repo, ids[0])
    assert _owned_names(repo, ids[1]) == set()


def test_unresolvable_worktree_root_fails_closed(tmp_path, monkeypatch, _retention_clock):
    repo = _TLR._repo(tmp_path)
    done_id = "1" * 32
    _TLR._run(repo, done_id, "TASK_DONE")

    def _boom(*_a, **_k):
        raise worker_workspace.WorkspaceError("root_unresolvable")

    monkeypatch.setattr(worker_workspace, "configured_worktree_root", _boom)
    preview = terminal_log_retention.preview(repo)

    assert preview["candidate_count"] == 0
    held = [row for row in preview["protected"] if row["request_id"] == done_id]
    assert [row["reason"] for row in held] == ["request_workspace_root_unresolved"]


# --- C: stateless review-orchestrator rows never replace lifecycle state ----------


def _orchestrator_row(event_type: str, request_id: str, task_id: str) -> dict:
    return {
        "event_type": event_type,
        "request_id": request_id,
        "task_id": task_id,
        "review_automation": {"state": "deferred", "reason": "x", "action_id": "a1"},
    }


@pytest.mark.parametrize("event_type", [WAIT, REWORK])
def test_orchestrator_row_after_terminal_row_keeps_latest_state(
    tmp_path, monkeypatch, event_type
):
    manager, card = _manager(tmp_path, monkeypatch)
    registration = {"target_task_id": card["task_id"], "target_request_id": GC_RID}
    manager._append_event({
        "request_id": GC_RID, "task_id": card["task_id"], "runner": card["runner"],
        "state": "review_ready", "exit_code": 0,
        "review_automation": {"state": "pending", "registration": registration},
    })
    before = manager._latest_by_request()[GC_RID]

    manager._append_event(_orchestrator_row(event_type, GC_RID, card["task_id"]))

    after = manager._latest_by_request()[GC_RID]
    assert after == before
    assert after["state"] == "review_ready"
    # The pending-registration reader still sees its registration ...
    assert after["review_automation"] == {"state": "pending", "registration": registration}
    # ... and the raw per-request history still carries the orchestrator row.
    assert manager._request_events(GC_RID)[-1]["event_type"] == event_type


def test_pending_registration_reader_still_retries_after_wait_row(tmp_path, monkeypatch):
    manager, card = _manager(tmp_path, monkeypatch)
    registration = {"target_task_id": card["task_id"], "target_request_id": GC_RID}
    manager._append_event({
        "request_id": GC_RID, "task_id": card["task_id"], "runner": card["runner"],
        "state": "review_ready",
        "review_automation": {"state": "pending", "registration": registration},
    })
    manager._append_event(_orchestrator_row(WAIT, GC_RID, card["task_id"]))
    seen: list[dict] = []

    def _register(_manager, *, db_path, registration):
        seen.append(dict(registration))
        return type("Chain", (), {"chain_identity_sha256": "c" * 64})()

    monkeypatch.setattr(review_orchestrator, "register_candidate", _register)
    result = review_orchestrator.retry_pending_registrations(
        manager, db_path=tmp_path / "review.db", events=manager._latest_by_request()
    )

    assert result["automation_retried"] == 1
    assert seen == [registration]


def test_disposed_request_with_trailing_wait_row_is_reclaimed(tmp_path, monkeypatch):
    manager, card = _manager(
        tmp_path, monkeypatch, status="superseded", worker_status="superseded", claimed_by="",
    )
    entry = _seed(manager, tmp_path, card)
    manager._append_event(_orchestrator_row(WAIT, GC_RID, card["task_id"]))

    assert manager._latest_by_request()[GC_RID]["state"] == "worker_failed"
    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 1, "gc_skipped": 0}
    assert not entry.exists() or not any(entry.iterdir())
