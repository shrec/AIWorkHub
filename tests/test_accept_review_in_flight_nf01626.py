"""NF-2026-01626: one in-flight accept per request, queryable while it runs.

Measured 2026-10-20 on needfix-NF-2026-01616 (request d7405d96...): the
accept-lane validation ran for 15 minutes, the MCP client gave up at 1800 s
with no progress, and the server recorded the accept (event 20892) about a
minute later. Holding only the timeout, the manager concluded the accept had
been cancelled and nearly started a second one.

The accept tests drive the real ``accept_review`` through the seams
``test_process_launcher_accept_review`` already proves -- the NF-805 candidate
whose supplemental round read its packet, which accepts -- with the declared
validation replaced by one that can be held open. The MCP tests drive the real
FastMCP tool through an in-memory client session.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import inspect
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, process_launcher, server, task_store  # noqa: E402
from aiworkhub import process_launcher_accept_review as par  # noqa: E402

import test_process_launcher_accept_review as _accept_env  # noqa: E402

REQUEST_ID = "R-1"
TASK_ID = "T-1"
_SIGHTED_REVIEWERS = [
    {"task_id": "QR-C", "request_id": "rq-c", "lens": "correctness",
     "findings": _accept_env._BLIND_FINDINGS, "packet": "sealed"},
    {"task_id": "QR-C-SUPPLEMENTAL-1", "request_id": "rq-c-s1",
     "lens": "correctness", "findings": [], "packet": "sealed"},
]
_ACCEPT_PARAMETERS = [
    "request_id", "task_id", "confirm_destructive_change", "requested_risk_tier",
    "risk_signals", "reviewer_reports", "reviewer_request_ids", "confirm_high_risk",
    "manager_correction", "include_card", "lesson_candidate", "promote_ai_memory",
    "extra_evidence_ids", "no_new_lesson_reason", "auto_commit", "commit_message",
]
_PAYLOAD_PREFIX = "NF01626_PAYLOAD="
_needs_sdk = pytest.mark.skipif(
    not server._MCP_SDK_AVAILABLE,
    reason="the mcp SDK is not importable, so there is no FastMCP tool to drive",
)


@pytest.fixture(autouse=True)
def _no_claim_outlives_its_test() -> Iterator[None]:
    yield
    leaked = dict(par._ACCEPT_IN_FLIGHT_HELD)
    par._ACCEPT_IN_FLIGHT_HELD.clear()
    assert leaked == {}, "an accept_review claim outlived its call"


@pytest.fixture
def accept_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    manager, _packet = _accept_env._supplemental_env(
        tmp_path, monkeypatch, _SIGHTED_REVIEWERS
    )
    env = SimpleNamespace(
        manager=manager,
        root=tmp_path,
        marker=par._accept_in_flight_path(manager, TASK_ID, REQUEST_ID),
        validating=[],  # the thread of every declared-validation run
        entered=threading.Event(),
        release=threading.Event(),
    )

    def _validation(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        env.validating.append(threading.get_ident())
        env.entered.set()
        assert env.release.wait(60), "the held accept was never released"
        return []

    monkeypatch.setattr(process_launcher, "_run_declared_validations", _validation)
    return env


@contextlib.contextmanager
def _first_accept_held_in_validation(
    env: SimpleNamespace, call: Any = None
) -> Iterator[tuple[dict[str, Any], threading.Thread]]:
    """Run one accept on its own thread and hold it inside its validation."""
    outcome: dict[str, Any] = {}

    def _run() -> None:
        try:
            outcome["result"] = (call or (lambda: _accept_env._accept(env.manager)))()
        except BaseException as exc:  # noqa: BLE001 -- surfaced by the assertions
            outcome["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 60
        while not env.entered.wait(0.05):
            assert worker.is_alive(), f"the first accept ended before validating: {outcome}"
            assert time.monotonic() < deadline, "the first accept never reached validation"
        yield outcome, worker
    finally:
        env.release.set()
        worker.join(60)
    assert not worker.is_alive(), "the first accept never finished"


@contextlib.contextmanager
def _waiting_child() -> Iterator[subprocess.Popen[bytes]]:
    """A python child blocked on stdin until the block exits; always reaped."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    try:
        yield child
    finally:
        with contextlib.suppress(OSError):
            if child.stdin is not None:
                child.stdin.close()
        try:
            child.wait(timeout=60)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=60)


def _exited_process() -> tuple[int, Any]:
    """The pid and creation ticks of a process that has exited and been reaped."""
    with _waiting_child() as child:
        pid, ticks = child.pid, process_launcher._pid_start_ticks(child.pid)
    return pid, ticks


def _tree(root: Path) -> dict[str, str]:
    """Every file under ``root`` and the sha256 of its bytes."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _marker(*, pid: int, ticks: Any, started_at: str) -> dict[str, Any]:
    """A marker in exactly the shape ``accept_review`` writes."""
    return {
        "schema_id": par.ACCEPT_IN_FLIGHT_SCHEMA_ID,
        "task_id": TASK_ID,
        "request_id": REQUEST_ID,
        "pid": pid,
        "pid_start_ticks": ticks,
        "started_at": started_at,
        "server_version": "0.0.0-another-server",
    }


def _plant(path: Path, marker: dict[str, Any]) -> bytes:
    data = json.dumps(marker, sort_keys=True).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def _events(marker_path: Path) -> list[dict[str, Any]]:
    log = marker_path.parent / par._ACCEPT_IN_FLIGHT_EVENTS_NAME
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- #
# One in-flight accept per (task_id, request_id).
# --------------------------------------------------------------------------- #


def test_two_concurrent_accepts_run_validation_exactly_once(accept_env):
    """The duplicate answers at once with the first one's start and holder, and
    runs nothing and writes nothing; the first one validates and accepts."""
    env = accept_env
    with _first_accept_held_in_validation(env) as (first, worker):
        held = json.loads(env.marker.read_text(encoding="utf-8"))
        before = _tree(env.root)
        launches = list(env.manager.reviewer_launches)

        second = _accept_env._accept(env.manager)

        assert _tree(env.root) == before, "the refused accept wrote to disk"
        assert env.manager.reviewer_launches == launches
    assert second == {
        "ok": False,
        "error": "accept_in_progress",
        "request_id": REQUEST_ID,
        "task_id": TASK_ID,
        "started_at": held["started_at"],
        "holder_pid": os.getpid(),
        "holder_server_version": held["server_version"],
        "holder_identity": "verified",
    }
    assert first.get("result", {}).get("ok") is True, first
    assert set(env.validating) == {worker.ident}


def test_accept_preview_reports_the_running_accept(accept_env):
    """A manager whose call timed out queries instead of retrying."""
    env = accept_env
    with _first_accept_held_in_validation(env) as (first, _worker):
        held = json.loads(env.marker.read_text(encoding="utf-8"))
        preview = par.accept_preview(env.manager, REQUEST_ID, TASK_ID)
    assert preview["ok"] is True
    assert preview["blocked"] is True
    assert preview["evaluated"] is False
    assert preview["accept_in_progress"] == {
        "started_at": held["started_at"],
        "holder_pid": os.getpid(),
        "holder_server_version": held["server_version"],
        "holder_identity": "verified",
    }
    assert [(row["kind"], row["error"]) for row in preview["blockers"]] == [
        ("accept_in_progress", "accept_in_progress")
    ]
    assert set(par.ACCEPT_BLOCKER_KINDS) >= {row["kind"] for row in preview["blockers"]}
    assert first.get("result", {}).get("ok") is True, first
    # Finished: nothing is in flight any more, so nothing says otherwise.
    assert par.accept_in_flight(env.manager, TASK_ID, REQUEST_ID) is None


def test_the_marker_is_released_after_a_successful_accept(accept_env):
    env = accept_env
    env.release.set()

    result = _accept_env._accept(env.manager)

    assert result["ok"] is True, result
    assert env.validating
    assert not env.marker.exists()
    assert par.accept_in_flight(env.manager, TASK_ID, REQUEST_ID) is None


def test_the_marker_is_released_when_the_accept_raises(accept_env, monkeypatch):
    env = accept_env
    claimed: list[bool] = []

    def _crash(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        claimed.append(env.marker.is_file())
        raise RuntimeError("validation runner crashed")

    monkeypatch.setattr(process_launcher, "_run_declared_validations", _crash)
    with pytest.raises(RuntimeError, match="validation runner crashed"):
        _accept_env._accept(env.manager)

    assert claimed == [True], "validation ran without the in-flight marker"
    assert not env.marker.exists()
    assert par.accept_in_flight(env.manager, TASK_ID, REQUEST_ID) is None
    # Nothing lingers to refuse the next attempt.
    monkeypatch.setattr(process_launcher, "_run_declared_validations", lambda *a, **k: [])
    assert _accept_env._accept(env.manager)["ok"] is True


def _stale_holder(case: str) -> tuple[int, Any]:
    if case == "holder_exited":
        return _exited_process()
    ticks = process_launcher._pid_start_ticks(os.getpid())
    if case == "pid_now_another_process":
        if ticks is None:
            pytest.skip("process creation ticks are not readable on this host")
        return os.getpid(), ticks + 1
    # What a release that failed in this very process leaves behind.
    return os.getpid(), ticks


@pytest.mark.parametrize(
    "case", ["holder_exited", "pid_now_another_process", "left_by_this_process"]
)
def test_a_stale_marker_is_recorded_cleared_and_the_accept_proceeds(accept_env, case):
    env = accept_env
    env.release.set()
    pid, ticks = _stale_holder(case)
    stale = _marker(pid=pid, ticks=ticks, started_at="2026-10-20T09:00:00+00:00")
    _plant(env.marker, stale)
    # Read-only callers already see nothing in flight; only an accept clears it.
    assert par.accept_in_flight(env.manager, TASK_ID, REQUEST_ID) is None
    assert env.marker.exists()

    result = _accept_env._accept(env.manager)

    assert result["ok"] is True, result
    assert env.validating, "the accept did not proceed past the stale marker"
    assert not env.marker.exists()
    events = _events(env.marker)
    assert [row["event"] for row in events] == ["stale_marker_cleared"]
    assert events[0]["stale_marker"] == stale
    assert events[0]["cleared_by_pid"] == os.getpid()
    assert (events[0]["task_id"], events[0]["request_id"]) == (TASK_ID, REQUEST_ID)


def test_a_live_holder_in_another_server_process_refuses_without_writing(accept_env):
    env = accept_env
    env.release.set()
    with _waiting_child() as holder:
        live = _marker(
            pid=holder.pid,
            ticks=process_launcher._pid_start_ticks(holder.pid),
            started_at="2026-10-20T08:45:00+00:00",
        )
        planted = _plant(env.marker, live)
        before = _tree(env.root)

        refused = _accept_env._accept(env.manager)
        preview = par.accept_preview(env.manager, REQUEST_ID, TASK_ID)

        assert _tree(env.root) == before, "a refusal or a preview wrote to disk"
    assert refused["ok"] is False
    assert refused["error"] == "accept_in_progress"
    assert (refused["holder_pid"], refused["started_at"]) == (
        holder.pid, "2026-10-20T08:45:00+00:00",
    )
    assert env.validating == []
    assert env.marker.read_bytes() == planted
    assert preview["accept_in_progress"]["holder_pid"] == holder.pid


def test_an_unreadable_marker_refuses_by_name_and_is_left_alone(accept_env):
    """Unknown is never "not running": nothing is cleared on a guess."""
    env = accept_env
    env.release.set()
    env.marker.parent.mkdir(parents=True)
    env.marker.write_bytes(b"{not json")

    refused = _accept_env._accept(env.manager)

    assert refused["ok"] is False
    assert refused["error"] == "accept_in_flight_marker_unreadable"
    assert refused["marker"] == f"{par._ACCEPT_IN_FLIGHT_DIRNAME}/{env.marker.name}"
    assert "accept_in_flight_marker_unreadable" in par.ACCEPT_BLOCKER_KINDS
    assert env.marker.read_bytes() == b"{not json"
    assert env.validating == []


def test_a_stale_marker_whose_clearing_cannot_be_recorded_stays(accept_env):
    """Evidence first: a stale marker is removed only once its line landed."""
    env = accept_env
    env.release.set()
    pid, ticks = _exited_process()
    planted = _plant(
        env.marker, _marker(pid=pid, ticks=ticks, started_at="2026-10-20T09:00:00+00:00")
    )
    (env.marker.parent / par._ACCEPT_IN_FLIGHT_EVENTS_NAME).mkdir()

    refused = _accept_env._accept(env.manager)

    assert refused["ok"] is False
    assert refused["error"].startswith("accept_in_flight_marker_unavailable:")
    assert env.marker.read_bytes() == planted
    assert env.validating == []


def test_a_stale_marker_that_cannot_be_removed_logs_the_failure(accept_env, monkeypatch):
    """The cleared line is followed by the failure, never left as the outcome."""
    env = accept_env
    env.release.set()
    pid, ticks = _exited_process()
    planted = _plant(
        env.marker, _marker(pid=pid, ticks=ticks, started_at="2026-10-20T09:00:00+00:00")
    )

    def _denied(_path: Path) -> None:
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(par, "_unlink_accept_marker", _denied)

    refused = _accept_env._accept(env.manager)

    assert refused["ok"] is False
    assert refused["error"].startswith("accept_in_flight_marker_unavailable:")
    assert env.marker.read_bytes() == planted
    assert [row["event"] for row in _events(env.marker)] == [
        "stale_marker_cleared", "stale_marker_clear_failed",
    ]
    assert env.validating == []


def test_a_deeply_nested_marker_refuses_by_name(accept_env):
    """Bytes the decoder cannot parse within its recursion limit are unreadable."""
    env = accept_env
    env.release.set()
    env.marker.parent.mkdir(parents=True)
    nested = b"[" * par._ACCEPT_IN_FLIGHT_MAX_BYTES
    env.marker.write_bytes(nested)

    refused = _accept_env._accept(env.manager)

    assert refused["ok"] is False
    assert refused["error"] == "accept_in_flight_marker_unreadable"
    assert env.marker.read_bytes() == nested
    assert env.validating == []
    early = server._accept_in_flight_refusal(env.manager, REQUEST_ID, TASK_ID)
    assert early["error"] == "accept_in_flight_marker_unreadable"


def test_a_process_dir_that_cannot_hold_a_marker_refuses_by_name(accept_env):
    env = accept_env
    env.release.set()
    occupied = env.root / "occupied"
    occupied.write_text("a file where the process dir should be", encoding="utf-8")
    env.manager.process_dir = occupied

    refused = _accept_env._accept(env.manager)

    assert refused["ok"] is False
    assert refused["error"].startswith("accept_in_flight_marker_unavailable:")
    assert env.validating == []


def test_a_duplicate_through_the_server_answers_instead_of_queueing(
    accept_env, monkeypatch
):
    """The lifecycle lock is held for a whole accept; a duplicate never waits on it."""
    env = accept_env
    env.manager.accept_review = functools.partial(par.accept_review, env.manager)
    monkeypatch.setattr(process_launcher, "default_manager", lambda: env.manager)
    monkeypatch.setattr(core, "repo_root", lambda: env.manager.repo)
    monkeypatch.setattr(
        task_store,
        "get_task",
        lambda _root, task_id: {"task_id": task_id, "status": "finished", "worker_status": "done"},
    )

    def _through_server() -> dict[str, Any]:
        return server.aiworkhub_agent_accept_review(
            request_id=REQUEST_ID, task_id=TASK_ID, requested_risk_tier="medium"
        )

    duplicate: dict[str, Any] = {}
    caller = threading.Thread(
        target=lambda: duplicate.update(result=_through_server()), daemon=True
    )
    with _first_accept_held_in_validation(env, _through_server) as (first, worker):
        caller.start()
        caller.join(10)
        assert not caller.is_alive(), "the duplicate queued behind the running accept"
    caller.join(60)
    assert duplicate["result"]["ok"] is False
    assert duplicate["result"]["error"] == "accept_in_progress"
    assert duplicate["result"]["holder_pid"] == os.getpid()
    assert first.get("result", {}).get("ok") is True, first
    assert first["result"]["schema_id"] == "aiworkhub.accept_review_receipt.v1"
    assert set(env.validating) == {worker.ident}


# --------------------------------------------------------------------------- #
# The published MCP tool: progress for a caller that asks, nothing else moves.
# --------------------------------------------------------------------------- #


class _SlowAccept:
    """``default_manager()`` for the MCP tests: an accept that takes a while."""

    def __init__(self, process_dir: Path, seconds: float) -> None:
        self.process_dir = process_dir
        self.seconds = seconds
        self.calls = 0

    def accept_review(self, request_id: str, task_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        time.sleep(self.seconds)
        return {
            "ok": True,
            "request_id": request_id,
            "task_id": task_id,
            "promoted_paths": ["src/aiworkhub/example.py"],
        }


@pytest.fixture
def slow_accept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _SlowAccept:
    manager = _SlowAccept(tmp_path / "processes", seconds=0.6)
    monkeypatch.setattr(process_launcher, "default_manager", lambda: manager)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        task_store,
        "get_task",
        lambda _root, task_id: {"task_id": task_id, "status": "finished", "worker_status": "done"},
    )
    monkeypatch.setattr(server, "_MCP_PROGRESS_INTERVAL_SECONDS", 0.05)
    return manager


async def _accept_over_mcp(*, with_progress: bool) -> tuple[list[float], list[Any], Any]:
    from mcp import types
    from mcp.shared.memory import create_connected_server_and_client_session

    reported: list[float] = []
    notes: list[Any] = []

    async def _on_progress(progress: float, _total: float | None, _message: str | None) -> None:
        reported.append(progress)

    async def _on_message(message: Any) -> None:
        if isinstance(message, types.ServerNotification) and isinstance(
            message.root, types.ProgressNotification
        ):
            notes.append(message.root)

    async with create_connected_server_and_client_session(
        server.mcp, message_handler=_on_message
    ) as client:
        result = await client.call_tool(
            "aiworkhub_agent_accept_review",
            {"request_id": REQUEST_ID, "task_id": TASK_ID},
            progress_callback=_on_progress if with_progress else None,
        )
    return reported, notes, result


def _tool_reply(result: Any) -> dict[str, Any]:
    assert result.isError is False, result
    if result.structuredContent is not None:
        return dict(result.structuredContent)
    return json.loads(result.content[0].text)


@_needs_sdk
def test_mcp_accept_reports_progress_while_a_slow_accept_runs(slow_accept):
    reported, notes, result = asyncio.run(_accept_over_mcp(with_progress=True))

    assert notes, "no notifications/progress reached the client during the accept"
    assert reported and reported == sorted(set(reported)), reported
    direct = server.aiworkhub_agent_accept_review(request_id=REQUEST_ID, task_id=TASK_ID)
    assert _tool_reply(result) == json.loads(json.dumps(direct))
    assert slow_accept.calls == 2


@_needs_sdk
def test_mcp_accept_without_a_progress_token_sends_none(slow_accept):
    reported, notes, result = asyncio.run(_accept_over_mcp(with_progress=False))

    assert reported == [] and notes == []
    direct = server.aiworkhub_agent_accept_review(request_id=REQUEST_ID, task_id=TASK_ID)
    assert _tool_reply(result) == json.loads(json.dumps(direct))


def test_the_accept_tool_function_stays_synchronous_with_its_parameters():
    tool = server.aiworkhub_agent_accept_review

    assert not inspect.iscoroutinefunction(tool)
    assert list(inspect.signature(tool).parameters) == _ACCEPT_PARAMETERS


@_needs_sdk
def test_the_published_accept_tool_keeps_its_parameters():
    listed = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    schema = listed["aiworkhub_agent_accept_review"].inputSchema

    assert list(schema["properties"]) == _ACCEPT_PARAMETERS
    assert schema.get("required") == ["request_id", "task_id"]


_STDLIB_CHILD = """
import json

from aiworkhub import process_launcher, server

assert not server._MCP_SDK_AVAILABLE


class _Refusing:
    def accept_review(self, request_id, task_id, **kwargs):
        return {
            "ok": False,
            "error": "accept_refused_for_test",
            "request_id": request_id,
            "task_id": task_id,
        }


process_launcher.default_manager = lambda: _Refusing()
tools = server.mcp._tools
listed = server._stdio_dispatch("AIWorkHub MCP", tools, "tools/list", None, "", 1)
called = server._stdio_dispatch(
    "AIWorkHub MCP",
    tools,
    "tools/call",
    # auto_commit off: this child's repo root is the real checkout, and no test runs git there.
    {
        "name": "aiworkhub_agent_accept_review",
        "arguments": {"request_id": "R-1", "task_id": "T-1", "auto_commit": False},
    },
    "",
    2,
)
print(PREFIX + json.dumps({"listed": [entry["name"] for entry in listed["tools"]], "called": called}))
"""


def test_the_stdio_fallback_still_lists_and_calls_the_accept_tool():
    inherited = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    env = dict(
        os.environ,
        AIWORKHUB_ALLOW_WRITES="1",
        AIWORKHUB_MCP_STDIO_BACKEND="stdlib",
        PYTHONPATH=os.pathsep.join([str(_SRC), *inherited]),
    )
    script = f"PREFIX = {_PAYLOAD_PREFIX!r}\n{_STDLIB_CHILD}"
    completed = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=180
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    lines = [line for line in completed.stdout.splitlines() if line.startswith(_PAYLOAD_PREFIX)]
    assert len(lines) == 1, completed.stdout[-2000:]
    payload = json.loads(lines[0][len(_PAYLOAD_PREFIX):])

    assert "aiworkhub_agent_accept_review" in payload["listed"]
    assert "isError" not in payload["called"]
    assert list(payload["called"]) == ["content"]
    assert json.loads(payload["called"]["content"][0]["text"]) == {
        "ok": False,
        "error": "accept_refused_for_test",
        "request_id": "R-1",
        "task_id": "T-1",
    }
