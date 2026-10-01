"""Opt-in live host probe for the Windows restricted-token primitive.

One test, and it runs only where it can mean anything: Windows, with
``AIWORKHUB_LIVE_RESTRICTED_TOKEN_REPO`` naming an absolute repository root.
Inside a worker sandbox neither condition holds, so it skips rather than fails.

It creates two probe slots under ``<repo>/.aiworkhub/runtime/sandboxes/slots``,
prepares both, and then measures what a child under slot A's token can actually
reach.  Two kinds of statement live here and they are kept apart deliberately:

ASSERTED -- the containment claims the primitive exists to make.  The drive map
and every ancestor ACL unchanged; a write into the own slot succeeding; writes
aimed at the repository root, the other slot, ``%TEMP%`` and ``%USERPROFILE%``
failing and leaving no file; the other slot unreadable; the user profile
unreadable; the slot root surviving the child's best attempt to delete it; and
``terminate()`` ending a whole child tree.  Each failure above has a specific
cause: the Low integrity label stops the writes to unlabelled objects even where
their DACL would allow them, and the restricting-SID check stops the other slot
even though it *is* Low-labelled.

RECORDED -- printed once as ``RESTRICTED_TOKEN_PROBE_REPORT=<json>`` (visible
with ``pytest -s``) and never asserted: which provider tools are readable and
launchable, and what a python child can reach over sockets, DNS, a named pipe and
tempfile.  Those are the measurements the later wiring card is meant to be
designed from; asserting a guess about them here would only encode the guess.

The probe writes nothing outside its two slots, and the report carries no user
name and no home path -- the user profile prefix is replaced with ``<home>``.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import socket
import sys
import time
import uuid
from pathlib import Path

import pytest

import aiworkhub.windows_appcontainer as wac
import aiworkhub.windows_restricted_token as wrt

REPO_ENV = "AIWORKHUB_LIVE_RESTRICTED_TOKEN_REPO"
_REPO = os.environ.get(REPO_ENV, "")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not os.path.isabs(_REPO),
    reason=(
        f"live restricted-token host probe: needs win32 and {REPO_ENV}=<absolute "
        "repo root>; the manager runs it on the host"
    ),
)

SLOT_RELATIVE = (".aiworkhub", "runtime", "sandboxes", "slots")
AGENT_CLIS = ("claude", "codex")
CHILD_TIMEOUT = 30.0
TERMINATE_BUDGET = 10.0
TAIL = 400

# PIPE_ACCESS_DUPLEX; byte-type, blocking mode; a NULL lpSecurityAttributes is
# what makes the pipe carry the DEFAULT security descriptor the probe measures.
PIPE_ACCESS_DUPLEX = 0x00000003

REACH_PROGRAM = """
import json, os, socket, tempfile


def attempt(action):
    try:
        action()
    except BaseException as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "error": ""}


port = int(os.environ["RTPROBE_PORT"])
pipe = os.environ["RTPROBE_PIPE"]
print(json.dumps({
    "tcp_localhost": attempt(
        lambda: socket.create_connection(("127.0.0.1", port), 5).close()
    ),
    "dns_api_anthropic_com": attempt(
        lambda: socket.getaddrinfo("api.anthropic.com", 443)
    ),
    "named_pipe": attempt(lambda: open(pipe, "rb").close()),
    "tempfile": attempt(lambda: tempfile.NamedTemporaryFile().close()),
}))
"""


def _scrub(text: str) -> str:
    """Replace the user profile prefix with ``<home>``: the report names no user."""

    scrubbed = text
    for home in {os.environ.get("USERPROFILE", ""), os.path.expanduser("~")}:
        if not home:
            continue
        parts = [part for part in re.split(r"[\\/]+", home) if part]
        pattern = r"[\\/]+".join(re.escape(part) for part in parts)
        scrubbed = re.sub(pattern, "<home>", scrubbed, flags=re.I)
    return scrubbed


def _logical_drives() -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetLogicalDrives.argtypes = []
    kernel32.GetLogicalDrives.restype = ctypes.c_uint32
    return int(kernel32.GetLogicalDrives())


def _acl_fingerprint(path: str) -> bytes:
    """``path``'s authenticated DACL digest, read through windows_appcontainer.

    ``AclSnapshot.authentication`` is a sha256 over the path, the DACL state and
    the raw ACL bytes, so comparing it compares the descriptor itself rather than
    a formatted rendering of it.
    """

    return wac.snapshot_filesystem_acl(path).authentication


def _create_named_pipe(name: str) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateNamedPipeW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateNamedPipeW.restype = ctypes.c_void_p
    handle = kernel32.CreateNamedPipeW(name, PIPE_ACCESS_DUPLEX, 0, 1, 4096, 4096, 0, None)
    invalid = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1
    if not handle or int(handle) == invalid:
        raise OSError(ctypes.get_last_error(), "CreateNamedPipeW")
    return int(handle)


def _close_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle(ctypes.c_void_p(handle))


def _run(argv, cwd, sid, capture_dir=None, environment=None):
    """Run ``argv`` under ``sid``'s restricted token; return (exit code, output).

    When ``capture_dir`` is given, stdout and stderr share one inherited handle on
    a file the TEST created, so the capture never depends on the child being able
    to create anything.  It is omitted for the ``rmdir`` case on purpose: an open
    handle anywhere inside the slot would block the delete for a reason other
    than the ACL under test.
    """

    import msvcrt

    request = {
        "argv": argv,
        "slot_sid": sid,
        "working_directory": cwd,
        "environment": environment,
    }
    if capture_dir is None:
        process = wrt.launch_restricted(wrt.RestrictedTokenRequest(**request))
        try:
            return process.wait(CHILD_TIMEOUT), ""
        finally:
            process.close()

    log = os.path.join(capture_dir, f"probe-{uuid.uuid4().hex[:8]}.log")
    with open(log, "w+b") as sink:
        handle = msvcrt.get_osfhandle(sink.fileno())
        process = wrt.launch_restricted(
            wrt.RestrictedTokenRequest(
                stdout_handle=handle, stderr_handle=handle, **request
            )
        )
        try:
            code = process.wait(CHILD_TIMEOUT)
        finally:
            process.close()
        sink.seek(0)
        text = sink.read().decode("utf-8", "replace")
    os.remove(log)
    return code, text


def _tool_paths() -> dict[str, str | None]:
    return {
        "python": os.path.join(sys.base_prefix, "python.exe"),
        "git": shutil.which("git"),
        "node": shutil.which("node"),
    }


def _tool_run(name, exe, sid, workspace, capture_dir):
    if name == "python":
        program = "import os;print(os.path.realpath('.'))"
        return _run([exe, "-I", "-c", program], workspace, sid, capture_dir)
    if name == "node":
        program = "console.log(require('fs').realpathSync('.'))"
        return _run([exe, "-e", program], workspace, sid, capture_dir)
    code, text = _run([exe, "init", "-q"], workspace, sid, capture_dir)
    if code != 0:
        return code, text
    return _run([exe, "status"], workspace, sid, capture_dir)


def _measure_tools(sid, workspace, capture_dir):
    """Per tool: readable, launched, exit code, scrubbed tail -- and the assertion.

    Readability decides whether the tool is launched at all, which is the whole
    point of ``restricted_access``: an unreadable interpreter is a recorded fact
    about this host, not a failed launch.  A READABLE one, though, must work.
    """

    measured = []
    for name, exe in _tool_paths().items():
        row = {
            "name": name,
            "readable": False,
            "launched": False,
            "exit_code": None,
            "stderr_tail": "",
        }
        if not exe or not os.path.exists(exe):
            measured.append(row)
            continue
        row["readable"] = wrt.restricted_access(sid, exe, "read_execute")
        if not row["readable"]:
            measured.append(row)
            continue
        row["launched"] = True
        code, text = _tool_run(name, exe, sid, workspace, capture_dir)
        row["exit_code"] = code
        row["stderr_tail"] = _scrub(text.strip())[-TAIL:]
        assert code == 0, f"{name}: {row['stderr_tail']}"
        if name in ("python", "node"):
            reported = [line.strip() for line in text.splitlines() if line.strip()]
            assert reported, f"{name} printed nothing"
            assert os.path.normcase(reported[-1]) == os.path.normcase(
                os.path.realpath(workspace)
            )
        measured.append(row)
    return measured


def _agent_reachability(sid: str, name: str) -> dict:
    """``read_execute`` for an agent CLI shim, reported by basename only."""

    exe = shutil.which(name)
    if not exe:
        return {"found": False, "basename": None, "readable": None}
    return {
        "found": True,
        "basename": os.path.basename(exe),
        "readable": wrt.restricted_access(sid, exe, "read_execute"),
    }


def _measure_python_reach(sid, workspace, slot_temp, capture_dir) -> dict:
    """What a python child under the slot token reaches.  Recorded, not asserted.

    The listener and the named pipe are created by the TEST, so a failure to
    connect is the restriction speaking rather than a missing endpoint.  ``TEMP``
    and ``TMP`` point at the slot's own temp directory, which is the only place a
    restricted child could legitimately put one.
    """

    exe = os.path.join(sys.base_prefix, "python.exe")
    pipe_name = r"\\.\pipe\aiworkhub-rtprobe-" + uuid.uuid4().hex[:12]
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    pipe = None
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        pipe = _create_named_pipe(pipe_name)
        environment = dict(os.environ)
        environment["TEMP"] = slot_temp
        environment["TMP"] = slot_temp
        environment["RTPROBE_PORT"] = str(listener.getsockname()[1])
        environment["RTPROBE_PIPE"] = pipe_name
        code, text = _run(
            [exe, "-I", "-c", REACH_PROGRAM],
            workspace,
            sid,
            capture_dir,
            environment=environment,
        )
    finally:
        if pipe is not None:
            _close_handle(pipe)
        listener.close()
    for line in reversed(text.splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line.strip())
    return {"exit_code": code, "unparsed_tail": _scrub(text.strip())[-TAIL:]}


def test_a_restricted_child_reaches_its_own_slot_and_nothing_above_it():
    repo = os.path.realpath(_REPO)
    slots = os.path.join(repo, *SLOT_RELATIVE)
    os.makedirs(slots, exist_ok=True)
    tag = uuid.uuid4().hex[:12]
    slot_a = os.path.join(slots, f"rtprobe-{tag}-a")
    slot_b = os.path.join(slots, f"rtprobe-{tag}-b")
    report: dict = {"tools": [], "agents": {}, "python_child": {}}
    try:
        for slot in (slot_a, slot_b):
            # 'workspace' and 'temp' exist BEFORE preparation, so the walk that
            # makes pre-existing children writable is itself under test.
            os.makedirs(os.path.join(slot, "workspace"))
            os.makedirs(os.path.join(slot, "temp"))
        workspace_a = os.path.join(slot_a, "workspace")
        workspace_b = os.path.join(slot_b, "workspace")
        temp_a = os.path.join(slot_a, "temp")
        Path(workspace_b, "seed.txt").write_text("seed\n", encoding="utf-8")

        sid_a = wrt.slot_sid("rtprobe", f"{tag}-a")
        sid_b = wrt.slot_sid("rtprobe", f"{tag}-b")

        watched = [
            os.path.splitdrive(repo)[0] + os.sep,
            repo,
            os.path.join(repo, ".aiworkhub"),
            os.path.join(repo, ".aiworkhub", "runtime"),
            os.path.join(repo, ".aiworkhub", "runtime", "sandboxes"),
            slots,
        ]
        drives_before = _logical_drives()
        acl_before = {path: _acl_fingerprint(path) for path in watched}

        assert wrt.prepare_slot_root(slot_a, sid_a, repo_root=repo).changed is True
        assert wrt.prepare_slot_root(slot_b, sid_b, repo_root=repo).changed is True
        # Second preparation of a canonical root writes nothing at all.
        assert wrt.prepare_slot_root(slot_a, sid_a, repo_root=repo).changed is False

        # The ONLY xfail in this file: a host that cannot build the token at all
        # has nothing to measure.  Every statement below is asserted.
        host = wrt.probe()
        if not host.available:
            pytest.xfail(host.reason)

        comspec = os.path.join(
            os.environ.get("SYSTEMROOT", "C:\\Windows"), "System32", "cmd.exe"
        )

        def write_attempt(target: str) -> int:
            code, _text = _run(
                [comspec, "/s", "/c", f"echo x> {target}"], workspace_a, sid_a, temp_a
            )
            return code

        # (3) the own slot workspace is writable.
        own = os.path.join(workspace_a, "w.txt")
        assert write_attempt(own) == 0
        assert os.path.exists(own)

        # (4) nothing else is.  The repository root, %TEMP% and %USERPROFILE%
        # fail on the Low label; the other slot fails the restricted check.
        home = os.environ["USERPROFILE"]
        for target in (
            os.path.join(repo, f"rtprobe-{tag}.txt"),
            os.path.join(workspace_b, "w.txt"),
            os.path.join(os.environ["TEMP"], f"rtprobe-{tag}.txt"),
            os.path.join(home, f"rtprobe-{tag}.txt"),
        ):
            assert write_attempt(target) != 0
            assert not os.path.exists(target)

        # (5) the other slot is not even readable.
        seed = os.path.join(workspace_b, "seed.txt")
        code, _text = _run(
            [comspec, "/s", "/c", f"type {seed}"], workspace_a, sid_a, temp_a
        )
        assert code != 0

        # (6) the user profile is unreachable, by check and by child.
        assert wrt.restricted_access(sid_a, home, "read") is False
        code, _text = _run(
            [comspec, "/s", "/c", f"dir {home}"], workspace_a, sid_a, temp_a
        )
        assert code != 0

        # (7) the slot root survives the child's best attempt to remove it: its
        # own ACE carries no DELETE.  No capture handle, so nothing of ours is
        # open inside the slot to block the delete for an unrelated reason.
        _run([comspec, "/s", "/c", f"rmdir /s /q {slot_a}"], workspace_a, sid_a)
        assert os.path.isdir(slot_a)
        # The child legitimately deleted its own empty temp_a; later steps capture into it.
        os.makedirs(temp_a, exist_ok=True)

        # (8) terminate ends the whole tree, and the wait returns promptly.
        process = wrt.launch_restricted(
            wrt.RestrictedTokenRequest(
                argv=[comspec, "/s", "/c", "ping -n 30 127.0.0.1"],
                slot_sid=sid_a,
                working_directory=workspace_a,
            )
        )
        try:
            assert process.poll() is None
            process.terminate()
            started = time.monotonic()
            process.wait(TERMINATE_BUDGET)
            assert time.monotonic() - started < TERMINATE_BUDGET
        finally:
            process.close()

        # (9) a readable provider tool must work; an unreadable one is recorded
        # and never launched.
        report["tools"] = _measure_tools(sid_a, workspace_a, temp_a)
        report["agents"] = {name: _agent_reachability(sid_a, name) for name in AGENT_CLIS}
        for row in report["tools"]:
            if row["name"] == "python" and row["launched"]:
                report["python_child"] = _measure_python_reach(
                    sid_a, workspace_a, temp_a, temp_a
                )

        # (1) and (2): nothing above the slot roots moved.
        assert _logical_drives() == drives_before
        assert {path: _acl_fingerprint(path) for path in watched} == acl_before
    finally:
        for slot in (slot_a, slot_b):
            shutil.rmtree(slot, ignore_errors=True)
        print(
            "RESTRICTED_TOKEN_PROBE_REPORT="
            + _scrub(json.dumps(report, sort_keys=True, default=str))
        )
