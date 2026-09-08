"""A role gets the tools it can use, never the union of every role.

Every worker received one flat allowed-tools list. A card marked
``read_only: true`` with an empty ``allowed_writes`` and ``repository_write``
forbidden was still handed ``Write``, ``Edit``,
``worker_semantic_edit_prepare``, ``worker_semantic_edit_apply`` and three
write-intent tools -- seven ways to change something, to a reviewer the sandbox
will refuse. And a build worker was handed ``quality_review_submit``, the
channel a reviewer files verdicts through.

Measured on reviewer request 5415654189de: seven write tools offered, zero
used. Each tool's schema is prompt text the model pays for on every turn, and
a tool the sandbox will refuse is worse than an absent one -- it is an
invitation to spend a turn discovering the refusal. The worker on the card this
reviewer was reviewing recorded exactly such a denial.

The sandbox stays the enforcement boundary. This is the layer above it: not
offering what cannot be used.

Run: python3 -m pytest -q tests/test_a_read_only_reviewer_holds_no_write_tools.py
"""

from __future__ import annotations

from aiworkhub import runtime_adapters as ra


def test_a_read_only_role_is_offered_no_way_to_write():
    tools = set(ra.claude_allowed_tools(read_only=True))
    assert not (tools & set(ra.CLAUDE_WRITE_TOOLS)), sorted(
        tools & set(ra.CLAUDE_WRITE_TOOLS)
    )
    assert "Write" not in tools
    assert "Edit" not in tools


def test_a_read_only_role_keeps_everything_it_needs_to_review():
    tools = set(ra.claude_allowed_tools(read_only=True))
    assert "Read" in tools
    assert set(ra.CLAUDE_READ_TOOLS) <= tools
    assert set(ra.CLAUDE_REVIEW_TOOLS) <= tools


def test_a_build_worker_keeps_its_write_tools():
    tools = set(ra.claude_allowed_tools(read_only=False))
    assert set(ra.CLAUDE_WRITE_TOOLS) <= tools
    assert set(ra.CLAUDE_READ_TOOLS) <= tools


def test_a_build_worker_cannot_file_a_review_of_itself():
    tools = set(ra.claude_allowed_tools(read_only=False))
    assert not (tools & set(ra.CLAUDE_REVIEW_TOOLS))


def test_the_two_sets_are_disjoint():
    """Overlap is how one flat list happens again."""
    assert not (set(ra.CLAUDE_READ_TOOLS) & set(ra.CLAUDE_WRITE_TOOLS))
    assert not (set(ra.CLAUDE_READ_TOOLS) & set(ra.CLAUDE_REVIEW_TOOLS))
    assert not (set(ra.CLAUDE_WRITE_TOOLS) & set(ra.CLAUDE_REVIEW_TOOLS))


def test_a_read_only_reviewer_may_search_natively():
    """The same reasoning, applied to a deny the sandbox makes pointless.

    The reviewer sandbox is Landlock read-only with an empty
    ``allowed_writes``, so ``Grep``/``Glob`` cannot change anything and denying
    them protected nothing.  Measured over 242 claude reviewer runs: 1,080 Bash
    permission denials (25% of every reviewer Bash call, in 181 runs), each a
    turn spent discovering a refusal and then substituting an unbounded
    workaround.  The raw SHELL forms stay denied -- the bounded native tools
    are the substitute, not an unbounded shell scan.
    """
    tools = set(ra.claude_allowed_tools(read_only=True))
    denied = set(ra.claude_disallowed_tools(read_only=True))

    assert {"Grep", "Glob"} <= tools
    assert not ({"Grep", "Glob"} & denied)
    assert set(ra.CLAUDE_RAW_DISCOVERY_SHELL_DENIES) <= denied
    assert "Bash(grep *)" in denied


def test_a_build_worker_keeps_the_full_raw_discovery_deny():
    """Source Graph is the build worker's discovery path and policy holds it there."""
    tools = set(ra.claude_allowed_tools(read_only=False))
    denied = set(ra.claude_disallowed_tools(read_only=False))

    assert not ({"Grep", "Glob"} & tools)
    assert denied == set(ra.CLAUDE_RAW_DISCOVERY_DENIES)
    assert {"Grep", "Glob"} <= denied


def test_a_reviewer_cannot_file_its_report_where_the_supervisor_never_reads():
    """``ReportFindings`` is the host's own code-review channel, not this one.

    73 of 242 claude reviewer runs filed through it; 9 filed ONLY there and
    lost the whole review.  ``ScheduleWakeup`` schedules a turn no supervisor
    will ever answer.  The one authoritative channel is the final JSON report
    the supervisor ingests and submits.
    """
    denied = set(ra.claude_disallowed_tools(read_only=True))

    assert set(ra.CLAUDE_REVIEWER_HOST_TOOL_DENIES) <= denied
    assert {"ReportFindings", "ScheduleWakeup"} <= denied
    assert not (set(ra.CLAUDE_REVIEWER_HOST_TOOL_DENIES)
                & set(ra.claude_allowed_tools(read_only=True)))
    # A build worker never held these; denying them there would be noise.
    assert not (set(ra.CLAUDE_REVIEWER_HOST_TOOL_DENIES)
                & set(ra.claude_disallowed_tools(read_only=False)))


def _plan(tmp_path, *, read_only: bool):
    """A real argv, built against a real executable path.

    A skipped assertion proves nothing, so the fake CLI is an actual file.
    """
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    return ra.build_runtime_command(
        "claude_cli",
        "review this",
        tmp_path,
        read_only=read_only,
        executable_overrides={"claude_cli": str(fake)},
    )


def test_the_argv_a_read_only_reviewer_actually_receives(tmp_path):
    plan = _plan(tmp_path, read_only=True)
    assert plan.argv, plan.validation_reason
    allowed = plan.argv[plan.argv.index("--allowedTools") + 1:]
    allowed = allowed[: allowed.index("--no-session-persistence")]
    assert "Write" not in allowed
    assert "Edit" not in allowed
    assert not any("semantic_edit" in tool for tool in allowed)
    assert not any("write_intent" in tool for tool in allowed)
    assert any("quality_review_submit" in tool for tool in allowed)


def test_the_argv_a_build_worker_actually_receives(tmp_path):
    plan = _plan(tmp_path, read_only=False)
    assert plan.argv, plan.validation_reason
    allowed = plan.argv[plan.argv.index("--allowedTools") + 1:]
    allowed = allowed[: allowed.index("--no-session-persistence")]
    assert "Write" in allowed
    assert any("semantic_edit_apply" in tool for tool in allowed)
    assert not any("quality_review_submit" in tool for tool in allowed)


def test_the_default_is_the_writing_role(tmp_path):
    """Omitting the flag must not silently disarm a build worker."""
    plan = ra.build_runtime_command(
        "claude_cli", "build this", tmp_path,
        executable_overrides={"claude_cli": str(tmp_path / "claude")},
    )
    if plan.argv:
        allowed = plan.argv[plan.argv.index("--allowedTools") + 1:]
        assert "Write" in allowed[: allowed.index("--no-session-persistence")]
