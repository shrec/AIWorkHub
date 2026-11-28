"""NF-2026-01024: a retained candidate that deletes a file stays recoverable.

``changed_path_hashes`` records a deleted path as ``None``; the Python
candidate authority records it as a ``deleted`` row without bytes.  Both
sides must agree on ``path -> None`` and nothing else is loosened.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import task_store

TASK_ID = "BLOCKED_RETAINED_DELETES_PATH"
REQUEST_ID = "c" * 32
MODIFIED = "src/aiworkhub/task_store.py"
DELETED = "src/aiworkhub/retired_module.py"
FEEDBACK = "NeedFix: replay retained candidate that deletes a path"


def _insert_processing_task(repo: Path, allowed_writes: list[str]) -> None:
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-11-28T00:00:00+00:00"
    card = {
        "task_id": TASK_ID,
        "runner": "codex_worker_test",
        "topic": "aiworkhub_blocked_rework_recovery",
        "allowed_writes": allowed_writes,
        "required_outputs": [],
        "claim_epoch": 1,
        "launch_request_id": REQUEST_ID,
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, "
            "started_at) VALUES (?, ?, ?, 'processing', 'in_progress', '', '', ?, ?, ?, ?, ?, ?)",
            (TASK_ID, "codex_worker_test", "aiworkhub_blocked_rework_recovery",
             json.dumps(card), now, now, "codex_worker_test", now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _fixture(
    tmp_path: Path,
    *,
    authority_hash_override: str | None = None,
    list_deleted_in_authority: bool = True,
) -> tuple[Path, dict[str, str | None]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    workspace = repo / ".aiworkhub" / "runtime" / "worktrees" / REQUEST_ID / "worktree"
    baseline_tokens: dict[str, str] = {}
    for path, data in ((MODIFIED, b"baseline\n"), (DELETED, b"retired\n")):
        baseline = repo / path
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_bytes(data)
        baseline_tokens[path] = "file:664:" + hashlib.sha256(data).hexdigest()
    candidate = workspace / MODIFIED
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"retained candidate\n")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    assert not (workspace / DELETED).exists()
    sources: list[dict[str, str]] = [
        {
            "path": MODIFIED,
            "state": "modified",
            "bytes_sha256": authority_hash_override or digest,
        }
    ]
    if list_deleted_in_authority:
        sources.append({"path": DELETED, "state": "deleted"})
    authority = {
        "schema_id": "aiworkhub.python_candidate_authority.v1",
        "sources": sources,
    }
    allowed_writes = [MODIFIED, DELETED]
    workspace_metadata = {
        "request_id": REQUEST_ID,
        "repo": str(repo),
        "path": str(workspace),
        "allowed_writes": list(allowed_writes),
        "parent_baseline": dict(baseline_tokens),
        "base_oid": "b" * 40,
        "python_candidate_authority": authority,
    }
    changed_hashes: dict[str, str | None] = {MODIFIED: digest, DELETED: None}
    evidence = {
        "required_outputs": [],
        "changed_paths": [MODIFIED, DELETED],
        "changed_path_hashes": dict(changed_hashes),
        "request_identity": {
            "request_id": REQUEST_ID,
            "task_id": TASK_ID,
            "runner": "codex_worker_test",
            "topic": "aiworkhub_blocked_rework_recovery",
            "repo": str(repo),
            "claim_epoch": 1,
            "allowed_writes": list(allowed_writes),
            "parent_baseline": dict(baseline_tokens),
            "base_oid": "b" * 40,
        },
        "workspace": workspace_metadata,
        "python_candidate_authority": authority,
    }
    _insert_processing_task(repo, allowed_writes)
    assert task_store.mark_terminal_failure(
        repo,
        TASK_ID,
        runner="codex_worker_test",
        substatus="finalize_failed",
        evidence=evidence,
        request_id=REQUEST_ID,
        claim_epoch=1,
    ) == (True, "blocked")
    return repo, changed_hashes


def _card(repo: Path) -> dict:
    card = task_store.get_task(repo, TASK_ID)
    assert card is not None
    return card


@pytest.mark.parametrize("validation_only_replay", [True, False])
def test_retained_candidate_with_deleted_path_recovers(
    tmp_path: Path, validation_only_replay: bool,
) -> None:
    repo, hashes = _fixture(tmp_path)
    assert task_store.recover_blocked_rework(
        repo,
        TASK_ID,
        actor="coordinator",
        feedback_reason=FEEDBACK,
        validation_only_replay=validation_only_replay,
    ) == (True, "recovered")
    card = _card(repo)
    assert card["rework_predecessor"]["request_id"] == REQUEST_ID
    assert card["rework_predecessor"]["changed_path_hashes"] == hashes
    assert card["recovery_feedback"] == FEEDBACK
    if validation_only_replay:
        assert card["validation_only_replay_authorization"]["next_claim_epoch"] == 2
    else:
        assert "validation_only_replay_authorization" not in card


def test_retained_candidate_modified_hash_mismatch_still_refuses(tmp_path: Path) -> None:
    repo, _hashes = _fixture(tmp_path, authority_hash_override="d" * 64)
    assert task_store.recover_blocked_rework(
        repo, TASK_ID, actor="coordinator", feedback_reason=FEEDBACK,
    ) == (False, "retained_terminal_candidate_identity_invalid:authority_hashes")
    assert _card(repo)["status"] == "blocked"


def test_retained_candidate_unlisted_deleted_path_still_refuses(tmp_path: Path) -> None:
    repo, _hashes = _fixture(tmp_path, list_deleted_in_authority=False)
    assert task_store.recover_blocked_rework(
        repo, TASK_ID, actor="coordinator", feedback_reason=FEEDBACK,
    ) == (False, "retained_terminal_candidate_identity_invalid:authority_hashes")
    assert _card(repo)["status"] == "blocked"


def test_retained_candidate_deleted_path_present_in_worktree_refuses(
    tmp_path: Path,
) -> None:
    repo, _hashes = _fixture(tmp_path)
    resurrected = (
        repo / ".aiworkhub" / "runtime" / "worktrees" / REQUEST_ID / "worktree" / DELETED
    )
    resurrected.write_bytes(b"not deleted after all\n")
    assert task_store.recover_blocked_rework(
        repo, TASK_ID, actor="coordinator", feedback_reason=FEEDBACK,
    ) == (False, "retained_terminal_candidate_bytes_invalid")
    assert _card(repo)["status"] == "blocked"


def test_retained_candidate_link_above_deleted_path_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _hashes = _fixture(tmp_path)
    workspace = repo / ".aiworkhub" / "runtime" / "worktrees" / REQUEST_ID / "worktree"
    deleted_parent = workspace.resolve().joinpath(*DELETED.split("/")).parent
    original = task_store._workspace_has_link_component
    probed: list[Path] = []

    def _link_above_deleted(root: Path, target: Path) -> bool:
        # Simulates a junction/symlink component above the deleted path only;
        # every other link check keeps its real answer.
        if Path(target) == deleted_parent:
            probed.append(Path(target))
            return True
        return original(root, target)

    monkeypatch.setattr(
        task_store, "_workspace_has_link_component", _link_above_deleted
    )
    assert task_store.recover_blocked_rework(
        repo, TASK_ID, actor="coordinator", feedback_reason=FEEDBACK,
    ) == (False, "retained_terminal_candidate_bytes_invalid")
    assert probed == [deleted_parent]
    assert _card(repo)["status"] == "blocked"
