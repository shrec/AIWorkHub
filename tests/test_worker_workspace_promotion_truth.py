"""Promotion-truth tests for :mod:`aiworkhub.worker_workspace`.

These tests pin the four scope/promotion-truth guarantees that keep a worker's
declared output honest against the parent repository:

1. ``changed_paths`` diffs against the worktree HEAD OID pinned at
   ``create_workspace`` -- not the live symbolic ``HEAD`` -- so a worker that
   commits inside its own detached worktree cannot make its work invisible.
2. A staged rename records both the deleted source and the added destination.
3. A single ``*`` in ``allowed_writes`` never crosses a path separator, while an
   explicit ``**`` still does.
4. ``create_workspace`` leaves no worktree behind when ``git worktree add``
   raises.

They also assert that a run which legitimately changes nothing still passes
through cleanly, so the base-OID pin does not manufacture false positives.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import worker_workspace  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "parent"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Promotion Truth Tests").returncode == 0
    (root / "read").mkdir()
    (root / "out").mkdir()
    (root / "src").mkdir()
    (root / "read" / "input.txt").write_bytes(b"input-v1\n")
    (root / "out" / "result.txt").write_bytes(b"result-v1\n")
    (root / "src" / "a.py").write_bytes(b"a-v1\n")
    assert _git(
        root, "add", "read/input.txt", "out/result.txt", "src/a.py"
    ).returncode == 0
    assert _git(root, "commit", "-qm", "fixture").returncode == 0
    return root


def _make(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
    request_id: str,
    allowed: list[str],
    read_first: list[str] | None = None,
) -> worker_workspace.WorkerWorkspace:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    return worker_workspace.create_workspace(
        repo,
        request_id,
        {"allowed_writes": allowed, "read_first": read_first or []},
        "validation",
    )


# --- Fix 1: base-OID pin ----------------------------------------------------


def test_changed_paths_uses_pinned_base_oid_after_worker_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    ws = _make(
        monkeypatch, tmp_path, repo, "commit-visible", ["out/result.txt"],
        ["read/input.txt"],
    )
    try:
        (ws.path / "out" / "result.txt").write_bytes(b"result-v2\n")
        # The worker commits inside its own detached worktree, moving HEAD.
        assert _git(ws.path, "add", "out/result.txt").returncode == 0
        assert _git(ws.path, "commit", "-qm", "worker commit").returncode == 0
        assert _git(ws.path, "rev-parse", "HEAD").stdout.strip() != ws.base_oid

        assert worker_workspace.changed_paths(ws) == ["out/result.txt"]
        assert worker_workspace.enforce_scope(ws) == ["out/result.txt"]
    finally:
        worker_workspace.cleanup_workspace(ws.repo, ws.path, ws.home)


def test_moved_head_the_pinned_oid_cannot_explain_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    # Give the parent two commits so HEAD can be rewound below its pinned base.
    (repo / "out" / "result.txt").write_bytes(b"result-v2\n")
    assert _git(repo, "add", "out/result.txt").returncode == 0
    assert _git(repo, "commit", "-qm", "second").returncode == 0
    parent = _git(repo, "rev-parse", "HEAD~1").stdout.strip()

    ws = _make(monkeypatch, tmp_path, repo, "rewound-head", ["out/result.txt"])
    try:
        # Rewind the worktree HEAD to a commit that the pinned base is NOT an
        # ancestor of -- a move the pin cannot explain.
        assert _git(ws.path, "reset", "--hard", parent).returncode == 0
        assert _git(ws.path, "rev-parse", "HEAD").stdout.strip() == parent
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="worktree_head_moved_unexplained",
        ):
            worker_workspace.changed_paths(ws)
    finally:
        worker_workspace.cleanup_workspace(ws.repo, ws.path, ws.home)


def test_committed_required_output_is_carried_by_promote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    # A new output created under a glob allowed-write and then committed inside
    # the worktree: before the pin, validate_required_outputs would pass on the
    # on-disk file while promote (diffing symbolic HEAD) carried nothing.
    ws = _make(monkeypatch, tmp_path, repo, "commit-newfile", ["out/*.txt"])
    try:
        (ws.path / "out" / "extra.txt").write_bytes(b"extra\n")
        assert _git(ws.path, "add", "out/extra.txt").returncode == 0
        assert _git(ws.path, "commit", "-qm", "add extra").returncode == 0

        changed = worker_workspace.changed_paths(ws)
        assert "out/extra.txt" in changed
        records = worker_workspace.validate_required_outputs(ws, ["out/extra.txt"])
        assert [r["path"] for r in records] == ["out/extra.txt"]
        promoted = worker_workspace.promote(ws, changed)
        assert "out/extra.txt" in promoted
        assert (repo / "out" / "extra.txt").read_bytes() == b"extra\n"
    finally:
        worker_workspace.cleanup_workspace(ws.repo, ws.path, ws.home)


# --- Fix 2: staged rename records both sides --------------------------------


def test_staged_rename_records_both_sides_and_is_scope_checked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    ws = _make(
        monkeypatch,
        tmp_path,
        repo,
        "staged-rename",
        ["src/b.py"],
        ["src/a.py"],
    )
    try:
        assert _git(ws.path, "mv", "-f", "src/a.py", "src/b.py").returncode == 0

        assert worker_workspace.changed_paths(ws) == ["src/a.py", "src/b.py"]
        # src/a.py's deletion is out of scope for allowed_writes=("src/b.py",).
        with pytest.raises(
            worker_workspace.WorkspaceError, match="scope_violation:src/a.py"
        ):
            worker_workspace.enforce_scope(ws)
    finally:
        worker_workspace.cleanup_workspace(ws.repo, ws.path, ws.home)


# --- Fix 3: single '*' stays inside one path segment ------------------------


def test_single_asterisk_does_not_cross_a_path_separator() -> None:
    assert worker_workspace._matches("docs/notes.md", ["docs/*.md"]) is True
    assert worker_workspace._matches("docs/private/secret.md", ["docs/*.md"]) is False
    assert worker_workspace._matches("a/b.py", ["*"]) is False
    assert worker_workspace._matches("top.py", ["*"]) is True


def test_explicit_recursive_pattern_still_crosses_separators() -> None:
    assert worker_workspace._matches("docs/private/secret.md", ["docs/**"]) is True
    assert worker_workspace._matches("docs/a/b/c.md", ["docs/**"]) is True
    assert worker_workspace._matches("docs/x.md", ["docs/**/x.md"]) is True
    assert worker_workspace._matches("docs/a/b/x.md", ["docs/**/x.md"]) is True


# --- Fix 4: no leaked worktree when git worktree add raises ------------------


def test_create_workspace_leaves_no_worktree_when_worktree_add_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    real_run = worker_workspace._run

    def raising_run(argv, **kwargs):
        if argv[:3] == ["git", "worktree", "add"]:
            # Actually create the worktree + registration, then raise as a
            # timeout would after git has already written partial state.
            real_run(argv, **kwargs)
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(worker_workspace, "_run", raising_run)
    with pytest.raises(subprocess.TimeoutExpired):
        worker_workspace.create_workspace(
            repo, "leaky", {"allowed_writes": ["out/result.txt"]}, "validation"
        )
    monkeypatch.undo()

    leaked = tmp_path / "worktrees" / "leaky"
    assert not leaked.exists()
    registered = _git(repo, "worktree", "list", "--porcelain").stdout
    assert "leaky" not in registered


# --- Zero-change lane: the pin manufactures no false positives ---------------


def test_zero_change_run_reports_nothing_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    ws = _make(
        monkeypatch, tmp_path, repo, "no-change", ["out/result.txt"],
        ["read/input.txt"],
    )
    try:
        # HEAD is unmoved and no file was touched: the pinned base equals the
        # live HEAD, so the result is identical to the pre-pin behaviour.
        assert _git(ws.path, "rev-parse", "HEAD").stdout.strip() == ws.base_oid
        assert worker_workspace.changed_paths(ws) == []
        assert worker_workspace.enforce_scope(ws) == []
        assert worker_workspace.promote(ws, []) == []
    finally:
        worker_workspace.cleanup_workspace(ws.repo, ws.path, ws.home)
