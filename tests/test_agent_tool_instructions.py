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
        "Every seat changes an existing file with"
        " aiworkhub_worker_semantic_edit_prepare then _apply, or the manager"
        " pair, on the smallest verified range" in canonical
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
    assert "record which one applies, never a silent raw edit" in exceptions


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
    assert instr.CANONICAL_MAX_BYTES == 7300
    assert instr.PROJECTION_MAX_BYTES == 9200
    assert len(canonical.encode("utf-8")) <= instr.CANONICAL_MAX_BYTES


def test_the_canonical_has_room_for_the_vocabulary_it_is_derived_from() -> None:
    """A derived document must leave room for its source to grow.

    The prohibition is rendered FROM ``RAW_DISCOVERY_DENIED_COMMANDS``, so the
    canonical gets longer whenever that tuple does. At 4 bytes of headroom --
    where the "every seat" sentences left it -- adding one realistic command
    would have raised ValueError inside ``render_canonical`` in production, at
    projection time, with no review to catch it. This measures the headroom
    against a real command rather than trusting the comment at the cap.
    """

    rendered, _worker = _rendered_with_extra_denied_command("ripgrep")
    for provider, text in rendered.items():
        assert "ripgrep" in text, provider
        assert len(text.encode("utf-8")) <= instr.PROJECTION_MAX_BYTES, provider
    # The canonical is what the cap actually binds, and it is the tightest.
    assert len(instr.render_canonical().encode("utf-8")) <= instr.CANONICAL_MAX_BYTES


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

    policy = instr.render_worker_runtime_policy("claude_cli")
    expected = "select:" + ",".join(
        f"mcp__aiworkhub_worker_ai_tools__{name}"
        for name in (
            "aiworkhub_worker_source_graph_query",
            "aiworkhub_worker_semantic_edit_prepare",
            "aiworkhub_worker_semantic_edit_apply",
            # The declaration channel rides with them: the worker who is about
            # to take a raw fallback is the one who will not search for a tool
            # that records it.
            "aiworkhub_worker_semantic_edit_exception_declare",
            # The bounded validation runner and the exit rehearsal are preloaded
            # too: a worker that has to discover them mid-run falls back to raw
            # Bash validation, which is the 42.7%-of-bytes defect they replace.
            "aiworkhub_worker_validation_run",
            "aiworkhub_worker_exit_preflight",
        )
    )
    assert instr.WORKER_CLAUDE_TOOL_SCHEMA_QUERY == expected
    assert set(instr.WORKER_CLAUDE_PRELOADED_TOOLS) <= set(
        instr.WORKER_MCP_TOOL_NAMES
    )
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
    assert "resolved absolute paths" in policy
    assert "never probe them first" in policy
    assert "--tb=short" in policy
    # Output shaping is the tool's job now, not the model's: the runner returns
    # a bounded record, so the policy must stop teaching a tail/head workaround
    # (777 such calls were measured) and must forbid it instead.
    assert "aiworkhub_worker_validation_run" in policy
    assert "Never retype a validation command" in policy
    assert "never pipe" in policy and "tail or head" in policy
    assert "pass -q" not in policy
    assert "aiworkhub_worker_exit_preflight" in policy
    assert "never marks anything satisfied" in policy
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


# ---------------------------------------------------------------------------
# Derived prohibition, derived substitution, and per-seat enforcement honesty.
#
# The prohibition and the enforced deny tuple used to be two hand-written lists
# that agreed by luck, and the policy forbade without ever naming a replacement.
# These tests fail against a restated string: they mutate the tuple and require
# the rendered text to move with it.
# ---------------------------------------------------------------------------

import importlib  # noqa: E402
import re  # noqa: E402

from aiworkhub import runtime_adapters  # noqa: E402


def _rendered_with_extra_denied_command(command: str) -> tuple[dict[str, str], str]:
    """Render every projection and the worker prompt with ``command`` denied.

    The module is reloaded so the derivation is exercised at its real source.
    Both the tuple and the module are restored before returning, and the
    restoration is asserted by the caller.
    """

    original = runtime_adapters.RAW_DISCOVERY_DENIED_COMMANDS
    try:
        runtime_adapters.RAW_DISCOVERY_DENIED_COMMANDS = (*original, command)
        reloaded = importlib.reload(instr)
        rendered = {
            provider: reloaded.render_projection(provider)
            for provider in reloaded.PROVIDERS
        }
        worker = reloaded.render_worker_runtime_policy()
        return rendered, worker
    finally:
        runtime_adapters.RAW_DISCOVERY_DENIED_COMMANDS = original
        importlib.reload(instr)


def test_the_raw_discovery_prohibition_is_derived_from_the_enforced_tuple() -> None:
    """A command added to the enforced tuple must reach every projection."""
    sentinel = "zzsentinelsearch"
    baseline = {p: instr.render_projection(p) for p in instr.PROVIDERS}
    for text in baseline.values():
        assert sentinel not in text

    rendered, _worker = _rendered_with_extra_denied_command(sentinel)
    for provider, text in rendered.items():
        assert sentinel in text, provider

    # Restored: the module is back to the real tuple for every later test.
    assert {p: instr.render_projection(p) for p in instr.PROVIDERS} == baseline


def test_the_worker_prompt_prohibition_is_derived_too() -> None:
    """The second hand-written copy lived in the worker prompt; derive it too."""
    sentinel = "zzsentinelsearch"
    before = instr.render_worker_runtime_policy()
    assert sentinel not in before

    _rendered, worker = _rendered_with_extra_denied_command(sentinel)
    assert sentinel in worker
    assert instr.render_worker_runtime_policy() == before


def test_the_canonical_prohibition_keeps_its_exact_wording() -> None:
    """The derivation changed the SOURCE, not the text: no document resync."""
    text = instr.render_canonical()
    assert (
        "- Never use grep, rg, find, tree, broad cat/sed or recursive listing"
        " while Source Graph can index/process the target." in text
    )
    assert len(text.encode("utf-8")) <= instr.CANONICAL_MAX_BYTES


def test_the_substitution_table_names_a_replacement_for_every_forbidden_surface() -> None:
    """Forbidding without substituting is what sends a model to cat and sed."""
    block = instr.WORKER_SUBSTITUTION_BLOCK
    assert block in instr.render_worker_runtime_policy()
    for command in runtime_adapters.RAW_DISCOVERY_DENIED_COMMANDS:
        assert command in block, command
    for native in runtime_adapters.CLAUDE_RAW_DISCOVERY_TOOL_DENIES:
        assert native in block, native
    for surface, tool in instr.WORKER_SUBSTITUTIONS:
        assert surface.strip() and tool.strip()
        assert f"{surface} -> {tool}." in block
    # The third column: what to do when the named tool is genuinely unavailable.
    assert "genuinely unavailable" in block
    assert "A fallback nobody named is an unrecorded one." in block


def test_every_tool_named_in_the_table_is_one_the_worker_server_registers() -> None:
    """A renamed tool must not leave a dangling instruction behind."""
    named = set(re.findall(r"aiworkhub_worker_[a-z_]+", instr.WORKER_SUBSTITUTION_BLOCK))
    assert named
    assert named <= set(instr.WORKER_MCP_TOOL_NAMES), sorted(
        named - set(instr.WORKER_MCP_TOOL_NAMES)
    )


def test_the_worker_table_is_seat_correct() -> None:
    """A worker prompt must never carry a manager-prefixed tool name."""
    assert "aiworkhub_manager_" not in instr.WORKER_SUBSTITUTION_BLOCK
    assert "aiworkhub_manager_" not in instr.render_worker_runtime_policy()


def test_an_unenforcing_transport_is_told_the_text_is_the_only_control() -> None:
    """Only a transport that enforces NOTHING gets the notice.

    Two mechanisms enforce, and the argv predicate knows about one of them. The
    first version of this test read it alone and required the notice on all six
    adapters without an argv deny -- which put it on the three vscode_lm routes,
    whose dispatch surface serves 20 tools, every one aiworkhub_*, with no raw
    search and no raw editor among them. Telling that seat "nothing here refuses
    you" was the plainest kind of false: it is the seat where refusal is total.
    """

    base = instr.render_worker_runtime_policy()
    unenforcing = []
    for adapter_id in runtime_adapters.SUPPORTED_ADAPTERS:
        rendered = instr.render_worker_runtime_policy(adapter_id)
        surface = runtime_adapters.tool_surface_enforcement_fact(adapter_id)
        if surface["mechanism"] != runtime_adapters.TOOL_SURFACE_MECHANISM_NONE:
            # Argv already refuses, or the surface never offered it. Either way
            # the worker meets the refusal without being told about it.
            if adapter_id == "claude_cli":
                # Only Claude has deferred schemas; keep its one-shot ToolSearch
                # instruction off every other adapter's paid prompt prefix.
                assert rendered == f"{base}\n\n{instr._CLAUDE_TOOL_SCHEMA_BLOCK}"
            else:
                assert rendered == base, adapter_id
            continue
        unenforcing.append(adapter_id)
        assert "RAW_DISCOVERY_ENFORCEMENT:" in rendered, adapter_id
        assert adapter_id in rendered
        assert "this instruction is the whole of the rule" in rendered
        assert "HMAC-authenticated MCP audit ledger" in rendered
    # codex_cli, grok_kilo_cli and deepseek_manual: no argv deny, and a tool
    # surface AIWorkHub does not own.
    assert unenforcing == ["codex_cli", "grok_kilo_cli", "deepseek_manual"], unenforcing


def test_an_unknown_transport_is_treated_as_unenforcing() -> None:
    """An unlisted adapter can never inherit an enforcement claim."""
    fact = instr.raw_discovery_enforcement("some_new_cli")
    assert fact["adapter_known"] is False
    assert fact["enforced"] is False
    assert "RAW_DISCOVERY_ENFORCEMENT:" in instr.render_worker_runtime_policy(
        "some_new_cli"
    )
    # No adapter at all keeps the historical, unannotated prefix.
    assert instr.render_worker_runtime_policy(None) == instr.WORKER_RUNTIME_POLICY


def test_the_contract_clauses_still_hold_in_both_carriers() -> None:
    """The rewrite must not drop a clause the consistency check requires."""
    canonical = instr.render_canonical()
    worker = instr.render_worker_runtime_policy()
    for clause, alternatives in instr.CONTRACT_CLAUSES.items():
        assert any(value in canonical for value in alternatives), clause
        assert any(value in worker for value in alternatives), clause
