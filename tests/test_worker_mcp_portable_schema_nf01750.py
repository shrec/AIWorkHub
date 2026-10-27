"""NF-2026-01750: every advertised worker MCP inputSchema is Gemini-portable.

Gemini workers on opencode_cli ended with MALFORMED_FUNCTION_CALL because the
advertised schemas carried $defs/$ref, additionalProperties and anyOf branches
of type null. ``build_server`` now rewrites only the advertised projection, on
both the FastMCP and the stdlib-fallback path; argument validation and tool
behaviour stay as they were.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import quality_reviewer
from aiworkhub import stdio_fastmcp
from aiworkhub import worker_ai_tools_mcp as worker_tools


FORBIDDEN_KEYS = frozenset(
    {"$ref", "$defs", "definitions", "additionalProperties", "propertyNames", "patternProperties", "$schema"}
)
GEMINI_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
HAS_MCP = importlib.util.find_spec("mcp") is not None


def _ctx(tmp_path: Path) -> worker_tools.WorkerToolContext:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir(parents=True)
    runtime.mkdir(parents=True)
    ledger = runtime / "audit.jsonl"
    ledger.write_text("", encoding="utf-8")
    key = runtime / "audit.key"
    key.write_bytes(b"k" * 32)
    return worker_tools.WorkerToolContext(
        task_id="PORTABLE_SCHEMA_TASK",
        runner="codex_worker",
        topic="implementation",
        request_id="c" * 32,
        repo=repo,
        authority_repo=repo,
        source_graph_targets=(),
        session_topic="implementation",
        audit_ledger_path=ledger,
        audit_hmac_key_path=key,
        quality_review_packet_path=None,
    )


def _assert_portable(name: str, schema: Any) -> None:
    stack: list[tuple[str, Any]] = [(name, schema)]
    while stack:
        where, value = stack.pop()
        if isinstance(value, dict):
            leaked = FORBIDDEN_KEYS & set(value)
            assert not leaked, (where, sorted(leaked))
            assert value.get("type") != "null", where
            assert not isinstance(value.get("type"), list), where
            for union in ("anyOf", "oneOf"):
                for branch in value.get(union, ()):
                    assert not (isinstance(branch, dict) and branch.get("type") == "null"), (where, union)
            if value.get("type") == "array":
                assert "items" in value, where
            stack.extend((f"{where}.{key}", child) for key, child in value.items())
        elif isinstance(value, list):
            stack.extend((f"{where}[{index}]", child) for index, child in enumerate(value))


def _fallback_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> stdio_fastmcp.FallbackFastMCP:
    # A None entry makes ``from mcp.server.fastmcp import FastMCP`` raise
    # ModuleNotFoundError, which is exactly the branch build_server falls back on.
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", None)
    server = worker_tools.build_server(_ctx(tmp_path))
    assert isinstance(server, stdio_fastmcp.FallbackFastMCP)
    return server


def _fastmcp_tools(server: Any) -> dict[str, dict[str, Any]]:
    return {tool.name: tool.inputSchema for tool in asyncio.run(server.list_tools())}


# --- the transform itself ---------------------------------------------------


PYDANTIC_SHAPED = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": {
        "Finding": {
            "type": "object",
            "title": "Finding",
            "properties": {
                "severity": {"type": "string", "title": "Severity"},
                "evidence": {
                    "anyOf": [{"type": "string"}, {"type": "object", "additionalProperties": True}],
                    "title": "Evidence",
                },
                "line": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None},
            },
            "required": ["severity", "evidence"],
            "additionalProperties": False,
        }
    },
    "type": "object",
    "title": "_submitArguments",
    "properties": {
        "target": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Target"},
        "budget": {"type": "integer", "default": 64, "title": "Budget"},
        "mode": {"enum": ["focus", "body"], "type": "string", "title": "Mode"},
        "kind": {"const": "fixed", "type": "string"},
        "labels": {"type": ["array", "null"], "items": {"type": "string"}},
        "meta": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "propertyNames": {"pattern": "^[a-z]+$"},
            "patternProperties": {"^x": {"type": "string"}},
        },
        "findings": {"type": "array", "items": {"$ref": "#/$defs/Finding"}, "description": "rows"},
        "bare": {"type": "array"},
    },
    "required": ["mode", "findings"],
    "additionalProperties": False,
}


def test_portable_schema_projects_the_documented_subset_exactly() -> None:
    assert stdio_fastmcp.portable_input_schema(PYDANTIC_SHAPED) == {
        "type": "object",
        "properties": {
            "target": {"type": "string", "title": "Target"},
            "budget": {"type": "integer", "default": 64, "title": "Budget"},
            "mode": {"type": "string", "enum": ["focus", "body"], "title": "Mode"},
            "kind": {"type": "string", "enum": ["fixed"]},
            "labels": {"type": "array", "items": {"type": "string"}},
            "meta": {"type": "object"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string", "title": "Severity"},
                        "evidence": {
                            "title": "Evidence",
                            "anyOf": [{"type": "string"}, {"type": "object"}],
                        },
                        "line": {"type": "integer"},
                    },
                    "required": ["severity", "evidence"],
                    "title": "Finding",
                },
                "description": "rows",
            },
            "bare": {"type": "array", "items": {}},
        },
        "required": ["mode", "findings"],
        "title": "_submitArguments",
    }


def test_portable_schema_is_pure_and_deterministic() -> None:
    original = copy.deepcopy(PYDANTIC_SHAPED)
    first = stdio_fastmcp.portable_input_schema(PYDANTIC_SHAPED)
    second = stdio_fastmcp.portable_input_schema(PYDANTIC_SHAPED)
    assert PYDANTIC_SHAPED == original
    assert first == second
    assert list(first["properties"]) == list(PYDANTIC_SHAPED["properties"])
    assert stdio_fastmcp.portable_input_schema(first) == first


def test_portable_schema_keeps_a_collapsed_optional_field_optional() -> None:
    portable = stdio_fastmcp.portable_input_schema(PYDANTIC_SHAPED)
    assert "target" not in portable["required"]
    assert "line" not in portable["properties"]["findings"]["items"]["required"]


@pytest.mark.parametrize(
    ("schema", "cause"),
    [
        ({"type": "object", "properties": {"a": {"$ref": "#/$defs/Missing"}}}, "portable_schema_unresolved_ref:#/$defs/Missing"),
        (
            {
                "$defs": {"Node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/Node"}}}},
                "$ref": "#/$defs/Node",
            },
            "portable_schema_ref_cycle:#/$defs/Node",
        ),
        ("not-a-schema", "portable_schema_not_object:str"),
    ],
)
def test_portable_schema_refuses_unresolvable_input_by_name(schema: Any, cause: str) -> None:
    with pytest.raises(ValueError, match=re.escape(cause)):
        stdio_fastmcp.portable_input_schema(schema)


def test_portable_schema_refuses_unbounded_nesting() -> None:
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(stdio_fastmcp.PORTABLE_SCHEMA_MAX_DEPTH + 2):
        schema = {"type": "array", "items": schema}
    with pytest.raises(ValueError, match="portable_schema_too_deep"):
        stdio_fastmcp.portable_input_schema(schema)


# --- stdlib-fallback path ---------------------------------------------------


def test_fallback_path_advertises_portable_schemas_for_every_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _fallback_server(tmp_path, monkeypatch)
    tools = {entry["name"]: entry["inputSchema"] for entry in server.advertised_tools()}
    assert set(tools) == set(worker_tools.MCP_TOOL_NAMES)
    for name, schema in tools.items():
        assert GEMINI_NAME.match(name), name
        assert schema["type"] == "object"
        _assert_portable(name, schema)
    source = tools["aiworkhub_worker_source_graph_query"]
    assert source["properties"]["target"]["type"] == "string"
    assert "target" not in source.get("required", ())
    # The raw fallback projection still carried additionalProperties before.
    raw = stdio_fastmcp._schema_for(server._tools["aiworkhub_worker_source_graph_query"])
    assert "additionalProperties" in raw


def test_fallback_tools_call_validation_ignores_the_advertised_projection() -> None:
    def probe(query: str, target: str | None = None, evidence: str | dict[str, Any] = "") -> dict[str, Any]:
        return {"query": query, "target": target, "evidence": evidence}

    calls = [
        {"query": "q"},
        {"query": "q", "target": "a.py"},
        {"query": "q", "evidence": "text"},
        {"query": "q", "evidence": {"path": "a.py"}},
    ]
    results = {}
    for transform in (None, stdio_fastmcp.portable_input_schema):
        server = stdio_fastmcp.FallbackFastMCP("probe")
        server.tool(name="probe")(probe)
        server.input_schema_transform = transform
        rows = [
            stdio_fastmcp._dispatch(
                "probe", server._tools, "tools/call", {"name": "probe", "arguments": arguments},
                input_schema_transform=transform,
            )
            for arguments in calls
        ]
        with pytest.raises(stdio_fastmcp.ProtocolError, match="unexpected_arguments"):
            stdio_fastmcp._dispatch(
                "probe", server._tools, "tools/call", {"name": "probe", "arguments": {"extra": 1}},
                input_schema_transform=transform,
            )
        results[transform is None] = rows
    assert results[True] == results[False]
    assert '"target": "a.py"' in results[True][1]["content"][0]["text"]


# --- FastMCP path -----------------------------------------------------------


@pytest.mark.skipif(not HAS_MCP, reason="optional mcp package not installed; fallback path covered above")
def test_fastmcp_path_advertises_portable_schemas_for_every_tool(tmp_path: Path) -> None:
    server = worker_tools.build_server(_ctx(tmp_path))
    assert not isinstance(server, stdio_fastmcp.FallbackFastMCP)
    tools = _fastmcp_tools(server)
    assert set(tools) == set(worker_tools.MCP_TOOL_NAMES)
    for name, schema in tools.items():
        assert GEMINI_NAME.match(name), name
        _assert_portable(name, schema)
    for name in (
        "aiworkhub_worker_source_graph_query",
        "aiworkhub_worker_semantic_edit_prepare",
        "aiworkhub_worker_semantic_edit_apply",
        "aiworkhub_worker_validation_run",
    ):
        assert tools[name]["type"] == "object", name
    source = tools["aiworkhub_worker_source_graph_query"]
    assert source["properties"]["target"]["type"] == "string"
    assert "target" not in source["required"]
    finding = tools["aiworkhub_worker_quality_review_submit"]["properties"]["findings"]["items"]
    assert frozenset(finding["properties"]) == quality_reviewer.QUALITY_REVIEW_FINDING_INPUT_KEYS
    assert frozenset(finding["required"]) == quality_reviewer.QUALITY_REVIEW_FINDING_INPUT_REQUIRED_KEYS


@pytest.mark.skipif(not HAS_MCP, reason="optional mcp package not installed; fallback path covered above")
def test_fastmcp_path_argument_validation_is_unchanged(tmp_path: Path) -> None:
    from mcp.server.fastmcp import FastMCP

    portable = worker_tools.build_server(_ctx(tmp_path / "portable"))
    raw = FastMCP("AIWorkHub Worker AI Tools")
    worker_tools.register_tools(raw, _ctx(tmp_path / "raw"))
    raw_tools = {tool.name: tool for tool in raw._tool_manager.list_tools()}
    portable_tools = {tool.name: tool for tool in portable._tool_manager.list_tools()}
    assert set(raw_tools) == set(portable_tools)
    for name, tool in portable_tools.items():
        assert tool.fn_metadata.arg_model.model_json_schema() == (
            raw_tools[name].fn_metadata.arg_model.model_json_schema()
        ), name
    assert "anyOf" in str(raw_tools["aiworkhub_worker_source_graph_query"].parameters)

    source = portable_tools["aiworkhub_worker_source_graph_query"].fn_metadata.arg_model
    omitted = source.model_validate({"mode": "focus", "query": "q"})
    passed = source.model_validate({"mode": "focus", "query": "q", "target": "a.py"})
    assert omitted.target is None
    assert passed.target == "a.py"

    submit = portable_tools["aiworkhub_worker_quality_review_submit"].fn_metadata.arg_model
    for evidence in ("text evidence", {"path": "a.py", "line": 3}):
        row = {"severity": "low", "summary": "s", "evidence": evidence}
        model = submit.model_validate({"packet_sha256": "0" * 64, "lens": "general", "findings": [row]})
        assert model.findings[0]["evidence"] == evidence


@pytest.mark.parametrize(
    "schema",
    (
        {"anyOf": [{"type": "string"}, {"type": "integer"}], "oneOf": [{"type": "string"}, {"type": "boolean"}]},
        {"type": ["string", "integer"], "anyOf": [{"type": "string"}, {"type": "boolean"}]},
        {"properties": {"x": {"type": ["string", "null", "integer"], "oneOf": [{"type": "string"}, {"type": "boolean"}]}}},
    ),
)
def test_two_live_unions_on_one_node_fail_closed(schema: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="portable_schema_union_conflict"):
        stdio_fastmcp.portable_input_schema(schema)


def test_a_union_that_collapses_to_one_branch_is_not_a_conflict() -> None:
    schema = {"anyOf": [{"type": "string"}, {"type": "null"}], "oneOf": [{"type": "string"}, {"type": "boolean"}]}
    assert stdio_fastmcp.portable_input_schema(schema) == {
        "type": "string",
        "anyOf": [{"type": "string"}, {"type": "boolean"}],
    }
