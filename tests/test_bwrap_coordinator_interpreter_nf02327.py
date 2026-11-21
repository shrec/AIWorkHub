"""NF-2026-02327: bubblewrap validation publishes the coordinator interpreter.

A bare ``python3`` head is rewritten to ``sys.executable`` (NF-2026-00448); the
sandbox must therefore bind that interpreter's venv and base prefix read-only
at their identity paths.  The pure bind computation runs on every host; the
full ``sandbox_argv`` assembly needs a POSIX path model and is skipped on
Windows (the manager reruns it in WSL).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from aiworkhub import validation_interpreter_binds as binds
from aiworkhub.worker_workspace import WorkerWorkspace, WorkspaceError, sandbox_argv


def _fake_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    base = tmp_path / "toolcache" / "Python" / "3.12" / "x64"
    venv = tmp_path / "ci" / ".venv-ci"
    exe = venv / "bin" / "python"
    for directory in (base / "bin", venv / "bin"):
        directory.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(base))
    return venv, base, exe


def _pairs(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i in range(len(args) - 1) if args[i] == flag]


def test_publishes_venv_and_base_prefix_at_identity_paths(tmp_path, monkeypatch):
    venv, base, exe = _fake_runtime(tmp_path, monkeypatch)
    args = binds.identity_runtime_binds("validation", str(exe))
    triples = list(zip(args, args[1:], args[2:]))
    dirs = _pairs(args, "--dir")
    for root in (venv, base.resolve()):
        assert ("--ro-bind", str(root), str(root)) in triples
        for parent in root.parents:
            if parent != Path(parent.anchor):
                assert str(parent) in dirs
    assert len(dirs) == len(set(dirs))
    assert len(_pairs(args, "--ro-bind")) == 2


def test_usr_resident_interpreter_adds_no_bind(tmp_path, monkeypatch):
    usr = tmp_path / "usr"
    (usr / "bin").mkdir(parents=True)
    exe = usr / "bin" / "python3"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(binds, "_USR", usr)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "prefix", str(usr))
    monkeypatch.setattr(sys, "base_prefix", str(usr))
    assert binds.identity_runtime_binds("validation", str(exe)) == []


def test_non_coordinator_head_adds_no_bind(tmp_path, monkeypatch):
    _venv, _base, _exe = _fake_runtime(tmp_path, monkeypatch)
    assert binds.identity_runtime_binds("validation", "/usr/bin/env") == []
    assert binds.identity_runtime_binds("codex_cli", sys.executable) == []


@pytest.mark.parametrize("which", ["prefix", "base_prefix"])
def test_filesystem_root_prefix_is_refused(tmp_path, monkeypatch, which):
    _venv, _base, exe = _fake_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, which, os.sep)
    with pytest.raises(WorkspaceError, match="identity_root_untrusted"):
        binds.identity_runtime_binds("validation", str(exe))


def test_executable_outside_published_roots_is_refused(tmp_path, monkeypatch):
    _venv, _base, _exe = _fake_runtime(tmp_path, monkeypatch)
    stray = tmp_path / "stray" / "python"
    stray.parent.mkdir()
    stray.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(stray))
    with pytest.raises(WorkspaceError, match="executable_outside_root"):
        binds.identity_runtime_binds("validation", str(stray))


def test_dotdot_executable_escaping_root_is_refused(tmp_path, monkeypatch):
    venv, _base, _exe = _fake_runtime(tmp_path, monkeypatch)
    stray = tmp_path / "stray" / "python"
    stray.parent.mkdir()
    stray.write_text("", encoding="utf-8")
    escaping = f"{venv}{os.sep}..{os.sep}..{os.sep}stray{os.sep}python"
    monkeypatch.setattr(sys, "executable", escaping)
    with pytest.raises(WorkspaceError, match="executable_outside_root"):
        binds.identity_runtime_binds("validation", escaping)


@pytest.mark.skipif(
    os.name != "posix",
    reason="bubblewrap argv assembly uses POSIX paths; rerun in WSL/Linux",
)
def test_bubblewrap_argv_execs_unresolved_sys_executable(tmp_path, monkeypatch):
    venv, base, exe = _fake_runtime(tmp_path, monkeypatch)
    root = tmp_path / "w"
    workspace = WorkerWorkspace(
        request_id="nf02327",
        repo=root,
        path=root / "worktree",
        home=root / "home",
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    workspace.path.mkdir(parents=True)
    (workspace.path / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
    workspace.home.mkdir()
    argv = sandbox_argv(
        workspace,
        "validation",
        [str(exe), "x.py"],
        backend="bubblewrap",
    )
    separator = argv.index("--")
    assert argv[separator + 1 :] == [str(exe), "x.py"]
    head = argv[:separator]
    for root_path in (venv.resolve(), base.resolve()):
        text = str(root_path)
        assert any(
            head[i : i + 3] == ["--ro-bind", text, text] for i in range(len(head) - 2)
        )
        assert head.count(text) == 2
