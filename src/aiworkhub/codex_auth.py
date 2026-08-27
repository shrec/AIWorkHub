"""Bounded, secret-free capability preflight for the first-party Codex CLI.

The probe asks the installed CLI whether its local login is usable, then opens
one short-lived app-server connection to read account presence and the exact
visible model ids.  It never starts a thread or turn, refreshes credentials,
or returns account details.
"""

from __future__ import annotations

import hashlib
import json
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Mapping


POSITIVE_CACHE_TTL_SECONDS = 300.0
NEGATIVE_CACHE_TTL_SECONDS = 30.0
LOGIN_TIMEOUT_SECONDS = 4.0
APP_SERVER_TIMEOUT_SECONDS = 6.0
SHUTDOWN_TIMEOUT_SECONDS = 1.0
MODEL_PAGE_LIMIT = 64
MAX_MODEL_PAGES = 4
MAX_MODELS = 128
MAX_MODEL_ID_BYTES = 128
MAX_CURSOR_BYTES = 512
MAX_MESSAGE_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_MESSAGES = 512
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,127}$")

_lock = threading.Lock()
_cache: dict[str, tuple[float, float, dict[str, Any]]] = {}


class _ProbeError(RuntimeError):
    pass


def _resolved_executable(executable: str | None) -> str:
    resolved = executable or shutil.which("codex") or ""
    if not resolved:
        raise _ProbeError("codex_cli_not_found")
    try:
        path = Path(resolved).resolve(strict=True)
        info = path.stat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _ProbeError("codex_cli_path_invalid") from exc
    if not path.is_file():
        raise _ProbeError("codex_cli_path_invalid")
    return str(path)


def _executable_identity(path: str) -> str:
    """Hash only executable metadata so cache keys expose no host path."""

    info = Path(path).stat()
    material = "\0".join(
        (
            path,
            str(getattr(info, "st_dev", 0)),
            str(getattr(info, "st_ino", 0)),
            str(info.st_size),
            str(info.st_mtime_ns),
        )
    )
    return hashlib.sha256(material.encode("utf-8", errors="surrogatepass")).hexdigest()


def _failure(reason: str, *, cache_key: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "authenticated": False,
        "access_observed": False,
        "launchable": False,
        "status": "access_unavailable",
        "blocker_reason": reason[:200],
        "observed_models": [],
        "model_catalog_complete": False,
        "quota_observed": False,
        "quota_state": "unavailable_from_provider_api",
        "cache_hit": False,
    }
    if cache_key:
        result["cache_key_sha256"] = cache_key
    return result


def _login_ready(path: str) -> bool:
    try:
        completed = subprocess.run(
            [path, "login", "status"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
            timeout=LOGIN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _reader(stream: BinaryIO, output: queue.Queue[object]) -> None:
    def publish(value: object) -> bool:
        try:
            output.put_nowait(value)
        except queue.Full:
            return False
        return True

    try:
        while True:
            line = stream.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                publish(None)
                return
            if not publish(line):
                return
    except (OSError, ValueError) as exc:
        publish(exc)


def _send(stream: BinaryIO, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise _ProbeError("codex_capability_request_oversized")
    try:
        stream.write(encoded)
        stream.flush()
    except (BrokenPipeError, OSError, ValueError) as exc:
        raise _ProbeError("codex_app_server_write_failed") from exc


def _response(
    responses: queue.Queue[object],
    request_id: int,
    deadline: float,
    counters: dict[str, int],
) -> Mapping[str, Any]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _ProbeError("codex_app_server_timeout")
        try:
            item = responses.get(timeout=remaining)
        except queue.Empty as exc:
            raise _ProbeError("codex_app_server_timeout") from exc
        if item is None:
            raise _ProbeError("codex_app_server_closed")
        if isinstance(item, BaseException):
            raise _ProbeError("codex_app_server_read_failed") from item
        if not isinstance(item, bytes):
            raise _ProbeError("codex_app_server_malformed")
        counters["messages"] += 1
        counters["bytes"] += len(item)
        if (
            counters["messages"] > MAX_MESSAGES
            or counters["bytes"] > MAX_OUTPUT_BYTES
            or len(item) > MAX_MESSAGE_BYTES
            or not item.endswith(b"\n")
        ):
            raise _ProbeError("codex_app_server_output_limit")
        try:
            payload = json.loads(item.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _ProbeError("codex_app_server_malformed") from exc
        if not isinstance(payload, Mapping):
            raise _ProbeError("codex_app_server_malformed")
        # Id-less notifications are irrelevant to this read-only probe. Their
        # bounded bytes/messages still count above. With one outstanding
        # request, any other response id is a protocol violation.
        if "id" not in payload:
            continue
        response_id = payload.get("id")
        if type(response_id) is not int:
            raise _ProbeError("codex_app_server_malformed")
        if response_id != request_id:
            raise _ProbeError("codex_app_server_unexpected_response_id")
        if "error" in payload:
            raise _ProbeError("codex_app_server_rpc_error")
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise _ProbeError("codex_app_server_malformed")
        return result


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.stdout is not None:
        try:
            process.stdout.close()
        except OSError:
            pass


def _probe_app_server(path: str) -> tuple[bool, list[str]]:
    try:
        process = subprocess.Popen(
            [path, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
        )
    except OSError as exc:
        raise _ProbeError("codex_app_server_start_failed") from exc
    if process.stdin is None or process.stdout is None:
        _terminate(process)
        raise _ProbeError("codex_app_server_pipe_unavailable")

    responses: queue.Queue[object] = queue.Queue(maxsize=MAX_MESSAGES + 1)
    reader = threading.Thread(
        target=_reader,
        args=(process.stdout, responses),
        name="aiworkhub-codex-capability-reader",
        daemon=True,
    )
    reader.start()
    deadline = time.monotonic() + APP_SERVER_TIMEOUT_SECONDS
    counters = {"messages": 0, "bytes": 0}
    try:
        _send(
            process.stdin,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "aiworkhub-capability-probe",
                        "title": "AIWorkHub capability probe",
                        "version": "1",
                    }
                },
            },
        )
        _response(responses, 1, deadline, counters)
        _send(process.stdin, {"method": "initialized", "params": {}})

        _send(
            process.stdin,
            {"id": 2, "method": "account/read", "params": {"refreshToken": False}},
        )
        account = _response(responses, 2, deadline, counters)
        requires_auth = account.get("requiresOpenaiAuth")
        raw_account = account.get("account")
        if not isinstance(requires_auth, bool):
            raise _ProbeError("codex_account_status_malformed")
        if raw_account is not None and not isinstance(raw_account, Mapping):
            raise _ProbeError("codex_account_status_malformed")
        authenticated = bool(raw_account is not None or not requires_auth)
        if not authenticated:
            return False, []

        models: list[str] = []
        seen_models: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        request_id = 3
        for _page in range(MAX_MODEL_PAGES):
            params: dict[str, Any] = {
                "limit": MODEL_PAGE_LIMIT,
                "includeHidden": False,
            }
            if cursor is not None:
                params["cursor"] = cursor
            _send(
                process.stdin,
                {"id": request_id, "method": "model/list", "params": params},
            )
            page = _response(responses, request_id, deadline, counters)
            request_id += 1
            data = page.get("data")
            if not isinstance(data, list) or len(data) > MODEL_PAGE_LIMIT:
                raise _ProbeError("codex_model_catalog_malformed")
            for raw_model in data:
                if not isinstance(raw_model, Mapping):
                    raise _ProbeError("codex_model_catalog_malformed")
                model_id = raw_model.get("id")
                if not isinstance(model_id, str) or not model_id:
                    raise _ProbeError("codex_model_catalog_malformed")
                if len(model_id.encode("utf-8", errors="strict")) > MAX_MODEL_ID_BYTES:
                    raise _ProbeError("codex_model_id_oversized")
                if not _MODEL_ID_RE.fullmatch(model_id):
                    raise _ProbeError("codex_model_id_invalid")
                if model_id not in seen_models:
                    seen_models.add(model_id)
                    models.append(model_id)
                if len(models) > MAX_MODELS:
                    raise _ProbeError("codex_model_catalog_oversized")
            if "nextCursor" not in page:
                raise _ProbeError("codex_model_catalog_incomplete")
            next_cursor = page.get("nextCursor")
            if next_cursor is None:
                return True, models
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or len(next_cursor.encode("utf-8", errors="strict")) > MAX_CURSOR_BYTES
                or next_cursor in seen_cursors
            ):
                raise _ProbeError("codex_model_cursor_invalid")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise _ProbeError("codex_model_catalog_incomplete")
    finally:
        _terminate(process)
        reader.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)


def capability_status(
    executable: str | None = None, *, force: bool = False
) -> dict[str, Any]:
    """Return current Codex access and exact visible models without secrets."""

    try:
        path = _resolved_executable(executable)
        cache_key = _executable_identity(path)
    except (OSError, _ProbeError) as exc:
        reason = str(exc) if isinstance(exc, _ProbeError) else "codex_cli_path_invalid"
        return _failure(reason)

    # Hold the lock while probing. Preflight is infrequent and bounded, and
    # this provides one simple single-flight for every executable identity.
    with _lock:
        now = time.monotonic()
        cached = _cache.get(cache_key)
        if not force and cached is not None and now - cached[0] < cached[1]:
            return {**cached[2], "cache_hit": True}

        if not _login_ready(path):
            result = _failure("codex_authentication_required", cache_key=cache_key)
        else:
            try:
                authenticated, models = _probe_app_server(path)
            except (OSError, UnicodeError, _ProbeError) as exc:
                reason = str(exc) if isinstance(exc, _ProbeError) else "codex_capability_probe_failed"
                result = _failure(reason, cache_key=cache_key)
            else:
                launchable = bool(authenticated and models)
                result = {
                    "ok": launchable,
                    "authenticated": authenticated,
                    "access_observed": authenticated,
                    "launchable": launchable,
                    "status": "ready" if launchable else "access_unavailable",
                    "blocker_reason": "" if launchable else "codex_model_catalog_empty",
                    "observed_models": models,
                    "model_catalog_complete": bool(authenticated),
                    "quota_observed": False,
                    "quota_state": "unavailable_from_provider_api",
                    "cache_hit": False,
                    "cache_key_sha256": cache_key,
                }
        ttl = (
            POSITIVE_CACHE_TTL_SECONDS
            if result.get("launchable") is True
            else NEGATIVE_CACHE_TTL_SECONDS
        )
        result["cache_ttl_seconds"] = ttl
        _cache[cache_key] = (now, ttl, dict(result))
        return result


def invalidate() -> None:
    with _lock:
        _cache.clear()


__all__ = ["capability_status", "invalidate"]
