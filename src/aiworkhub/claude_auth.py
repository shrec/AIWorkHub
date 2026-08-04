"""Bounded, non-secret preflight for the first-party Claude subscription CLI.

Claude Code's subscription session is distinct from Copilot/VS Code Language
Model authorization.  AIWorkHub never copies either credential.  It asks the
installed CLI for its redacted status, caches the result briefly, and blocks a
launch before task claim when the subscription session is not usable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


CACHE_TTL_SECONDS = 300.0
RUNTIME_AUTH_FAILURE_TTL_SECONDS = 300.0
STATUS_TIMEOUT_SECONDS = 5.0
MAX_STATUS_BYTES = 16 * 1024
MAX_FAILURE_STATE_BYTES = 4 * 1024

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_runtime_failures: dict[str, tuple[float, int]] = {}
_EDITOR_LAUNCHER_NAMES = frozenset(
    {
        "code",
        "code-insiders",
        "codium",
        "vscodium",
    }
)


def _failure_state_path() -> Path:
    override = os.environ.get("AIWORKHUB_CLAUDE_AUTH_STATE_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".aiworkhub" / "runtime" / "claude_auth_failure.json"


def _executable_identity(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8", errors="surrogatepass")).hexdigest()


def _persist_runtime_failure(path: str, http_status: int) -> None:
    """Persist only non-secret circuit metadata across MCP/runtime reloads."""

    state_path = _failure_state_path()
    temporary = state_path.with_name(
        f".{state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    payload = {
        "schema_id": "aiworkhub.claude_auth_failure.v1",
        "executable_sha256": _executable_identity(path),
        "observed_at": time.time(),
        "http_status": http_status,
    }
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            state_path.parent.chmod(0o700)
        except OSError:
            pass
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state_path)
        try:
            state_path.chmod(0o600)
        except OSError:
            pass
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _persisted_runtime_failure(path: str) -> tuple[int, float] | None:
    state_path = _failure_state_path()
    try:
        raw = state_path.read_bytes()
    except OSError:
        return None
    if len(raw) > MAX_FAILURE_STATE_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
        observed_at = float(payload["observed_at"])
        http_status = int(payload["http_status"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if payload.get("schema_id") != "aiworkhub.claude_auth_failure.v1":
        return None
    if payload.get("executable_sha256") != _executable_identity(path):
        return None
    age = time.time() - observed_at
    if age < 0 or age >= RUNTIME_AUTH_FAILURE_TTL_SECONDS:
        try:
            state_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    if http_status not in {401, 403}:
        return None
    return http_status, age


def _is_editor_launcher(path: str) -> bool:
    """Reject editor CLIs before appending Claude-specific subcommands.

    Running ``code auth status --json`` makes VS Code interpret ``auth`` and
    ``status`` as files.  A stale override or host PATH collision must fail
    closed instead of repeatedly opening empty editor buffers.
    """

    name = Path(path).name.casefold()
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".sh"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name in _EDITOR_LAUNCHER_NAMES


def auth_status(executable: str | None = None, *, force: bool = False) -> dict[str, Any]:
    resolved = executable or shutil.which("claude") or ""
    if not resolved:
        return {
            "ok": False,
            "authenticated": False,
            "launchable": False,
            "status": "not_installed",
            "blocker_reason": "claude_executable_not_found",
        }
    try:
        path = str(Path(resolved).resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return {
            "ok": False,
            "authenticated": False,
            "launchable": False,
            "status": "not_installed",
            "blocker_reason": "claude_executable_invalid",
        }
    if _is_editor_launcher(str(resolved)) or _is_editor_launcher(path):
        return {
            "ok": False,
            "authenticated": False,
            "launchable": False,
            "status": "not_installed",
            "blocker_reason": "claude_executable_is_editor_launcher",
        }
    now = time.monotonic()
    with _lock:
        runtime_failure = _runtime_failures.get(path)
        if runtime_failure is not None:
            observed_at, http_status = runtime_failure
            if now - observed_at < RUNTIME_AUTH_FAILURE_TTL_SECONDS:
                return {
                    "ok": False,
                    "authenticated": False,
                    "launchable": False,
                    "status": "authentication_expired",
                    "blocker_reason": "claude_runtime_authentication_failed",
                    "http_status": http_status,
                    "runtime_observed": True,
                    "cache_hit": True,
                }
            _runtime_failures.pop(path, None)
        persisted_failure = _persisted_runtime_failure(path)
        if persisted_failure is not None:
            http_status, _age = persisted_failure
            return {
                "ok": False,
                "authenticated": False,
                "launchable": False,
                "status": "authentication_expired",
                "blocker_reason": "claude_runtime_authentication_failed",
                "http_status": http_status,
                "runtime_observed": True,
                "cache_hit": True,
                "persisted_runtime_observation": True,
            }
        cached = _cache.get(path)
        if not force and cached and now - cached[0] < CACHE_TTL_SECONDS:
            return {**cached[1], "cache_hit": True}
    try:
        completed = subprocess.run(
            [path, "auth", "status", "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=STATUS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = {
            "ok": False,
            "authenticated": False,
            "launchable": False,
            "status": "auth_status_unavailable",
            "blocker_reason": f"claude_auth_status_failed:{type(exc).__name__}",
            "cache_hit": False,
        }
    else:
        stdout = bytes(completed.stdout or b"")[:MAX_STATUS_BYTES]
        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        logged_in = bool(isinstance(payload, dict) and payload.get("loggedIn"))
        method = str(payload.get("authMethod") or "")[:80] if isinstance(payload, dict) else ""
        subscription = (
            str(payload.get("subscriptionType") or "")[:80]
            if isinstance(payload, dict)
            else ""
        )
        launchable = completed.returncode == 0 and logged_in
        result = {
            "ok": completed.returncode == 0,
            "authenticated": logged_in,
            "launchable": launchable,
            "status": "ready" if launchable else "authentication_required",
            "auth_method": method,
            "subscription_type": subscription,
            "blocker_reason": "" if launchable else "claude_authentication_required",
            "cache_hit": False,
        }
    with _lock:
        _cache[path] = (now, dict(result))
    return result


def record_runtime_auth_failure(
    executable: str | None = None, *, http_status: int = 401
) -> None:
    """Temporarily trip Claude readiness after an authoritative live 401/403.

    ``claude auth status`` reports the presence of a local login, not whether
    its OAuth access token can still complete a provider request.  The worker
    result stream is the stronger authority. Keep the circuit time-bounded and
    persist only a path hash, timestamp, and HTTP status so runtime reloads do
    not erase live evidence. AIWorkHub never copies, refreshes, or persists
    provider credentials.
    """

    resolved = executable or shutil.which("claude") or ""
    if not resolved:
        return
    try:
        path = str(Path(resolved).resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return
    status = int(http_status) if int(http_status) in {401, 403} else 401
    with _lock:
        _cache.pop(path, None)
        _runtime_failures[path] = (time.monotonic(), status)
    _persist_runtime_failure(path, status)


def invalidate() -> None:
    with _lock:
        _cache.clear()
        _runtime_failures.clear()


__all__ = ["auth_status", "invalidate", "record_runtime_auth_failure"]
