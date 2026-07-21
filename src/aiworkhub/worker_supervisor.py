"""Durable timeout/cancel supervisor for one sandboxed model process."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, BinaryIO


POLL_SECONDS = 0.1
KILL_GRACE_SECONDS = 5.0
_PR_SET_PDEATHSIG = 1
# Token-free liveness contract (B412): the supervisor -- never the model --
# atomically refreshes this owner-only status artifact on a fixed cadence
# while the child runs. Heartbeat cadence is deliberately independent of any
# model turn, dashboard read, or MCP request.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0


def _write_json_0600(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        os.close(fd)
        temp.unlink(missing_ok=True)


def _open_0600(path: Path) -> BinaryIO:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "ab", buffering=0)


def _terminate_child(child: subprocess.Popen[bytes], grace: float = KILL_GRACE_SECONDS) -> int:
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return int(child.wait())
    try:
        return int(child.wait(timeout=grace))
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return int(child.wait(timeout=grace))


def _unlink_if_regular(path: Path) -> None:
    """Remove ``path`` only if it exists and is not a symlink.

    Defense-in-depth for the spec-file cleanup below: ``unlink(2)`` never
    dereferences a symlink for removal, but an auditor-flagged short-lived
    attacker-writable-looking filename should still refuse to act on one if
    the expected regular file was ever replaced by a symlink.
    """
    try:
        if path.is_symlink():
            return
        path.unlink(missing_ok=True)
    except OSError:
        return


def _load_spec(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode & 0o077:
            raise ValueError(f"insecure_spec_mode:{mode:o}")
        with os.fdopen(fd, "r", closefd=False, encoding="utf-8") as fh:
            payload = json.loads(fh.read())
    finally:
        os.close(fd)
    if not isinstance(payload, dict):
        raise ValueError("invalid_spec_object")
    return payload


def _validated_argv(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("invalid_worker_argv")
    if any(not isinstance(item, str) or not item or "\x00" in item for item in value):
        raise ValueError("invalid_worker_argv_item")
    return list(value)


def _die_with_supervisor() -> None:
    """Ensure an abruptly killed supervisor cannot orphan its worker."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        os._exit(126)
    if os.getppid() == 1:
        os.kill(os.getpid(), signal.SIGKILL)


def _pid_start_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        _head, separator, tail = raw.rpartition(")")
        if not separator:
            return None
        return int(tail.strip().split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _file_size(handle: BinaryIO) -> int:
    try:
        return os.fstat(handle.fileno()).st_size
    except OSError:
        return 0


def supervise(spec: dict[str, Any]) -> int:
    argv = _validated_argv(spec.get("argv"))
    cwd = str(spec["cwd"])
    timeout = int(spec["timeout_seconds"])
    if timeout < 1 or timeout > 86_400:
        raise ValueError("timeout_out_of_range")
    status_path = Path(str(spec["status_path"]))
    cancel_path = Path(str(spec["cancel_path"]))
    stdout_path = Path(str(spec["stdout_path"]))
    stderr_path = Path(str(spec["stderr_path"]))
    try:
        heartbeat_interval = float(spec.get("heartbeat_interval_seconds") or DEFAULT_HEARTBEAT_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        heartbeat_interval = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    heartbeat_interval = max(0.05, min(heartbeat_interval, 3600.0))
    supervisor_pid = os.getpid()
    supervisor_pid_start_ticks = _pid_start_ticks(supervisor_pid)
    started_epoch = time.time()
    deadline_epoch = started_epoch + timeout
    deadline_monotonic = time.monotonic() + timeout
    cancel_requested = False
    child: subprocess.Popen[bytes] | None = None

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal cancel_requested
        cancel_requested = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    _write_json_0600(status_path, {
        "state": "starting",
        "supervisor_pid": supervisor_pid,
        "supervisor_pid_start_ticks": supervisor_pid_start_ticks,
        "started_at_epoch": started_epoch,
        "deadline_epoch": deadline_epoch,
        "timeout_seconds": timeout,
    })

    try:
        with _open_0600(stdout_path) as out, _open_0600(stderr_path) as err:
            try:
                child = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=os.environ.copy(),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    shell=False,
                    start_new_session=True,
                    preexec_fn=_die_with_supervisor,
                )
            except Exception as exc:
                _write_json_0600(status_path, {
                    "state": "spawn_failed",
                    "supervisor_pid": supervisor_pid,
                    "exit_code": 126,
                    "error": f"{type(exc).__name__}:{exc}"[:500],
                    "started_at_epoch": started_epoch,
                    "finished_at_epoch": time.time(),
                    "timeout_seconds": timeout,
                })
                return 126

            child_start_ticks = _pid_start_ticks(child.pid)
            heartbeat_seq = 0
            last_stdout_bytes = _file_size(out)
            last_stderr_bytes = _file_size(err)
            last_output_change_epoch = started_epoch
            next_heartbeat_monotonic = time.monotonic()
            _write_json_0600(status_path, {
                "state": "running",
                "supervisor_pid": supervisor_pid,
                "supervisor_pid_start_ticks": supervisor_pid_start_ticks,
                "child_pid": child.pid,
                "child_pid_start_ticks": child_start_ticks,
                "started_at_epoch": started_epoch,
                "deadline_epoch": deadline_epoch,
                "timeout_seconds": timeout,
                "heartbeat_seq": heartbeat_seq,
                "heartbeat_at_epoch": time.time(),
                "stdout_bytes": last_stdout_bytes,
                "stderr_bytes": last_stderr_bytes,
                "last_output_change_epoch": last_output_change_epoch,
            })
            final_state = "exited"
            while True:
                returncode = child.poll()
                if returncode is not None:
                    break
                if cancel_requested or cancel_path.exists():
                    final_state = "cancelled"
                    returncode = _terminate_child(child)
                    break
                if time.monotonic() >= deadline_monotonic:
                    final_state = "timed_out"
                    returncode = _terminate_child(child)
                    break
                now_monotonic = time.monotonic()
                if now_monotonic >= next_heartbeat_monotonic:
                    # Heartbeat is a supervisor-owned liveness signal only --
                    # it never touches task lifecycle/updated_at and is
                    # orthogonal to "state" (semantic progress), which is
                    # still set exclusively by the transitions above/below.
                    stdout_bytes = _file_size(out)
                    stderr_bytes = _file_size(err)
                    if stdout_bytes != last_stdout_bytes or stderr_bytes != last_stderr_bytes:
                        last_output_change_epoch = time.time()
                        last_stdout_bytes = stdout_bytes
                        last_stderr_bytes = stderr_bytes
                    heartbeat_seq += 1
                    _write_json_0600(status_path, {
                        "state": "running",
                        "supervisor_pid": supervisor_pid,
                        "supervisor_pid_start_ticks": supervisor_pid_start_ticks,
                        "child_pid": child.pid,
                        "child_pid_start_ticks": child_start_ticks,
                        "started_at_epoch": started_epoch,
                        "deadline_epoch": deadline_epoch,
                        "timeout_seconds": timeout,
                        "heartbeat_seq": heartbeat_seq,
                        "heartbeat_at_epoch": time.time(),
                        "stdout_bytes": last_stdout_bytes,
                        "stderr_bytes": last_stderr_bytes,
                        "last_output_change_epoch": last_output_change_epoch,
                    })
                    next_heartbeat_monotonic = now_monotonic + heartbeat_interval
                time.sleep(POLL_SECONDS)

            _write_json_0600(status_path, {
                "state": final_state,
                "supervisor_pid": supervisor_pid,
                "supervisor_pid_start_ticks": supervisor_pid_start_ticks,
                "child_pid": child.pid,
                "child_pid_start_ticks": child_start_ticks,
                "exit_code": returncode,
                "started_at_epoch": started_epoch,
                "finished_at_epoch": time.time(),
                "timeout_seconds": timeout,
                "heartbeat_seq": heartbeat_seq,
                "heartbeat_at_epoch": time.time(),
                "stdout_bytes": _file_size(out),
                "stderr_bytes": _file_size(err),
                "last_output_change_epoch": last_output_change_epoch,
            })
    except Exception as exc:
        if child is not None and child.poll() is None:
            _terminate_child(child)
        _write_json_0600(status_path, {
            "state": "supervisor_error",
            "supervisor_pid": supervisor_pid,
            "supervisor_pid_start_ticks": supervisor_pid_start_ticks,
            "child_pid": child.pid if child is not None else None,
            "exit_code": 126,
            "error": f"{type(exc).__name__}:{exc}"[:500],
            "started_at_epoch": started_epoch,
            "finished_at_epoch": time.time(),
            "timeout_seconds": timeout,
        })
        return 126
    finally:
        cancel_path.unlink(missing_ok=True)

    if final_state == "cancelled":
        return 125
    if final_state == "timed_out":
        return 124
    return int(returncode)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    args = parser.parse_args()
    spec_path = Path(args.spec)
    try:
        spec = _load_spec(spec_path)
    except Exception as exc:
        print(f"invalid supervisor spec: {exc}", file=sys.stderr)
        raise SystemExit(126)
    finally:
        _unlink_if_regular(spec_path)
    try:
        code = supervise(spec)
    except Exception as exc:
        status_raw = spec.get("status_path")
        if status_raw:
            try:
                _write_json_0600(Path(str(status_raw)), {
                    "state": "supervisor_error",
                    "supervisor_pid": os.getpid(),
                    "exit_code": 126,
                    "error": f"{type(exc).__name__}:{exc}"[:500],
                    "finished_at_epoch": time.time(),
                })
            except OSError:
                pass
        raise SystemExit(126)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
