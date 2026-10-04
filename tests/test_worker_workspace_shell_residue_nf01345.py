"""NF-2026-01345: shell residue is environment noise, not candidate output.

A worker's bash ``> $null`` / ``> nul`` leaves an empty file of that name and a
crashing ``bash.exe`` drops ``*.stackdump``.  Before the fix those untracked
files entered ``changed_paths`` and ended a correct run as ``scope_rejected``
(``scope_violation:$null``).  They must now be dropped from the candidate delta
-- so they are neither scope-checked nor promoted -- while real bytes under the
same names, explicitly allowed outputs and base-tracked paths keep their delta.
"""

from __future__ import annotations

import dataclasses
import os
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
        ["git", *args], cwd=repo, text=True, capture_output=True, check=False
    )


def _raw(path: Path) -> str:
    # Windows treats ``nul`` as a device unless the path bypasses Win32 parsing.
    if os.name == "nt":
        return "\\\\?\\" + str(path.parent.resolve() / path.name)
    return str(path)


def _write(path: Path, data: bytes) -> None:
    with open(_raw(path), "wb") as handle:
        handle.write(data)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "parent"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Shell Residue Tests").returncode == 0
    (root / "out").mkdir()
    (root / "out" / "result.txt").write_bytes(b"result-v1\n")
    (root / "kept.stackdump").write_bytes(b"tracked-v1\n")
    assert _git(root, "add", "out/result.txt", "kept.stackdump").returncode == 0
    assert _git(root, "commit", "-qm", "fixture").returncode == 0
    return root


@pytest.fixture
def make(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    created: list[worker_workspace.WorkerWorkspace] = []

    def _make(request_id: str, allowed: list[str]) -> worker_workspace.WorkerWorkspace:
        ws = worker_workspace.create_workspace(
            repo, request_id, {"allowed_writes": allowed, "read_first": []}, "validation"
        )
        created.append(ws)
        return ws

    yield _make
    for ws in created:
        nul = ws.path / "out" / "nul"
        if os.path.lexists(_raw(nul)):
            os.remove(_raw(nul))
        worker_workspace.cleanup_workspace(ws.repo, ws.path, ws.home)


def test_shell_residue_is_neither_scope_checked_nor_promoted(make, repo: Path) -> None:
    ws = make("residue-ignored", ["out/result.txt"])
    (ws.path / "out" / "result.txt").write_bytes(b"result-v2\n")
    _write(ws.path / "$null", b"")
    _write(ws.path / "out" / "nul", b"")
    _write(ws.path / "bash.exe.stackdump", b"Stack trace:\nFrame  Function\n")

    assert worker_workspace.changed_paths(ws) == ["out/result.txt"]
    changed = worker_workspace.enforce_scope(ws)
    assert changed == ["out/result.txt"]
    assert worker_workspace.promote(ws, changed) == ["out/result.txt"]
    assert not (repo / "$null").exists()
    assert not (repo / "bash.exe.stackdump").exists()


def test_committed_shell_residue_is_still_not_candidate_delta(make) -> None:
    ws = make("residue-committed", ["out/result.txt"])
    _write(ws.path / "$null", b"")
    assert _git(ws.path, "add", "--sparse", "--", "$null").returncode == 0
    assert _git(ws.path, "commit", "-qm", "worker committed residue").returncode == 0

    assert worker_workspace.enforce_scope(ws) == []


def test_non_empty_null_sink_still_fails_scope(make) -> None:
    ws = make("residue-bytes", ["out/result.txt"])
    _write(ws.path / "$null", b"real bytes are output, not residue\n")

    with pytest.raises(worker_workspace.WorkspaceError, match=r"scope_violation:\$null"):
        worker_workspace.enforce_scope(ws)


def test_base_tracked_residue_name_keeps_its_delta(make) -> None:
    ws = make("residue-tracked", ["out/result.txt"])
    (ws.path / "kept.stackdump").write_bytes(b"tracked-v2\n")

    with pytest.raises(
        worker_workspace.WorkspaceError, match=r"scope_violation:kept\.stackdump"
    ):
        worker_workspace.enforce_scope(ws)


def test_explicitly_allowed_residue_name_is_real_output(make) -> None:
    ws = make("residue-allowed", ["out/result.txt", "crash.stackdump"])
    _write(ws.path / "crash.stackdump", b"declared diagnostic output\n")

    assert worker_workspace.enforce_scope(ws) == ["crash.stackdump"]


def test_manifest_fallback_drops_new_shell_residue(make) -> None:
    ws = make("residue-manifest", ["out/result.txt"])
    ws = dataclasses.replace(ws, tree_baseline=worker_workspace._worktree_manifest(ws.path))
    (ws.path / "out" / "result.txt").write_bytes(b"result-v2\n")
    _write(ws.path / "$null", b"")
    _write(ws.path / "bash.exe.stackdump", b"Stack trace:\n")
    _write(ws.path / "other.txt", b"a real stray write\n")

    assert worker_workspace._manifest_changed_paths(ws, git_phase="t") == [
        "other.txt",
        "out/result.txt",
    ]
