"""Bounded, non-secret preflight for the first-party Claude subscription CLI.

Claude Code's subscription session is distinct from Copilot/VS Code Language
Model authorization.  AIWorkHub never copies either credential.  It asks the
installed CLI for its redacted status, caches the result briefly, and blocks a
launch before task claim when the subscription session is not usable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


CACHE_TTL_SECONDS = 300.0
STATUS_TIMEOUT_SECONDS = 5.0
MAX_STATUS_BYTES = 16 * 1024

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


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
    now = time.monotonic()
    with _lock:
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


def invalidate() -> None:
    with _lock:
        _cache.clear()


__all__ = ["auth_status", "invalidate"]
