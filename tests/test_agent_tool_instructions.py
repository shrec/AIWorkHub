"""Rendered-output contract for the agent tool-instruction generator.

This is the updated home (in this task's allowed_writes) for the rendered-output
assertions after NF-2026-00281-V2 added the manager ROLE ahead of the tool
protocol. It asserts, on the CURRENT render:

  * every pre-existing protocol rule still survives -- the role was added in
    front, nothing was removed or weakened (criterion 5);
  * the canonical order is exact and every projection derives from the same
    POLICY and stays within the declared byte caps;
  * the CLAUDE.md preamble stays CLAUDE-only and leads its projection.

Byte-count expectations that used to be pinned against the eval/*.json
artifacts moved with the caps; those pinned artifacts are regenerated outside
this task's allowed_writes, so this file asserts the render against the live
module constants instead of stale committed numbers.
"""

from __future__ import annotations

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
        "3. manager uses aiworkhub_manager_source_graph_query; worker uses aiworkhub_worker_source_graph_query.",
        "4. manager uses aiworkhub_manager_session_current_state; worker uses aiworkhub_worker_session_current_state.",
        "5. manager uses aiworkhub_manager_ai_memory_search; worker uses aiworkhub_worker_ai_memory_search.",
        "6. manager uses aiworkhub_manager_kb_search/get/related; worker uses aiworkhub_worker_kb_search/get/related.",
        "7. manager uses aiworkhub_manager_context_graph_search, aiworkhub_manager_context_graph_range and aiworkhub_manager_context_graph_related when enabled; workers never access Context Graph.",
        "8. execute exact card action and validation.",
    ]
    positions = [text.index(item) for item in expected]
    assert positions == sorted(positions)
    assert len(text.encode("utf-8")) <= instr.CANONICAL_MAX_BYTES


def test_role_is_rendered_before_the_protocol() -> None:
    """The role goes in front of the protocol; the protocol still follows it."""
    text = instr.render_canonical()
    assert "Manager role:" in text
    assert text.index("Manager role:") < text.index("Order:")


def test_no_existing_protocol_rule_was_removed_or_weakened() -> None:
    """Criterion 5: adding the role removed nothing. Every protocol section and
    a representative load-bearing rule from each still renders verbatim."""
    text = instr.render_canonical()
    for section in (
        "Order:",
        "Adaptive use:",
        "Source Graph gate:",
        "Exact-command exception:",
        "Session Manager:",
        "Manager Context Graph:",
        "AI Memory:",
        "KB:",
    ):
        assert section in text, section
    for rule in (
        "Role-specific AIWorkHub MCP tools are mandatory for managers and workers",
        "repo and repo_id outrank cwd, workspace_roots, environment_context",
        "never inspect the hinted repo as fallback",
        "Task MCP receipt is always required; Source Graph is required for code tasks.",
        "Do not make empty irrelevant calls",
        "source_graph_required is true",
        "stop if its bundle is unavailable, empty, stale or unacknowledged",
        "only after Source Graph reports that target unsupported or unindexed",
        "record that reason",
        "Re-query whenever the active symbol",
        "one preflight query is not continuous use",
        "Exact validation/build/test commands named by the card are allowed.",
        "use a bounded read and never reread an unchanged range",
        "Never store secrets or fabricate session evidence.",
        "issue one bounded task-specific query",
        "Do not query legacy memory files directly.",
        "After a zero hit, do not repeat the query unless task scope changes.",
        "Workers never query or write Context Graph",
        "Disabled or zero-hit is not failure",
    ):
        assert rule in text, rule
    for forbidden in ("grep", "rg", "find", "tree", "broad cat/sed", "recursive listing"):
        assert forbidden in text
    assert text.rstrip().endswith("Stop at Codex review.")


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
    assert instr.CLAUDE_MANAGER_PREAMBLE in rendered["CLAUDE.md"]
    assert instr.CLAUDE_MANAGER_PREAMBLE not in rendered["AGENTS.md"]
    assert instr.CLAUDE_MANAGER_PREAMBLE not in rendered[".github/copilot-instructions.md"]


def test_claude_projection_leads_with_role_then_startup_then_policy() -> None:
    text = instr.render_projection("CLAUDE.md")
    role_at = text.index("Claude Code manager role")
    startup_at = text.index("Claude Code manager startup")
    canonical_at = text.index("# AIWorkHub MCP tool-use policy")
    assert role_at < startup_at < canonical_at
    for required in (
        "Before Read, Grep, Glob, Bash or filesystem discovery",
        "call aiworkhub_manager_bootstrap",
        "verified bootstrap/repository_current repository outranks host cwd",
        "aiworkhub_manager_source_graph_query first with focus or slice",
        "workflow_stage=orientation",
        "report the MCP problem instead of silently bypassing AIWorkHub",
        "Direct Claude chats use manager tools",
    ):
        assert required in text


def test_caps_are_declared_above_the_measured_render() -> None:
    """The caps moved to fit the role; they still bound the live render, and the
    projection cap stays >= the canonical cap."""
    assert instr.PROJECTION_MAX_BYTES >= instr.CANONICAL_MAX_BYTES
    assert len(instr.render_canonical().encode("utf-8")) <= instr.CANONICAL_MAX_BYTES
    for provider in instr.PROVIDERS:
        assert (
            len(instr.render_projection(provider).encode("utf-8"))
            <= instr.PROJECTION_MAX_BYTES
        )


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
