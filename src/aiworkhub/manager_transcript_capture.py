"""Passive manager-chat capture for the repository Context Graph.

The adapter consumes only authoritative completed user/assistant message items
already passing through a verified Codex App Server mux.  It never records
reasoning, tool output, commands, approvals or streaming deltas, and all SQLite
work happens on a bounded background queue so transparent chat transport is
never blocked by repository storage.
"""

from __future__ import annotations

import hashlib
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import context_graph, feature_settings, shared_router, storage_registry


MAX_CAPTURE_QUEUE = 256
MAX_CAPTURE_CONTENT_BYTES = context_graph.MAX_CONTENT_BYTES
ROUTE_RETRY_SECONDS = 10.0
ROUTE_RETRY_INTERVAL_SECONDS = 0.25


@dataclass(frozen=True)
class CompletedChatMessage:
    thread_id: str
    turn_id: str
    item_id: str
    role: str
    content: str
    occurred_at: str
    message_type: str


def _token(value: Any, maximum: int = 256) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or "\x00" in text or len(text.encode("utf-8")) > maximum:
        return ""
    return text


def _user_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part)


def extract_codex_completed_message(message: dict[str, Any]) -> CompletedChatMessage | None:
    """Project one stable final-message item; ignore every other protocol event."""
    if not isinstance(message, dict) or message.get("method") != "item/completed":
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    item = params.get("item")
    if not isinstance(item, dict):
        return None
    message_type = _token(item.get("type"), 64)
    if message_type == "userMessage":
        role = "user"
        content = _user_text(item)
    elif message_type == "agentMessage":
        role = "assistant"
        content = str(item.get("text") or "")
    else:
        return None
    thread_id = _token(params.get("threadId"))
    turn_id = _token(params.get("turnId"))
    item_id = _token(item.get("id"))
    if not thread_id or not turn_id or not item_id or not content or "\x00" in content:
        return None
    if len(content.encode("utf-8")) > MAX_CAPTURE_CONTENT_BYTES:
        return None
    completed_at_ms = params.get("completedAtMs")
    if isinstance(completed_at_ms, bool) or not isinstance(completed_at_ms, int):
        return None
    try:
        occurred_at = datetime.fromtimestamp(
            completed_at_ms / 1000, tz=timezone.utc
        ).isoformat()
    except (OverflowError, OSError, ValueError):
        return None
    return CompletedChatMessage(
        thread_id=thread_id,
        turn_id=turn_id,
        item_id=item_id,
        role=role,
        content=content,
        occurred_at=occurred_at,
        message_type=message_type,
    )


def write_completed_message(
    repo: Path,
    *,
    repo_id: str,
    message: CompletedChatMessage,
) -> dict[str, Any]:
    registry = storage_registry.load_storage_registry(repo)
    if registry.repo.manifest.repo_id != repo_id:
        return {"ok": False, "error": "capture_repo_id_mismatch"}
    digest = hashlib.sha256(
        f"{repo_id}\0{message.thread_id}\0{message.turn_id}\0{message.item_id}".encode()
    ).hexdigest()
    return context_graph.append_event(
        repo,
        thread_id=message.thread_id,
        session_id=message.thread_id,
        provider="codex",
        role=message.role,
        event_type="chat_message",
        content=message.content,
        source_ref=f"codex_app_server:item/completed:{message.turn_id}:{message.item_id}",
        idempotency_key=f"codex-item-{digest}",
        metadata={
            "manager_only": True,
            "message_type": message.message_type,
            "turn_id": message.turn_id,
            "item_id": message.item_id,
        },
        occurred_at=message.occurred_at,
    )


class ManagerTranscriptCapture:
    """Bounded async sink tied to one mux repository/extension-host identity."""

    def __init__(
        self,
        *,
        repo_id: str,
        extension_host_pid: int,
        route_resolver: Callable[..., dict[str, Any]] | None = None,
        route_retry_seconds: float = ROUTE_RETRY_SECONDS,
        route_retry_interval_seconds: float = ROUTE_RETRY_INTERVAL_SECONDS,
    ) -> None:
        self._repo_id = repo_id
        self._extension_host_pid = extension_host_pid
        self._route_resolver = route_resolver or shared_router.resolve_repository_route
        self._route_retry_seconds = max(0.0, float(route_retry_seconds))
        self._route_retry_interval_seconds = max(
            0.01, float(route_retry_interval_seconds)
        )
        self._queue: queue.Queue[CompletedChatMessage | None] = queue.Queue(
            maxsize=MAX_CAPTURE_QUEUE
        )
        self._thread: threading.Thread | None = None
        self._accepting = threading.Event()
        self._accepting.set()
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._counts = {
            "seen": 0,
            "queued": 0,
            "captured": 0,
            "idempotent": 0,
            "skipped": 0,
            "dropped": 0,
            "errors": 0,
        }

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="aiworkhub-manager-transcript-capture",
            daemon=True,
        )
        self._thread.start()

    def offer(self, raw_message: dict[str, Any]) -> None:
        message = extract_codex_completed_message(raw_message)
        if message is None:
            return
        with self._lock:
            self._counts["seen"] += 1
        if not self._accepting.is_set():
            with self._lock:
                self._counts["dropped"] += 1
            return
        try:
            self._queue.put_nowait(message)
        except queue.Full:
            with self._lock:
                self._counts["dropped"] += 1
        else:
            with self._lock:
                self._counts["queued"] += 1

    def _resolve_repo(self, thread_id: str) -> Path | None:
        deadline = time.monotonic() + self._route_retry_seconds
        while not self._closed.is_set():
            try:
                route = self._route_resolver(
                    provider="codex",
                    thread_id=thread_id,
                    extension_host_pid=self._extension_host_pid,
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                route = {}
            if (
                isinstance(route, dict)
                and route.get("ok") is True
                and route.get("repo_id") == self._repo_id
            ):
                root = Path(str(route.get("repo_root") or "")).resolve()
                if root.is_dir():
                    return root
            if time.monotonic() >= deadline:
                return None
            self._closed.wait(self._route_retry_interval_seconds)
        return None

    def _consume(self, message: CompletedChatMessage) -> None:
        repo = self._resolve_repo(message.thread_id)
        if repo is None:
            with self._lock:
                self._counts["skipped"] += 1
            return
        try:
            if not feature_settings.enabled(repo, "context_graph"):
                with self._lock:
                    self._counts["skipped"] += 1
                return
            result = write_completed_message(repo, repo_id=self._repo_id, message=message)
        except (
            context_graph.ContextGraphError,
            feature_settings.FeatureSettingsError,
            OSError,
            sqlite3.Error,
            storage_registry.StorageRegistryError,
        ):
            with self._lock:
                self._counts["errors"] += 1
            return
        with self._lock:
            if result.get("ok"):
                key = "idempotent" if result.get("idempotent") else "captured"
                self._counts[key] += 1
            else:
                self._counts["errors"] += 1

    def _run(self) -> None:
        while True:
            message = self._queue.get()
            try:
                if message is None:
                    return
                self._consume(message)
            finally:
                self._queue.task_done()

    def close(self, timeout: float = 2.0) -> None:
        if self._closed.is_set():
            return
        self._accepting.clear()
        try:
            self._queue.put(None, timeout=min(max(0.0, timeout), 0.25))
        except queue.Full:
            with self._lock:
                self._counts["dropped"] += 1
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))
        self._closed.set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            counts = dict(self._counts)
        return {
            "state": "closed" if self._closed.is_set() else "running",
            "queue_depth": self._queue.qsize(),
            **counts,
        }


__all__ = [
    "CompletedChatMessage",
    "ManagerTranscriptCapture",
    "extract_codex_completed_message",
    "write_completed_message",
]
