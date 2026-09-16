"""Claude must be able to hold the manager seat on Windows.

Measured on this host: the MCP server's process chain is

    pid 2652  python.exe   (the server)
    pid 38272 python.exe   (the venv Scripts\\python.exe REDIRECTOR)
    pid 29612 claude.exe   (the interactive Claude Code VS Code session)

and ``~/.claude/sessions/29612.json`` validates cleanly. The verification read
only ``os.getppid()``, found ``python.exe`` rather than ``claude.exe`` and
refused, so ``_claude_manager_identity()`` answered None and every manager
operation fell through to the Codex route -- which after a window reload has no
observed thread id, so ``aiworkhub_task_create`` refused with
``callback_route_pending:codex_thread_id_not_observed``.

POSIX has no such hop: ``.venv/bin/python`` is a symlink the loader follows in
place, so the server really is claude's direct child there. That is why the same
code verified fine on Linux and could never verify here.

What must NOT change: only a hop that is THIS interpreter's own image, owned by
the same user, is skipped, and only a bounded number of them. The seat is still
granted by one exact ``claude`` ancestor with a valid, repository-bound session
descriptor.

Run: python3 -m pytest -q tests/test_claude_manager_route_windows.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from aiworkhub import core  # noqa: E402

_SESSION_ID = "9ea55703-f15e-4c35-8253-4e0c96516781"
_SAME_USER = "S-1-5-21-389243392-615521012-1854199069-1001"
_SERVER_PID = 2652
_STUB_PID = 38272
_CLAUDE_PID = 29612


def _descriptor(pid: int, repo: Path) -> dict:
    return {
        "pid": pid,
        "kind": "interactive",
        "entrypoint": "claude-vscode",
        "sessionId": _SESSION_ID,
        "cwd": str(repo),
    }


def _bind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    tree: dict[int, tuple[int, str]],
    owners: dict[int, str] | None = None,
    descriptor_pid: int = _CLAUDE_PID,
    descriptor_cwd: Path | None = None,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    home = tmp_path / "home"
    sessions = home / ".claude" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{descriptor_pid}.json").write_text(
        json.dumps(_descriptor(descriptor_pid, descriptor_cwd or repo)),
        encoding="utf-8",
        newline="",
    )

    resolved_owners = owners if owners is not None else dict.fromkeys(tree, _SAME_USER)
    monkeypatch.setattr(core.os, "getppid", lambda: tree[_SERVER_PID][0])
    monkeypatch.setattr(core.os, "getpid", lambda: _SERVER_PID)
    monkeypatch.setattr(core, "_windows_process_tree", lambda: tree)
    monkeypatch.setattr(
        core, "_windows_process_owner_sid", lambda pid: resolved_owners.get(pid)
    )
    monkeypatch.setattr(core, "repo_root", lambda: repo.resolve())
    monkeypatch.setattr(core.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(core.sys, "executable", r"D:\repo\.venv\Scripts\python.exe")


def test_a_venv_redirector_hop_still_grants_the_claude_seat(tmp_path, monkeypatch):
    _bind(
        monkeypatch,
        tmp_path,
        tree={
            _SERVER_PID: (_STUB_PID, "python.exe"),
            _STUB_PID: (_CLAUDE_PID, "python.exe"),
            _CLAUDE_PID: (39124, "claude.exe"),
        },
    )

    identity = core._claude_windows_manager_identity()

    assert identity == {
        "provider": "claude",
        "session_id": _SESSION_ID,
        "window_id": f"claude_vscode_{_CLAUDE_PID}",
    }


def test_a_direct_claude_parent_still_works(tmp_path, monkeypatch):
    _bind(
        monkeypatch,
        tmp_path,
        tree={
            _SERVER_PID: (_CLAUDE_PID, "python.exe"),
            _CLAUDE_PID: (39124, "claude.exe"),
        },
    )

    assert core._claude_windows_manager_identity() is not None


def test_a_foreign_intermediate_process_is_refused(tmp_path, monkeypatch):
    # Only this interpreter's own re-exec stub may be skipped. A shell, a
    # debugger or any other process between us and claude ends the chain.
    _bind(
        monkeypatch,
        tmp_path,
        tree={
            _SERVER_PID: (_STUB_PID, "python.exe"),
            _STUB_PID: (_CLAUDE_PID, "bash.exe"),
            _CLAUDE_PID: (39124, "claude.exe"),
        },
    )

    assert core._claude_windows_manager_identity() is None


def test_more_stubs_than_the_hop_limit_are_refused(tmp_path, monkeypatch):
    chain = {
        _SERVER_PID: (101, "python.exe"),
        101: (102, "python.exe"),
        102: (103, "python.exe"),
        103: (_CLAUDE_PID, "python.exe"),
        _CLAUDE_PID: (39124, "claude.exe"),
    }
    _bind(monkeypatch, tmp_path, tree=chain)

    assert core._claude_windows_manager_identity() is None


def test_a_hop_owned_by_another_user_is_refused(tmp_path, monkeypatch):
    _bind(
        monkeypatch,
        tmp_path,
        tree={
            _SERVER_PID: (_STUB_PID, "python.exe"),
            _STUB_PID: (_CLAUDE_PID, "python.exe"),
            _CLAUDE_PID: (39124, "claude.exe"),
        },
        owners={
            _SERVER_PID: _SAME_USER,
            _STUB_PID: "S-1-5-21-000000000-000000000-000000000-1002",
            _CLAUDE_PID: _SAME_USER,
        },
    )

    assert core._claude_windows_manager_identity() is None


def test_a_descriptor_bound_to_another_repository_is_refused(tmp_path, monkeypatch):
    elsewhere = tmp_path / "other-repo"
    elsewhere.mkdir()
    _bind(
        monkeypatch,
        tmp_path,
        tree={
            _SERVER_PID: (_STUB_PID, "python.exe"),
            _STUB_PID: (_CLAUDE_PID, "python.exe"),
            _CLAUDE_PID: (39124, "claude.exe"),
        },
        descriptor_cwd=elsewhere,
    )

    assert core._claude_windows_manager_identity() is None


def test_a_missing_session_descriptor_is_refused(tmp_path, monkeypatch):
    _bind(
        monkeypatch,
        tmp_path,
        tree={
            _SERVER_PID: (_STUB_PID, "python.exe"),
            _STUB_PID: (_CLAUDE_PID, "python.exe"),
            _CLAUDE_PID: (39124, "claude.exe"),
        },
        descriptor_pid=999999,
    )

    assert core._claude_windows_manager_identity() is None
