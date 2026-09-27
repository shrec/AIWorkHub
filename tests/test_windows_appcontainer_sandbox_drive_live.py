"""NF-2026-01027 live probe: a real AppContainer inside a repo sandbox root.

The child sees ``<repo>\\.aiworkhub\\runtime\\worktrees`` as a per-logon-session
drive letter, so python, git and node run with no ACE above that root and
nothing written to the repository's own DACLs.

Opt-in: runs only on Windows with ``AIWORKHUB_LIVE_APPCONTAINER_PROBE=1``
(``-s`` shows the report-only git-worktree measurement).
"""

from __future__ import annotations

import ctypes
import msvcrt
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

import aiworkhub.windows_appcontainer as wac
from aiworkhub.windows_appcontainer import (
    AppContainerLifecycleState,
    AppContainerRequest,
    container_sid,
    launch_appcontainer,
    python_read_grants,
    request_scoped_grants,
)

pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("AIWORKHUB_LIVE_APPCONTAINER_PROBE") != "1",
    reason="live AppContainer probe: Windows and AIWORKHUB_LIVE_APPCONTAINER_PROBE=1",
)

REPO = Path(__file__).resolve().parents[1]
PYTHON = r"C:\Python312\python.exe"
GIT = r"C:\Program Files\Git\cmd\git.exe"
NODE = r"C:\Program Files\nodejs\node.exe"
REPO_ID, KIND = "aiworkhub-probe", "probe"

CASES = {
    "python": [
        PYTHON,
        "-I",
        "-c",
        "import os,pathlib;p=pathlib.Path('f.txt');p.write_text('ok');"
        "print(p.read_text());print(os.path.realpath(os.getcwd()))",
    ],
    "git_init": [GIT, "init", "-q"],
    "git_status": [GIT, "status", "--short", "--branch"],
    "node": [NODE, "-e", "console.log(require('fs').realpathSync(process.cwd()))"],
}
# Report only: what the drive does to Python's own final-path lookup.
FINAL_PATH = [
    PYTHON,
    "-I",
    "-c",
    "import nt,os\ntry: print(nt._getfinalpathname(os.getcwd()))\n"
    "except OSError as e: print('ERR', e.winerror)",
]


def _icacls(path: Path) -> str:
    return subprocess.run(
        ["icacls", str(path)], capture_output=True, text=True, check=False
    ).stdout


def _letters_naming(root: str) -> set[str]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    wanted = wac._dos_device_target(root)
    found = set()
    for letter in wac._SANDBOX_DRIVE_LETTERS:
        buffer = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryDosDeviceW(letter + ":", buffer, len(buffer)):
            if buffer.value.lower() == wanted:
                found.add(letter + ":")
    return found


def _run(argv, cwd: Path, env, grants, out: Path) -> tuple[int | None, str]:
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    launch = None
    try:
        os.set_inheritable(fd, True)
        handle = msvcrt.get_osfhandle(fd)
        launch = launch_appcontainer(
            AppContainerRequest(
                argv=argv,
                repo_id=REPO_ID,
                worker_kind=KIND,
                executable=argv[0],
                working_directory=str(cwd),
                environment=env,
                stdout_handle=handle,
                stderr_handle=handle,
                filesystem_grants=grants,
            )
        )
        result = launch.wait(60_000, terminate_on_timeout=True)
        code = result.exit_code if result.state is AppContainerLifecycleState.EXITED else None
    finally:
        if launch is not None:
            launch.close()
            assert launch.grant_revoke_failures == []
        os.close(fd)
    return code, out.read_text(errors="replace").strip()


def _remove_tree(path: Path) -> None:
    def writable(function, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onexc=writable)


def test_python_git_and_node_run_under_the_sandbox_drive(tmp_path):
    root = (REPO / ".aiworkhub" / "runtime" / "worktrees").resolve()
    live = root / f"live-{uuid.uuid4().hex[:8]}"
    worktree = live / "worktree"
    home = live / "home"
    (home / "tmp").mkdir(parents=True)
    worktree.mkdir()
    root_key = os.path.normcase(str(root))
    watched = [REPO, REPO / ".aiworkhub", REPO / ".aiworkhub" / "runtime"]
    before_acl = {path: _icacls(path) for path in watched}
    before_letters = _letters_naming(root_key)
    sid = container_sid(REPO_ID, KIND)
    env = {
        "SystemRoot": os.environ["SystemRoot"],
        "PATH": os.pathsep.join(
            [
                r"C:\Program Files\Git\cmd",
                r"C:\Program Files\nodejs",
                os.environ["SystemRoot"] + r"\System32",
            ]
        ),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TEMP": str(home / "tmp"),
        "TMP": str(home / "tmp"),
    }
    grants = request_scoped_grants(env, str(worktree)) + python_read_grants(
        PYTHON, "", covered=[str(worktree)]
    )
    results: dict[str, tuple[int | None, str]] = {}
    created: set[str] = set()
    try:
        for name, argv in CASES.items():
            results[name] = _run(argv, worktree, env, grants, tmp_path / f"{name}.txt")
        results["final_path"] = _run(
            FINAL_PATH, worktree, env, grants, tmp_path / "final_path.txt"
        )
        leaf_acl = _icacls(worktree)
        drives = _letters_naming(root_key)
        created = drives - before_letters

        # Report only: a linked worktree's absolute ``.git`` pointer names the
        # real D: path above the sandbox root; a relative one stays inside it.
        origin, linked = live / "origin", live / "linked"
        host_git = [GIT, "-c", "user.name=probe", "-c", "user.email=probe@invalid"]
        subprocess.run([*host_git, "init", "-q", str(origin)], check=True)
        subprocess.run(
            [*host_git, "-C", str(origin), "commit", "-q", "--allow-empty", "-m", "i"],
            check=True,
        )
        subprocess.run(
            [*host_git, "-C", str(origin), "worktree", "add", "-q", "--detach",
             str(linked), "HEAD"],
            check=True,
        )
        linked_grants = request_scoped_grants(env, str(linked), str(origin)) + (
            python_read_grants(PYTHON, "", covered=[str(linked)])
        )
        pointer = (linked / ".git").read_text().strip()
        results["linked_absolute"] = _run(
            CASES["git_status"], linked, env, linked_grants, tmp_path / "la.txt"
        )
        # git hides this file, and CREATE_ALWAYS refuses a hidden file: rewrite in place.
        with open(linked / ".git", "r+") as pointer_file:
            pointer_file.write("gitdir: ../origin/.git/worktrees/linked\n")
            pointer_file.truncate()
        results["linked_relative"] = _run(
            CASES["git_status"], linked, env, linked_grants, tmp_path / "lr.txt"
        )
    finally:
        after_acl = {path: _icacls(path) for path in watched}
        for letter in _letters_naming(root_key) - before_letters:
            ctypes.windll.kernel32.DefineDosDeviceW(2 | 4, letter, root_key)
        if live.exists():
            _remove_tree(live)

    print(f"\nsandbox root {root_key} -> {sorted(drives)} (created {sorted(created)})")
    print(f"linked .git pointer: {pointer}")
    for name, (code, output) in results.items():
        print(f"{name}: exit={code} out={output!r}")

    assert len(drives) == 1
    (drive,) = drives
    for name in CASES:
        assert results[name][0] == 0, (name, results[name])
    assert results["python"][1].splitlines()[-1].upper().startswith(drive + "\\")
    assert results["node"][1].upper().startswith(drive + "\\")
    assert "No commits yet" in results["git_status"][1]
    assert sid not in leaf_acl
    assert after_acl == before_acl
    assert _letters_naming(root_key) == before_letters
