from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aiworkhub import process_launcher
from aiworkhub import successful_rework_recovery
from aiworkhub import worker_workspace
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


def test_validation_failed_seal_pairs_the_published_hashes_with_the_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NF-2026-01199: this branch's ONE capture feeds both published halves.

    The branch used to hash the worktree here and re-read it to seal, so a
    writer between the two reads published a ``changed_path_hashes`` the
    sealed artifact disagreed with and the successor was refused with
    ``rework_predecessor_hash_mismatch``.
    """
    workspace = _workspace(tmp_path, "failed-request-3")
    (workspace.path / "candidate.py").write_bytes(b"value = 3\n")
    monkeypatch.setenv(worker_workspace.RUNTIME_ROOT_ENV, str(tmp_path / "runtime"))
    captures: list[tuple[Path, list[str]]] = []

    def capture(root, paths):
        captures.append((Path(root), sorted(paths)))
        return [
            (relative, (Path(root) / relative).read_bytes())
            for relative in sorted(paths)
        ]

    monkeypatch.setattr(successful_rework_recovery, "capture_candidate_paths", capture)

    evidence = process_launcher._retained_candidate_seal_evidence(
        workspace,
        {
            "task_id": "FAILED_TASK_3",
            "runner": "deepseek_worker",
            "topic": "implementation",
            "claim_epoch": 4,
        },
        "failed-request-3",
        ["candidate.py"],
        "processing",
    )

    # Exactly one read of the candidate bytes backs both published halves.
    assert captures == [(workspace.path, ["candidate.py"])]
    assert evidence["changed_path_hashes"] == {
        "candidate.py": hashlib.sha256(b"value = 3\n").hexdigest()
    }
    descriptor = evidence["rework_delta"]
    assert descriptor["sealed"] is True
    assert worker_workspace.verify_rework_delta_artifact(
        {"path": descriptor["artifact_path"], "digest": descriptor["artifact_sha256"]},
        workspace.repo,
        "failed-request-3",
        "FAILED_TASK_3",
        4,
        evidence["changed_path_hashes"],
        workspace.allowed_writes,
    ) == [("candidate.py", b"value = 3\n")]
