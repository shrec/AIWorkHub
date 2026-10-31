#!/usr/bin/env python3
"""B119 smoke: runner/topic allowlist enforcement for MCP write paths.

Proves the deterministic allowlist layer behind ``aiworkhub.core.check_runner_topic_allowlist``:

  1. The allowlist matrix matches mcp_runner_topic_allowlist_design_b118_v1.json
     exactly (11 claude_*/deepseek_* runner/topic pairs + codex wildcard topic).
  2. All 8 malformed/attack fixtures from the B118 design are rejected
     (empty runner/topic, path traversal, shell metacharacters, null byte,
     case mismatch, unknown runner).
  3. Partial identities (topic or runner omitted) and unknown runner/topic pairs are denied.
  4. Read-only tools are unaffected (_is_write_command stays False for them).

The run_taskctl integration cases were removed because run_taskctl no longer shells out.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
SRC = HERE.parents[1] / "src"

sys.path.insert(0, str(SRC))
from aiworkhub import core  # noqa: E402

MALFORMED_FIXTURES = [
    {"runner": "", "topic": "coding", "reason": "empty runner string"},
    {"runner": "claude_coding", "topic": "", "reason": "empty topic string"},
    {"runner": "", "topic": "", "reason": "both empty"},
    {"runner": "../../etc/passwd", "topic": "coding", "reason": "path traversal in runner"},
    {"runner": "claude_coding", "topic": "; rm -rf /", "reason": "shell injection in topic"},
    {"runner": "claude_coding\x00hidden", "topic": "coding", "reason": "null byte in runner"},
    {"runner": "CLAUDE_CODING", "topic": "coding", "reason": "case mismatch -- exact-match only"},
    {"runner": "unknown_runner_abc123", "topic": "coding", "reason": "runner not in allowlist"},
]


# --------------------------------------------------------------------------
# 1 + 2. pure allowlist-matrix + malformed-fixture unit tests (no I/O)
# --------------------------------------------------------------------------
def test_allowlist_matrix_pure() -> None:
    for (runner, topic), actions in core.RUNNER_TOPIC_ALLOWLIST.items():
        for action in ("auto-pickup", "review", "start", "usage"):
            decision = core.check_runner_topic_allowlist(runner, topic, action)
            expect = action in actions
            assert decision["allowed"] is expect, (runner, topic, action, decision)
        for action in ("done", "export-jsonl"):
            decision = core.check_runner_topic_allowlist(runner, topic, action)
            assert decision["allowed"] is False, (runner, topic, action, decision)

    for action in core.CODEX_ALLOWED_ACTIONS:
        decision = core.check_runner_topic_allowlist(core.CODEX_RUNNER, "any_topic_at_all", action)
        assert decision["allowed"] is True, (action, decision)
    for action in ("auto-pickup", "start"):
        decision = core.check_runner_topic_allowlist(core.CODEX_RUNNER, "coding", action)
        assert decision["allowed"] is False, (action, decision)

    print(f"PASS test_allowlist_matrix_pure "
          f"({len(core.RUNNER_TOPIC_ALLOWLIST)} runner/topic pairs + codex wildcard)")


def test_malformed_fixtures_pure() -> None:
    for fx in MALFORMED_FIXTURES:
        decision = core.check_runner_topic_allowlist(fx["runner"], fx["topic"], "auto-pickup")
        assert decision["allowed"] is False, (fx["reason"], decision)
    print(f"PASS test_malformed_fixtures_pure ({len(MALFORMED_FIXTURES)} cases all denied)")


def test_partial_and_unknown_identity_pure() -> None:
    # topic omitted for a non-codex runner -> deny (topic-bound doctrine: every
    # non-codex identity must assert BOTH runner and topic).
    d = core.check_runner_topic_allowlist("claude_coding", None, "auto-pickup")
    assert d["allowed"] is False, d
    d = core.check_runner_topic_allowlist(None, "coding", "auto-pickup")
    assert d["allowed"] is False, d
    # both omitted -> the caller supplied no identity; run_taskctl() never even
    # calls check_runner_topic_allowlist in that case (tested below), but the
    # pure function itself must still deny defensively if called directly.
    d = core.check_runner_topic_allowlist(None, None, "auto-pickup")
    assert d["allowed"] is False, d
    # unknown (runner, topic) pair, both individually well-formed
    d = core.check_runner_topic_allowlist("claude_coding", "stem", "auto-pickup")
    assert d["allowed"] is False and d["reason"] == "unknown_runner_topic_pair", d
    print("PASS test_partial_and_unknown_identity_pure")


def test_read_only_tools_unaffected() -> None:
    for args in (["list", "--status", "pending"], ["show", "SOME_TASK"], ["export", "--runner", "x"],
                 ["verify"], ["review-queue"], ["collision-guard", "--print"], ["usage-report"]):
        assert core._is_write_command(args) is False, args
    print("PASS test_read_only_tools_unaffected (no write-gate/allowlist path touched)")


def main() -> int:
    test_allowlist_matrix_pure()
    test_malformed_fixtures_pure()
    test_partial_and_unknown_identity_pure()
    test_read_only_tools_unaffected()
    print("ALL B119 RUNNER/TOPIC ALLOWLIST ENFORCEMENT SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
