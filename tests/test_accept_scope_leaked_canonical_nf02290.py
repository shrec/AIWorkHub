"""NF-2026-02290: canonical files leaked into a retained review worktree.

A peer card accepted into canonical ``HEAD`` while this card sat in review.
The completion quality gate's sparse toolchain fill
(``quality_evidence._fill_sparse_toolchain_inputs``, NF-2026-02284) copies
tracked canonical ``scripts/``/``src/``/``tests/`` files the candidate root
lacks from ``baseline_root``; with the retained worktree as that root, the
peer's new file appears there untracked, and a peer's newer bytes for a file
the pinned base tracks appear tracked-modified; both are byte-identical to
canonical HEAD, and ``accept_review`` failed ``revalidation_failed:scope_violation:<path>``.

Accept-time scope (``enforce_scope(git_phase="review_acceptance")``) now drops
exactly that shape and nothing else: any byte difference from canonical, a
path absent from canonical HEAD, or a path the candidate may write keeps its
delta.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from aiworkhub import quality_evidence, worker_workspace
from aiworkhub.accept_scope_canonical_identity import leaked_canonical_paths

ACCEPT_PHASE = "review_acceptance"
PEER = "src/peer.py"
PEER_BYTES = b"PEER = 'accepted while this card sat in review'\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        stdin=subprocess.DEVNULL, timeout=60,
    )


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "AIWorkHub Test")
    _git(repo, "config", "core.autocrlf", "false")
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "source.py")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _commit_peer(repo: Path, relative: str = PEER, data: bytes = PEER_BYTES) -> None:
    """Another card's accepted file lands in canonical HEAD after this card's base."""

    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    _git(repo, "add", relative)
    _git(repo, "commit", "-qm", f"peer accept {relative}")


@pytest.fixture
def retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, worker_workspace.WorkerWorkspace]]:
    repo = _repo(tmp_path, monkeypatch)
    workspace = worker_workspace.create_workspace(
        repo,
        "c" * 32,
        {"allowed_writes": ["source.py", "src/mine.py"], "required_outputs": []},
        "validation",
    )
    try:
        # The candidate's recorded delta, sealed before the peer's accept.
        (workspace.path / "source.py").write_text("value = 2\n", encoding="utf-8")
        yield repo, workspace
    finally:
        worker_workspace.cleanup_workspace(workspace.repo, workspace.path, workspace.home)


def _leak(workspace: worker_workspace.WorkerWorkspace, relative: str, data: bytes) -> None:
    target = workspace.path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def test_quality_gate_fill_leak_no_longer_fails_accept_scope(retained) -> None:
    repo, workspace = retained
    _commit_peer(repo)

    # The materializing step: the gate's sparse fill writes into the retained
    # worktree whenever it is handed that worktree as the candidate root.
    fill = quality_evidence._fill_sparse_toolchain_inputs(
        workspace.path, repo, ["source.py"]
    )
    assert PEER in fill.filled
    assert (workspace.path / PEER).read_bytes() == PEER_BYTES

    # Reproduction: the leaked path is a candidate-visible delta outside scope.
    observed = worker_workspace.changed_paths(workspace)
    assert observed == ["source.py", PEER]
    with pytest.raises(worker_workspace.WorkspaceError, match=f"scope_violation:{PEER}"):
        worker_workspace.enforce_scope(workspace)

    # Fix: accept revalidation does not count canonical HEAD's own bytes.
    assert worker_workspace.enforce_scope(workspace, git_phase=ACCEPT_PHASE) == ["source.py"]


def test_outside_scope_bytes_that_differ_from_canonical_still_violate(retained) -> None:
    repo, workspace = retained
    _commit_peer(repo)
    _leak(workspace, PEER, PEER_BYTES + b"# candidate edit\n")

    with pytest.raises(worker_workspace.WorkspaceError, match=f"scope_violation:{PEER}"):
        worker_workspace.enforce_scope(workspace, git_phase=ACCEPT_PHASE)


def test_outside_scope_file_absent_from_canonical_head_still_violates(retained) -> None:
    repo, workspace = retained
    _leak(workspace, "src/stray.py", b"STRAY = 1\n")
    # Present in the canonical working tree but never committed: HEAD decides.
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src/stray.py").write_bytes(b"STRAY = 1\n")

    with pytest.raises(
        worker_workspace.WorkspaceError, match="scope_violation:src/stray.py"
    ):
        worker_workspace.enforce_scope(workspace, git_phase=ACCEPT_PHASE)


def test_path_the_candidate_may_write_keeps_its_delta(retained) -> None:
    repo, workspace = retained
    data = b"MINE = 1\n"
    _commit_peer(repo, "src/mine.py", data)
    _leak(workspace, "src/mine.py", data)

    changed = worker_workspace.enforce_scope(workspace, git_phase=ACCEPT_PHASE)

    assert changed == ["source.py", "src/mine.py"]


def test_identity_probe_names_only_byte_identical_head_copies(retained) -> None:
    repo, workspace = retained
    _commit_peer(repo)
    _leak(workspace, PEER, PEER_BYTES)
    _leak(workspace, "src/other.py", b"OTHER = 1\n")

    leaked = leaked_canonical_paths(
        repo,
        workspace.path,
        [PEER, "src/other.py", "source.py", "src/missing.py"],
        run=worker_workspace._run,
        timeout=60,
        phase=ACCEPT_PHASE,
    )

    # source.py differs from canonical HEAD, other.py is not in HEAD and
    # missing.py does not exist: none of them is a leaked canonical copy.
    assert leaked == {PEER}


SHARED = "src/shared.py"
SHARED_BASE = b"SHARED = 'base'\n"
SHARED_PEER = b"SHARED = 'newer canonical bytes from a peer accept'\n"


@pytest.fixture
def retained_tracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, worker_workspace.WorkerWorkspace]]:
    """A retained worktree whose pinned base TRACKS ``SHARED`` outside scope."""

    repo = _repo(tmp_path, monkeypatch)
    _commit_peer(repo, SHARED, SHARED_BASE)
    workspace = worker_workspace.create_workspace(
        repo,
        "d" * 32,
        {"allowed_writes": ["source.py"], "required_outputs": []},
        "validation",
    )
    try:
        (workspace.path / "source.py").write_text("value = 2\n", encoding="utf-8")
        _commit_peer(repo, SHARED, SHARED_PEER)
        yield repo, workspace
    finally:
        worker_workspace.cleanup_workspace(workspace.repo, workspace.path, workspace.home)


def _tracked_without_skip_worktree(workspace: worker_workspace.WorkerWorkspace) -> bool:
    listed = subprocess.run(
        ["git", "ls-files", "-v", "--", SHARED], cwd=workspace.path, check=True,
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
    ).stdout
    return listed.startswith("H ")


def test_tracked_base_file_overwritten_with_canonical_head_is_dropped(
    retained_tracked,
) -> None:
    _repo_path, workspace = retained_tracked
    # The measured accept shape: a tracked base file (``H``, no skip-worktree
    # bit) overwritten in place with canonical HEAD's newer bytes -> `` M``.
    _leak(workspace, SHARED, SHARED_PEER)
    assert _tracked_without_skip_worktree(workspace)

    assert worker_workspace.changed_paths(workspace) == ["source.py", SHARED]
    with pytest.raises(worker_workspace.WorkspaceError, match=f"scope_violation:{SHARED}"):
        worker_workspace.enforce_scope(workspace)

    assert worker_workspace.enforce_scope(workspace, git_phase=ACCEPT_PHASE) == ["source.py"]


def test_tracked_base_file_modified_to_non_canonical_bytes_still_violates(
    retained_tracked,
) -> None:
    _repo_path, workspace = retained_tracked
    _leak(workspace, SHARED, SHARED_PEER + b"# candidate edit\n")
    assert _tracked_without_skip_worktree(workspace)

    with pytest.raises(worker_workspace.WorkspaceError, match=f"scope_violation:{SHARED}"):
        worker_workspace.enforce_scope(workspace, git_phase=ACCEPT_PHASE)
