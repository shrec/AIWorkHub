"""NF-2026-02012: the worker Source Graph tool answers within a deadline.

A measured worker call sent nothing for 1801 s. These tests drive the REAL
MCP-registered closure (``register_tools`` with a capturing fake mcp) and
prove a blocked engine returns a typed ``source_graph_timeout`` while a fast
call keeps its normal payload and delivery bookkeeping.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from aiworkhub import feature_settings
from aiworkhub import worker_ai_tools_mcp as worker_tools

DEADLINE = 0.5
GUARD = DEADLINE + 2.0
TOOL_NAME = "aiworkhub_worker_source_graph_query"


class _CapturingMCP:
    """Minimal stand-in for FastMCP: records every decorated tool by name."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, name: str | None = None, **_kwargs: Any):
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name or fn.__name__] = fn
            return fn

        return decorator

    def __getattr__(self, _attr: str):
        def factory(*_args: Any, **_kwargs: Any):
            return lambda fn: fn

        return factory


def _ctx(tmp_path: Path) -> worker_tools.WorkerToolContext:
    repo = tmp_path / "repo"
    repo.mkdir()
    return worker_tools.WorkerToolContext(
        task_id="needfix-NF-2026-02012",
        runner="claude",
        topic="code",
        request_id="a" * 32,
        repo=repo,
        authority_repo=repo,
        source_graph_targets=(),
        session_topic="code",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
    )


def _registered_tool(ctx: worker_tools.WorkerToolContext) -> Callable[..., Any]:
    mcp = _CapturingMCP()
    worker_tools.register_tools(mcp, ctx)
    return mcp.tools[TOOL_NAME]


def _call_with_guard(tool: Callable[..., Any], **kwargs: Any) -> tuple[Any, float]:
    """Call ``tool`` on a guard thread; fail (never hang) past GUARD seconds."""

    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["result"] = tool(**kwargs)
        except BaseException as exc:  # surfaced by the assertion below
            box["error"] = exc

    started = time.monotonic()
    guard = threading.Thread(target=_run, daemon=True)
    guard.start()
    guard.join(GUARD)
    elapsed = time.monotonic() - started
    assert not guard.is_alive(), (
        f"source graph tool gave no reply within {GUARD}s (deadline {DEADLINE}s)"
    )
    assert "error" not in box, repr(box.get("error"))
    return box["result"], elapsed


@pytest.fixture
def release() -> Any:
    event = threading.Event()
    yield event
    event.set()


def _block_engine(monkeypatch: pytest.MonkeyPatch, release: threading.Event) -> list[int]:
    entered: list[int] = []

    def _blocked(_ctx: Any) -> Any:
        entered.append(1)
        release.wait(60.0)
        raise worker_tools.WorkerToolError("released_by_teardown")

    monkeypatch.setattr(feature_settings, "enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(worker_tools, "_resolve_source_graph_db", _blocked)
    monkeypatch.setattr(
        worker_tools, "SOURCE_GRAPH_TOOL_DEADLINE_SECONDS", DEADLINE, raising=False,
    )
    return entered


def test_blocked_engine_returns_typed_timeout_within_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release: threading.Event,
) -> None:
    entered = _block_engine(monkeypatch, release)
    tool = _registered_tool(_ctx(tmp_path))

    reply, elapsed = _call_with_guard(
        tool, mode="body", query="ProcessManager._finalize_isolated_request",
    )

    assert entered, "the engine entry was never reached"
    assert elapsed < GUARD
    assert reply["ok"] is False
    assert reply["error"] == "source_graph_timeout"
    assert reply["mode"] == "body"
    assert reply["deadline_seconds"] == pytest.approx(DEADLINE)
    assert reply["elapsed_seconds"] >= DEADLINE
    assert isinstance(reply["phase"], str) and reply["phase"]
    assert reply["phase"] == "resolve_binding"


def test_later_call_behind_abandoned_one_reaches_its_own_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release: threading.Event,
) -> None:
    entered = _block_engine(monkeypatch, release)
    tool = _registered_tool(_ctx(tmp_path))

    first, _ = _call_with_guard(tool, mode="focus", query="first blocked call")
    second, elapsed = _call_with_guard(tool, mode="slice", query="second call")

    assert len(entered) == 2
    assert first["error"] == second["error"] == "source_graph_timeout"
    assert second["mode"] == "slice"
    assert elapsed < GUARD


def test_fast_call_returns_normal_payload_and_records_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"ok": True, "tool": "source_graph", "mode": "body", "content": {"x": 1}}
    calls: list[dict[str, Any]] = []
    delivered: list[Any] = []

    def _fast(ctx: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return payload

    monkeypatch.setattr(worker_tools, "source_graph_query", _fast)
    monkeypatch.setattr(
        worker_tools.WorkerSemanticEditSession,
        "note_source_graph_delivery",
        lambda self, result: delivered.append(result),
    )
    monkeypatch.setattr(
        worker_tools, "SOURCE_GRAPH_TOOL_DEADLINE_SECONDS", DEADLINE, raising=False,
    )
    tool = _registered_tool(_ctx(tmp_path))

    reply, _ = _call_with_guard(tool, mode="body", query="Fast.symbol", budget=16)

    assert reply is payload
    assert reply == {"ok": True, "tool": "source_graph", "mode": "body", "content": {"x": 1}}
    assert delivered == [payload]
    assert calls and calls[0]["mode"] == "body" and calls[0]["budget"] == 16
