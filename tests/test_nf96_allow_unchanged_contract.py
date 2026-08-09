"""NF-2026-00096 immutable-fixture contract tests for allow_unchanged_required_outputs.

Acceptance criteria (verbatim from the task contract):
1. A changed required output listed in allow_unchanged_required_outputs passes as
   an ordinary changed output in both canonical implementations.
2. An unchanged listed output passes only with exact parent and workspace
   baseline identity, regular non-symlink file, and non-empty content.
3. An unchanged unlisted output still fails required_output_unchanged.
4. Replay authorization, parent mismatch, symlink, empty-output, and promotion
   controls remain fail-closed.
5. Accepted NF98 fd-safe atomic _copy_one behavior remains intact and its
   TestCopyOne suite passes.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import worker_workspace  # noqa: E402

# ---------------------------------------------------------------------------
# Immutable fixtures
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402

_FIXTURE_CONTENT_V1 = b"result-v1\n"
_FIXTURE_CONTENT_V2 = b"result-v2\n"
_FIXTURE_HASH_V1 = hashlib.sha256(_FIXTURE_CONTENT_V1).hexdigest()
_FIXTURE_HASH_V2 = hashlib.sha256(_FIXTURE_CONTENT_V2).hexdigest()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Immutable git repo fixture for contract tests."""
    root = tmp_path / "parent"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "NF96 Contract Test"], cwd=root, check=True
    )
    (root / "read").mkdir()
    (root / "out").mkdir()
    (root / "read" / "input.txt").write_text("input-v1\n", encoding="utf-8")
    (root / "out" / "result.txt").write_bytes(_FIXTURE_CONTENT_V1)
    subprocess.run(
        ["git", "add", "read/input.txt", "out/result.txt"], cwd=root, check=True
    )
    subprocess.run(["git", "commit", "-qm", "contract-fixture"], cwd=root, check=True)
    return root


class WorkspaceFixtures:
    """Pre-built workspace with stable baselines for deterministic assertions."""

    @staticmethod
    def build(
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        repo: Path,
        request: str,
    ) -> worker_workspace.WorkerWorkspace:
        monkeypatch.setenv(
            worker_workspace.WORKTREE_ROOT_ENV,
            str(tmp_path / "worktrees"),
        )
        return worker_workspace.create_workspace(
            repo,
            request,
            {
                "allowed_writes": ["out/result.txt", "out/extra.txt"],
                "read_first": ["read/input.txt"],
            },
            "validation",
        )


# ---------------------------------------------------------------------------
# Acceptance Criterion 1 — changed listed output passes as ordinary changed
# ---------------------------------------------------------------------------


def test_acceptance_1_changed_listed_output_passes_as_ordinary_changed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    """A changed required output listed in allow_unchanged_required_outputs
    must pass as an ordinary changed output — no rejection, and the record
    shows unchanged_allowed=False so it enters the promotable set."""
    workspace = WorkspaceFixtures.build(monkeypatch, tmp_path, repo, "ac1-changed-listed")
    try:
        # Write content that differs from the parent baseline (result-v1).
        (workspace.path / "out" / "result.txt").write_bytes(_FIXTURE_CONTENT_V2)
        records = worker_workspace.validate_required_outputs(
            workspace,
            ["out/result.txt"],
            allow_unchanged=("out/result.txt",),
        )
        assert len(records) == 1
        rec = records[0]
        assert rec["path"] == "out/result.txt"
        assert rec["sha256"] == worker_workspace._hash_path(workspace.path / "out" / "result.txt")
        assert rec["unchanged_allowed"] is False

        # It must appear in the promotable set (ordinary changed output).
        changed = worker_workspace.enforce_scope(workspace)
        promotable = sorted(
            set(changed) | {r["path"] for r in records if not r["unchanged_allowed"]}
        )
        assert "out/result.txt" in promotable
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# ---------------------------------------------------------------------------
# Acceptance Criterion 2 — unchanged listed output passes with all guards
# ---------------------------------------------------------------------------


def test_acceptance_2a_unchanged_listed_exact_baseline_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    """Unchanged listed output with exact parent and workspace baseline match,
    regular file, non-empty — must pass."""
    workspace = WorkspaceFixtures.build(monkeypatch, tmp_path, repo, "ac2a-unchanged-ok")
    try:
        records = worker_workspace.validate_required_outputs(
            workspace,
            ["out/result.txt"],
            allow_unchanged=("out/result.txt",),
        )
        assert len(records) == 1
        rec = records[0]
        assert rec["path"] == "out/result.txt"
        assert rec["sha256"] == worker_workspace._hash_path(workspace.path / "out" / "result.txt")
        assert rec["unchanged_allowed"] is True
        assert rec["bytes"] == len(_FIXTURE_CONTENT_V1)

        changed = worker_workspace.enforce_scope(workspace)
        promotable = sorted(
            set(changed) | {r["path"] for r in records if not r["unchanged_allowed"]}
        )
        assert "out/result.txt" not in promotable
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_acceptance_2b_parent_mismatch_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    """Unchanged listed output whose hash differs from parent baseline must be
    rejected — fail-closed."""
    workspace = WorkspaceFixtures.build(monkeypatch, tmp_path, repo, "ac2b-parent-mismatch")
    try:
        # Poison the workspace baseline to match the on-disk file but diverge
        # from the parent baseline.
        workspace.parent_baseline["out/result.txt"] = _FIXTURE_HASH_V2
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="required_output_unchanged_parent_mismatch",
        ):
            worker_workspace.validate_required_outputs(
                workspace,
                ["out/result.txt"],
                allow_unchanged=("out/result.txt",),
            )
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_acceptance_2c_empty_output_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    """Unchanged listed output with zero bytes must be rejected."""
    workspace = WorkspaceFixtures.build(monkeypatch, tmp_path, repo, "ac2c-empty")
    try:
        target = workspace.path / "out" / "result.txt"
        target.write_bytes(b"")
        # Adjust both baselines so the file appears "unchanged" to the
        # is_unchanged check.
        empty_hash = hashlib.sha256(b"").hexdigest()
        workspace.workspace_baseline["out/result.txt"] = empty_hash
        workspace.parent_baseline["out/result.txt"] = empty_hash
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="required_output_zero_bytes",
        ):
            worker_workspace.validate_required_outputs(
                workspace,
                ["out/result.txt"],
                allow_unchanged=("out/result.txt",),
            )
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_acceptance_2d_symlink_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    """Unchanged listed symlink output must be rejected — fail-closed."""
    if not hasattr(os, "symlink"):
        pytest.skip("os.symlink not available")
    workspace = WorkspaceFixtures.build(monkeypatch, tmp_path, repo, "ac2d-symlink")
    try:
        target = workspace.path / "out" / "result.txt"
        target.unlink()
        real = workspace.path / "out" / "real.txt"
        real.write_bytes(_FIXTURE_CONTENT_V1)
        os.symlink(real, target)
        # Make the symlink appear as unchanged.
        workspace.workspace_baseline["out/result.txt"] = _FIXTURE_HASH_V1
        workspace.parent_baseline["out/result.txt"] = _FIXTURE_HASH_V1
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="symlink_path_component_forbidden",
        ):
            worker_workspace.validate_required_outputs(
                workspace,
                ["out/result.txt"],
                allow_unchanged=("out/result.txt",),
            )
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# ---------------------------------------------------------------------------
# Acceptance Criterion 3 — unchanged unlisted output still fails
# ---------------------------------------------------------------------------


def test_acceptance_3_unchanged_unlisted_still_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    """An unchanged required output *not* in allow_unchanged_required_outputs
    must still raise required_output_unchanged."""
    workspace = WorkspaceFixtures.build(monkeypatch, tmp_path, repo, "ac3-unchanged-unlisted")
    try:
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="required_output_unchanged",
        ):
            worker_workspace.validate_required_outputs(
                workspace,
                ["out/result.txt"],
                # No allow_unchanged at all.
            )
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# ---------------------------------------------------------------------------
# Acceptance Criterion 4 — promotion controls remain fail-closed
# ---------------------------------------------------------------------------


def test_acceptance_4a_unchanged_listed_not_promoted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    """Unchanged listed output must not appear in the promotable set — even
    after enforce_scope sees no scope violations."""
    workspace = WorkspaceFixtures.build(monkeypatch, tmp_path, repo, "ac4a-no-promote")
    try:
        records = worker_workspace.validate_required_outputs(
            workspace,
            ["out/result.txt"],
            allow_unchanged=("out/result.txt",),
        )
        changed = worker_workspace.enforce_scope(workspace)
        promotable = sorted(
            set(changed) | {r["path"] for r in records if not r["unchanged_allowed"]}
        )
        assert "out/result.txt" not in promotable
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_acceptance_4b_changed_listed_is_promoted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    """Changed listed output appears in promotable set and promotion succeeds."""
    workspace = WorkspaceFixtures.build(monkeypatch, tmp_path, repo, "ac4b-promoted")
    try:
        (workspace.path / "out" / "result.txt").write_bytes(_FIXTURE_CONTENT_V2)
        records = worker_workspace.validate_required_outputs(
            workspace,
            ["out/result.txt"],
            allow_unchanged=("out/result.txt",),
        )
        changed = worker_workspace.enforce_scope(workspace)
        promotable = sorted(
            set(changed) | {r["path"] for r in records if not r["unchanged_allowed"]}
        )
        assert promotable == ["out/result.txt"]
        worker_workspace.promote(workspace, promotable)
        assert (repo / "out" / "result.txt").read_bytes() == _FIXTURE_CONTENT_V2
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# ---------------------------------------------------------------------------
# Acceptance Criterion 5 — NF98 _copy_one fd-safe atomic behavior preserved
# ---------------------------------------------------------------------------


def test_acceptance_5a_copy_one_fd_atomic_bytes(
    tmp_path: Path,
) -> None:
    """_copy_one preserves exact byte content."""
    src = tmp_path / "src"
    src.write_bytes(b"nf96-contract-bytes")
    dst = tmp_path / "dst"
    worker_workspace._copy_one(src, dst)
    assert dst.read_bytes() == b"nf96-contract-bytes"


def test_acceptance_5b_copy_one_hardlink_safety(
    tmp_path: Path,
) -> None:
    """_copy_one does not follow or overwrite through hardlinks."""
    if not hasattr(os, "link"):
        pytest.skip("os.link not available")
    src = tmp_path / "src"
    src.write_bytes(b"nf96-original")
    dst = tmp_path / "dst"
    dst.write_bytes(b"nf96-preexisting")
    link = tmp_path / "link"
    os.link(dst, link)
    worker_workspace._copy_one(src, dst)
    assert dst.read_bytes() == b"nf96-original"
    assert link.read_bytes() == b"nf96-preexisting"


def test_acceptance_5c_copy_one_symlink_source_fails(
    tmp_path: Path,
) -> None:
    """_copy_one rejects symlink sources (fail-closed)."""
    if not hasattr(os, "symlink"):
        pytest.skip("os.symlink not available")
    src = tmp_path / "real"
    src.write_bytes(b"x")
    sym = tmp_path / "sym"
    os.symlink(src, sym)
    with pytest.raises(worker_workspace.WorkspaceError, match="symlink_seed_forbidden"):
        worker_workspace._copy_one(sym, tmp_path / "dst")


def test_acceptance_5d_copy_one_executable_mode(
    tmp_path: Path,
) -> None:
    """_copy_one preserves executable mode bits."""
    src = tmp_path / "src"
    src.write_bytes(b"#!/bin/sh\necho nf96\n")
    src.chmod(0o755)
    dst = tmp_path / "dst"
    worker_workspace._copy_one(src, dst)
    assert stat.S_IMODE(os.stat(dst).st_mode) == 0o755
