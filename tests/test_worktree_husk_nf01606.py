"""NF-2026-01606: a worktree "husk" is treated as an absent workspace.

A husk is ``<root>/<request_id>/worktree`` with no ``.git`` whose only entries
are regular ``*.stackdump`` files (or nothing).  Reroute verifies a husk
predecessor through its sealed delta, and the retained-workspace GC reclaims a
metadata-less husk only once its card is finished/archived/superseded.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path

import pytest

from aiworkhub import core, worker_workspace

_SIBLING = Path(__file__).with_name("test_finalized_workspace_gc_b512_v1.py")
_SPEC = importlib.util.spec_from_file_location("nf01606_b512_fixtures", _SIBLING)
assert _SPEC is not None and _SPEC.loader is not None
_B512 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_B512)

RID = "0123456789abcdef0123456789abcdef"
TASK_ID = "TASK_NF01606"
RELATIVE = "src/app.py"
CONTENT = b"print('candidate')\n"
CONTENT_SHA = hashlib.sha256(CONTENT).hexdigest()


@pytest.fixture(autouse=True)
def _bridge_chmod_sandbox_restriction(monkeypatch):
    if _B512._chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


# --- is_workspace_husk ---------------------------------------------------------


def test_husk_predicate_true_for_empty_and_stackdump_only(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert worker_workspace.is_workspace_husk(empty) is True

    dump = tmp_path / "dump"
    dump.mkdir()
    (dump / "bash.exe.stackdump").write_text("crash")
    assert worker_workspace.is_workspace_husk(dump) is True


def test_husk_predicate_false_for_real_content(tmp_path):
    git_file = tmp_path / "git_file"
    git_file.mkdir()
    (git_file / ".git").write_text("gitdir: elsewhere\n")
    assert worker_workspace.is_workspace_husk(git_file) is False

    git_dir = tmp_path / "git_dir"
    (git_dir / ".git").mkdir(parents=True)
    assert worker_workspace.is_workspace_husk(git_dir) is False

    subdir = tmp_path / "subdir"
    (subdir / "nested").mkdir(parents=True)
    assert worker_workspace.is_workspace_husk(subdir) is False

    other = tmp_path / "other"
    other.mkdir()
    (other / "bash.exe.stackdump").write_text("crash")
    (other / "notes.txt").write_text("keep")
    assert worker_workspace.is_workspace_husk(other) is False

    assert worker_workspace.is_workspace_husk(tmp_path / "missing") is False
    plain_file = tmp_path / "plain_file"
    plain_file.write_text("x")
    assert worker_workspace.is_workspace_husk(plain_file) is False


def test_husk_predicate_false_for_symlinks(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    assert worker_workspace.is_workspace_husk(target) is True
    assert worker_workspace.is_workspace_husk(link) is False

    real_file = tmp_path / "real.txt"
    real_file.write_text("x")
    dump = tmp_path / "dump"
    dump.mkdir()
    try:
        (dump / "linked.stackdump").symlink_to(real_file)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    assert worker_workspace.is_workspace_husk(dump) is False


def test_husk_predicate_home_must_be_absent_or_empty(tmp_path):
    path = tmp_path / "worktree"
    path.mkdir()
    home = tmp_path / "home"
    assert worker_workspace.is_workspace_husk(path, home) is True
    home.mkdir()
    assert worker_workspace.is_workspace_husk(path, home) is True
    (home / "state.txt").write_text("x")
    assert worker_workspace.is_workspace_husk(path, home) is False


# --- reroute retained-candidate verifier ---------------------------------------


def _reroute_fixture(tmp_path, monkeypatch, *, sealed):
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    root = (tmp_path / "wtroot").resolve()
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(root))
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    worktree = root / RID / "worktree"
    worktree.parent.mkdir(parents=True)
    worktree.mkdir()
    calls = {"changed_paths": 0, "materialize": 0}

    def fake_changed_paths(workspace, **_kwargs):
        calls["changed_paths"] += 1
        raise worker_workspace.WorkspaceError("not_a_git_worktree")

    def fake_materialize(*, worktree, **_kwargs):
        calls["materialize"] += 1
        target = Path(worktree) / RELATIVE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(CONTENT)
        return [RELATIVE]

    monkeypatch.setattr(worker_workspace, "changed_paths", fake_changed_paths)
    monkeypatch.setattr(
        worker_workspace, "has_verified_rework_delta", lambda *a, **k: sealed
    )
    monkeypatch.setattr(
        worker_workspace, "materialize_rework_delta_artifact", fake_materialize
    )
    card = {
        "task_id": TASK_ID,
        "allowed_writes": [RELATIVE],
        "rework_predecessor": {
            "schema_id": "aiworkhub.rework_predecessor.v1",
            "task_id": TASK_ID,
            "request_id": RID,
            "claim_epoch": 1,
            "changed_path_hashes": {RELATIVE: CONTENT_SHA},
            "delta_artifact": {"sealed": True},
            "workspace": {
                "request_id": RID,
                "repo": str(repo),
                "path": str(worktree),
                "home": str(root / RID / "home"),
                "allowed_writes": [RELATIVE],
                "parent_baseline": {},
                "workspace_baseline": {},
                "base_oid": "a" * 40,
            },
        },
    }
    return card, worktree, calls


def _verify(card, **kwargs):
    return core._verified_retained_predecessor_receipt(
        card, task_id=TASK_ID, **kwargs
    )


@pytest.mark.parametrize("dump", [False, True])
def test_husk_with_sealed_delta_verifies_through_the_delta(
    tmp_path, monkeypatch, dump
):
    card, worktree, calls = _reroute_fixture(tmp_path, monkeypatch, sealed=True)
    if dump:
        (worktree / "bash.exe.stackdump").write_text("crash")

    receipt, error = _verify(card)

    assert error is None, error
    assert receipt["retained_candidate_preserved"] is True
    assert receipt["retained_changed_path_count"] == 1
    assert calls == {"changed_paths": 0, "materialize": 1}


def test_husk_without_sealed_delta_matches_absent_workspace(
    tmp_path, monkeypatch
):
    replay = {"authorized": True}
    card, worktree, calls = _reroute_fixture(tmp_path, monkeypatch, sealed=False)
    husk_outcome = _verify(card, validation_replay_receipt=replay)
    worktree.rmdir()
    absent_outcome = _verify(card, validation_replay_receipt=replay)

    assert husk_outcome == absent_outcome
    assert husk_outcome == (None, "reroute_retained_candidate_workspace_invalid")
    assert calls == {"changed_paths": 0, "materialize": 0}


def test_husk_without_sealed_delta_or_receipt_is_delta_unverified(
    tmp_path, monkeypatch
):
    card, _worktree, calls = _reroute_fixture(tmp_path, monkeypatch, sealed=False)
    assert _verify(card) == (None, "reroute_retained_candidate_delta_unverified")
    assert calls["changed_paths"] == 0


def test_non_husk_workspace_keeps_the_git_verification_path(
    tmp_path, monkeypatch
):
    card, worktree, calls = _reroute_fixture(tmp_path, monkeypatch, sealed=True)
    (worktree / ".git").write_text("gitdir: elsewhere\n")
    (worktree / RELATIVE).parent.mkdir(parents=True)
    (worktree / RELATIVE).write_bytes(CONTENT)

    assert _verify(card) == (None, "reroute_retained_candidate_workspace_unverifiable")
    assert calls == {"changed_paths": 1, "materialize": 0}

    monkeypatch.setattr(
        worker_workspace, "changed_paths", lambda *a, **k: [RELATIVE]
    )
    receipt, error = _verify(card)
    assert error is None, error
    assert receipt["retained_candidate_preserved"] is True
    assert calls["materialize"] == 0


def test_non_husk_directory_without_git_is_still_unverifiable(
    tmp_path, monkeypatch
):
    card, worktree, calls = _reroute_fixture(tmp_path, monkeypatch, sealed=True)
    (worktree / "notes.txt").write_text("real content")

    assert _verify(card) == (None, "reroute_retained_candidate_workspace_unverifiable")
    assert calls == {"changed_paths": 1, "materialize": 0}


# --- retained-workspace GC -----------------------------------------------------

GC_RID = "req-husk-nf01606"


def _seed_husk(manager, tmp_path, card, *, request_id=GC_RID, files=(), home=False,
               metadata="missing"):
    root = tmp_path / "wtroot"
    path = root / request_id / "worktree"
    path.mkdir(parents=True)
    for name in files:
        (path / name).write_text("entry")
    if home:
        (root / request_id / "home").mkdir()
    event = {
        "request_id": request_id,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "state": "worker_failed",
        "pid": 2_147_483_090,
        "pid_start_ticks": 999_999_940,
        "workspace_retained": True,
        "error": "",
    }
    if metadata == "missing":
        event["metadata_path"] = str(tmp_path / "processes" / f"{request_id}.request.json")
    manager._append_event(event)
    return root / request_id, path


def _husk_manager(tmp_path, monkeypatch, **status):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _B512._card()
    card.update(status)
    return _B512._build_manager(tmp_path, card), card


@pytest.mark.parametrize(
    "status,archived_at",
    [("finished", ""), ("finished", "2026-07-16T00:00:00Z")],
)
def test_disposed_card_husk_is_reclaimed(tmp_path, monkeypatch, status, archived_at):
    manager, card = _husk_manager(
        tmp_path, monkeypatch,
        status=status, worker_status="done", claimed_by="", archived_at=archived_at,
    )
    entry, path = _seed_husk(
        manager, tmp_path, card, files=("bash.exe.stackdump",), home=True
    )

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 1, "gc_skipped": 0}
    assert not entry.exists()
    latest = manager._request_events(GC_RID)[-1]
    assert latest["workspace_disposition"] == "removed"
    assert latest["workspace_retained"] is False
    assert latest["workspace_gc"] is True
    assert latest["workspace_gc_reason"] == "husk_reclaimed"
    assert latest["state"] == "worker_failed"


def test_empty_husk_without_metadata_path_is_reclaimed(tmp_path, monkeypatch):
    manager, card = _husk_manager(
        tmp_path, monkeypatch, status="finished", worker_status="done", claimed_by=""
    )
    entry, _path = _seed_husk(manager, tmp_path, card, metadata="absent")

    result = manager._gc_finalized_workspace(
        GC_RID, manager._latest_by_request()[GC_RID]
    )

    assert result == {"request_id": GC_RID, "gc": True, "reason": "husk_reclaimed"}
    assert not entry.exists()


@pytest.mark.parametrize(
    "status,worker_status",
    [("pending", "unclaimed"), ("blocked", "blocked"), ("processing", "in_progress")],
)
def test_undisposed_card_husk_is_kept(tmp_path, monkeypatch, status, worker_status):
    manager, card = _husk_manager(
        tmp_path, monkeypatch, status=status, worker_status=worker_status
    )
    entry, path = _seed_husk(manager, tmp_path, card, files=("bash.exe.stackdump",))
    events_before = len(manager._request_events(GC_RID))

    result = manager._gc_finalized_workspaces()

    assert result == {"gc_scanned": 1, "gc_cleaned": 0, "gc_skipped": 1}
    assert path.exists()
    assert len(manager._request_events(GC_RID)) == events_before
    detail = manager._gc_finalized_workspace(
        GC_RID, manager._latest_by_request()[GC_RID]
    )
    assert detail["gc"] is False
    assert detail["reason"] != "metadata_missing"


@pytest.mark.parametrize("files", [("notes.txt",), ("bash.exe.stackdump", "notes.txt")])
def test_non_husk_with_missing_metadata_stays_metadata_missing(
    tmp_path, monkeypatch, files
):
    manager, card = _husk_manager(
        tmp_path, monkeypatch, status="finished", worker_status="done", claimed_by=""
    )
    entry, path = _seed_husk(manager, tmp_path, card, files=files)

    result = manager._gc_finalized_workspace(
        GC_RID, manager._latest_by_request()[GC_RID]
    )

    assert result == {"request_id": GC_RID, "gc": False, "reason": "metadata_missing"}
    assert path.exists()


def test_husk_with_populated_home_is_not_reclaimed(tmp_path, monkeypatch):
    manager, card = _husk_manager(
        tmp_path, monkeypatch, status="finished", worker_status="done", claimed_by=""
    )
    entry, path = _seed_husk(manager, tmp_path, card, home=True)
    (entry / "home" / "state.txt").write_text("kept")

    result = manager._gc_finalized_workspace(
        GC_RID, manager._latest_by_request()[GC_RID]
    )

    assert result["gc"] is False
    assert result["reason"] == "metadata_missing"
    assert entry.exists()
