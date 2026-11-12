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
from collections import deque
from pathlib import Path
from typing import Any


# Only a launchable result is cached (a negative one never is). It is served
# without re-probing for CACHE_REFRESH_SECONDS, and until CACHE_TTL_SECONDS it
# may still vouch for a launch whose re-probe merely timed out.
CACHE_TTL_SECONDS = 300.0
CACHE_REFRESH_SECONDS = 120.0
RUNTIME_AUTH_FAILURE_TTL_SECONDS = 300.0
# `claude auth status` was measured at 8.7 s wall (0.18 s CPU) on a loaded
# 16-core host while reporting loggedIn=true. Probe bounds are derived from
# recently measured latency and never drop below a floor with headroom over it.
MEASURED_LOADED_STATUS_LATENCY_SECONDS = 8.7
STATUS_LATENCY_HEADROOM = 2.0
STATUS_TIMEOUT_FLOOR_SECONDS = 20.0
STATUS_TIMEOUT_CEILING_SECONDS = 90.0
STATUS_LATENCY_SAMPLES = 8
MAX_PROBE_LATENCY_PATHS = 16
STATUS_RETRY_BACKOFF_SECONDS = 1.0
STATUS_TRANSIENT_RETRY_AFTER_SECONDS = 30.0
MAX_STATUS_BYTES = 16 * 1024
MAX_FAILURE_STATE_BYTES = 4 * 1024
MAX_SESSION_ID_HASH_BYTES = 64
RUNTIME_AUTH_FAILURE_REASON = "claude_subscription_session_refresh_required"

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_runtime_failures: dict[str, tuple[float, int, str]] = {}
_probe_latencies: dict[str, deque[float]] = {}
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


def _session_identity(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8", errors="surrogatepass")).hexdigest()


def _classify_runtime_auth_failure(
    *, http_status: object, error_code: object, session_id: object
) -> tuple[int, str] | None:
    if type(http_status) is not int:
        return None
    # Status 0 is "the provider reported no HTTP status": a mid-run OAuth expiry
    # arrives as a structured authentication_failed with api_error_status null.
    if http_status not in (0, 401):
        return None
    if error_code != "authentication_failed":
        return None
    if not isinstance(session_id, str) or not session_id:
        return None
    return http_status, _session_identity(session_id)[:MAX_SESSION_ID_HASH_BYTES]


def classify_runtime_auth_failure(
    *, http_status: object, error_code: object, session_id: object
) -> dict[str, Any] | None:
    classified = _classify_runtime_auth_failure(
        http_status=http_status,
        error_code=error_code,
        session_id=session_id,
    )
    if classified is None:
        return None
    status, session_hash = classified
    return {
        "http_status": status,
        "session_id_sha256": session_hash,
    }


def _clear_persisted_runtime_failure(path: str) -> None:
    state_path = _failure_state_path()
    try:
        raw = state_path.read_bytes()
    except OSError:
        return
    if len(raw) > MAX_FAILURE_STATE_BYTES:
        return
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if payload.get("executable_sha256") != _executable_identity(path):
        return
    try:
        state_path.unlink(missing_ok=True)
    except OSError:
        pass


def _persist_runtime_failure(path: str, http_status: int, session_hash: str) -> None:
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
        "session_id_sha256": session_hash,
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


def _persisted_runtime_failure(path: str) -> tuple[int, float, str] | None:
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
        http_status = payload["http_status"]
        session_hash = payload["session_id_sha256"]
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
    if type(http_status) is not int or http_status != 401:
        return None
    if not isinstance(session_hash, str) or not session_hash:
        return None
    if (
        len(session_hash.encode("utf-8", errors="surrogatepass"))
        > MAX_SESSION_ID_HASH_BYTES
    ):
        return None
    return http_status, age, session_hash


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


def _status_probe_unavailable(
    path: str, failure: str, attempts: list[dict[str, Any]]
) -> dict[str, Any]:
    """The probe could not run at all: fail closed and never cache it."""

    return {
        "ok": False,
        "authenticated": False,
        "launchable": False,
        "status": "auth_status_unavailable",
        "blocker_reason": f"claude_auth_status_failed:{failure}",
        "cache_hit": False,
        "executable_path": path,
        "probe_attempts": attempts,
    }


def _status_probe_transient(path: str, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """Every bounded attempt timed out: the login state is unknown, not absent.

    Unknown never maps to launchable, but it is reported as a transient probe
    condition with a retry hint instead of an authentication failure.
    """

    return {
        "ok": False,
        "authenticated": False,
        "launchable": False,
        "status": "auth_status_transient",
        "transient": True,
        "blocker_reason": "claude_auth_status_transient:TimeoutExpired",
        "retry_after_seconds": STATUS_TRANSIENT_RETRY_AFTER_SECONDS,
        "cache_hit": False,
        "executable_path": path,
        "probe_attempts": attempts,
    }


def _record_probe_latency(path: str, seconds: float) -> None:
    with _lock:
        samples = _probe_latencies.pop(path, None)
        if samples is None:
            samples = deque(maxlen=STATUS_LATENCY_SAMPLES)
        samples.append(max(0.0, float(seconds)))
        # Re-insert as most recent; evict the least recently probed executable.
        _probe_latencies[path] = samples
        while len(_probe_latencies) > MAX_PROBE_LATENCY_PATHS:
            _probe_latencies.pop(next(iter(_probe_latencies)))


def status_probe_timeouts(path: str) -> tuple[float, float]:
    """Return the (first, retry) probe bounds derived from measured latency.

    The first bound is ``STATUS_LATENCY_HEADROOM`` times the slowest recently
    completed probe, never below ``STATUS_TIMEOUT_FLOOR_SECONDS`` and never
    above ``STATUS_TIMEOUT_CEILING_SECONDS``; the retry doubles it within the
    same ceiling.
    """

    with _lock:
        samples = tuple(_probe_latencies.get(path, ()))
    observed = max(samples, default=0.0)
    first = min(
        STATUS_TIMEOUT_CEILING_SECONDS,
        max(STATUS_TIMEOUT_FLOOR_SECONDS, STATUS_LATENCY_HEADROOM * observed),
    )
    return first, min(STATUS_TIMEOUT_CEILING_SECONDS, 2.0 * first)


def _probe_auth_status(path: str) -> dict[str, Any]:
    """Run Claude's bounded local status probe without changing failure circuits.

    The CLI's status call is I/O and scheduler bound: on a loaded host it was
    measured at 8.7 s wall for 0.18 s CPU. The bounds therefore come from
    ``status_probe_timeouts``; a timeout is retried once after a short backoff
    with a longer bound, and if every attempt times out the result is
    *transient*, never an authentication failure. Each attempt's timeout and
    elapsed wall time are reported so a remaining failure is measurable.
    """

    attempts: list[dict[str, Any]] = []
    completed = None
    for index, timeout in enumerate(status_probe_timeouts(path)):
        if index:
            time.sleep(STATUS_RETRY_BACKOFF_SECONDS)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [path, "auth", "status", "--json"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failure = type(exc).__name__
            attempts.append(
                {
                    "timeout_seconds": timeout,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "outcome": failure,
                }
            )
            if isinstance(exc, subprocess.TimeoutExpired):
                continue
            return _status_probe_unavailable(path, failure, attempts)
        _record_probe_latency(path, time.monotonic() - started)
        break
    if completed is None:
        return _status_probe_transient(path, attempts)
    stdout = bytes(completed.stdout or b"")[:MAX_STATUS_BYTES]
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    logged_in = bool(isinstance(payload, dict) and payload.get("loggedIn"))
    method = (
        str(payload.get("authMethod") or "")[:80]
        if isinstance(payload, dict)
        else ""
    )
    subscription = (
        str(payload.get("subscriptionType") or "")[:80]
        if isinstance(payload, dict)
        else ""
    )
    launchable = completed.returncode == 0 and logged_in
    return {
        "ok": completed.returncode == 0,
        "authenticated": logged_in,
        "launchable": launchable,
        "status": "ready" if launchable else "authentication_required",
        "auth_method": method,
        "subscription_type": subscription,
        "blocker_reason": "" if launchable else "claude_authentication_required",
        "cache_hit": False,
    }


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
            observed_at, http_status, _session_hash = runtime_failure
            if now - observed_at < RUNTIME_AUTH_FAILURE_TTL_SECONDS:
                return {
                    "ok": False,
                    "authenticated": False,
                    "launchable": False,
                    "status": "authentication_expired",
                    "blocker_reason": RUNTIME_AUTH_FAILURE_REASON,
                    "http_status": http_status,
                    "runtime_observed": True,
                    "cache_hit": True,
                }
            _runtime_failures.pop(path, None)
        persisted_failure = None if force else _persisted_runtime_failure(path)
        if persisted_failure is not None:
            http_status, _age, _session_hash = persisted_failure
            return {
                "ok": False,
                "authenticated": False,
                "launchable": False,
                "status": "authentication_expired",
                "blocker_reason": RUNTIME_AUTH_FAILURE_REASON,
                "http_status": http_status,
                "runtime_observed": True,
                "cache_hit": True,
                "persisted_runtime_observation": True,
            }
        # Only a launchable result is ever cached; within the refresh window it
        # is served without re-probing.
        cached = _cache.get(path)
        if (
            not force
            and cached is not None
            and 0 <= now - cached[0] < CACHE_REFRESH_SECONDS
        ):
            return {**cached[1], "cache_hit": True}
    result = _probe_auth_status(path)
    finished = time.monotonic()
    with _lock:
        if result.get("launchable") is True:
            _runtime_failures.pop(path, None)
            _clear_persisted_runtime_failure(path)
            _cache[path] = (now, dict(result))
            return result
        cached = _cache.get(path)
        if result.get("transient") is True and cached is not None:
            # A probe that merely timed out says nothing new about the login: a
            # positive observed within the bounded TTL still vouches for it.
            # The cache entry keeps its original timestamp so the TTL bounds
            # how long the fallback can be used.
            age = finished - cached[0]
            if 0 <= age < CACHE_TTL_SECONDS:
                return {
                    **cached[1],
                    "cache_hit": True,
                    "probe_transient": True,
                    "cached_age_seconds": round(age, 3),
                    "probe_attempts": list(result.get("probe_attempts") or []),
                }
        # A negative or expired result is never cached and evicts any positive.
        _cache.pop(path, None)
    return result


def refresh_subscription_session_for_retry(
    executable: str | None = None,
) -> dict[str, Any]:
    """Ask the host CLI to refresh OAuth before copying credentials to a worker.

    This deliberately bypasses cached/persisted failure circuits, but it does
    not clear them. Only a successful retried provider request may do that.
    """

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
    return _probe_auth_status(path)


def record_runtime_auth_failure(
    executable: str | None = None,
    *,
    http_status: object = 401,
    error_code: object = "authentication_failed",
    session_id: object = "",
) -> bool:
    """Temporarily trip Claude readiness after an authoritative live 401.

    ``claude auth status`` reports the presence of a local login, not whether
    its OAuth access token can still complete a provider request.  The worker
    result stream is the stronger authority when it supplies the exact bounded
    non-secret failure receipt. Keep the circuit time-bounded and persist only
    hashes, timestamp, and HTTP status so runtime reloads do not erase live
    evidence. AIWorkHub never copies, refreshes, or persists provider
    credentials.
    """

    classified = _classify_runtime_auth_failure(
        http_status=http_status,
        error_code=error_code,
        session_id=session_id,
    )
    if classified is None:
        return False

    resolved = executable or shutil.which("claude") or ""
    if not resolved:
        return False
    try:
        path = str(Path(resolved).resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False
    status, session_hash = classified
    with _lock:
        _cache.pop(path, None)
        _runtime_failures[path] = (time.monotonic(), status, session_hash)
    _persist_runtime_failure(path, status, session_hash)
    return True


def clear_runtime_auth_failure(executable: str | None = None) -> bool:
    resolved = executable or shutil.which("claude") or ""
    if not resolved:
        return False
    try:
        path = str(Path(resolved).resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False
    with _lock:
        _runtime_failures.pop(path, None)
        _cache.pop(path, None)
    _clear_persisted_runtime_failure(path)
    return True


def invalidate() -> None:
    with _lock:
        _cache.clear()
        _runtime_failures.clear()
        _probe_latencies.clear()


__all__ = [
    "auth_status",
    "classify_runtime_auth_failure",
    "clear_runtime_auth_failure",
    "invalidate",
    "refresh_subscription_session_for_retry",
    "record_runtime_auth_failure",
    "status_probe_timeouts",
]
