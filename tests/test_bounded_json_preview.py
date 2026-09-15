from __future__ import annotations

import hashlib
import json

import pytest

from aiworkhub import output_spill_store, project_context, worker_ai_tools_mcp

# worker_ai_tools_mcp now spills the full original via output_spill_store
# before building its preview (RM-2026-00044), so its envelope carries a
# spill locator, retrieval hint and telemetry block the project_context
# variant does not -- it needs a larger cap to keep priority-key content
# intact rather than falling back to a truncated placeholder.
_CASES = (
    ("project_context", 1800),
    ("worker_ai_tools_mcp", 2900),
)


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


def _render(kind: str, text: str, cap: int, repo) -> tuple[str, bool]:
    if kind == "project_context":
        return project_context._canonical_json_output(
            "source_graph", text, max_bytes=cap
        )
    return worker_ai_tools_mcp._canonical_json_output(
        "source_graph", text, max_bytes=cap, repo=repo
    )


def _assert_spill_round_trips(
    envelope: dict[str, object], repo, bounded: str
) -> None:
    locator = envelope["spill_locator"]
    assert isinstance(locator, str) and locator.startswith("aiworkhub-spill-sha256:")
    assert str(repo) not in locator
    original = output_spill_store.retrieve_text(locator, repo=repo)
    assert (
        hashlib.sha256(original.encode("utf-8")).hexdigest()
        == envelope["original_sha256"]
    )
    telemetry = envelope["telemetry"]
    assert telemetry["provider_token_savings"] == "UNKNOWN"
    assert telemetry["spilled_bytes"] == envelope["original_bytes"]
    assert telemetry["presented_bytes"] == len(bounded.encode("utf-8"))


@pytest.mark.parametrize("kind, cap", _CASES)
def test_truncated_json_preview_preserves_semantic_priority_keys(
    kind, cap, tmp_path
) -> None:
    bounded, truncated = _render(
        kind, json.dumps(_payload(), ensure_ascii=False), cap, tmp_path
    )

    assert truncated is True
    assert len(bounded.encode("utf-8")) <= cap
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
    if kind == "worker_ai_tools_mcp":
        _assert_spill_round_trips(envelope, tmp_path, bounded)


@pytest.mark.parametrize("kind, cap", _CASES)
def test_nested_symbol_preview_prioritizes_semantic_identity(kind, cap, tmp_path) -> None:
    symbol = {f"aaa_noise_{index}": "x" * 200 for index in range(14)}
    symbol.update({
        "name": "critical_symbol",
        "qualname": "pkg.module.critical_symbol",
        "file_path": "pkg/module.py",
        "priority_score": 99,
    })
    payload = {"mode": "focus", "query": "critical", "ranked_symbols": [symbol]}

    bounded, truncated = _render(kind, json.dumps(payload), cap, tmp_path)

    assert truncated is True
    envelope = json.loads(bounded)
    preview = envelope["preview"]["ranked_symbols"][0]
    assert preview["name"] == "critical_symbol"
    assert preview["qualname"] == "pkg.module.critical_symbol"
    assert preview["file_path"] == "pkg/module.py"
    assert preview["priority_score"] == 99
    if kind == "worker_ai_tools_mcp":
        _assert_spill_round_trips(envelope, tmp_path, bounded)


@pytest.mark.parametrize("kind, cap", _CASES)
def test_small_json_remains_complete(kind, cap, tmp_path) -> None:
    bounded, truncated = _render(kind, '{"b":2,"a":1}', cap, tmp_path)

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
