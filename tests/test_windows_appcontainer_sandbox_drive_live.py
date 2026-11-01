"""NF-2026-01154 live probe: the sandbox drive letter is private to its session.

The owner decision (NF-2026-01154) is that no AIWorkHub launch may ever add a
DOS drive letter to the owner's interactive logon session or Explorer, even
temporarily. The production path reaches ``sandbox_drive`` only after re-execing
into a private ``CreateProcessWithLogonW`` (NewCredentials, logon type 9) session
via :mod:`aiworkhub.windows_private_session`, so the letter lives in that private
session and its descendants and is never visible to the owner session.

This opt-in probe launches the real AppContainer through that same private-session
entry point from a normal owner-session pytest. It asserts that python and node
``realpath`` start with the sandbox letter and that ``git status`` works, while a
concurrent owner-session sampler (``GetLogicalDrives`` + ``QueryDosDeviceW`` every
<=25 ms) sees NO letter naming the sandbox root before, during or after the run.
NF-2026-01027's per-logon-session visibility is kept; the old in-process
``len(drives) == 1`` assertion is dropped because the owner session, by design,
now sees zero.

Opt-in: runs only on Windows with ``AIWORKHUB_LIVE_APPCONTAINER_PROBE=1``
(``-s`` shows the report-only measurement). The manager runs this probe on the
host before accept.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from pathlib import Path

import pytest

import aiworkhub.windows_appcontainer as wac
from aiworkhub import windows_private_session as wps
from aiworkhub.windows_appcontainer import build_command_line

pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("AIWORKHUB_LIVE_APPCONTAINER_PROBE") != "1",
    reason="live AppContainer probe: Windows and AIWORKHUB_LIVE_APPCONTAINER_PROBE=1",
)
# Windows-only module: importing it unguarded fails collection elsewhere.
msvcrt = pytest.importorskip("msvcrt")

REPO = Path(__file__).resolve().parents[1]
PYTHON = r"C:\Python312\python.exe"
GIT = r"C:\Program Files\Git\cmd\git.exe"
NODE = r"C:\Program Files\nodejs\node.exe"
REPO_ID, KIND = "aiworkhub-probe", "probe"
_DRIVE_LETTER_RE = re.compile(r"^[A-Z]:\\")

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

# The relay runs INSIDE the private logon session: it is the first process that
# may legally reach sandbox_drive. It performs the AppContainer launch, relays
# the child's captured output to a result file, and exits with the child's code.
_RELAY = r"""
import json, os, sys, threading
import aiworkhub.windows_appcontainer as wac
from aiworkhub.windows_appcontainer import (
    AppContainerLifecycleState,
    AppContainerRequest,
    launch_appcontainer,
    python_read_grants,
    request_scoped_grants,
)
import msvcrt


def main():
    spec = json.loads(open(sys.argv[1], encoding="utf-8").read())
    argv, cwd, env = spec["argv"], spec["cwd"], spec["env"]
    grants = request_scoped_grants(env, cwd) + python_read_grants(
        spec["python"], "", covered=[cwd]
    )
    reader, writer = os.pipe()
    os.set_inheritable(writer, True)
    handle = msvcrt.get_osfhandle(writer)
    launch = launch_appcontainer(
        AppContainerRequest(
            argv=argv,
            repo_id=spec["repo_id"],
            worker_kind=spec["kind"],
            executable=argv[0],
            working_directory=cwd,
            environment=env,
            stdout_handle=handle,
            stderr_handle=handle,
            filesystem_grants=grants,
        )
    )
    os.close(writer)
    chunks = []

    def drain():
        while True:
            block = os.read(reader, 65536)
            if not block:
                break
            chunks.append(block)

    thread = threading.Thread(target=drain)
    thread.start()
    outcome = launch.wait(60_000, terminate_on_timeout=True)
    thread.join(5)
    code = outcome.exit_code if outcome.state is AppContainerLifecycleState.EXITED else None
    launch.close()
    os.close(reader)
    with open(spec["result"], "w", encoding="utf-8") as handle_out:
        handle_out.write(json.dumps({
            "exit": code,
            "output": b"".join(chunks).decode("utf-8", errors="replace").strip(),
            "grant_revoke_failures": list(launch.grant_revoke_failures),
        }))


main()
"""


def _icacls(path: Path) -> str:
    import subprocess

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


def _logical_drives() -> int:
    return int(ctypes.WinDLL("kernel32", use_last_error=True).GetLogicalDrives())


class _OwnerSessionSampler:
    """Poll the owner session for any letter naming the sandbox root, <=25 ms.

    This thread runs in the owner's logon session for the whole launch. The
    owner decision says that session must never see the sandbox letter, so every
    letter it ever observes is a violation and is recorded. ``drive_mask`` is the
    union of every ``GetLogicalDrives`` bitmask it sampled.
    """

    def __init__(self, root_key: str, interval: float = 0.02) -> None:
        self._root_key = root_key
        self._interval = interval
        self._stop = threading.Event()
        self.letters_seen: set[str] = set()
        self.drive_mask = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.letters_seen |= _letters_naming(self._root_key)
            self.drive_mask |= _logical_drives()
            self._stop.wait(self._interval)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(5)
        self.letters_seen |= _letters_naming(self._root_key)
        self.drive_mask |= _logical_drives()


def _remove_tree(path: Path) -> None:
    def writable(function, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onexc=writable)


def _run_through_private_session(
    relay: Path, argv, cwd: Path, env: dict, stem: Path
) -> tuple[int | None, str, int]:
    """Run one case through the production private-session entry point.

    Returns the AppContainer child's exit code, its captured output, and the
    private-session relay's own exit code.
    """

    spec_path = stem.with_suffix(".spec.json")
    result_path = stem.with_suffix(".result.json")
    spec_path.write_text(
        json.dumps({
            "argv": list(argv),
            "cwd": str(cwd),
            "env": env,
            "python": PYTHON,
            "repo_id": REPO_ID,
            "kind": KIND,
            "result": str(result_path),
        }),
        encoding="utf-8",
    )
    command = build_command_line([PYTHON, str(relay), str(spec_path)])
    # The relay itself gets the caller's environment, as the production hops do
    # (reexec_supervisor_if_owner_session / _relay_environment); a stripped one
    # makes the AppContainer CreateProcessW fail with ERROR_ENVVAR_NOT_FOUND.
    relay_code = wps.spawn_in_private_logon_session(
        command,
        environment=dict(os.environ, PYTHONPATH=env["PYTHONPATH"]),
        working_directory=str(cwd),
    )
    data = json.loads(result_path.read_text(encoding="utf-8"))
    assert data["grant_revoke_failures"] == [], data
    return data["exit"], data["output"], relay_code


def test_python_git_and_node_run_under_the_sandbox_drive(tmp_path):
    root = (REPO / ".aiworkhub" / "runtime" / "worktrees").resolve()
    live = root / f"live-{uuid.uuid4().hex[:8]}"
    worktree = live / "worktree"
    home = live / "home"
    (home / "tmp").mkdir(parents=True)
    worktree.mkdir()
    relay = live / "relay.py"
    relay.write_text(_RELAY, encoding="utf-8")
    root_key = os.path.normcase(str(root))
    watched = [REPO, REPO / ".aiworkhub", REPO / ".aiworkhub" / "runtime"]
    before_acl = {path: _icacls(path) for path in watched}
    assert _letters_naming(root_key) == set(), "a concurrent launch already holds this root's letter"
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
        # The private-session relay needs the package it launches through.
        "PYTHONPATH": str(REPO / "src"),
    }
    results: dict[str, tuple[int | None, str, int]] = {}
    sampler = _OwnerSessionSampler(root_key)
    sampler.start()
    try:
        for name, argv in CASES.items():
            results[name] = _run_through_private_session(
                relay, argv, worktree, env, tmp_path / name
            )
        leaf_acl = _icacls(worktree)
    finally:
        sampler.stop()
        after_acl = {path: _icacls(path) for path in watched}
        if live.exists():
            _remove_tree(live)

    for name, (code, output, relay_code) in results.items():
        print(f"{name}: child_exit={code} relay_exit={relay_code} out={output!r}")

    # The owner session never saw a letter naming the sandbox root -- before,
    # during, or after. This is the NF-2026-01154 owner decision.
    assert sampler.letters_seen == set()
    assert _letters_naming(root_key) == set()
    for name in CASES:
        assert results[name][0] == 0, (name, results[name])
        assert results[name][2] == 0, (name, results[name])
    # python and node both report a realpath under the SAME sandbox drive letter,
    # observed from inside the private session (never from the owner session).
    python_realpath = results["python"][1].splitlines()[-1].upper()
    node_realpath = results["node"][1].upper()
    assert _DRIVE_LETTER_RE.match(python_realpath)
    drive = python_realpath[:2]
    assert drive[0] + ":" in {letter + ":" for letter in wac._SANDBOX_DRIVE_LETTERS}
    assert node_realpath.startswith(drive + "\\")
    assert "No commits yet" in results["git_status"][1]
    # No DACL above the sandbox root changed, and the container SID is not on the
    # leaf: NF-2026-01027 / NF-2026-01015 still hold.
    assert wac.container_sid(REPO_ID, KIND) not in leaf_acl
    assert after_acl == before_acl


def test_closing_the_only_private_launch_leaves_no_owner_session_letter(tmp_path):
    """The private session dies with the relay, and the owner session never saw
    its letter."""
    root = (REPO / ".aiworkhub" / "runtime" / "worktrees").resolve()
    live = root / f"live-{uuid.uuid4().hex[:8]}"
    worktree = live / "worktree"
    home = live / "home"
    (home / "tmp").mkdir(parents=True)
    worktree.mkdir()
    relay = live / "relay.py"
    relay.write_text(_RELAY, encoding="utf-8")
    root_key = os.path.normcase(str(root))
    assert _letters_naming(root_key) == set(), "a concurrent launch already holds this root's letter"
    env = {
        "SystemRoot": os.environ["SystemRoot"],
        "PATH": os.environ["SystemRoot"] + r"\System32",
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TEMP": str(home / "tmp"),
        "TMP": str(home / "tmp"),
        "PYTHONPATH": str(REPO / "src"),
    }
    sampler = _OwnerSessionSampler(root_key)
    sampler.start()
    try:
        child_exit, _output, relay_exit = _run_through_private_session(
            relay, [PYTHON, "-I", "-c", "pass"], worktree, env, tmp_path / "close"
        )
    finally:
        sampler.stop()
        if live.exists():
            _remove_tree(live)

    assert child_exit == 0
    assert relay_exit == 0
    # The letter lived only inside the private session; the owner session never
    # saw it, and nothing remains now that the session has ended.
    assert sampler.letters_seen == set()
    assert _letters_naming(root_key) == set()


def test_the_validation_lane_runs_from_the_owner_session_with_no_owner_letter():
    """NF-2026-01154: finalize/accept's AppContainer validation lane, called from
    this owner-session pytest exactly as the MCP server calls it, relays through
    the private-session helper, succeeds, and adds no drive letter to the owner
    session -- sampled by GetLogicalDrives and QueryDosDeviceW before, while and
    after it runs."""
    from aiworkhub import worker_workspace

    assert not wps.in_private_logon_session(), "this probe must start in the owner session"
    root = (REPO / ".aiworkhub" / "runtime" / "worktrees").resolve()
    request_id = f"live-{uuid.uuid4().hex[:8]}"
    live = root / request_id
    worktree = live / "worktree"
    home = live / "home"
    worktree.mkdir(parents=True)
    home.mkdir()
    root_key = os.path.normcase(str(root))
    assert _letters_naming(root_key) == set(), "a concurrent launch already holds this root's letter"
    workspace = worker_workspace.WorkerWorkspace(
        request_id=request_id,
        repo=REPO,
        path=worktree,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    env = {
        "SystemRoot": os.environ["SystemRoot"],
        "PATH": os.environ["SystemRoot"] + r"\System32",
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(home),
        "TEMP": str(home),
        "TMP": str(home),
    }
    drives_before = _logical_drives()
    sampler = _OwnerSessionSampler(root_key)
    sampler.start()
    try:
        result = wps.run_appcontainer_validation_hopped(
            [PYTHON, "-I", "-c", "import os;print(os.path.realpath(os.getcwd()))"],
            workspace=workspace,
            adapter_id="claude_cli",
            cwd=worktree,
            env=env,
            timeout_seconds=120,
            run_direct=worker_workspace._run_appcontainer_validation,
        )
    finally:
        sampler.stop()
        drives_after = _logical_drives()
        if live.exists():
            _remove_tree(live)

    print(f"validation lane: rc={result.returncode} out={result.stdout!r} err={result.stderr!r}")
    assert result.returncode == 0, (result.stdout, result.stderr)
    # Inside the private session the lane saw its sandbox letter...
    realpath = result.stdout.strip().splitlines()[-1].upper()
    assert _DRIVE_LETTER_RE.match(realpath)
    assert realpath[0] in wac._SANDBOX_DRIVE_LETTERS
    # ...and the owner session never did: no letter naming the root, and no new
    # bit in the owner's logical-drive mask before, during or after the run.
    assert sampler.letters_seen == set()
    assert _letters_naming(root_key) == set()
    assert sampler.drive_mask & ~drives_before == 0
    assert drives_after & ~drives_before == 0
