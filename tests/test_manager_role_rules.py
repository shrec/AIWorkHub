"""NF-2026-00281-V2: the generated rules must teach the manager ROLE, not only
the tool protocol.

These tests pin the role statements the owner declared load-bearing so a future
edit cannot silently regress the projections back to protocol-only. The role
lives in the shared ``POLICY`` (so AGENTS.md and copilot-instructions.md get it
too -- a manager may be any model in that seat) and is restated, before the tool
protocol, in the Claude-specific ``CLAUDE_MANAGER_PREAMBLE``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import agent_tool_instructions as instr  # noqa: E402


# Load-bearing role statements. Each must survive in EVERY provider projection,
# because the role is carried by the shared POLICY, not by a Claude-only block.
ROLE_STATEMENTS = (
    "The manager does not write code",
    "distributes work to workers by difficulty and cost",
    "building features is the workers' job",
    "the manager is the independent reviewer",
    "independence is this role separation",
    "a single-provider install is fully supported and not degraded",
    "mechanical gates and tests first because they cannot be faked",
    "Every card that reaches review is closed the same turn",
    "returned with concrete code-level findings",
    "nothing accumulates",
    "Acceptance is decided by measurement",
    "does not ask the owner to approve a production accept",
    "Launch in parallel only cards whose allowed_writes do not overlap",
    "two cards that need the same file are sequential work, not parallel",
    "A card's allowed_writes must include the tests that assert the contract it changes",
    "the production call sites it must wire",
    "it is never a requirement that one vendor review another",
    "Record obstacles as NeedFix with measured evidence",
    "Intermediate release rule:",
)


BREAK_GLASS_STATEMENTS = (
    "Self-hosting break-glass authority:",
    "installed AIWorkHub plugin or Task MCP itself blocks canonical task progress",
    "manager may temporarily bypass Task MCP",
    "implement the smallest replacement fix",
    "validate it independently, build and install the replacement",
    "return immediately to canonical Task MCP flow",
    "record the blocker and evidence",
    "preserve unrelated work",
    "keep scope limited to restoring the task system",
    "never use the exception for ordinary feature development",
)


# NF-2026-00294: the multicore-by-default principle rides the shared POLICY, so
# it must reach EVERY provider projection (not just the Claude preamble) and
# cannot silently regress. Each fragment is a distinctive slice of one of the
# five rendered principle lines.
MULTICORE_STATEMENTS = (
    "Multicore by default:",
    "AIWorkHub is written for multiple cores",
    "work that is independent per item runs across cores by default",
    "a sequential path is the exception",
    "derived from the observed core count, never a hardcoded constant",
    "leaves headroom so a scan cannot starve the interactive MCP server",
    "Parallelism changes only how fast, never what is measured or produced",
    "results stay identical to the sequential path",
    "Threads for IO-bound work that releases the GIL, processes for CPU-bound work",
    "chosen from a measurement, not a rule of thumb",
    "A path left sequential after measurement is a valid outcome",
)


def test_multicore_principle_present_in_every_provider_projection() -> None:
    """Criterion 7: the multicore-by-default principle cannot regress.

    It lives in the shared POLICY, so AGENTS.md and copilot must carry it just
    like CLAUDE.md -- the principle applies to any model writing code here, not
    only Claude.
    """
    rendered = instr.render_all()
    assert tuple(rendered) == instr.PROVIDERS
    for provider, text in rendered.items():
        for statement in MULTICORE_STATEMENTS:
            assert statement in text, f"{provider!r} missing multicore statement: {statement!r}"


def test_multicore_principle_is_in_the_shared_canonical_not_claude_only() -> None:
    """The principle rides the shared canonical body, so it is model-agnostic
    and is NOT confined to the Claude-specific preamble."""
    canonical = instr.render_canonical()
    for statement in MULTICORE_STATEMENTS:
        assert statement in canonical, statement
    # It is a shared-policy rule, not a Claude preamble line.
    assert "Multicore by default:" not in instr.CLAUDE_MANAGER_PREAMBLE


def test_role_present_in_every_provider_projection() -> None:
    """Criterion 6: the role cannot silently regress to protocol-only.

    Asserted on all three providers -- AGENTS.md and copilot must carry the role
    just like CLAUDE.md, because the shared POLICY (not a Claude-only block)
    holds it.
    """
    rendered = instr.render_all()
    assert tuple(rendered) == instr.PROVIDERS
    for provider, text in rendered.items():
        assert "Manager role:" in text, provider
        for statement in ROLE_STATEMENTS:
            assert statement in text, f"{provider!r} missing role statement: {statement!r}"


def test_role_is_in_the_shared_canonical_not_claude_only() -> None:
    """The role rides the shared canonical body, so it is model-agnostic."""
    canonical = instr.render_canonical()
    assert "Manager role:" in canonical
    for statement in ROLE_STATEMENTS:
        assert statement in canonical, statement


def test_self_hosting_break_glass_authority_reaches_every_provider() -> None:
    rendered = instr.render_all()
    for provider, text in rendered.items():
        for statement in BREAK_GLASS_STATEMENTS:
            assert statement in text, f"{provider!r} missing break-glass statement: {statement!r}"


def test_self_hosting_break_glass_authority_is_manager_only_and_bounded() -> None:
    canonical = instr.render_canonical()
    assert canonical.index("Manager role:") < canonical.index("Self-hosting break-glass authority:")
    assert canonical.index("Self-hosting break-glass authority:") < canonical.index("Order:")
    for statement in BREAK_GLASS_STATEMENTS:
        assert statement in canonical, statement


def test_role_precedes_the_tool_protocol_in_the_canonical() -> None:
    """The role goes in FRONT of the protocol; the protocol still follows."""
    canonical = instr.render_canonical()
    assert canonical.index("Manager role:") < canonical.index("Order:")


def test_parallel_launch_rule_is_present() -> None:
    """Overlapping allowed_writes are sequential work, not a parallel launch."""
    canonical = instr.render_canonical()
    assert "Launch in parallel only cards whose allowed_writes do not overlap" in canonical
    assert "two cards that need the same file are sequential work, not parallel" in canonical


def test_scope_rule_is_present() -> None:
    """A card's allowed_writes must include the asserting tests and call sites."""
    canonical = instr.render_canonical()
    assert "must include the tests that assert the contract it changes" in canonical
    assert "the production call sites it must wire" in canonical


def test_independence_is_role_separation_and_single_provider_is_supported() -> None:
    """Independence comes from role separation, not a second vendor/model/process."""
    canonical = instr.render_canonical()
    assert "the manager is the independent reviewer" in canonical
    assert "not a second vendor, model or process" in canonical
    assert "a single-provider install is fully supported and not degraded" in canonical
    # And routing is never a cross-vendor-review requirement.
    assert "it is never a requirement that one vendor review another" in canonical


def test_close_every_card_same_turn_and_decide_by_measurement() -> None:
    canonical = instr.render_canonical()
    assert "Every card that reaches review is closed the same turn" in canonical
    assert "accepted into the canonical tree" in canonical
    assert "blocked with a reason" in canonical
    assert "nothing accumulates" in canonical
    assert "Acceptance is decided by measurement" in canonical
    assert "does not ask the owner to approve a production accept" in canonical


def test_mechanical_gates_run_first_and_obstacles_are_needfix() -> None:
    canonical = instr.render_canonical()
    assert "mechanical gates and tests first because they cannot be faked" in canonical
    assert "Record obstacles as NeedFix with measured evidence" in canonical


def test_claude_preamble_states_the_role_before_the_protocol() -> None:
    """Criterion 1: CLAUDE_MANAGER_PREAMBLE leads with the role, ahead of the
    tool-startup protocol and ahead of the shared canonical body."""
    text = instr.render_projection("CLAUDE.md")
    role_at = text.index("Claude Code manager role")
    startup_at = text.index("Claude Code manager startup")
    canonical_at = text.index("# AIWorkHub MCP tool-use policy")
    assert role_at < startup_at < canonical_at
    # The five role statements the preamble itself must make.
    for required in (
        "The manager does not write code",
        "distributes work to workers by difficulty and cost",
        "the manager is the independent reviewer",
        "independence is this role separation",
        "a single-provider install is fully supported and not degraded",
        "Every card that reaches review is closed the same turn",
        "acceptance is decided by measurement, never by asking the owner to approve a production accept",
    ):
        assert required in instr.CLAUDE_MANAGER_PREAMBLE, required


def test_preamble_role_is_claude_local_but_shared_role_reaches_all_providers() -> None:
    """The preamble block is CLAUDE.md-only; the shared role still reaches the
    other providers via the canonical body."""
    rendered = instr.render_all()
    assert "Claude Code manager role" in rendered["CLAUDE.md"]
    assert "Claude Code manager role" not in rendered["AGENTS.md"]
    assert "Claude Code manager role" not in rendered[".github/copilot-instructions.md"]
    # But the model-agnostic role IS in AGENTS.md / copilot.
    assert "The manager does not write code" in rendered["AGENTS.md"]
    assert "The manager does not write code" in rendered[".github/copilot-instructions.md"]


def test_role_addition_keeps_every_projection_within_the_declared_caps() -> None:
    """The caps moved (documented in the module); render must still hold them."""
    assert len(instr.render_canonical().encode("utf-8")) <= instr.CANONICAL_MAX_BYTES
    for provider in instr.PROVIDERS:
        assert (
            len(instr.render_projection(provider).encode("utf-8"))
            <= instr.PROJECTION_MAX_BYTES
        )
