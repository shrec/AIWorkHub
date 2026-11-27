"""NF-2026-00965 phase 1: protocol negotiation and tools/list envelope.

Both stdio paths -- ``server._stdio_dispatch`` (stdlib FastMCP fallback) and
``stdio_fastmcp._dispatch`` (worker MCP) -- echo a supported requested
``protocolVersion`` and otherwise answer the newest; list tools sorted by
name with ``resultType``/``ttlMs``/``cacheScope``; and the worker path carries
the same ``derive_annotations`` hints the server path emits.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import mcp_tool_metadata, stdio_fastmcp  # noqa: E402

NEWEST = "2025-11-25"


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


def _worker_tools() -> dict:
    def zeta_list(limit: int = 1) -> dict:
        """READ-ONLY: list zeta rows."""
        return {"limit": limit}

    def alpha_purge(task_id: str) -> dict:
        """DESTRUCTIVE: purge one alpha record."""
        return {"task_id": task_id}

    def mid_write(value: str) -> dict:
        """Write one value."""
        return {"value": value}

    return {"zeta_list": zeta_list, "alpha_purge": alpha_purge, "mid_write": mid_write}


def test_supported_tuple_and_helper_have_one_owner(fallback_server):
    assert stdio_fastmcp.SUPPORTED_PROTOCOL_VERSIONS == (
        "2024-11-05",
        "2025-03-26",
        "2025-06-18",
        "2025-11-25",
    )
    assert fallback_server.negotiate_protocol_version is stdio_fastmcp.negotiate_protocol_version
    assert not hasattr(fallback_server, "_FALLBACK_PROTOCOL_VERSION")
    assert not hasattr(stdio_fastmcp, "PROTOCOL_VERSION")


@pytest.mark.parametrize("version", stdio_fastmcp.SUPPORTED_PROTOCOL_VERSIONS)
def test_worker_initialize_echoes_supported_version(version):
    result = stdio_fastmcp._dispatch("srv", {}, "initialize", {"protocolVersion": version}, 1)
    assert result["protocolVersion"] == version


@pytest.mark.parametrize("version", stdio_fastmcp.SUPPORTED_PROTOCOL_VERSIONS)
def test_server_initialize_echoes_supported_version(fallback_server, version):
    result = fallback_server._stdio_dispatch(
        "srv", {}, "initialize", {"protocolVersion": version}, "", 1
    )
    assert result["protocolVersion"] == version


@pytest.mark.parametrize(
    "params",
    [None, {}, {"protocolVersion": "1999-01-01"}, {"protocolVersion": 20251125}, {"protocolVersion": None}],
)
def test_both_paths_answer_newest_for_unknown_missing_or_non_string(fallback_server, params):
    worker = stdio_fastmcp._dispatch("srv", {}, "initialize", params, 1)
    server = fallback_server._stdio_dispatch("srv", {}, "initialize", params, "", 1)
    assert worker["protocolVersion"] == NEWEST
    assert server["protocolVersion"] == NEWEST


def _assert_envelope(result: dict) -> None:
    # NF-2026-00993: an initialize-era tools/list is exactly ListToolsResult;
    # the 2026-07-28 cache envelope is stamped on stateless requests only.
    names = [entry["name"] for entry in result["tools"]]
    assert names == sorted(names)
    assert set(result) == {"tools"}


_STATELESS_META = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}


def _assert_stateless_envelope(result: dict) -> None:
    names = [entry["name"] for entry in result["tools"]]
    assert names == sorted(names)
    assert result["resultType"] == "complete"
    assert result["cacheScope"] == "private"
    assert result["ttlMs"] == stdio_fastmcp.TOOLS_LIST_TTL_MS
    assert isinstance(result["ttlMs"], int) and result["ttlMs"] > 0


def test_both_paths_stamp_the_cache_envelope_only_on_stateless_requests(fallback_server):
    worker = stdio_fastmcp._dispatch("srv", _worker_tools(), "tools/list", _STATELESS_META, 1)
    server = fallback_server._stdio_dispatch(
        "srv", fallback_server.mcp._tools, "tools/list", _STATELESS_META, "", 1
    )
    _assert_stateless_envelope(worker)
    _assert_stateless_envelope(server)
    assert worker["tools"] == stdio_fastmcp._dispatch(
        "srv", _worker_tools(), "tools/list", {}, 2
    )["tools"]


def test_worker_tools_list_is_sorted_annotated_and_enveloped():
    result = stdio_fastmcp._dispatch("srv", _worker_tools(), "tools/list", {}, 1)
    _assert_envelope(result)
    assert [entry["name"] for entry in result["tools"]] == ["alpha_purge", "mid_write", "zeta_list"]
    for entry in result["tools"]:
        expected = mcp_tool_metadata.derive_annotations(
            mcp_tool_metadata.canonical_tool_name(entry["name"]), entry["description"]
        )
        assert entry["annotations"] == expected
    by_name = {entry["name"]: entry for entry in result["tools"]}
    assert by_name["zeta_list"]["annotations"]["readOnlyHint"] is True
    assert by_name["alpha_purge"]["annotations"]["destructiveHint"] is True


def test_worker_advertised_tools_match_dispatch():
    server = stdio_fastmcp.FallbackFastMCP("srv")
    for name, function in _worker_tools().items():
        server.tool(name=name)(function)
    listed = stdio_fastmcp._dispatch("srv", server._tools, "tools/list", {}, 1)["tools"]
    assert server.advertised_tools() == listed


def test_server_tools_list_is_sorted_enveloped_and_bounded(fallback_server):
    result = fallback_server._stdio_dispatch(
        "srv", fallback_server.mcp._tools, "tools/list", {}, "", 1
    )
    _assert_envelope(result)
    assert result["tools"], "the stdlib server must list its registry"
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": result}, ensure_ascii=False, default=str
    ).encode("utf-8")
    assert len(payload) < fallback_server._FALLBACK_MAX_RESPONSE_BYTES


def test_server_tools_list_order_is_independent_of_registration_order(fallback_server):
    tools = fallback_server.mcp._tools
    reversed_tools = dict(reversed(list(tools.items())))
    forward = fallback_server._stdio_dispatch("srv", tools, "tools/list", {}, "", 1)
    backward = fallback_server._stdio_dispatch("srv", reversed_tools, "tools/list", {}, "", 2)
    assert forward == backward
