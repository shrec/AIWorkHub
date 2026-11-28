"""Compatibility relay for AppContainer validations (no session hop, no drive).

NF-2026-01023 (owner decision 2026-11-28): AIWorkHub never creates a virtual
drive letter, so the private NewCredentials logon-session hop that existed
only to hide that letter from the owner's session is gone.  AppContainer
children run on real worktree paths in the caller's own session.

What remains, and why: finalization may still start
``python -P -c _VALIDATION_RELAY_ENTRY`` with one relay spec on stdin.  That
entry point is kept so such a caller keeps working, but it now runs
``worker_workspace._run_appcontainer_validation`` directly in this process
and writes exactly one result record to stdout.  ``run_appcontainer_validation_hopped``
is kept as a direct delegate for the same reason; production
``run_validations`` calls the AppContainer runner directly.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

__all__ = [
    "run_appcontainer_validation_hopped",
    "validation_relay_main",
]


_VALIDATION_RELAY_SCHEMA = "aiworkhub.appcontainer_validation_relay.v1"
# A spec on stdin is bounded: a larger one is a failure, never a truncation.
_MAX_VALIDATION_RELAY_BYTES = 256 * 1024 * 1024
_VALIDATION_RELAY_ENTRY = (
    "from aiworkhub.windows_private_session import validation_relay_main as main;"
    " raise SystemExit(main())"
)


class ValidationRelayError(RuntimeError):
    """A relay spec could not be read; reported as an ``oserror`` record."""


def run_appcontainer_validation_hopped(
    argv: list[str],
    *,
    workspace: Any,
    adapter_id: str,
    cwd: str | Path,
    env: Mapping[str, str],
    timeout_seconds: int,
    run_direct: Callable[..., subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    """Call ``run_direct`` with exactly these arguments: there is no hop."""

    return run_direct(
        argv,
        workspace=workspace,
        adapter_id=adapter_id,
        cwd=cwd,
        env=env,
        timeout_seconds=timeout_seconds,
    )


def _relay_failure(reason: str, exit_code: int, diagnostic: bytes) -> OSError:
    tail = diagnostic.decode("utf-8", errors="replace").strip()
    return OSError(f"windows_private_session_validation_{reason}:exit={exit_code}:{tail}")


def _relayed_text(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("relayed stream is not text")
    return value


def _relayed_validation_result(
    exit_code: int, result: tuple[bytes, bool] | None, diagnostic: bytes
) -> subprocess.CompletedProcess[str]:
    """Map one :func:`validation_relay_main` record back onto
    ``subprocess.run``'s shapes, for a caller that started the relay entry.

    Fails closed: a relay that did not exit 0, or a missing, oversized,
    unparsable or unknown record, is an :class:`OSError`, never a pass.
    """

    if exit_code != 0:
        raise _relay_failure("relay_exit_nonzero", exit_code, diagnostic)
    if result is None or not result[0]:
        raise _relay_failure("result_missing", exit_code, diagnostic)
    raw, oversized = result
    if oversized:
        raise _relay_failure("result_oversized", exit_code, diagnostic)
    try:
        record = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise _relay_failure("result_unparsable", exit_code, diagnostic) from exc
    if not isinstance(record, dict) or record.get("schema_id") != _VALIDATION_RELAY_SCHEMA:
        raise _relay_failure("result_unparsable", exit_code, diagnostic)
    kind = record.get("kind")
    try:
        if kind == "completed":
            return subprocess.CompletedProcess(
                [str(part) for part in record["args"]],
                int(record["returncode"]),
                _relayed_text(record["stdout"]),
                _relayed_text(record["stderr"]),
            )
        if kind == "stalled":
            from .validation_stall import ValidationStalled

            raise ValidationStalled(
                [str(part) for part in record["args"]],
                float(record["stall_seconds"]),
                output=_relayed_text(record["stdout"]),
                stderr=_relayed_text(record["stderr"]),
            )
        if kind == "timeout":
            raise subprocess.TimeoutExpired(
                [str(part) for part in record["args"]],
                float(record["timeout"]),
                output=_relayed_text(record["stdout"]),
                stderr=_relayed_text(record["stderr"]),
            )
        if kind == "oserror":
            errno_value, strerror = record.get("errno"), record.get("strerror")
            if isinstance(errno_value, int) and isinstance(strerror, str):
                raise OSError(errno_value, strerror)
            raise OSError(_relayed_text(record["message"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise _relay_failure("result_unparsable", exit_code, diagnostic) from exc
    raise _relay_failure("result_kind_unknown", exit_code, diagnostic)


def _captured_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _relayed_args(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(part) for part in value]
    return [str(value)]


def _run_relayed_validation(
    raw: bytes, run_direct: Callable[..., subprocess.CompletedProcess[str]] | None = None
) -> dict[str, Any]:
    if len(raw) > _MAX_VALIDATION_RELAY_BYTES:
        raise ValidationRelayError("validation_relay_spec_oversized")
    spec = json.loads(raw.decode("utf-8"))
    if not isinstance(spec, dict) or spec.get("schema_id") != _VALIDATION_RELAY_SCHEMA:
        raise ValidationRelayError("validation_relay_spec_schema_mismatch")
    from .validation_stall import ValidationStalled
    from .worker_workspace import WorkerWorkspace, _run_appcontainer_validation

    runner = run_direct if run_direct is not None else _run_appcontainer_validation
    try:
        completed = runner(
            [str(part) for part in spec["argv"]],
            workspace=WorkerWorkspace.from_metadata(spec["workspace"]),
            adapter_id=str(spec["adapter_id"]),
            cwd=str(spec["cwd"]),
            env={str(key): str(value) for key, value in dict(spec["env"]).items()},
            timeout_seconds=spec["timeout_seconds"],
        )
    except ValidationStalled as exc:
        # NF-2026-02269: a hang the stall watchdog ended stays a hang.
        return {
            "kind": "stalled",
            "args": _relayed_args(exc.cmd),
            "stall_seconds": exc.stall_seconds,
            "stdout": _captured_text(exc.output),
            "stderr": _captured_text(exc.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "kind": "timeout",
            "args": _relayed_args(exc.cmd),
            "timeout": exc.timeout,
            "stdout": _captured_text(exc.output),
            "stderr": _captured_text(exc.stderr),
        }
    except OSError as exc:
        return {
            "kind": "oserror",
            "errno": exc.errno,
            "strerror": exc.strerror,
            "message": str(exc),
        }
    return {
        "kind": "completed",
        "args": _relayed_args(completed.args),
        "returncode": completed.returncode,
        "stdout": _captured_text(completed.stdout),
        "stderr": _captured_text(completed.stderr),
    }


def validation_relay_main() -> int:
    """Run one relayed AppContainer validation directly in this process.

    Reads the spec from stdin and writes exactly one result record to stdout;
    anything else this process prints goes to stderr, so it cannot corrupt the
    record.  Any fault becomes an ``oserror`` record: a fault is a failed
    launch, never a pass.
    """

    result_stream = sys.stdout.buffer
    sys.stdout = sys.stderr
    try:
        record = _run_relayed_validation(
            sys.stdin.buffer.read(_MAX_VALIDATION_RELAY_BYTES + 1)
        )
    except Exception as exc:
        record = {
            "kind": "oserror",
            "errno": None,
            "strerror": None,
            "message": (
                "windows_private_session_validation_relay_failed:"
                f"{type(exc).__name__}:{exc}"
            ),
        }
    record["schema_id"] = _VALIDATION_RELAY_SCHEMA
    result_stream.write(json.dumps(record, sort_keys=True).encode("utf-8"))
    result_stream.flush()
    return 0
