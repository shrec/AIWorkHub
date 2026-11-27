"""NF-2026-00973: MCP 2026-07-28 stateless core and Tasks extension.

Both stdio paths -- ``server._stdio_dispatch`` (production stdlib backend) and
``stdio_fastmcp._dispatch`` (worker MCP) -- route through the one owner
``aiworkhub.mcp_stateless``: initialize stays exactly as before for
<=2025-11-25 clients, a ``_meta``-versioned 2026-07-28 request is served
without initialize, an unknown ``_meta`` version is refused with -32022,
``server/discover`` answers in both modes, and only a client that declares
``io.modelcontextprotocol/tasks`` gets a CreateTaskResult from
``aiworkhub_agent_launch_task``.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from aiworkhub import mcp_stateless, stdio_fastmcp, task_fsm  # noqa: E402

STATELESS = "2026-07-28"
NEWEST_INITIALIZE = "2025-11-25"
TASKS = mcp_stateless.TASKS_EXTENSION


@pytest.fixture()
def fallback_server(monkeypatch):
    """Import ``aiworkhub.server`` on its bounded stdlib MCP backend."""

    monkeypatch.setenv("AIWORKHUB_MCP_STDIO_BACKEND", "stdlib")
    monkeypatch.setenv("AIWORKHUB_MCP_SURFACE", "all")
    package = sys.modules.get("aiworkhub")
    saved_module = sys.modules.pop("aiworkhub.server", None)
    sentinel = object()
    saved_attr = package.__dict__.pop("server", sentinel) if package is not None else sentinel
    from aiworkhub import server as server_module

    yield server_module

    sys.modules.pop("aiworkhub.server", None)
    if saved_module is not None:
        sys.modules["aiworkhub.server"] = saved_module
    if package is not None:
        if saved_attr is sentinel:
            package.__dict__.pop("server", None)
        else:
            package.__dict__["server"] = saved_attr


@pytest.fixture(params=["worker", "server"])
def dispatcher(request):
    """One callable per stdio path: ``(tools, method, params) -> result``."""

    if request.param == "worker":
        def call(tools: dict, method: str, params: Any) -> Any:
            return stdio_fastmcp._dispatch("srv", tools, method, params, 1)

        call.error_type = stdio_fastmcp.ProtocolError  # type: ignore[attr-defined]
        return call
    server = request.getfixturevalue("fallback_server")

    def call(tools: dict, method: str, params: Any) -> Any:  # type: ignore[no-redef]
        return server._stdio_dispatch("srv", tools, method, params, "", 1)

    call.error_type = server._StdioProtocolError  # type: ignore[attr-defined]
    return call


class FakeLauncher:
    """Fake launch/status/cancel tools registered under the real tool names."""

    def __init__(self) -> None:
        self.launches: list[str] = []
        self.cancels: list[tuple[str, str]] = []
        self.status: dict[str, Any] = {"state": "running", "task_card": {"status": "processing"}}
        self.known = {"req-1"}

    def tools(self) -> dict[str, Any]:
        def aiworkhub_agent_launch_task(task_id: str, owner_prompt: str = "") -> dict:
            """DUAL-GATED: launch one worker."""
            self.launches.append(task_id)
            return {"ok": True, "request_id": "req-1", "task_id": task_id, "state": "starting"}

        def aiworkhub_agent_task_status(request_id: str = "", detail: str = "brief") -> dict:
            """READ-ONLY: inspect one launched request."""
            if request_id not in self.known:
                return {"ok": False, "request_id": request_id, "state": "not_found"}
            return {"ok": True, "request_id": request_id, **self.status}

        def aiworkhub_agent_cancel_task(request_id: str, reason: str = "owner_cancelled") -> dict:
            """DUAL-GATED: cancel one launched request."""
            self.cancels.append((request_id, reason))
            return {"ok": True, "request_id": request_id, "state": "cancelled"}

        return {
            "aiworkhub_agent_launch_task": aiworkhub_agent_launch_task,
            "aiworkhub_agent_task_status": aiworkhub_agent_task_status,
            "aiworkhub_agent_cancel_task": aiworkhub_agent_cancel_task,
        }


def _meta(*, tasks: bool = False, version: Any = STATELESS) -> dict[str, Any]:
    capabilities: dict[str, Any] = {}
    if tasks:
        capabilities["extensions"] = {TASKS: {}}
    return {
        mcp_stateless.META_PROTOCOL_VERSION: version,
        mcp_stateless.META_CLIENT_CAPABILITIES: capabilities,
        mcp_stateless.META_CLIENT_INFO: {"name": "test-client", "version": "1"},
    }


def _unstamped(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key not in ("resultType", "_meta")}


def _assert_stamped(result: dict[str, Any], result_type: str = "complete") -> None:
    assert result["resultType"] == result_type
    assert result["_meta"][mcp_stateless.META_SERVER_INFO]["name"] == "srv"


def _launch_params(meta: dict[str, Any] | None) -> dict[str, Any]:
    params: dict[str, Any] = {"name": "aiworkhub_agent_launch_task", "arguments": {"task_id": "T-1"}}
    if meta is not None:
        params["_meta"] = meta
    return params


def _echo_tools() -> dict[str, Any]:
    def echo(text: str) -> dict:
        """READ-ONLY: echo text."""
        return {"text": text}

    return {"echo": echo}


def test_supported_versions_keep_initialize_vocabulary_unchanged():
    assert STATELESS not in stdio_fastmcp.SUPPORTED_PROTOCOL_VERSIONS
    assert stdio_fastmcp.LATEST_PROTOCOL_VERSION == NEWEST_INITIALIZE
    assert mcp_stateless.supported_protocol_versions() == (
        *stdio_fastmcp.SUPPORTED_PROTOCOL_VERSIONS,
        STATELESS,
    )


def test_server_imports_the_one_owner(fallback_server):
    assert fallback_server._stdio_stateless is mcp_stateless
    assert stdio_fastmcp.mcp_stateless is mcp_stateless


@pytest.mark.parametrize("requested", [STATELESS, NEWEST_INITIALIZE, "2024-11-05", None])
def test_initialize_is_unchanged_and_never_offers_2026(dispatcher, requested):
    params = {} if requested is None else {"protocolVersion": requested}
    result = dispatcher({}, "initialize", params)
    expected = requested if requested in stdio_fastmcp.SUPPORTED_PROTOCOL_VERSIONS else NEWEST_INITIALIZE
    assert result["protocolVersion"] == expected
    assert result["protocolVersion"] != STATELESS
    assert "resultType" not in result and "_meta" not in result
    assert result["capabilities"] == {"tools": {}, "resources": {}}


def test_legacy_requests_are_byte_identical_without_meta_version(dispatcher):
    tools = _echo_tools()
    params = {"name": "echo", "arguments": {"text": "hi"}}
    legacy = dispatcher(tools, "tools/call", params)
    assert legacy == {"content": [{"type": "text", "text": json.dumps({"text": "hi"})}]}
    # A legacy client's _meta (progress token only) does not switch modes.
    with_progress = dispatcher(tools, "tools/call", {**params, "_meta": {"progressToken": 7}})
    assert with_progress == legacy
    assert dispatcher(tools, "ping", {}) == {}


def test_stateless_request_is_served_without_initialize(dispatcher):
    tools = _echo_tools()
    legacy_list = dispatcher(tools, "tools/list", {})
    result = dispatcher(tools, "tools/list", {"_meta": _meta()})
    _assert_stamped(result)
    assert result["tools"] == legacy_list["tools"]
    call = dispatcher(tools, "tools/call", {"name": "echo", "arguments": {"text": "x"}, "_meta": _meta()})
    _assert_stamped(call)
    assert _unstamped(call) == {"content": [{"type": "text", "text": json.dumps({"text": "x"})}]}


@pytest.mark.parametrize("version", ["2099-01-01", "", 20260728, None])
def test_unknown_meta_version_is_refused_with_32022(dispatcher, version):
    with pytest.raises(dispatcher.error_type) as caught:
        dispatcher(_echo_tools(), "tools/list", {"_meta": _meta(version=version)})
    exc = caught.value
    assert exc.code == mcp_stateless.UNSUPPORTED_PROTOCOL_VERSION == -32022
    supported = list(mcp_stateless.supported_protocol_versions())
    assert exc.data["supported"] == supported
    for name in supported:
        assert name in exc.message
    assert mcp_stateless.error_object(exc)["data"]["supported"] == supported


@pytest.mark.parametrize("method", ["initialize", "ping", "logging/setLevel"])
def test_methods_removed_in_2026_are_refused_only_for_2026(dispatcher, method):
    with pytest.raises(dispatcher.error_type) as caught:
        dispatcher({}, method, {"_meta": _meta()})
    assert caught.value.code == -32601


@pytest.mark.parametrize("meta", [None, "stateless", "unknown"])
def test_server_discover_answers_in_both_modes(dispatcher, meta):
    params: dict[str, Any] = {}
    if meta == "stateless":
        params["_meta"] = _meta()
    elif meta == "unknown":
        params["_meta"] = _meta(version="2099-01-01")
    result = dispatcher(_echo_tools(), "server/discover", params)
    _assert_stamped(result)
    assert result["serverInfo"]["name"] == "srv"
    assert result["capabilities"] == {"tools": {}, "resources": {}}
    assert result["supportedVersions"] == list(mcp_stateless.supported_protocol_versions())
    assert result["ttlMs"] == mcp_stateless.DISCOVER_TTL_MS > 0
    assert result["cacheScope"] == "private"


def test_server_discover_advertises_tasks_only_with_task_tools(dispatcher):
    result = dispatcher(FakeLauncher().tools(), "server/discover", {"_meta": _meta()})
    assert result["capabilities"]["extensions"] == {TASKS: {}}


def test_launch_without_declared_extension_is_unchanged(dispatcher):
    launcher = FakeLauncher()
    tools = launcher.tools()
    legacy = dispatcher(tools, "tools/call", _launch_params(None))
    assert legacy == {
        "content": [{
            "type": "text",
            "text": json.dumps({"ok": True, "request_id": "req-1", "task_id": "T-1", "state": "starting"}),
        }]
    }
    stateless = dispatcher(tools, "tools/call", _launch_params(_meta(tasks=False)))
    _assert_stamped(stateless)
    assert _unstamped(stateless) == legacy
    assert "taskId" not in stateless
    # Declaring tasks on a legacy (no _meta version) request is not an opt-in.
    legacy_meta = {mcp_stateless.META_CLIENT_CAPABILITIES: {"extensions": {TASKS: {}}}}
    assert dispatcher(tools, "tools/call", _launch_params(legacy_meta)) == legacy
    assert launcher.launches == ["T-1", "T-1", "T-1"]


def test_task_create_get_cancel(dispatcher):
    launcher = FakeLauncher()
    tools = launcher.tools()
    meta = _meta(tasks=True)
    created = dispatcher(tools, "tools/call", _launch_params(meta))
    _assert_stamped(created, "task")
    assert created["taskId"] == "req-1"
    assert created["status"] == "working"
    assert created["ttlMs"] == mcp_stateless.TASK_TTL_MS
    assert created["pollIntervalMs"] == mcp_stateless.TASK_POLL_INTERVAL_MS
    assert "content" not in created

    polled = dispatcher(tools, "tasks/get", {"taskId": "req-1", "_meta": meta})
    _assert_stamped(polled)
    assert polled["taskId"] == "req-1" and polled["status"] == "working"
    assert "result" not in polled and "error" not in polled

    launcher.status = {"state": "review_ready", "task_card": {"status": "review"}}
    done = dispatcher(tools, "tasks/get", {"taskId": "req-1", "_meta": meta})
    assert done["status"] == "completed"
    assert done["result"]["resultType"] == "complete"
    payload = json.loads(done["result"]["content"][0]["text"])
    assert payload["state"] == "review_ready"

    launcher.status = {"state": "worker_failed", "task_card": {"status": "review"}}
    failed = dispatcher(tools, "tasks/get", {"taskId": "req-1", "_meta": meta})
    assert failed["status"] == "failed"
    assert failed["error"]["data"]["state"] == "worker_failed"
    assert "result" not in failed

    cancelled = dispatcher(tools, "tasks/cancel", {"taskId": "req-1", "_meta": meta})
    _assert_stamped(cancelled)
    assert _unstamped(cancelled) == {}
    assert launcher.cancels == [("req-1", mcp_stateless.TASK_CANCEL_REASON)]


def test_tasks_update_is_invalid_params(dispatcher):
    with pytest.raises(dispatcher.error_type) as caught:
        dispatcher(
            FakeLauncher().tools(),
            "tasks/update",
            {"taskId": "req-1", "inputResponses": {}, "_meta": _meta(tasks=True)},
        )
    assert caught.value.code == -32602


def test_tasks_methods_require_declared_extension(dispatcher):
    with pytest.raises(dispatcher.error_type) as caught:
        dispatcher(FakeLauncher().tools(), "tasks/get", {"taskId": "req-1", "_meta": _meta()})
    assert caught.value.code == mcp_stateless.MISSING_REQUIRED_CLIENT_CAPABILITY == -32021


def test_tasks_get_unknown_or_missing_task_is_invalid_params(dispatcher):
    tools = FakeLauncher().tools()
    for params in ({"taskId": "req-404"}, {}, {"taskId": ""}):
        with pytest.raises(dispatcher.error_type) as caught:
            dispatcher(tools, "tasks/get", {**params, "_meta": _meta(tasks=True)})
        assert caught.value.code == -32602


def test_tasks_methods_absent_without_task_tools(dispatcher):
    with pytest.raises(dispatcher.error_type) as caught:
        dispatcher(_echo_tools(), "tasks/get", {"taskId": "req-1", "_meta": _meta(tasks=True)})
    assert caught.value.code == -32601


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"state": "running"}, "working"),
        ({"state": "pending"}, "working"),
        ({"state": "starting"}, "working"),
        ({"state": "worker_failed"}, "failed"),
        ({"state": "launch_failed"}, "failed"),
        ({"state": "blocked"}, "failed"),
        ({"state": "exited_without_review"}, "failed"),
        ({"state": "review_ready"}, "completed"),
        ({"state": "accepted"}, "completed"),
        ({"state": "cancelled"}, "cancelled"),
        # state decides when present; the card is only a fallback.
        ({"state": "running", "task_card": {"status": "done"}}, "working"),
        ({"task_card": {"status": "done"}}, "completed"),
        ({"task_card": {"status": "blocked"}}, "failed"),
        ({"task_card": {"status": "failed"}}, "failed"),
        ({"task_card": {"status": "cancelled"}}, "cancelled"),
        ({"task_card": {"status": "archived"}}, "cancelled"),
        ({"task_card": {"status": "processing"}}, "working"),
        ({"task_card": "not-a-dict"}, "working"),
        # The brief payload has no task_state key; it must not decide anything.
        ({"task_state": "review_ready"}, "working"),
        ({}, "working"),
    ],
)
def test_task_status_mapping(status, expected):
    assert mcp_stateless.task_status_for(status) == expected


@pytest.mark.parametrize("state", sorted(task_fsm.LAUNCHER_TERMINAL_SUBSTATUSES))
def test_every_launcher_terminal_state_is_terminal(state):
    mapped = mcp_stateless.task_status_for({"state": state})
    assert mapped != "working"
    expected = {"review_ready": "completed", "cancelled": "cancelled"}.get(state, "failed")
    assert mapped == expected


def test_failed_set_is_derived_from_the_launcher_vocabulary():
    assert mcp_stateless.TASK_STATE_FAILED == (
        task_fsm.LAUNCHER_TERMINAL_SUBSTATUSES
        - mcp_stateless.TASK_STATE_COMPLETED
        - mcp_stateless.TASK_STATE_CANCELLED
    )
    assert "worker_failed" in mcp_stateless.TASK_STATE_FAILED


def test_worker_stdio_subprocess_speaks_both_modes(tmp_path):
    script = (
        "from aiworkhub.stdio_fastmcp import FallbackFastMCP\n"
        "server = FallbackFastMCP('srv')\n"
        "@server.tool()\n"
        "def echo(text: str) -> dict:\n"
        "    '''READ-ONLY: echo text.'''\n"
        "    return {'text': text}\n"
        "server.run()\n"
    )
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": STATELESS}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "echo", "arguments": {"text": "hi"}, "_meta": _meta()}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list",
         "params": {"_meta": _meta(version="2099-01-01")}},
        {"jsonrpc": "2.0", "id": 4, "method": "server/discover"},
    ]
    stdin = "".join(json.dumps(request) + "\n" for request in requests).encode("utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH", "")]))
    env.pop("AIWORKHUB_REPO_ROOT", None)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=stdin,
        capture_output=True,
        cwd=tmp_path,
        env=env,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    replies = {reply["id"]: reply for reply in map(json.loads, completed.stdout.splitlines())}
    assert replies[1]["result"]["protocolVersion"] == NEWEST_INITIALIZE
    assert "resultType" not in replies[1]["result"]
    assert replies[2]["result"]["resultType"] == "complete"
    assert replies[2]["result"]["_meta"][mcp_stateless.META_SERVER_INFO]["name"] == "srv"
    assert replies[3]["error"]["code"] == -32022
    assert replies[3]["error"]["data"]["supported"] == list(mcp_stateless.supported_protocol_versions())
    assert replies[4]["result"]["supportedVersions"][-1] == STATELESS


def test_server_stdio_loop_carries_32022_data(fallback_server, monkeypatch):
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": _meta(version="1999-01-01")}},
        {"jsonrpc": "2.0", "id": 2, "method": "server/discover", "params": {"_meta": _meta()}},
    ]
    stdin = io.BytesIO("".join(json.dumps(request) + "\n" for request in requests).encode("utf-8"))
    stdout = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=stdin))
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=stdout))
    fallback_server._run_stdio_fallback_server("srv", _echo_tools())
    replies = {reply["id"]: reply for reply in map(json.loads, stdout.getvalue().splitlines())}
    assert replies[1]["error"]["code"] == -32022
    assert replies[1]["error"]["data"]["supported"] == list(mcp_stateless.supported_protocol_versions())
    assert replies[2]["result"]["cacheScope"] == "private"
    assert replies[2]["result"]["resultType"] == "complete"
