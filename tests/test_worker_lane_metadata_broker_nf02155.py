"""NF-2026-02155: the Landlock WORKER lane brokers chmod beneath its worker temp.

A worker launch has no ``--exec-scratch``; before this card it fell through to
the path-blind deny filter, so cmake/ninja/the linker got EPERM on their own
TMPDIR outputs.  The lane decision is one pure function (tested on every OS);
the real Landlock + seccomp-notify behaviour is tested only where the host can
actually provide both mechanisms.
"""

from __future__ import annotations

import errno
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import worker_workspace as ww

_SCRIPT = Path(ww.__file__).resolve()


def _broker_capable() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        return ww.landlock_abi_version() >= 1 and ww._seccomp_notify_supported()
    except Exception:  # noqa: BLE001 - any probe failure is a capability gap
        return False


linux_broker = pytest.mark.skipif(
    not _broker_capable(),
    reason="needs Linux Landlock and seccomp user notification",
)


# ---------------------------------------------------------------- pure lane


def test_validation_lane_roots_at_exec_scratch(tmp_path: Path) -> None:
    scratch, temp = tmp_path / "scratch", tmp_path / "temp"
    assert ww._metadata_broker_root(scratch, temp, False) == scratch
    assert ww._metadata_broker_root(scratch, temp, True) == scratch
    assert ww._metadata_broker_root(scratch, None, False) == scratch


def test_worker_lane_roots_at_worker_temp_only(tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    assert ww._metadata_broker_root(None, temp, False) == temp


def test_noop_chmod_and_missing_worker_temp_keep_the_fallback(tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    # opencode_cli keeps --noop-chmod: no broker, the existing fallback filter.
    assert ww._metadata_broker_root(None, temp, True) is None
    assert ww._metadata_broker_root(None, None, False) is None
    assert ww._metadata_broker_root(None, None, True) is None


def test_broker_deadline_default_reads_the_live_constant() -> None:
    parameters = inspect.signature(ww._run_metadata_broker).parameters
    deadline, lane = parameters["deadline_seconds"], parameters["worker_lane"]
    assert deadline.kind is inspect.Parameter.KEYWORD_ONLY
    assert lane.kind is inspect.Parameter.KEYWORD_ONLY
    # ``...`` = read _METADATA_BROKER_DEADLINE_SECONDS per call, so a patched
    # constant applies; a value bound at import time would silently ignore it.
    assert deadline.default is ...
    assert lane.default is False


def test_worker_lane_notify_filter_brokers_chmod_only() -> None:
    # utime stays denied in the worker lane: the helper's filter takes the flag.
    parameter = inspect.signature(ww._install_metadata_notify_filter).parameters[
        "chmod_only"
    ]
    assert parameter.default is False
    assert "utimensat" not in ww._METADATA_BROKER_CHMOD_SYSCALLS
    assert "utimensat" in ww._SECCOMP_DENIED_SYSCALLS


# ---------------------------------------------------------------- Linux lane


def _worker_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    worktree = tmp_path / "worktree"
    home = tmp_path / "home"
    worker_temp = tmp_path / "repo" / ".aiworkhub" / "temp" / "worker" / "req"
    for directory in (worktree, home, worker_temp):
        directory.mkdir(parents=True, mode=0o700)
    return worktree, home, worker_temp


def _landlock_flags(worktree: Path, home: Path, worker_temp: Path) -> list[str]:
    # A WORKER launch: --worker-temp set, no --exec-scratch, no --noop-chmod.
    return [
        "--landlock-exec",
        "--workspace", str(worktree),
        "--home", str(home),
        "--worker-temp", str(worker_temp),
    ]


def _sandbox_env(home: Path, tmpdir: Path) -> dict[str, str]:
    # Mirror the real launch env: TMPDIR is the Landlock-writable request temp.
    # The broker helper re-imports this module AFTER Landlock is applied, so an
    # inherited TMPDIR outside the ruleset (pytest's /tmp) can kill it before
    # the handshake and silently select the deny-only fallback filter.
    tmp = str(tmpdir)
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": tmp,
        "TMP": tmp,
        "TEMP": tmp,
    }


_PROBE = r"""
import json, os, stat, sys
temp, outside = sys.argv[1], sys.argv[2]
own = os.path.join(temp, "built.o")
with open(own, "w") as handle:
    handle.write("x")
def errno_of(call):
    try:
        call()
    except OSError as exc:
        return exc.errno
    return 0
result = {"pgid": os.getpgrp()}
result["temp_chmod_errno"] = errno_of(lambda: os.chmod(own, 0o750))
result["temp_mode"] = stat.S_IMODE(os.stat(own).st_mode)
result["outside_errno"] = errno_of(lambda: os.chmod(outside, 0o600))
result["utime_errno"] = errno_of(lambda: os.utime(own, (1, 1)))
print(json.dumps(result))
"""


@linux_broker
def test_worker_lane_brokers_chmod_beneath_worker_temp_only(tmp_path: Path) -> None:
    worktree, home, worker_temp = _worker_layout(tmp_path)
    outside = worktree / "outside.txt"
    outside.write_text("worktree file outside the worker temp\n", encoding="utf-8")
    outside_mode = outside.stat().st_mode
    argv = [
        sys.executable,
        str(_SCRIPT),
        *_landlock_flags(worktree, home, worker_temp),
        "--",
        sys.executable,
        "-c",
        _PROBE,
        str(worker_temp),
        str(outside),
    ]
    with subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=_sandbox_env(home, worker_temp),
    ) as process:
        stdout, stderr = process.communicate(timeout=120)
    diagnostic = stderr.decode(errors="replace")
    assert process.returncode == 0, diagnostic
    result = json.loads(stdout.decode().strip().splitlines()[-1])
    # Owned file beneath the worker temp: emulated, mode really applied.
    assert result["temp_chmod_errno"] == 0, (result, diagnostic)
    assert result["temp_mode"] == 0o750
    # The worktree is never a broker root, and utime stays denied in every lane.
    assert result["outside_errno"] == errno.EPERM
    assert outside.stat().st_mode == outside_mode
    assert result["utime_errno"] == errno.EPERM
    # The helper stayed in the launched process group, so the supervisor's
    # killpg on that group still reaches the command and its descendants.
    assert result["pgid"] == process.pid


# Loads the bare script exactly as the real --landlock-exec child does (no
# package context), shrinks the validation deadline, then runs the lane.
_DEADLINE_WRAPPER = r"""
import importlib.util, os, sys
script = sys.argv[1]
sys.path.insert(0, os.path.dirname(script))
spec = importlib.util.spec_from_file_location("worker_workspace", script)
module = importlib.util.module_from_spec(spec)
sys.modules["worker_workspace"] = module
spec.loader.exec_module(module)
module._METADATA_BROKER_DEADLINE_SECONDS = 0.2
raise SystemExit(module._landlock_exec(sys.argv[2:]))
"""


_LATE_CHMOD = (
    "import os, sys, time; time.sleep(1.5); "
    "p = os.path.join(sys.argv[1], 'late.o'); open(p, 'w').close(); os.chmod(p, 0o750)"
)


def _run_with_tiny_deadline(
    flags: list[str], env: dict[str, str], temp: Path
) -> subprocess.CompletedProcess[bytes]:
    # Sleeps past the patched deadline, THEN needs the broker: a pass proves the
    # broker was installed and still alive, never merely that it was absent.
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _DEADLINE_WRAPPER,
            str(_SCRIPT),
            *flags,
            "--",
            sys.executable,
            "-c",
            _LATE_CHMOD,
            str(temp),
        ],
        capture_output=True,
        timeout=120,
        start_new_session=True,
        check=False,
        env=env,
    )


@linux_broker
def test_worker_lane_broker_has_no_overall_deadline(tmp_path: Path) -> None:
    worktree, home, worker_temp = _worker_layout(tmp_path)
    completed = _run_with_tiny_deadline(
        _landlock_flags(worktree, home, worker_temp),
        _sandbox_env(home, worker_temp),
        worker_temp,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert (worker_temp / "late.o").stat().st_mode & 0o777 == 0o750


@linux_broker
def test_validation_lane_still_enforces_the_patched_deadline(tmp_path: Path) -> None:
    # Positive control: the same patch DOES stop the validation lane, so the
    # worker-lane pass above is the absence of a deadline, not a dead patch.
    worktree, home, worker_temp = _worker_layout(tmp_path)
    scratch = tmp_path / "exec-scratch"
    scratch.mkdir(mode=0o700)
    flags = [
        *_landlock_flags(worktree, home, worker_temp),
        "--exec-scratch",
        str(scratch),
    ]
    # run_validations exports the exec scratch as the command's TMPDIR.
    completed = _run_with_tiny_deadline(flags, _sandbox_env(home, scratch), scratch)
    stderr = completed.stderr.decode(errors="replace")
    assert completed.returncode != 0, stderr
    assert "metadata_broker_deadline_exceeded" in stderr


# --------------------------------------------------------- worker-lane kill

# Runs inside a FRESH session leader (never pytest's own group). The child is
# not a session leader -- exactly the worker lane -- and forks a grandchild that
# killpg(child_pid) could never reach.
_KILL_GROUP_WRAPPER = r"""
import importlib.util, json, os, subprocess, sys, time
script = sys.argv[1]
sys.path.insert(0, os.path.dirname(script))
spec = importlib.util.spec_from_file_location("worker_workspace", script)
module = importlib.util.module_from_spec(spec)
sys.modules["worker_workspace"] = module
spec.loader.exec_module(module)
leader = os.getpgrp() == os.getpid()
child = subprocess.Popen(
    [sys.executable, "-c",
     "import subprocess, sys, time\n"
     "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
     "print(g.pid, flush=True)\n"
     "time.sleep(300)\n"],
    stdout=subprocess.PIPE,
)
grandchild = int(child.stdout.readline())
module._kill_worker_lane_group(child.pid)
child.wait(timeout=30)
def gone(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            return handle.read().rsplit(b")", 1)[1].split()[0] == b"Z"
    except FileNotFoundError:
        return True
deadline = time.monotonic() + 15
while not gone(grandchild) and time.monotonic() < deadline:
    time.sleep(0.05)
print(json.dumps({"leader": leader, "grandchild_gone": gone(grandchild)}))
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs Linux /proc")
def test_worker_lane_group_kill_reaches_grandchildren() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _KILL_GROUP_WRAPPER, str(_SCRIPT)],
        capture_output=True,
        timeout=120,
        start_new_session=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    result = json.loads(completed.stdout.decode().strip().splitlines()[-1])
    assert result == {"leader": True, "grandchild_gone": True}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs Linux signals")
def test_worker_lane_group_kill_survives_unreadable_proc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[int] = []

    def _no_proc(_path: str) -> list[str]:
        raise PermissionError("proc unreadable")

    monkeypatch.setattr(os, "kill", lambda pid, _sig: sent.append(pid))
    monkeypatch.setattr(os, "getpgrp", os.getpid)
    monkeypatch.setattr(os, "listdir", _no_proc)
    ww._kill_worker_lane_group(4242)
    assert sent == [4242]
