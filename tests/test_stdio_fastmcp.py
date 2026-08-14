"""Fail-closed JSON-RPC params guard for the dependency-free stdio fallback.

Covers the shared params guard applied in ``stdio_fastmcp._dispatch`` before
any tool/child dispatch: empty-string and non-object params must be rejected
with a structured reason, schema-valid absent/empty-object params must still
be accepted, and one bounded redacted durable protocol-alert record must be
written when a repository root is known.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import stdio_fastmcp  # noqa: E402


def _tools() -> dict:
    return {}


def test_dispatch_rejects_empty_string_params(monkeypatch):
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    with pytest.raises(stdio_fastmcp.ProtocolError) as exc_info:
        stdio_fastmcp._dispatch("srv", _tools(), "ping", "", "req-1")
    assert exc_info.value.code == -32602
    assert exc_info.value.message == "invalid_params:empty_string"


@pytest.mark.parametrize(
    "bad_params",
    [[], [1, 2], 5, 1.5, True, "not empty"],
)
def test_dispatch_rejects_non_object_params(monkeypatch, bad_params):
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    with pytest.raises(stdio_fastmcp.ProtocolError) as exc_info:
        stdio_fastmcp._dispatch("srv", _tools(), "ping", bad_params, "req-2")
    assert exc_info.value.code == -32602
    assert exc_info.value.message == f"invalid_params:non_object:{type(bad_params).__name__}"


def test_dispatch_accepts_absent_params_as_empty_object(monkeypatch):
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    assert stdio_fastmcp._dispatch("srv", _tools(), "ping", None, "req-3") == {}


def test_dispatch_accepts_valid_empty_object_params(monkeypatch):
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    assert stdio_fastmcp._dispatch("srv", _tools(), "ping", {}, "req-4") == {}


def test_dispatch_still_dispatches_valid_object_params(monkeypatch):
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    result = stdio_fastmcp._dispatch(
        "srv", _tools(), "initialize", {"clientInfo": {"name": "x"}}, "req-5"
    )
    assert result["serverInfo"]["name"] == "srv"


def test_invalid_params_records_one_bounded_durable_alert(tmp_path, monkeypatch):
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(tmp_path))
    alert_path = tmp_path / ".aiworkhub" / "runtime" / "mcp_protocol_alerts.json"

    with pytest.raises(stdio_fastmcp.ProtocolError):
        stdio_fastmcp._dispatch("srv", _tools(), "tools/call", "", "req-a")

    payload = json.loads(alert_path.read_text(encoding="utf-8"))
    assert payload["count"] == 1
    latest = payload["latest"]
    assert latest["method"] == "tools/call"
    assert latest["request_id"] == "req-a"
    assert latest["boundary"] == "stdio_fastmcp"
    assert latest["reason"] == "invalid_params:empty_string"
    assert latest["repo_identity"] == tmp_path.name
    assert isinstance(latest["timestamp"], str) and latest["timestamp"]
    # Bounded/redacted: the raw (rejected) params value is never persisted.
    raw_text = alert_path.read_text(encoding="utf-8")
    assert '""' not in raw_text

    with pytest.raises(stdio_fastmcp.ProtocolError):
        stdio_fastmcp._dispatch("srv", _tools(), "tools/call", [1, 2], "req-b")

    payload = json.loads(alert_path.read_text(encoding="utf-8"))
    assert payload["count"] == 2
    assert payload["latest"]["reason"] == "invalid_params:non_object:list"
    assert payload["latest"]["request_id"] == "req-b"


def test_invalid_params_alert_is_best_effort_without_repo_root(tmp_path, monkeypatch):
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    with pytest.raises(stdio_fastmcp.ProtocolError):
        stdio_fastmcp._dispatch("srv", _tools(), "ping", "", "req-c")
    assert not (tmp_path / ".aiworkhub").exists()


def test_run_passes_raw_params_and_request_id_through_to_dispatch(monkeypatch):
    """``_run`` must forward the raw (possibly falsy) params value -- not the
    old ``params or {}`` coercion that silently turned "" into a valid {}."""

    captured = {}

    def fake_dispatch(server_name, tools, method, params, request_id):
        captured["method"] = method
        captured["params"] = params
        captured["request_id"] = request_id
        raise stdio_fastmcp.ProtocolError(-32602, "invalid_params:empty_string")

    monkeypatch.setattr(stdio_fastmcp, "_dispatch", fake_dispatch)

    written = []
    monkeypatch.setattr(stdio_fastmcp, "_write", lambda message: written.append(message))

    lines = iter([json.dumps({"jsonrpc": "2.0", "id": "rid-1", "method": "tools/call", "params": ""}).encode() + b"\n", b""])

    class FakeStdin:
        def readline(self, limit):
            return next(lines)

    monkeypatch.setattr(stdio_fastmcp.sys, "stdin", type("S", (), {"buffer": FakeStdin()})())

    stdio_fastmcp._run("srv", {})

    assert captured["params"] == ""
    assert captured["request_id"] == "rid-1"
    assert written[0]["error"]["message"] == "invalid_params:empty_string"
