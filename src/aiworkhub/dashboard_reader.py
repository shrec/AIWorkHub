"""Dashboard-only stdio process for the VS Code dashboard (NF-2026-02287).

Run as ``python -m aiworkhub.dashboard_reader --repo <root>``. The VS Code
dashboard webview talks to this process, never to the MCP server: it reads and
writes repository data itself and never loads the MCP/dispatcher process.

Protocol: newline-delimited JSON on stdin/stdout, one object per line.

* request ``{"id": <int|str>, "tool": <name>, "arguments": {...}}``
* reply   ``{"id": <same id>, "result": <view output>}`` or
  ``{"id": <same id|null>, "error": {"code": <stable code>, ...}}``

Every tool is dispatched to the existing view callable in
``aiworkhub.dashboard_mcp_app``; nothing is reimplemented here, so payloads,
section digests and the snapshot delta protocol are the ones those views
produce. Write views keep their own confirmation and authority checks. Only
the dashboard tool maps named in ``DASHBOARD_TOOL_MAP_NAMES`` are served; any
other tool name is refused with ``tool_not_allowed``.

This module never imports ``aiworkhub.server`` and starts no dispatcher,
Source Graph daemon, reconciler or background thread. Requests are served
one at a time on the main thread. The view modules' own background refreshers
(storage telemetry, SQLite quick-check, repo-policy cache) are fenced: their
``threading.Thread`` is a stand-in that queues the target, and the queue is
drained on the main thread after each reply is written. The snapshot read
pool runs its reads inline. Owner requirement: a single-threaded,
dashboard-only process; the extension-side client queues its calls.
"""

from __future__ import annotations

import argparse
import collections
import importlib
import json
import os
import sys
import threading
import types
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, Sequence

SCHEMA_ID = "aiworkhub.dashboard_reader.v1"

# The exact dashboard tool maps of ``dashboard_mcp_app`` this process serves:
# a named subset of the owner module, never a restated list of tool names.
# The task-retention maps (archive/restore/restart/quarantine of tasks) are
# deliberately absent -- task restart launches a worker, which is a
# coordinator duty, not a dashboard read/write.
DASHBOARD_TOOL_MAP_NAMES: tuple[str, ...] = (
    "READONLY_TOOLS",
    "INITIALIZE_TOOLS",
    "LIVE_OUTPUT_TOOLS",
    "MEMORY_TOOLS",
    "SESSION_TOOLS",
    "KB_TOOLS",
    "SKILLS_TOOLS",
    "NEEDFIX_READ_TOOLS",
    "NEEDFIX_WRITE_TOOLS",
    "ROADMAP_READ_TOOLS",
    "HUB_MAIL_READ_TOOLS",
    "HUB_MAIL_WRITE_TOOLS",
    "SETTINGS_TOOLS",
    "SETTINGS_UPDATE_TOOLS",
    "MODEL_SETTINGS_UPDATE_TOOLS",
    "SOURCE_GRAPH_SETTINGS_UPDATE_TOOLS",
    "STORAGE_RETENTION_READ_TOOLS",
    "STORAGE_RETENTION_WRITE_TOOLS",
    "TERMINAL_LOG_RETENTION_READ_TOOLS",
    "TERMINAL_LOG_RETENTION_WRITE_TOOLS",
)

# Modules whose view-path background refreshers are one-shot
# ``threading.Thread(target=...).start()`` calls; the fence queues them.
FENCED_THREAD_MODULES: tuple[str, ...] = (
    "aiworkhub.storage_observability",
    "aiworkhub.task_store",
    "aiworkhub.repo_policy",
)

# Bound on one request line. The largest legitimate request is a snapshot
# delta carrying at most 256 section digests or a NeedFix capture with an
# 8000-character description; 1 MiB leaves ample headroom.
MAX_REQUEST_LINE_BYTES = 1024 * 1024
MAX_REQUEST_ID_CHARS = 128
MAX_TOOL_NAME_CHARS = 128
MAX_ERROR_DETAIL_CHARS = 240
# Bound on queued background jobs run after one reply; the rest wait for the
# next drain, so one request can never pin the loop indefinitely.
MAX_DEFERRED_JOBS_PER_DRAIN = 64
MAX_DEFERRED_QUEUE = 256


class DashboardReaderError(Exception):
    """A refused request: ``code`` is a stable error-taxonomy name."""

    def __init__(self, code: str, **detail: Any) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail

    def as_error(self) -> dict[str, Any]:
        return {"code": self.code, **self.detail}


_DEFERRED: collections.deque[_DeferredThread] = collections.deque()


class _DeferredThread:
    """``threading.Thread`` stand-in: ``start`` queues, the main loop runs it.

    ``join`` runs a queued job inline, so a caller that waits for its own
    refresh still observes the result instead of blocking forever.
    """

    def __init__(
        self,
        group: Any = None,
        target: Callable[..., Any] | None = None,
        name: str | None = None,
        args: Sequence[Any] = (),
        kwargs: Mapping[str, Any] | None = None,
        *,
        daemon: bool | None = None,
    ) -> None:
        self.name = str(name or "dashboard-reader-deferred")
        self.daemon = bool(daemon)
        self.ident: int | None = None
        self.native_id: int | None = None
        self._target = target
        self._args = tuple(args)
        self._kwargs = dict(kwargs or {})
        self._state = "new"

    def start(self) -> None:
        if self._state != "new":
            raise RuntimeError("threads can only be started once")
        if len(_DEFERRED) >= MAX_DEFERRED_QUEUE:
            # Same contract as an exhausted thread budget: callers fall back
            # to their synchronous path (task_store) or retry on a later read.
            raise RuntimeError("dashboard_reader_deferred_queue_full")
        self._state = "queued"
        _DEFERRED.append(self)

    def run(self) -> None:
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def _run_now(self) -> None:
        if self._state != "queued":
            return
        self._state = "running"
        try:
            self.run()
        except Exception as exc:  # noqa: BLE001 -- a background refresh never ends the process
            sys.stderr.write(
                f"dashboard_reader_deferred_failed:{self.name}:{type(exc).__name__}:"
                f"{str(exc)[:MAX_ERROR_DETAIL_CHARS]}\n"
            )
        finally:
            self._state = "done"

    def join(self, timeout: float | None = None) -> None:
        if self._state == "new":
            raise RuntimeError("cannot join thread before it is started")
        self._run_now()

    def is_alive(self) -> bool:
        return self._state in ("queued", "running")


class _ThreadingFence(types.ModuleType):
    """The ``threading`` module as fenced modules see it: only ``Thread`` differs."""

    Thread = _DeferredThread

    def __init__(self) -> None:
        super().__init__("threading")

    def __getattr__(self, name: str) -> Any:
        return getattr(threading, name)


def install_single_thread_fence() -> None:
    """Keep every view-path refresher and read pool on the main thread."""
    fence = _ThreadingFence()
    for module_name in FENCED_THREAD_MODULES:
        module = importlib.import_module(module_name)
        if getattr(module, "threading", None) is not threading:
            raise RuntimeError(f"dashboard_reader_fence_unrecognized:{module_name}")
        setattr(module, "threading", fence)
    from aiworkhub import dashboard

    # build_snapshot's own sequential path (the one-core branch): reads run
    # inline on this thread and the shared read pool is never created.
    setattr(dashboard, "_snapshot_read_worker_limit", lambda: 1)


def drain_deferred_work(limit: int = MAX_DEFERRED_JOBS_PER_DRAIN) -> int:
    """Run up to ``limit`` queued background jobs on this thread; return the count."""
    ran = 0
    while _DEFERRED and ran < limit:
        _DEFERRED.popleft()._run_now()
        ran += 1
    return ran


def dashboard_tools() -> dict[str, Callable[..., Any]]:
    """Return tool name -> existing view callable for every served map.

    Imported lazily so ``main`` binds the repository environment first. A
    tool name present in two maps is a configuration fault and is refused
    rather than silently resolved to either callable.
    """
    from aiworkhub import dashboard_mcp_app

    tools: dict[str, Callable[..., Any]] = {}
    for map_name in DASHBOARD_TOOL_MAP_NAMES:
        mapping = getattr(dashboard_mcp_app, map_name)
        for name, fn in mapping.items():
            if name in tools:
                raise RuntimeError(f"dashboard_reader_duplicate_tool:{name}")
            tools[name] = fn
    return tools


def _request_id(raw: Any) -> int | str:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise DashboardReaderError("request_id_invalid")
    if isinstance(raw, str) and not 0 < len(raw) <= MAX_REQUEST_ID_CHARS:
        raise DashboardReaderError("request_id_invalid", limit=MAX_REQUEST_ID_CHARS)
    return raw


def handle_request(tools: Mapping[str, Callable[..., Any]], request: Any) -> dict[str, Any]:
    """Serve one decoded request object; always returns exactly one reply."""
    request_id: int | str | None = None
    try:
        if not isinstance(request, dict):
            raise DashboardReaderError("request_not_object")
        request_id = _request_id(request.get("id"))
        tool = request.get("tool")
        if not isinstance(tool, str) or not 0 < len(tool) <= MAX_TOOL_NAME_CHARS:
            raise DashboardReaderError("tool_name_invalid", limit=MAX_TOOL_NAME_CHARS)
        fn = tools.get(tool)
        if fn is None:
            raise DashboardReaderError("tool_not_allowed", tool=tool)
        arguments = request.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict) or not all(isinstance(key, str) for key in arguments):
            raise DashboardReaderError("arguments_invalid", tool=tool)
        try:
            result = fn(**arguments)
        except TypeError as exc:
            # A view signature mismatch (unknown/missing keyword) is a caller
            # error; the view itself already fails closed on bad values.
            raise DashboardReaderError(
                "arguments_invalid", tool=tool, reason=str(exc)[:MAX_ERROR_DETAIL_CHARS]
            ) from exc
        except Exception as exc:  # noqa: BLE001 -- one failing view never ends the process
            raise DashboardReaderError(
                "tool_failed", tool=tool, error_type=type(exc).__name__,
                reason=str(exc)[:MAX_ERROR_DETAIL_CHARS],
            ) from exc
        return {"id": request_id, "result": result}
    except DashboardReaderError as refusal:
        return {"id": request_id, "error": refusal.as_error()}


def _encode_reply(reply: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(reply, ensure_ascii=True, separators=(",", ":"), default=str, allow_nan=False)
    except (TypeError, ValueError) as exc:
        text = json.dumps(
            {
                "id": reply.get("id"),
                "error": {
                    "code": "result_not_serializable",
                    "reason": str(exc)[:MAX_ERROR_DETAIL_CHARS],
                },
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
    return (text + "\n").encode("ascii")


def _read_request_line(stdin: BinaryIO) -> tuple[bytes, bool] | None:
    """Return (line, oversized) or None at EOF; an oversized line is drained."""
    line = stdin.readline(MAX_REQUEST_LINE_BYTES + 1)
    if not line:
        return None
    if len(line) <= MAX_REQUEST_LINE_BYTES or line.endswith(b"\n"):
        return line, False
    while True:
        rest = stdin.readline(MAX_REQUEST_LINE_BYTES)
        if not rest or rest.endswith(b"\n"):
            return b"", True


def serve(stdin: BinaryIO, stdout: BinaryIO, tools: Mapping[str, Callable[..., Any]]) -> int:
    """Serve requests until stdin closes; one reply line per request line."""
    while True:
        read = _read_request_line(stdin)
        if read is None:
            return 0
        line, oversized = read
        if oversized:
            reply: dict[str, Any] = {
                "id": None,
                "error": {"code": "request_too_large", "limit": MAX_REQUEST_LINE_BYTES},
            }
        else:
            text = line.strip()
            if not text:
                continue
            try:
                request = json.loads(text.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                reply = {"id": None, "error": {"code": "request_invalid_json"}}
            else:
                reply = handle_request(tools, request)
        stdout.write(_encode_reply(reply))
        stdout.flush()
        # The reply is already on the wire; background refreshes the view
        # scheduled run now, so the next poll publishes their result.
        drain_deferred_work()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m aiworkhub.dashboard_reader")
    parser.add_argument("--repo", required=True, help="repository root this dashboard is bound to")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    root = Path(args.repo).expanduser()
    if not root.is_dir():
        sys.stderr.write(f"dashboard_reader_repo_not_directory:{root}\n")
        return 2
    root = root.resolve()
    # core.repo_root() is the process-wide repository authority; bind it to
    # the one repository this process serves before any view module loads.
    os.environ["AIWORKHUB_REPO_ROOT"] = str(root)
    os.environ["AIWORKHUB_REPO"] = str(root)
    reply_stream = sys.stdout.buffer
    # Anything a view module prints must never corrupt the reply stream.
    sys.stdout = sys.stderr
    install_single_thread_fence()
    tools = dashboard_tools()
    return serve(sys.stdin.buffer, reply_stream, tools)


if __name__ == "__main__":
    raise SystemExit(main())
