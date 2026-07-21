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
        ["git", *args], cwd=repo, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, shell=False,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b660@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B660").returncode == 0
    (repo / "read").mkdir()
    (repo / "out").mkdir()
    (repo / "read" / "manifest.json").write_text("{}\n", encoding="utf-8")
    (repo / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.safp6461\n", encoding="utf-8")
    assert _git(repo, "add", ".gitignore", "read/manifest.json", "out/result.txt").returncode == 0
    assert _git(repo, "commit", "-qm", "fixture").returncode == 0
    return repo


def test_terminal_recursive_glob_hydrates_untracked_ignored_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    shards = repo / "shards"
    (shards / "shard_0000").mkdir(parents=True)
    (shards / "shard_0001").mkdir()
    payloads = {
        "shards/shard_0000/CURRENT": b"generation-0\n",
        "shards/shard_0000/packet.safp6461": b"\x00\x01\x02\x03",
        "shards/shard_0000/packet.safp6461.sha256": b"hash-0\n",
        "shards/shard_0001/packet.safp6461": b"\x04\x05\x06\x07",
    }
    for relative, payload in payloads.items():
        (repo / relative).write_bytes(payload)
    assert "shards/shard_0000/packet.safp6461" not in _git(
        repo, "ls-files"
    ).stdout.splitlines()

    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    workspace = worker_workspace.create_workspace(
        repo,
        "b660-live-seed",
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["read/manifest.json", "shards/**"],
        },
        "validation",
    )
    try:
        for relative, payload in payloads.items():
            assert (workspace.path / relative).read_bytes() == payload
        (workspace.path / "shards/shard_0000/packet.safp6461").write_bytes(b"worker")
        assert (repo / "shards/shard_0000/packet.safp6461").read_bytes() == b"\x00\x01\x02\x03"
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.parametrize("symlink_directory", [False, True])
def test_terminal_recursive_glob_rejects_selected_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    symlink_directory: bool,
) -> None:
    repo = _repo(tmp_path)
    shards = repo / "shards"
    shards.mkdir()
    outside = repo / "outside"
    outside.mkdir()
    (outside / "payload.bin").write_bytes(b"outside")
    link = shards / ("linked_dir" if symlink_directory else "linked.bin")
    link.symlink_to(outside if symlink_directory else outside / "payload.bin")
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="symlink_(?:seed|path_component)_forbidden",
    ):
        worker_workspace.create_workspace(
            repo,
            "b660-symlink-dir" if symlink_directory else "b660-symlink-file",
            {"allowed_writes": ["out/result.txt"], "read_first": ["shards/**"]},
            "validation",
        )


def test_terminal_recursive_glob_preserves_seed_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root = tmp_path / "plain"
    shards = root / "shards"
    shards.mkdir(parents=True)
    for index in range(4):
        (shards / f"{index}.bin").write_bytes(b"x")
    monkeypatch.setattr(worker_workspace, "MAX_SEED_FILES", 3)
    with pytest.raises(worker_workspace.WorkspaceError, match="seed_file_limit_exceeded:4"):
        worker_workspace._expand_declared(root, ["shards/**"])
