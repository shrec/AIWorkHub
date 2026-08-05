from __future__ import annotations

import json

import pytest

from aiworkhub import project_context, worker_ai_tools_mcp


def _payload() -> dict[str, object]:
    return {
        "aaa_large_prefix": [
            {"path": f"src/module_{index}.py", "body": "x" * 1200}
            for index in range(20)
        ],
        "mode": "focus",
        "query": "quality review boundary",
        "ranked_symbols": [{"name": "critical_symbol", "score": 99}],
        "related_tests": ["tests/test_critical.py"],
        "risks": [{"reason": "authority_boundary"}],
        "todos": [{"text": "repair exact boundary"}],
        "recommended_next_steps": ["run impact before edit"],
        "zzz_tail": "tail" * 1000,
    }


@pytest.mark.parametrize(
    "renderer",
    [
        lambda text, cap: project_context._canonical_json_output(
            "source_graph", text, max_bytes=cap
        ),
        lambda text, cap: worker_ai_tools_mcp._canonical_json_output(
            "source_graph", text, max_bytes=cap
        ),
    ],
)
def test_truncated_json_preview_preserves_semantic_priority_keys(renderer) -> None:
    bounded, truncated = renderer(json.dumps(_payload(), ensure_ascii=False), 1800)

    assert truncated is True
    assert len(bounded.encode("utf-8")) <= 1800
    envelope = json.loads(bounded)
    assert envelope["preview_semantics"] == "structure_aware_priority_preserving"
    preview = envelope["preview"]
    for key in (
        "ranked_symbols",
        "related_tests",
        "risks",
        "todos",
        "recommended_next_steps",
    ):
        assert key in preview
        assert key in envelope["priority_keys_present"]
    assert "critical_symbol" in json.dumps(preview["ranked_symbols"])
    assert len(envelope["original_sha256"]) == 64


@pytest.mark.parametrize(
    "renderer",
    [
        lambda text, cap: project_context._canonical_json_output(
            "source_graph", text, max_bytes=cap
        ),
        lambda text, cap: worker_ai_tools_mcp._canonical_json_output(
            "source_graph", text, max_bytes=cap
        ),
    ],
)
def test_nested_symbol_preview_prioritizes_semantic_identity(renderer) -> None:
    symbol = {f"aaa_noise_{index}": "x" * 200 for index in range(14)}
    symbol.update({
        "name": "critical_symbol",
        "qualname": "pkg.module.critical_symbol",
        "file_path": "pkg/module.py",
        "priority_score": 99,
    })
    payload = {"mode": "focus", "query": "critical", "ranked_symbols": [symbol]}

    bounded, truncated = renderer(json.dumps(payload), 1800)

    assert truncated is True
    preview = json.loads(bounded)["preview"]["ranked_symbols"][0]
    assert preview["name"] == "critical_symbol"
    assert preview["qualname"] == "pkg.module.critical_symbol"
    assert preview["file_path"] == "pkg/module.py"
    assert preview["priority_score"] == 99


@pytest.mark.parametrize(
    "renderer",
    [
        lambda text, cap: project_context._canonical_json_output(
            "source_graph", text, max_bytes=cap
        ),
        lambda text, cap: worker_ai_tools_mcp._canonical_json_output(
            "source_graph", text, max_bytes=cap
        ),
    ],
)
def test_small_json_remains_complete(renderer) -> None:
    bounded, truncated = renderer('{"b":2,"a":1}', 1800)

    assert truncated is False
    assert json.loads(bounded) == {"a": 1, "b": 2}


@pytest.mark.parametrize("mode", ["focus", "slice"])
def test_source_graph_orientation_modes_use_smallest_output_cap(mode) -> None:
    assert worker_ai_tools_mcp._source_graph_output_cap(mode) == 8 * 1024


@pytest.mark.parametrize(
    "mode",
    [
        "context", "impact", "trace", "deps", "coverage", "testmap",
        "calls", "symbols", "bottlenecks", "auditmap", "complexity",
    ],
)
def test_source_graph_analysis_modes_use_intermediate_output_cap(mode) -> None:
    assert worker_ai_tools_mcp._source_graph_output_cap(mode) == 12 * 1024


@pytest.mark.parametrize("mode", ["file", "body", "bundle", "hotspots", "stats"])
def test_source_graph_rich_modes_retain_global_output_cap(mode) -> None:
    assert (
        worker_ai_tools_mcp._source_graph_output_cap(mode)
        == worker_ai_tools_mcp.MAX_TOOL_OUTPUT_BYTES
    )
