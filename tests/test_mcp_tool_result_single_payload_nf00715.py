"""NF-2026-00715: every stdio tools/call result carries its JSON exactly once.

Both dependency-free stdio paths -- ``server._stdio_tools_call`` and
``stdio_fastmcp._tool_result`` -- used to return the same JSON twice, as
``content[0].text`` and again as ``structuredContent``. No outputSchema is
declared in tools/list, so structuredContent is optional; the only production
structured reader already falls back to parsing the text block. Every surface
is now text-only, so a ~480 KB dashboard snapshot crosses the pipe once.
"""

from __future__ import annotations

import importlib.util
import json
from typing import Any

import pytest

from aiworkhub import server, stdio_fastmcp

_LARGE = {"rows": [{"id": index, "label": f"row-{index}", "ტექსტი": "ქართული"} for index in range(4000)]}
_RESULTS: dict[str, Any] = {
    "echo_dict": {"echo": "plain", "nested": {"items": [1, 2, 3], "ratio": 0.5, "flag": True, "none": None}},
    "echo_list": [1, "two", {"three": 3}],
    "echo_int": 7,
    "echo_none": None,
    "echo_text": "ტექსტი",
    "echo_large": _LARGE,
}


def _tools() -> dict[str, Any]:
    tools: dict[str, Any] = {name: (lambda value=value: value) for name, value in _RESULTS.items()}

    def failing() -> None:
        raise RuntimeError("boom")

    tools["failing"] = failing
    return tools


@pytest.fixture(scope="module")
def stdlib_server():
    """``server`` re-executed on the stdlib backend, where ``_stdio_tools_call`` lives."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("AIWORKHUB_MCP_STDIO_BACKEND", "stdlib")
        spec = importlib.util.spec_from_file_location("aiworkhub.server", server.__file__)
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "aiworkhub"
        spec.loader.exec_module(module)
    assert module._MCP_SDK_AVAILABLE is False, "stdlib stdio fallback must be active"
    return module


def _server_call(module, monkeypatch, surface: str, name: str) -> dict[str, Any]:
    monkeypatch.setenv(module.MCP_SURFACE_ENV, surface)
    return module._stdio_tools_call(_tools(), {"name": name, "arguments": {}})


def _fastmcp_call(name: str) -> dict[str, Any]:
    return stdio_fastmcp._tool_result(_tools(), {"name": name, "arguments": {}})


def _wire_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _assert_single_payload(response: dict[str, Any], value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, default=str)
    assert response == {"content": [{"type": "text", "text": text}]}
    assert "structuredContent" not in response
    assert json.loads(response["content"][0]["text"]) == value
    envelope = _wire_bytes({"content": [{"type": "text", "text": ""}]})
    # The empty envelope already counts the two quotes of the text string.
    assert _wire_bytes(response) == envelope + _wire_bytes(text) - 2


@pytest.mark.parametrize("surface", ["all", "manager"])
@pytest.mark.parametrize("name", sorted(_RESULTS))
def test_server_stdio_tools_call_returns_one_text_copy(stdlib_server, monkeypatch, surface, name) -> None:
    _assert_single_payload(_server_call(stdlib_server, monkeypatch, surface, name), _RESULTS[name])


@pytest.mark.parametrize("name", sorted(_RESULTS))
def test_stdio_fastmcp_tool_result_returns_one_text_copy(name) -> None:
    _assert_single_payload(_fastmcp_call(name), _RESULTS[name])


def test_large_result_crosses_the_pipe_once(stdlib_server, monkeypatch) -> None:
    text_bytes = len(json.dumps(_LARGE, ensure_ascii=False).encode("utf-8"))
    assert text_bytes > 100_000
    for response in (
        _server_call(stdlib_server, monkeypatch, "all", "echo_large"),
        _server_call(stdlib_server, monkeypatch, "manager", "echo_large"),
        _fastmcp_call("echo_large"),
    ):
        # A second copy would roughly double the response; only escaping overhead remains.
        assert _wire_bytes(response) < text_bytes * 1.5


def test_tool_errors_are_unchanged(stdlib_server, monkeypatch) -> None:
    expected = {"content": [{"type": "text", "text": "boom"}], "isError": True}
    assert _server_call(stdlib_server, monkeypatch, "all", "failing") == expected
    assert _server_call(stdlib_server, monkeypatch, "manager", "failing") == expected
    assert _fastmcp_call("failing") == expected
