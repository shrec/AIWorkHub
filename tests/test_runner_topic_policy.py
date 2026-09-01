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


# NF-2026-00549 slice A: one canonical runner-id grammar. Case and the
# interchangeable ``-``/``_`` separators are the only non-identity-bearing
# differences, so the six measured spellings fold onto exactly two ids.
_REGISTERED_RUNNER_IDS = ("claude_opus-5", "claude_sonnet-5")


@pytest.mark.parametrize("variant", ["claude_opus-5", "claude_opus_5", "claude_opus5"])
def test_opus_variants_canonicalize_to_one_registered_id(variant: str) -> None:
    assert (
        policy.canonical_runner_id(variant, _REGISTERED_RUNNER_IDS) == "claude_opus-5"
    )


@pytest.mark.parametrize(
    "variant", ["claude_sonnet-5", "claude_sonnet_5", "claude_sonnet5"]
)
def test_sonnet_variants_canonicalize_to_one_registered_id(variant: str) -> None:
    assert (
        policy.canonical_runner_id(variant, _REGISTERED_RUNNER_IDS)
        == "claude_sonnet-5"
    )


def test_runner_id_fold_key_ignores_only_case_and_separators() -> None:
    assert (
        policy.runner_id_fold_key("Claude_Opus-5")
        == policy.runner_id_fold_key("claude_opus_5")
        == policy.runner_id_fold_key("claudeopus5")
    )
    # ``.`` is identity-bearing, so distinct versions never collide.
    assert policy.runner_id_fold_key("codex_gpt-5.5") != policy.runner_id_fold_key(
        "codex_gpt-5.3"
    )


@pytest.mark.parametrize("registered", ["claude_opus-5", "claude_sonnet-5"])
def test_registered_runner_ids_are_byte_stable_under_grammar(registered: str) -> None:
    assert (
        policy.canonical_runner_id(registered, _REGISTERED_RUNNER_IDS) == registered
    )


@pytest.mark.parametrize(
    "value",
    ["claude_haiku-4.5", "deepseek_v4-pro", "bad/runner", "", "\x00"],
)
def test_unresolvable_or_malformed_runner_id_returns_none(value: str) -> None:
    assert policy.canonical_runner_id(value, _REGISTERED_RUNNER_IDS) is None


def test_create_time_runner_folds_variants_and_refuses_unknown_claude_family() -> None:
    # NF-2026-00549: variant spellings fold onto the registered launcher route
    # at create; an unresolvable claude-family runner is refused with a named
    # reason instead of dying later at launch with workforce_route_absent.
    from aiworkhub import core

    assert core.resolve_create_time_runner("claude_opus_5") == ("claude_opus-5", None)
    assert core.resolve_create_time_runner("claude_opus5") == ("claude_opus-5", None)
    assert core.resolve_create_time_runner("claude_opus-5") == ("claude_opus-5", None)
    assert core.resolve_create_time_runner("claude_sonnet_5") == ("claude_sonnet-5", None)
    # Non-claude families keep adapter-level resolution exactly as at launch.
    assert core.resolve_create_time_runner("codex_5_6_sol") == ("codex_5_6_sol", None)
    assert core.resolve_create_time_runner("codex_gpt-5.6-terra") == (
        "codex_gpt-5.6-terra",
        None,
    )

    # A runner that folds onto no registered route passes through unchanged:
    # synthetic and unpinned claude-family runners are legitimate at launch.
    assert core.resolve_create_time_runner("claude_ghost-9") == ("claude_ghost-9", None)
    assert core.resolve_create_time_runner("claude_worker") == ("claude_worker", None)
