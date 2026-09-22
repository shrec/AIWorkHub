"""NF-2026-00966: a validation-only replay must judge a mandatory output
against the canonical base its predecessor started from, not against the
replay workspace's own seeded (predecessor) bytes.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import worker_workspace

_Workspace = worker_workspace.WorkerWorkspace

REQUIRED_OUTPUT = "out/result.txt"
CANONICAL_BASE_CONTENT = b"canonical-base\n"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert (
        _git(root, "config", "user.name", "Replay Mandatory Output Tests").returncode
        == 0
    )
    (root / "out").mkdir()
    (root / REQUIRED_OUTPUT).write_bytes(CANONICAL_BASE_CONTENT)
    assert _git(root, "add", REQUIRED_OUTPUT).returncode == 0
    assert _git(root, "commit", "-qm", "baseline").returncode == 0
    return root


def _seeded_successor(
    repo: Path,
    predecessor_id: str,
    successor_id: str,
    predecessor_content: bytes,
) -> tuple[_Workspace, _Workspace, dict[str, Any]]:
    """Seed a successor exactly as a validation-only replay launch does:
    materialize one hash-pinned predecessor path into a fresh worktree."""
    predecessor = worker_workspace.create_workspace(
        repo, predecessor_id, {"allowed_writes": [REQUIRED_OUTPUT]}, "validation"
    )
    candidate = predecessor.path / REQUIRED_OUTPUT
    candidate.write_bytes(predecessor_content)
    candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
    rework_predecessor = {
        "schema_id": "aiworkhub.rework_predecessor.v1",
        "request_id": predecessor_id,
        "workspace": predecessor.as_metadata(),
        "changed_path_hashes": {REQUIRED_OUTPUT: candidate_hash},
    }
    successor = worker_workspace.create_workspace(
        repo,
        successor_id,
        {
            "allowed_writes": [REQUIRED_OUTPUT],
            "rework_predecessor": rework_predecessor,
        },
        "validation",
    )
    return predecessor, successor, rework_predecessor


def _cleanup(repo: Path, *workspaces: _Workspace | None) -> None:
    for workspace in workspaces:
        if workspace is not None:
            worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def _assert_unchanged_mismatch(excinfo: pytest.ExceptionInfo[Exception]) -> None:
    message = str(excinfo.value)
    assert message.startswith("required_output_mismatch:")
    diagnostics = json.loads(message[len("required_output_mismatch:") :])
    assert diagnostics["unchanged_mandatory_outputs"] == [REQUIRED_OUTPUT]
    expected_code = f"required_output_unchanged:{REQUIRED_OUTPUT}"
    assert expected_code in diagnostics["legacy_error_codes"]


def test_validation_only_replay_accepts_bytes_that_differ_from_canonical_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    predecessor = successor = None
    try:
        predecessor, successor, rework_predecessor = _seeded_successor(
            repo, "nf966-pred-changed", "nf966-succ-changed", b"predecessor-changed\n"
        )
        records = worker_workspace.validate_required_outputs(
            successor,
            [REQUIRED_OUTPUT],
            rework_predecessor=rework_predecessor,
            strict_rework_inheritance=True,
            validation_only_replay=True,
        )
        assert [rec["path"] for rec in records] == [REQUIRED_OUTPUT]
        assert records[0]["unchanged_allowed"] is False
    finally:
        _cleanup(repo, successor, predecessor)


def test_validation_only_replay_still_rejects_bytes_that_equal_the_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    predecessor = successor = None
    try:
        # The predecessor's manifest claims this path as "changed", but its
        # recorded bytes are byte-identical to the canonical base -- a bogus
        # or stale claim that must never be let through even in replay mode.
        predecessor, successor, rework_predecessor = _seeded_successor(
            repo, "nf966-pred-noop", "nf966-succ-noop", CANONICAL_BASE_CONTENT
        )
        with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
            worker_workspace.validate_required_outputs(
                successor,
                [REQUIRED_OUTPUT],
                rework_predecessor=rework_predecessor,
                strict_rework_inheritance=True,
                validation_only_replay=True,
            )
        _assert_unchanged_mismatch(excinfo)
    finally:
        _cleanup(repo, successor, predecessor)


def test_normal_launch_keeps_todays_behavior_and_failure_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    predecessor = successor = None
    try:
        # Same genuinely-changed bytes as the first test above, but without
        # ``validation_only_replay``: an ordinary strict finalization must
        # keep failing closed exactly as it did before that flag existed.
        predecessor, successor, rework_predecessor = _seeded_successor(
            repo, "nf966-pred-normal", "nf966-succ-normal", b"predecessor-changed\n"
        )
        with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
            worker_workspace.validate_required_outputs(
                successor,
                [REQUIRED_OUTPUT],
                rework_predecessor=rework_predecessor,
                strict_rework_inheritance=True,
            )
        _assert_unchanged_mismatch(excinfo)
    finally:
        _cleanup(repo, successor, predecessor)
