from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import agent_tool_instructions as instr  # noqa: E402


def test_canonical_order_is_exact_and_compact() -> None:
    text = instr.render_canonical()
    expected = [
        "1. validate the injected AIWorkHub Task MCP receipt, identity and scope.",
        "2. consume and acknowledge the injected project-context receipt.",
        "3. call aiworkhub_worker_source_graph_query for code discovery, slicing, impact and exact read targets.",
        "4. call aiworkhub_worker_session_current_state for non-trivial continuity.",
        "5. call aiworkhub_worker_ai_memory_search for one bounded task-specific memory query.",
        "6. call aiworkhub_worker_kb_search/get/related for unresolved authoritative project facts.",
        "7. execute exact card action and validation.",
    ]
    positions = [text.index(item) for item in expected]
    assert positions == sorted(positions)
    assert len(text.encode("utf-8")) <= instr.CANONICAL_MAX_BYTES


def test_adaptive_invocation_without_empty_ceremony() -> None:
    text = instr.render_canonical()
    assert "AIWorkHub MCP is the only authority for project AI tools" in text
    assert "Task MCP receipt is always required; Source Graph is required for code tasks." in text
    assert "run only when the card requests them or the task is non-trivial" in text
    assert "Do not make empty irrelevant calls" in text


def test_source_graph_required_blocks_raw_discovery_fallback() -> None:
    text = instr.render_canonical()
    assert "source_graph_required is true" in text
    assert "stop if its bundle is unavailable, empty, stale or unacknowledged" in text
    for forbidden in ("grep", "rg", "find", "tree", "broad cat/sed", "recursive listing"):
        assert forbidden in text
    assert "manual full-repository parsing" in text


def test_exact_validation_exception_is_not_discovery_escape_hatch() -> None:
    text = instr.render_canonical()
    assert "Exact validation/build/test commands named by the card are allowed." in text
    assert "Exact known-path reads from the card or Source Graph are allowed" in text
    assert "they are not broad discovery" in text


def test_session_memory_kb_are_tool_use_only_and_zero_hit_is_bounded() -> None:
    text = instr.render_canonical()
    assert "Never store secrets or fabricate session evidence." in text
    assert "issue one bounded task-specific query" in text
    assert "Do not query legacy memory files directly." in text
    assert "After a zero hit, do not repeat the query unless task scope changes." in text
    assert "authoritative project contracts/docs" in text


def test_three_provider_projections_derive_from_same_policy_and_stay_bounded() -> None:
    canonical = instr.render_canonical()
    rendered = instr.render_all()
    assert tuple(rendered) == instr.PROVIDERS
    for provider, text in rendered.items():
        assert provider in text
        assert canonical in text
        assert text.count(instr.START) == 1
        assert text.count(instr.END) == 1
        assert len(text.encode("utf-8")) <= instr.PROJECTION_MAX_BYTES
    bodies = {
        text.split("\n", 2)[2].rsplit(instr.END, 1)[0]
        for text in rendered.values()
    }
    assert bodies == {canonical}


def test_unsupported_provider_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported_provider"):
        instr.render_projection("README.md")  # type: ignore[arg-type]


def test_managed_blocks_are_idempotent_and_preserve_owner_text() -> None:
    owner = "# Local notes\n\nKeep this.\n"
    first = instr.build_apply_plan("AGENTS.md", owner)
    assert first["ok"] is True
    assert first["reason"] == "append_managed_block"
    assert first["planned_text"].startswith(owner)
    second = instr.build_apply_plan("AGENTS.md", first["planned_text"])
    assert second["ok"] is True
    assert second["reason"] == "replace_managed_block"
    assert second["planned_text"] == first["planned_text"]
    assert "Keep this." in second["planned_text"]


def test_corrupt_or_duplicate_markers_fail_closed_without_mutation() -> None:
    corrupt = f"owner\n{instr.START}\nold\n"
    duplicate = f"{instr.START}\nold\n{instr.END}\n{instr.START}\nold2\n{instr.END}\n"
    for text in (corrupt, duplicate):
        plan = instr.build_apply_plan("CLAUDE.md", text)
        assert plan["ok"] is False
        assert plan["reason"] == "managed_block_marker_corrupt_or_duplicate"
        assert plan["planned_text"] == text
        assert plan["current_text"] == text


def test_diff_api_is_non_mutating() -> None:
    owner = "# Copilot\n"
    diff = instr.diff_projection(".github/copilot-instructions.md", owner)
    assert "--- a/.github/copilot-instructions.md" in diff
    assert "+++ b/.github/copilot-instructions.md" in diff
    assert instr.START in diff


def test_eval_artifact_matches_rendered_policy() -> None:
    path = Path(__file__).resolve().parents[1] / "eval" / "aiworkhub_agent_tool_instruction_generator_b816_v2.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_id"] == "aiworkhub.task_mcp.agent_tool_instruction_generator_b816_v2.eval.v1"
    assert payload["canonical_sha256"] == payload["projections"]["AGENTS.md"]["canonical_sha256"]
    assert payload["canonical_bytes"] == len(instr.render_canonical().encode("utf-8"))
    for provider in instr.PROVIDERS:
        assert payload["projections"][provider]["bytes"] == len(
            instr.render_projection(provider).encode("utf-8")
        )
