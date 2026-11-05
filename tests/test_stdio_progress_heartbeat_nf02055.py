"""NF-2026-02055: the stdlib MCP backend heartbeats progress for opted-in tools.

The stdlib fallback server runs in a child process, so the real FastMCP.tool
and FastMCP.run wiring, the real dispatch_request and the real stdout framing
are exercised over pipes. Inside the child only two seams are changed: the
surface gate admits the injected ``nf02055_`` test tools, and the heartbeat
interval is shortened. No network and no host-only path is used.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
CHILD_TIMEOUT_SECONDS = 60

_CHILD = r'''
import time

import aiworkhub.server as server

if server.FastMCP.run.__code__.co_filename != server.__file__:
    raise SystemExit("nf02055: the stdlib FastMCP is not the active backend")

_original_callable = server.mcp_tool_callable


def _gate(name, surface=None):
    if isinstance(name, str) and name.startswith("nf02055_"):
        return True
    return _original_callable(name, surface)


server.mcp_tool_callable = _gate
server._MCP_PROGRESS_INTERVAL_SECONDS = 0.2


def nf02055_slow_opt_in():
    time.sleep(1.0)
    return {"ok": True}


def nf02055_slow_plain():
    time.sleep(1.0)
    return {"ok": True}


app = server.FastMCP("nf02055")
app.tool(name="nf02055_slow_opt_in", report_progress=True)(nf02055_slow_opt_in)
app.tool(name="nf02055_slow_plain")(nf02055_slow_plain)
app.run()
'''

INIT: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}


def _tools_call(request_id: int, name: str, meta: Any = None) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name, "arguments": {}}
    if meta is not None:
        params["_meta"] = meta
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}


def _run_child(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = "".join(json.dumps(request) + "\n" for request in requests).encode("utf-8")
    env = dict(os.environ)
    env["AIWORKHUB_MCP_STDIO_BACKEND"] = "stdlib"
    env["PYTHONPATH"] = str(SRC_ROOT)
    with subprocess.Popen(
        [sys.executable, "-B", "-c", _CHILD],
        cwd=str(REPO_ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(payload, timeout=CHILD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            pytest.fail("nf02055 child did not exit within the timeout")
    assert proc.returncode == 0, stderr.decode("utf-8", "replace")[-2000:]
    return [json.loads(line) for line in stdout.decode("utf-8").splitlines() if line.strip()]


def _response_index(frames: list[dict[str, Any]], request_id: int) -> int:
    matches = [
        index
        for index, frame in enumerate(frames)
        if frame.get("id") == request_id and "method" not in frame
    ]
    assert len(matches) == 1, frames
    return matches[0]


def _progress_frames(frames: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    return [
        (index, frame)
        for index, frame in enumerate(frames)
        if frame.get("method") == "notifications/progress"
    ]


@pytest.mark.parametrize("token", ["nf02055-token", 7])
def test_opted_in_slow_call_heartbeats_before_its_response(token: Any) -> None:
    frames = _run_child([INIT, _tools_call(2, "nf02055_slow_opt_in", {"progressToken": token})])
    response_at = _response_index(frames, 2)
    assert json.loads(frames[response_at]["result"]["content"][0]["text"]) == {"ok": True}

    progress = _progress_frames(frames)
    mine = [(index, frame) for index, frame in progress if frame["params"]["progressToken"] == token]
    assert mine, "expected at least one notifications/progress for the opted-in call"
    # Every heartbeat for this call precedes its response; none follows any response.
    assert all(index < response_at for index, _ in mine)
    assert not [index for index, _ in progress if index > response_at]
    for _, frame in mine:
        assert frame["jsonrpc"] == "2.0"
        assert set(frame["params"]) == {"progressToken", "progress"}
    values = [frame["params"]["progress"] for _, frame in mine]
    assert values == sorted(values)
    assert len(set(values)) == len(values), values


def test_calls_without_a_usable_progress_token_get_no_progress() -> None:
    # No _meta, an empty _meta, a bool token, a None token and a float token:
    # none of them is a str or int progressToken, so none may heartbeat.
    requests = [
        INIT,
        _tools_call(2, "nf02055_slow_opt_in"),
        _tools_call(3, "nf02055_slow_opt_in", {}),
        _tools_call(4, "nf02055_slow_opt_in", {"progressToken": True}),
        _tools_call(5, "nf02055_slow_opt_in", {"progressToken": None}),
        _tools_call(6, "nf02055_slow_opt_in", {"progressToken": 1.5}),
    ]
    frames = _run_child(requests)
    for request_id in (2, 3, 4, 5, 6):
        assert "result" in frames[_response_index(frames, request_id)]
    assert _progress_frames(frames) == []


def test_slow_tool_not_opted_in_gets_no_progress_even_with_a_token() -> None:
    frames = _run_child(
        [INIT, _tools_call(2, "nf02055_slow_plain", {"progressToken": "nf02055-token"})]
    )
    assert "result" in frames[_response_index(frames, 2)]
    assert _progress_frames(frames) == []
