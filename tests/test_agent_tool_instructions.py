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


MANAGER_ORDER_LINES = (
    "Manager Order:",
    "1. aiworkhub_manager_source_graph_query.",
    "2. aiworkhub_manager_session_current_state.",
    "3. aiworkhub_manager_ai_memory_search.",
    "4. aiworkhub_manager_kb_search/get/related.",
    "5. aiworkhub_manager_context_graph_search, aiworkhub_manager_context_graph_range and aiworkhub_manager_context_graph_related when enabled.",
    "6. launch, review and close cards through the manager task tools.",
)

WORKER_ORDER_LINES = (
    "Worker Order:",
    "1. the coordinator records the injected bundle receipt; do not print it.",
    "2. aiworkhub_worker_source_graph_query.",
    "3. aiworkhub_worker_session_current_state.",
    "4. aiworkhub_worker_ai_memory_search.",
    "5. aiworkhub_worker_kb_search/get/related.",
    "6. never Context Graph.",
    "7. execute exact card action and validation.",
)


def test_canonical_order_is_exact_and_compact() -> None:
    """Audit startup-5: the order is one sequence per seat. The two receipt
    steps were worker-side facts rendered for the manager, and nothing injects
    a receipt on a direct manager chat; the worker keeps one line that says the
    coordinator records the receipt itself."""
    text = instr.render_canonical()
    expected = [*MANAGER_ORDER_LINES, *WORKER_ORDER_LINES]
    positions = [text.index(item) for item in expected]
    assert positions == sorted(positions)
    manager_section = text[text.index("Manager Order:") : text.index("Worker Order:")]
    assert "receipt" not in manager_section
    assert "injected" not in manager_section
    assert "validate the injected" not in text
    assert "consume and acknowledge" not in text
    assert "PROJECT_CONTEXT_RECEIPT" not in text
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


# ---------------------------------------------------------------------------
# Audit startup-5: the policy is rendered once per document.
# ---------------------------------------------------------------------------

ROLE_SENTENCES = (
    "The manager does not write code",
    "the manager is the independent reviewer",
    "a single-provider install is fully supported and not degraded",
    "Every card that reaches review is closed the same turn",
    "Acceptance is decided by measurement",
)


def test_claude_preamble_binds_the_shared_role_without_restating_it() -> None:
    """CLAUDE.md rendered each role sentence twice inside the managed block,
    once in the preamble ('different vendor') and once in the shared body
    ('second vendor'). One version of each rule exists now: the preamble points
    at the shared 'Manager role:' section and restates nothing."""
    text = instr.render_projection("CLAUDE.md")
    assert '"Manager role:" rules' in instr.CLAUDE_MANAGER_PREAMBLE
    for sentence in ROLE_SENTENCES:
        assert text.count(sentence) == 1, sentence
        assert sentence not in instr.CLAUDE_MANAGER_PREAMBLE, sentence
    assert "different vendor" not in text
    assert text.index("Claude Code manager role") < text.index("Claude Code manager startup")
    assert text.index("Claude Code manager startup") < text.index("Manager role:")


def _stale_copy(provider: str = "CLAUDE.md") -> str:
    block = instr.render_projection(provider)
    body = block[len(instr.START) + 1 : block.index(instr.END)]
    return body  # "Target: ...\n<preamble>\n<canonical>" without the markers


def test_apply_plan_strips_a_verbatim_policy_copy_outside_the_block() -> None:
    block = instr.render_projection("CLAUDE.md")
    owner_prose = "# Local notes\n\nKeep this.\n\n"
    stale = _stale_copy()
    owner = owner_prose + stale + "\n" + block
    plan = instr.build_apply_plan("CLAUDE.md", owner)
    assert plan["ok"] is True
    assert plan["reason"] == "replace_managed_block"
    assert plan["planned_text"] == owner_prose + block
    assert len(plan["stripped_lines"]) == len([line for line in stale.splitlines() if line])
    assert plan["inspection"]["outside_verbatim_lines"] == len(plan["stripped_lines"])
    assert plan["inspection"]["outside_drifted_lines"] == 0
    # Nothing but the block left: the leading copy is gone entirely.
    bare = instr.build_apply_plan("CLAUDE.md", stale + "\n" + block)
    assert bare["planned_text"] == block
    # A trailing copy after the block is stripped the same way.
    trailing = instr.build_apply_plan("CLAUDE.md", block + "\n" + stale)
    assert trailing["planned_text"] == block
    # Idempotent afterwards.
    again = instr.build_apply_plan("CLAUDE.md", plan["planned_text"])
    assert again["planned_text"] == plan["planned_text"]
    assert again["stripped_lines"] == []


def test_apply_plan_fails_closed_on_a_drifted_policy_copy_outside_the_block() -> None:
    """The outside copy differs from the rendered text: report, never guess."""
    block = instr.render_projection("CLAUDE.md")
    stale = _stale_copy().replace("not a second vendor", "not a different vendor")
    assert stale != _stale_copy()
    owner = stale + "\n" + block
    plan = instr.build_apply_plan("CLAUDE.md", owner)
    assert plan["ok"] is False
    assert plan["reason"] == "policy_text_drifted_outside_managed_block"
    assert plan["planned_text"] == owner
    assert plan["current_text"] == owner
    assert len(plan["drifted_lines"]) == 1
    assert plan["drifted_lines"][0].startswith("- Because the manager did not write the code")
    assert plan["inspection"]["outside_drifted_lines"] == 1
    # The verbatim lines around the drifted one are counted but not stripped.
    assert plan["inspection"]["outside_verbatim_lines"] > 0


def test_bare_section_labels_and_prose_in_owner_text_are_not_policy_copies() -> None:
    block = instr.render_projection("AGENTS.md")
    owner = "KB:\n- my own knowledge-base note\n\nOrder:\n\n" + block
    plan = instr.build_apply_plan("AGENTS.md", owner)
    assert plan["ok"] is True
    assert plan["planned_text"] == owner
    assert plan["stripped_lines"] == []
    assert plan["inspection"]["outside_verbatim_lines"] == 0


def test_repository_agents_owner_text_scans_clean() -> None:
    """AGENTS.md used to carry a "Kilo manager startup (copied from CLAUDE.md
    ...)" block above its START marker: 1,018 B of the CLAUDE.md manager
    startup rules with one word renamed, invisible to a scan that only knew
    AGENTS.md's own preamble-free projection. It is gone, and the remaining
    owner text must stay at zero duplicates and zero drift under the wider
    scan so --check keeps passing on it."""
    path = Path(__file__).resolve().parents[1] / "AGENTS.md"
    text = path.read_text(encoding="utf-8")
    assert "Kilo manager startup" not in text
    assert "copied from CLAUDE.md" not in text
    before, after = instr._split_around_block(text)
    scan = instr.scan_outside_block(before, after, instr.render_projection("AGENTS.md"))
    assert scan.verbatim == ()
    assert scan.drifted == ()


def test_rule_set_is_every_projection_not_just_this_document() -> None:
    """A rule copied out of a different provider's block is still a copy."""
    rules = instr.managed_policy_rule_lines()
    for provider in instr.PROVIDERS:
        for line in instr.render_projection(provider).splitlines():
            if line and line not in (instr.START, instr.END):
                assert line in rules, line
    # The CLAUDE-only preamble is in the set even though AGENTS.md never
    # renders it; that is what makes the AGENTS.md copy detectable.
    for line in instr.CLAUDE_MANAGER_PREAMBLE.splitlines():
        assert line in rules, line
    assert instr.START not in rules and instr.END not in rules


def test_a_copy_of_another_providers_rules_is_a_duplicate() -> None:
    """The AGENTS.md defect: preamble lines parked above a preamble-free
    block. Verbatim copies are reported and stripped, wherever they came
    from."""
    block = instr.render_projection("AGENTS.md")
    owner = instr.CLAUDE_MANAGER_PREAMBLE + block
    plan = instr.build_apply_plan("AGENTS.md", owner)
    assert plan["ok"] is True
    assert len(plan["stripped_lines"]) == len(
        [line for line in instr.CLAUDE_MANAGER_PREAMBLE.splitlines() if line]
    )
    assert plan["planned_text"] == block
    assert plan["inspection"]["outside_drifted_lines"] == 0


def test_a_renamed_copy_of_another_providers_rules_fails_closed() -> None:
    """The real duplicate renamed "Claude" to "Kilo" inside the first nine
    characters of two lines, so a shared-40-character-prefix test read them as
    owner prose. Similarity catches them, and a copy that differs from the
    rendered text is reported, never silently rewritten."""
    block = instr.render_projection("AGENTS.md")
    renamed = instr.CLAUDE_MANAGER_PREAMBLE.replace(
        "Claude Code manager startup (mandatory",
        "Kilo manager startup (copied from CLAUDE.md; mandatory",
    ).replace("- Direct Claude chats", "- Direct Kilo chats")
    owner = renamed + block
    plan = instr.build_apply_plan("AGENTS.md", owner)
    assert plan["ok"] is False
    assert plan["reason"] == "policy_text_drifted_outside_managed_block"
    assert plan["planned_text"] == owner
    assert plan["current_text"] == owner
    assert len(plan["drifted_lines"]) == 2
    assert any("Kilo manager startup" in line for line in plan["drifted_lines"])
    assert any("- Direct Kilo chats" in line for line in plan["drifted_lines"])


def test_outside_scan_handles_crlf_copies() -> None:
    block = instr.render_projection(".github/copilot-instructions.md")
    before = "Target: .github/copilot-instructions.md\r\n\r\n"
    scan = instr.scan_outside_block(before, "\n", block)
    assert scan.verbatim == ("Target: .github/copilot-instructions.md",)
    assert scan.before == ""
    assert scan.after == "\n"


# ---------------------------------------------------------------------------
# Audit edit-1: the semantic editor is mandatory, and prepare is not a reader.
# ---------------------------------------------------------------------------

def test_semantic_edit_is_mandatory_in_every_projection() -> None:
    """Measured over 659 worker runs: about half of every edit bypassed the
    semantic editor. The mandate is in the shared canonical, so it reaches
    AGENTS.md, CLAUDE.md and copilot, not just one provider."""
    canonical = instr.render_canonical()
    assert "Semantic edit (mandatory):" in canonical
    assert (
        "Change an existing file with aiworkhub_worker_semantic_edit_prepare"
        " then _apply on the smallest verified range" in canonical
    )
    assert "a whole-file rewrite is not an editing strategy" in canonical
    for provider in instr.PROVIDERS:
        text = instr.render_projection(provider)
        assert "Semantic edit (mandatory):" in text, provider
        assert "a whole-file rewrite is not an editing strategy" in text, provider


def test_semantic_edit_exceptions_are_named_so_the_rule_is_followable() -> None:
    """A rule with unnamed exceptions is decided by taste. All three are
    written down, with what to do instead."""
    canonical = instr.render_canonical()
    exceptions = canonical[canonical.index("Exceptions:") :].splitlines()[0]
    assert "a new file" in exceptions
    assert "a change spanning most of a file" in exceptions
    assert "an adapter without these tools" in exceptions
    assert "make the smallest bounded edit and record why" in exceptions


def test_prepare_is_an_edit_step_not_a_reader() -> None:
    """2,396 of codex's 2,901 prepares (83%, 24.9 MB of fragment bytes) were
    never applied, 93% on a file the run had already read."""
    canonical = instr.render_canonical()
    assert "prepare is an edit step, not a reader" in canonical
    assert "read with body/file preview" in canonical
    policy = instr.render_worker_runtime_policy()
    assert "prepare is an EDIT step, not a reader" in policy
    assert "Never prepare a range you are not\n  about to change" in policy
    # The server already answers a re-prepare of a delivered range hash-only;
    # the policy must describe that behaviour, not contradict it.
    assert "hash-only (fragment_omitted)" in policy


def test_the_semantic_edit_rule_is_stated_once() -> None:
    """The two Source Graph lines this section supersedes were removed, not
    left beside it: the canonical says it once, and stays under its cap."""
    canonical = instr.render_canonical()
    assert "For edits prefer aiworkhub_worker_semantic_edit_prepare/apply" not in canonical
    assert "After Source Graph finds an exact target" not in canonical
    assert canonical.count("aiworkhub_worker_semantic_edit_prepare") == 1
    assert canonical.count("a bounded read and never reread an unchanged range") == 1
    assert instr.CANONICAL_MAX_BYTES == 7200
    assert instr.PROJECTION_MAX_BYTES == 9200
    assert len(canonical.encode("utf-8")) <= instr.CANONICAL_MAX_BYTES


def test_worker_prompt_mandates_the_semantic_editor_with_named_exceptions() -> None:
    policy = instr.render_worker_runtime_policy()
    assert "SEMANTIC_EDIT_IS_MANDATORY:" in policy
    assert "A whole-file rewrite of an existing file is not an editing strategy" in policy
    assert "raw apply_patch, Edit or Write" in policy
    assert "creating a NEW file" in policy
    assert "genuinely\n  spans most of a file" in policy
    assert "does not expose these two\n  tools" in policy
    assert "smallest possible bounded edit and say which\n  exception applied" in policy
    # The applier's guarantee is still stated.
    assert "full-file preimage and fragment hash" in policy
    # The clause names the contract-consistency gate reads must survive.
    for clause in instr.CONTRACT_CLAUSES["semantic_edit"]:
        assert clause in policy
        assert clause in instr.render_canonical()


# ---------------------------------------------------------------------------
# Audit worker_prompt-5 / source_graph-8: stage and refresh are server-side.
# ---------------------------------------------------------------------------

def test_stage_and_refresh_rules_are_server_side_now() -> None:
    text = instr.render_canonical()
    assert "workflow_stage is inferred by the server; pass it only to override." in text
    assert "Set workflow_stage on every Source Graph call" not in text
    assert "never relabel" not in text
    assert "refresh once" not in text
    assert "Use body for an exact symbol and bodygrep for indexed literal/body text." in text


# ---------------------------------------------------------------------------
# Audit worker_prompt-4 / worker_prompt-6: the worker prompt.
# ---------------------------------------------------------------------------

def test_worker_policy_names_the_one_shot_tool_schema_load() -> None:
    from aiworkhub import runtime_adapters, worker_ai_tools_mcp

    policy = instr.render_worker_runtime_policy()
    expected = "select:" + ",".join(
        f"mcp__aiworkhub_worker_ai_tools__{name}"
        for name in (
            "aiworkhub_worker_source_graph_query",
            "aiworkhub_worker_semantic_edit_prepare",
            "aiworkhub_worker_semantic_edit_apply",
        )
    )
    assert instr.WORKER_CLAUDE_TOOL_SCHEMA_QUERY == expected
    assert f'"{expected}"' in policy
    assert policy.count("ToolSearch") == 1
    assert "never search schemas by keyword" in policy
    # The prefix is the registered server name, not a guess.
    assert instr.WORKER_MCP_SERVER_NAME == worker_ai_tools_mcp.SERVER_NAME
    assert runtime_adapters._WORKER == f"mcp__{instr.WORKER_MCP_SERVER_NAME}__aiworkhub_worker_"
    for name in instr.WORKER_MCP_TOOL_NAMES[:3]:
        assert name in expected


def test_worker_policy_states_resolved_toolchain_and_bounded_output() -> None:
    policy = instr.render_worker_runtime_policy()
    assert "$AIWORKHUB_CANONICAL_PYTHON" in policy
    assert "$AIWORKHUB_CANONICAL_RUFF" in policy
    assert "$AIWORKHUB_CANONICAL_MYPY" in policy
    assert "already resolved" in policy
    assert "substitute them verbatim" in policy
    assert "never probe them first" in policy
    assert "pass -q" in policy
    assert "tail -n 40 or head" in policy
    assert "--tb=short" in policy
    assert "re-run it only after you changed something it tests" in policy
    assert "at most 12 lines" in policy
    assert "name tests plus changed paths" not in policy
    assert "do not list files or paste test output" in policy
    # Phrases the launcher and enforcement tests still rely on.
    for kept in (
        "SANDBOX_VALIDATION_FACTS",
        "validation_unsupported_in_sandbox:",
        "never stub a denied call",
        "coordinator-side supervisor will",
        "Never install, download, unpack, vendor, or bootstrap",
        "MANDATORY_AIWORKHUB_TOOLS:",
        "HMAC-authenticated MCP audit ledger",
    ):
        assert kept in policy, kept
    assert "Call aiworkhub_worker_session_current_state for continuity" not in policy
