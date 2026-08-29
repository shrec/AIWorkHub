from __future__ import annotations

import hashlib
from pathlib import Path

from aiworkhub import process_launcher
from aiworkhub.worker_workspace import WorkerWorkspace


def _workspace(tmp_path: Path, request_id: str) -> WorkerWorkspace:
    repo = tmp_path / f"repo-{request_id}"
    worktree = tmp_path / f"worktree-{request_id}"
    home = tmp_path / f"home-{request_id}"
    repo.mkdir()
    worktree.mkdir()
    home.mkdir()
    return WorkerWorkspace(
        request_id=request_id,
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=("candidate.py",),
        parent_baseline={},
        workspace_baseline={},
    )


def test_validation_failed_candidate_carries_pin_capable_request_identity(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, "failed-request-1")
    (workspace.path / "candidate.py").write_bytes(b"value = 2\n")

    evidence = process_launcher._retained_candidate_identity_evidence(
        workspace,
        {
            "task_id": "FAILED_TASK_1",
            "runner": "deepseek_worker",
            "topic": "implementation",
        },
        "failed-request-1",
        ["candidate.py"],
        "processing",
    )

    assert evidence["changed_path_hashes"] == {
        "candidate.py": hashlib.sha256(b"value = 2\n").hexdigest()
    }
    workspace_metadata = dict(evidence["workspace"])
    nested_candidate_authority = workspace_metadata.pop("python_candidate_authority")
    assert workspace_metadata == workspace.as_metadata()
    assert nested_candidate_authority == evidence["python_candidate_authority"]
    assert evidence["python_candidate_authority"]["sources"] == [
        {
            "path": "candidate.py",
            "state": "added",
            "bytes_sha256": evidence["changed_path_hashes"]["candidate.py"],
        },
    ]
    assert evidence["request_identity"] == {
        "request_id": "failed-request-1",
        "task_id": "FAILED_TASK_1",
        "runner": "deepseek_worker",
        "topic": "implementation",
        "repo": str(workspace.repo),
        "claim_epoch": None,
        "allowed_writes": list(workspace.allowed_writes),
        "base_oid": workspace.base_oid,
        "parent_baseline": workspace.parent_baseline,
    }
    assert evidence["claim_state"] == "processing"


def test_empty_failed_candidate_is_not_claimed_as_rework_authority(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, "failed-request-2")

    assert process_launcher._retained_candidate_identity_evidence(
        workspace,
        {"task_id": "FAILED_TASK_2", "runner": "worker", "topic": "audit"},
        "failed-request-2",
        [],
        "processing",
    ) == {}
