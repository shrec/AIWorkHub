from __future__ import annotations

import pytest

from aiworkhub import core
from aiworkhub import runner_topic_policy as policy


@pytest.mark.parametrize(
    ("runner", "topic", "action", "allowed", "reason"),
    [
        ("claude_coding", "coding", "claim-start", True, "allowlisted"),
        ("claude_coding", "coding", "done", False, "action_not_allowed_for_runner_topic:done"),
        ("claude_coding", "stem", "claim-start", False, "unknown_runner_topic_pair"),
        ("claude_task_mcp_wave_1", "task_mcp", "claim-start", True, "per_wave_prefix_allowlisted"),
        ("claude_task_mcp_wave_1", "task_mcp", "done", False, "per_wave_action_not_allowed:done"),
        ("codex", "arbitrary.topic", "done", True, "codex_wildcard_topic_allowed"),
        ("codex", "arbitrary.topic", "claim-start", False, "codex_action_not_allowed:claim-start"),
        ("codex_gpt-5.5", "quality_review", "review", True, "allowlisted"),
        (
            "codex_gpt-5.5",
            "quality_review",
            "claim-start",
            False,
            "action_not_allowed_for_runner_topic:claim-start",
        ),
        ("codex_gpt-5.3-codex-spark", "quality_review", "review", False, "unknown_runner_topic_pair"),
        ("codex_gpt-5.5-native", "quality_review", "review", False, "unknown_runner_topic_pair"),
        ("copilot", "quality_review", "review", False, "unknown_runner_topic_pair"),
        ("vscode_lm", "quality_review", "review", False, "unknown_runner_topic_pair"),
        ("codex_gpt-5.5", "coding", "review", False, "unknown_runner_topic_pair"),
        ("bad/runner", "coding", "claim-start", False, "malformed_runner:invalid_characters"),
        ("claude_coding", "bad topic", "claim-start", False, "malformed_topic:invalid_characters"),
        (None, "coding", "claim-start", False, "runner_and_topic_required_for_non_codex"),
    ],
)
def test_runner_topic_policy_characterization(
    runner: str | None,
    topic: str | None,
    action: str,
    allowed: bool,
    reason: str,
) -> None:
    expected = {"allowed": allowed, "reason": reason}
    assert policy.check_runner_topic_allowlist(runner, topic, action) == expected
    assert core.check_runner_topic_allowlist(runner, topic, action) == expected


def test_core_reexports_single_policy_authority() -> None:
    assert core.RUNNER_TOPIC_ALLOWLIST is policy.RUNNER_TOPIC_ALLOWLIST
    assert core.PER_WAVE_RUNNER_TOPIC_ALLOWLIST is policy.PER_WAVE_RUNNER_TOPIC_ALLOWLIST
    assert core.CODEX_ALLOWED_ACTIONS is policy.CODEX_ALLOWED_ACTIONS
    assert core.check_runner_topic_allowlist is policy.check_runner_topic_allowlist
    assert core._is_malformed_identity_token is policy._is_malformed_identity_token


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("valid.runner-1:topic", None),
        ("", "empty_string"),
        ("bad\x00runner", "null_byte"),
        ("../escape", "invalid_characters"),
    ],
)
def test_identity_token_characterization(value: str, reason: str | None) -> None:
    assert policy._is_malformed_identity_token(value) == reason
