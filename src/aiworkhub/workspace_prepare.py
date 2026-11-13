"""Repo hook ``workspace_prepare`` (NF-2026-02238).

``.aiworkhub/config/project.json`` may declare::

    {"workspace_prepare": ["python", "tools/prepare.py", "--root", "{worktree}"]}

an argv list (never a shell string) that the coordinator runs once after a
worker worktree is created and before the worker launches:

* every ``{worktree}`` in every argument is replaced with the worktree path;
* ``cwd`` is the worktree; ``stdin`` is closed; stdout and stderr are merged
  into a request-scoped temporary file of which only a bounded tail
  (``TAIL_BYTES``) is kept;
* it runs outside the worker sandbox, in its own process group, with an
  allowlisted environment (``ENV_ALLOWLIST``) built from the coordinator's own
  environment -- never the worker's isolated HOME, provider env or
  credentials, and no ``*_API_KEY`` or token variable reaches it;
* it is bounded by ``TIMEOUT_SECONDS``; on timeout the whole process group is
  killed;
* its receipt (status, exit code, duration, output tail) is written next to
  the request's process logs, keyed by the worktree, so a second launch on the
  same worktree reuses a succeeded receipt instead of running it twice;
* any status other than ``succeeded`` blocks the launch with
  ``workspace_prepare_<status>:...`` carrying the exit code and the tail.

No hook configured is ``not_configured``; a configured hook that cannot be
read or parsed blocks the launch (``workspace_prepare_invalid:<reason>``)
rather than being skipped.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import platform_io

CONFIG_RELATIVE_PATH = (".aiworkhub", "config", "project.json")
CONFIG_KEY = "workspace_prepare"
PLACEHOLDER = "{worktree}"
MAX_CONFIG_BYTES = 1024 * 1024
MAX_ARGV = 64
MAX_ARG_CHARS = 4096
TIMEOUT_SECONDS = 600.0
TAIL_BYTES = 4096
_ERROR_TAIL_CHARS = 200
RECEIPT_SCHEMA_ID = "aiworkhub.workspace_prepare.v1"
ENV_ALLOWLIST = frozenset({
    "PATH", "PATHEXT", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ", "HOME",
    "USER", "LOGNAME", "USERNAME", "USERPROFILE", "SYSTEMROOT", "SYSTEMDRIVE",
    "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
})


class WorkspacePrepareError(ValueError):
    """Typed refusal: ``workspace_prepare_<reason>:<detail>``."""

    def __init__(
        self, reason: str, detail: object = "", receipt: Mapping[str, Any] | None = None
    ) -> None:
        self.reason = reason
        self.receipt = dict(receipt) if receipt is not None else None
        super().__init__(f"workspace_prepare_{reason}:{str(detail)[:400]}")


def _invalid(reason: str, detail: object = "") -> WorkspacePrepareError:
    return WorkspacePrepareError(f"invalid:{reason}", detail)


def load_hook(repo: str | os.PathLike[str]) -> list[str] | None:
    """The declared argv, or ``None`` when no hook is configured."""
    path = Path(repo).joinpath(*CONFIG_RELATIVE_PATH)
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file():
        raise _invalid("config_not_a_regular_file", path.name)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        raise _invalid("config_unreadable", type(exc).__name__) from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise _invalid("config_too_large", f">{MAX_CONFIG_BYTES}")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _invalid("config_not_json", type(exc).__name__) from exc
    if not isinstance(document, dict):
        raise _invalid("config_not_an_object", type(document).__name__)
    hook = document.get(CONFIG_KEY)
    if hook is None:
        return None
    return validate_hook(hook)


def validate_hook(hook: Any) -> list[str]:
    if not isinstance(hook, list):
        raise _invalid("not_an_argv_list", type(hook).__name__)
    if not hook or len(hook) > MAX_ARGV:
        raise _invalid("argv_length", f"{len(hook)} not in 1..{MAX_ARGV}")
    for arg in hook:
        if not isinstance(arg, str):
            raise _invalid("argument_not_a_string", type(arg).__name__)
        if "\x00" in arg:
            raise _invalid("nul_character", "")
        if len(arg) > MAX_ARG_CHARS:
            raise _invalid("argument_too_long", f">{MAX_ARG_CHARS}")
    if not hook[0].strip():
        raise _invalid("empty_executable", "")
    return list(hook)


def substitute(argv: Sequence[str], worktree: str | os.PathLike[str]) -> list[str]:
    root = os.fspath(worktree)
    return [arg.replace(PLACEHOLDER, root) for arg in argv]


def hook_env(
    base: Mapping[str, str] | None = None, *, worktree: str | os.PathLike[str]
) -> dict[str, str]:
    """The allowlisted coordinator environment plus ``AIWORKHUB_WORKTREE``."""
    source = os.environ if base is None else base
    env = {key: value for key, value in source.items() if key.upper() in ENV_ALLOWLIST}
    env["AIWORKHUB_WORKTREE"] = os.fspath(worktree)
    return env


def _group_kwargs() -> dict[str, Any]:
    if platform_io.is_windows():
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_tree(process: Any) -> None:
    if platform_io.is_windows():
        taskkill = os.path.join(
            os.environ.get("SystemRoot") or r"C:\Windows", "System32", "taskkill.exe"
        )
        try:
            subprocess.run(
                [taskkill, "/T", "/F", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.kill()
        except OSError:
            pass
        return
    try:
        platform_io.signal_process_group(process.pid, graceful=False)
    except (ProcessLookupError, PermissionError):
        pass


def _tail(sink: Any) -> tuple[str, int]:
    sink.seek(0, os.SEEK_END)
    size = sink.tell()
    sink.seek(max(0, size - TAIL_BYTES))
    return sink.read(TAIL_BYTES).decode("utf-8", errors="replace"), size


def run_hook(
    argv: Sequence[str],
    worktree: str | os.PathLike[str],
    *,
    timeout: float = TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run the substituted argv once and return its receipt.

    Spawn failure, non-zero exit and timeout are receipts; only an ``OSError``
    from the temporary output sink propagates (``prepare_once`` types it).
    """
    started = clock()
    receipt: dict[str, Any] = {
        "schema_id": RECEIPT_SCHEMA_ID,
        "status": "spawn_failed",
        "returncode": None,
        "argv0": str(argv[0])[:240] if argv else "",
        "argc": len(argv),
        "timeout_seconds": timeout,
        "output_bytes": 0,
        "output_tail": "",
    }
    with tempfile.TemporaryFile() as sink:
        try:
            process = popen(
                list(argv),
                cwd=os.fspath(worktree),
                env=dict(env if env is not None else hook_env(worktree=worktree)),
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                shell=False,
                **_group_kwargs(),
            )
        except (OSError, ValueError) as exc:
            receipt["output_tail"] = f"{type(exc).__name__}: {exc}"[:TAIL_BYTES]
            receipt["duration_ms"] = int((clock() - started) * 1000)
            return receipt
        try:
            returncode = process.wait(timeout=timeout)
            receipt["status"] = "succeeded" if returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            returncode = process.wait()
            receipt["status"] = "timed_out"
        if not platform_io.is_windows():
            _kill_tree(process)  # reap anything the hook left in its group
        receipt["returncode"] = returncode
        receipt["output_tail"], receipt["output_bytes"] = _tail(sink)
    receipt["duration_ms"] = int((clock() - started) * 1000)
    return receipt


def receipt_path(state_dir: str | os.PathLike[str], worktree: str | os.PathLike[str]) -> Path:
    key = hashlib.sha256(os.path.realpath(os.fspath(worktree)).encode("utf-8")).hexdigest()
    return Path(state_dir) / f"workspace-prepare-{key[:32]}.json"


def _read_receipt(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    """Atomic 0600 write; ``OSError`` propagates after the temp is removed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, sort_keys=True)
        os.replace(temp, path)
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def prepare_once(
    repo: str | os.PathLike[str],
    worktree: str | os.PathLike[str],
    *,
    state_dir: str | os.PathLike[str],
    run: Callable[..., dict[str, Any]] = run_hook,
) -> dict[str, Any]:
    """Run the repo hook once for ``worktree``; raise unless it succeeded.

    An ``OSError`` from the hook's temporary output sink or from writing the
    receipt is a typed refusal too (``workspace_prepare_failed:<ErrName>``),
    never an untyped launch crash.
    """
    argv = load_hook(repo)
    if argv is None:
        return {"schema_id": RECEIPT_SCHEMA_ID, "status": "not_configured"}
    marker = receipt_path(state_dir, worktree)
    prior = _read_receipt(marker)
    if prior is not None and prior.get("status") == "succeeded":
        return {**prior, "reused": True}
    receipt: dict[str, Any] | None = None
    try:
        receipt = run(substitute(argv, worktree), worktree)
        _write_receipt(marker, receipt)
    except OSError as exc:
        raise WorkspacePrepareError("failed", type(exc).__name__, receipt) from exc
    status = str(receipt.get("status") or "unknown")
    if status != "succeeded":
        tail = str(receipt.get("output_tail") or "")[-_ERROR_TAIL_CHARS:]
        raise WorkspacePrepareError(
            status, f"exit={receipt.get('returncode')}:{tail}", receipt
        )
    return receipt
