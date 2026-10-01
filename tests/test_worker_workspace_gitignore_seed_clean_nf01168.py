"""NF-2026-01168: a zero-change provisioned worktree must be porcelain-clean.

Provisioning live-seeds the root ``.gitignore`` and the parent ``.gitignore`` of
every seeded path from the canonical working copy.  These cases provision a
real worktree through ``create_workspace`` and read ``git status --porcelain``
inside it, for a ``.gitignore`` whose canonical bytes equal the HEAD blob and
for one checked out with CRLF on a repository where Git normalises line
endings.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import worker_workspace  # noqa: E402

_ROOT_IGNORE = b"*.log\nbuild/\n"
_NESTED_IGNORE = b"*.cache\n"


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


def _crlf(data: bytes) -> bytes:
    return data.replace(b"\n", b"\r\n")


def _make_repo(tmp_path: Path, *, autocrlf: str) -> Path:
    root = tmp_path / "parent"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Task MCP Tests").returncode == 0
    # Pin the line-ending policy so the fixture does not inherit host config.
    assert _git(root, "config", "core.autocrlf", autocrlf).returncode == 0
    crlf = autocrlf != "false"
    (root / "read").mkdir()
    (root / "out").mkdir()
    (root / "read" / "input.txt").write_bytes(b"input-v1\n")
    (root / "out" / "result.txt").write_bytes(b"result-v1\n")
    (root / "AGENTS.md").write_bytes(b"agents-v1\n")
    root_ignore = _crlf(_ROOT_IGNORE) if crlf else _ROOT_IGNORE
    nested_ignore = _crlf(_NESTED_IGNORE) if crlf else _NESTED_IGNORE
    (root / ".gitignore").write_bytes(root_ignore)
    (root / "read" / ".gitignore").write_bytes(nested_ignore)
    assert (
        _git(
            root,
            "add",
            ".gitignore",
            "read/.gitignore",
            "read/input.txt",
            "out/result.txt",
            "AGENTS.md",
        ).returncode
        == 0
    )
    assert _git(root, "commit", "-qm", "fixture").returncode == 0
    # The canonical working copy keeps exactly the bytes written above.  With
    # core.autocrlf set Git stores the normalised LF blob in HEAD while the
    # canonical copy stays CRLF -- the shape of the measured host file.  With
    # ``input`` the worktree checkout is LF, so the raw canonical copy differs
    # in size from the checkout although Git's normalised content is equal;
    # that is the case that read " M .gitignore" before NF-2026-01168.
    assert (root / ".gitignore").read_bytes() == root_ignore
    assert _git(root, "cat-file", "-s", "HEAD:.gitignore").stdout.strip() == str(len(_ROOT_IGNORE))
    assert _git(root, "status", "--porcelain").stdout == ""
    return root


@pytest.fixture(params=["false", "true", "input"], ids=lambda v: f"autocrlf_{v}")
def repo(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    return _make_repo(tmp_path, autocrlf=request.param)


def _append_line(data: bytes, line: bytes) -> bytes:
    return data + line + (b"\r\n" if data.endswith(b"\r\n") else b"\n")


def _workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path, request_id: str
) -> worker_workspace.WorkerWorkspace:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    return worker_workspace.create_workspace(
        repo,
        request_id,
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["read/input.txt"],
        },
        "validation",
    )


def test_zero_change_provisioned_worktree_has_empty_porcelain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "req-nf01168-clean")
    try:
        status = _git(workspace.path, "status", "--porcelain")
        assert status.returncode == 0, status.stderr
        assert status.stdout == ""
        assert worker_workspace.enforce_scope(workspace) == []
        # The live seed keeps copying the canonical bytes; only the index stat
        # is refreshed.
        for relative in ("out/result.txt", "read/input.txt", ".gitignore"):
            assert (workspace.path / relative).read_bytes() == (
                repo / relative
            ).read_bytes(), relative
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_uncommitted_canonical_gitignore_edit_is_seeded_and_visible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    edited = _append_line((repo / ".gitignore").read_bytes(), b"dist/")
    (repo / ".gitignore").write_bytes(edited)
    workspace = _workspace(monkeypatch, tmp_path, repo, "req-nf01168-edit")
    try:
        assert (workspace.path / ".gitignore").read_bytes() == edited
        status = _git(workspace.path, "status", "--porcelain")
        assert status.returncode == 0, status.stderr
        assert status.stdout.splitlines() == [" M .gitignore"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_uncommitted_nested_gitignore_edit_is_seeded_and_visible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    nested = repo / "read" / ".gitignore"
    edited = _append_line(nested.read_bytes(), b"*.tmp")
    nested.write_bytes(edited)
    workspace = _workspace(monkeypatch, tmp_path, repo, "req-nf01168-nested-edit")
    try:
        assert (workspace.path / "read" / ".gitignore").read_bytes() == edited
        status = _git(workspace.path, "status", "--porcelain")
        assert status.returncode == 0, status.stderr
        assert status.stdout.splitlines() == [" M read/.gitignore"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_canonical_ignore_rules_stay_effective_in_the_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "req-nf01168-ignored")
    try:
        assert (workspace.path / ".gitignore").is_file()
        assert (workspace.path / "read" / ".gitignore").is_file()
        (workspace.path / "worker.log").write_bytes(b"log\n")
        (workspace.path / "read" / "data.cache").write_bytes(b"cache\n")
        for relative in ("worker.log", "read/data.cache"):
            assert _git(workspace.path, "check-ignore", "-q", relative).returncode == 0
        status = _git(workspace.path, "status", "--porcelain")
        assert status.returncode == 0, status.stderr
        assert status.stdout == ""
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_nested_gitignore_next_to_a_seeded_path_is_clean_like_the_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "req-nf01168-nested")
    try:
        nested = workspace.path / "read" / ".gitignore"
        assert nested.is_file()
        status = _git(
            workspace.path,
            "status",
            "--porcelain",
            "--",
            ".gitignore",
            "read/.gitignore",
        )
        assert status.returncode == 0, status.stderr
        assert status.stdout == ""
        assert _git(workspace.path, "diff", "--quiet", "HEAD", "--").returncode == 0
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_seed_restat_runs_a_bounded_number_of_git_processes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _make_repo(tmp_path, autocrlf="true")
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    real_run = worker_workspace._run
    calls: list[tuple[str, ...]] = []

    def recording_run(argv, **kwargs):
        calls.append(tuple(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(worker_workspace, "_run", recording_run)
    seeded = ("read/input.txt", "AGENTS.md", ".gitignore", "read/.gitignore")
    workspace = worker_workspace.create_workspace(
        repo,
        "req-nf01168-batched",
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["read/input.txt", "AGENTS.md"],
        },
        "validation",
    )
    try:
        subcommands = [call[1] for call in calls if len(call) > 1]
        for subcommand in ("ls-files", "hash-object", "update-index"):
            assert subcommands.count(subcommand) == 1, subcommands
        for relative in seeded:
            assert (workspace.path / relative).read_bytes() == (
                repo / relative
            ).read_bytes(), relative
        status = _git(workspace.path, "status", "--porcelain")
        assert status.returncode == 0, status.stderr
        assert status.stdout == ""
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


_GEORGIAN = "ქართული.txt"


def test_seed_restat_survives_an_undecodable_tracked_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _make_repo(tmp_path, autocrlf="true")
    (repo / _GEORGIAN).write_bytes(b"georgian\n")
    assert _git(repo, "add", "--", _GEORGIAN).returncode == 0
    assert _git(repo, "commit", "-qm", "georgian").returncode == 0
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    real_run = worker_workspace._run
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def recording_run(argv, **kwargs):
        calls.append((tuple(argv), dict(kwargs)))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(worker_workspace, "_run", recording_run)
    workspace = _workspace(monkeypatch, tmp_path, repo, "req-nf01168-georgian")
    try:
        listed = [
            kwargs
            for argv, kwargs in calls
            if len(argv) > 1 and argv[1] == "ls-files" and "-s" in argv
        ]
        assert listed, calls
        assert all(kwargs["text"] is False for kwargs in listed)
        status = _git(
            workspace.path,
            "status",
            "--porcelain",
            "--",
            ".gitignore",
            "read/.gitignore",
        )
        assert status.returncode == 0, status.stderr
        assert status.stdout == ""
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_seed_restat_skips_a_non_ascii_seeded_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / _GEORGIAN).write_bytes(b"georgian\n")
    calls: list[tuple[str, ...]] = []

    def failing_run(argv, **kwargs):
        calls.append(tuple(argv))
        pytest.fail(f"unexpected git call {argv!r}")

    monkeypatch.setattr(worker_workspace, "_run", failing_run)
    worker_workspace._restat_seeds_equal_to_index_blobs(tmp_path, [_GEORGIAN])
    assert calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bit")
def test_seed_restat_never_stages_a_mode_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _make_repo(tmp_path, autocrlf="false")
    assert _git(repo, "config", "core.filemode", "true").returncode == 0
    (repo / "read" / "input.txt").chmod(0o755)
    workspace = _workspace(monkeypatch, tmp_path, repo, "req-nf01168-filemode")
    try:
        assert _git(workspace.path, "diff", "--cached", "--quiet").returncode == 0
        staged = _git(workspace.path, "ls-files", "-s", "--", "read/input.txt")
        assert staged.stdout.startswith("100644"), staged.stdout
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)
