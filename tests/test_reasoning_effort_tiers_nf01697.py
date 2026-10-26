"""NF-2026-01697: tiered Claude effort for implementers and reviewers.

Bounded low/medium-risk implementer work resolves to ``medium`` and standard
work to ``high``. Complex difficulty, security work and architecture work lift
to ``high``; only critical risk reaches ``max`` (NF-2026-01703). A reviewer
baselines at ``medium``, lifts to ``high`` from the *target* card (high risk,
security or architecture work, complex) or a security lens, and reaches ``max``
only for a critical target, never from the reviewer card's own derived fields.
"""

from __future__ import annotations

import pytest

from aiworkhub import reasoning_policy, runtime_adapters, vscode_lm_bridge

CLAUDE_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5")


def _implementer(card: dict[str, object], model: str = "claude-opus-5-5"):
    decision = runtime_adapters.resolve_adapter_reasoning(
        "claude_cli", card, model=model
    )
    assert decision is not None
    return decision


def _reviewer(
    target_card: dict[str, object] | None,
    lens: str | None = "correctness",
    *,
    reviewer_card: dict[str, object] | None = None,
    model: str = "claude-opus-5-5",
):
    decision = runtime_adapters.resolve_adapter_reasoning(
        "claude_cli",
        reviewer_card if reviewer_card is not None else {"risk_tier": "low"},
        is_reviewer=True,
        model=model,
        target_card=target_card,
        lens=lens,
    )
    assert decision is not None
    return decision


def _applied_key(decision: reasoning_policy.ReasoningDecision) -> str | None:
    assert decision.route_effort is not None
    assert decision.route_effort.applied is True
    return decision.route_effort.applied_key


def _reasons(decision: reasoning_policy.ReasoningDecision) -> set[str]:
    return {decision.rationale.baseline_reason, *decision.rationale.reason_codes}


# --- ladder shape -----------------------------------------------------------


def test_the_ladder_has_medium_as_its_floor_and_no_low_or_minimal() -> None:
    names = {member.name for member in reasoning_policy.ReasoningProfile}
    assert names == {"MEDIUM", "MEDIUM_HIGH", "HIGH", "MAXIMUM"}
    assert reasoning_policy.ReasoningProfile.MEDIUM.value == "canonical_medium"
    assert reasoning_policy.ABSOLUTE_FLOOR is reasoning_policy.ReasoningProfile.MEDIUM
    assert not {"LOW", "MINIMAL"} & names


# --- implementer table ------------------------------------------------------


@pytest.mark.parametrize("model", CLAUDE_MODELS)
@pytest.mark.parametrize("risk", ["low", "medium"])
def test_bounded_low_or_medium_risk_implementer_resolves_to_medium(
    model: str, risk: str
) -> None:
    decision = _implementer(
        {"work_kind": "generic", "risk_tier": risk, "difficulty": "bounded"}, model
    )
    assert decision.profile is reasoning_policy.ReasoningProfile.MEDIUM
    assert _applied_key(decision) == "medium"
    assert "bounded_implementer_medium" in _reasons(decision)


@pytest.mark.parametrize("model", CLAUDE_MODELS)
@pytest.mark.parametrize("risk", ["low", "medium"])
def test_standard_implementer_resolves_to_high(model: str, risk: str) -> None:
    decision = _implementer(
        {"work_kind": "generic", "risk_tier": risk, "difficulty": "standard"}, model
    )
    assert decision.profile is reasoning_policy.ReasoningProfile.HIGH
    assert _applied_key(decision) == "high"
    assert "standard_implementer_high" in _reasons(decision)


@pytest.mark.parametrize("model", CLAUDE_MODELS)
def test_bounded_high_risk_implementer_resolves_to_high(model: str) -> None:
    decision = _implementer(
        {"work_kind": "generic", "risk_tier": "high", "difficulty": "bounded"}, model
    )
    assert decision.profile is reasoning_policy.ReasoningProfile.HIGH
    assert _applied_key(decision) == "high"
    assert "bounded_high_risk_implementer_high" in _reasons(decision)


@pytest.mark.parametrize("model", CLAUDE_MODELS)
@pytest.mark.parametrize(
    ("card", "reason", "expected", "key"),
    [
        (
            {"risk_tier": "low", "difficulty": "complex"},
            "complex_difficulty",
            reasoning_policy.ReasoningProfile.HIGH,
            "high",
        ),
        (
            {"risk_tier": "critical", "difficulty": "bounded"},
            "critical_risk_tier",
            reasoning_policy.ReasoningProfile.MAXIMUM,
            "max",
        ),
        (
            {"work_kind": "security", "risk_tier": "low", "difficulty": "bounded"},
            "security_work",
            reasoning_policy.ReasoningProfile.HIGH,
            "high",
        ),
        (
            {"work_kind": "architecture", "risk_tier": "low", "difficulty": "bounded"},
            "architecture_work",
            reasoning_policy.ReasoningProfile.HIGH,
            "high",
        ),
    ],
)
def test_only_critical_risk_resolves_to_max_and_the_other_lifts_resolve_to_high(
    model: str,
    card: dict[str, object],
    reason: str,
    expected: reasoning_policy.ReasoningProfile,
    key: str,
) -> None:
    decision = _implementer(card, model)
    assert decision.profile is expected
    assert _applied_key(decision) == key
    assert reason in _reasons(decision)


def test_the_claude_maximum_default_is_gone() -> None:
    for card in (
        {"risk_tier": "low"},
        {"work_kind": "generic", "risk_tier": "medium"},
        {"work_kind": "feature", "risk_tier": "low", "difficulty": "standard"},
    ):
        assert _implementer(card).profile is not reasoning_policy.ReasoningProfile.MAXIMUM


# --- reviewer table ---------------------------------------------------------


@pytest.mark.parametrize("model", CLAUDE_MODELS)
@pytest.mark.parametrize("target_risk", ["low", "medium"])
@pytest.mark.parametrize("target_difficulty", ["bounded", "standard"])
def test_reviewer_of_a_low_or_medium_non_complex_target_resolves_to_medium(
    model: str, target_risk: str, target_difficulty: str
) -> None:
    decision = _reviewer(
        {"risk_tier": target_risk, "difficulty": target_difficulty}, model=model
    )
    assert decision.profile is reasoning_policy.ReasoningProfile.MEDIUM
    assert _applied_key(decision) == "medium"
    assert "reviewer_baseline_medium" in _reasons(decision)


@pytest.mark.parametrize("model", CLAUDE_MODELS)
def test_security_lens_reviewer_resolves_to_high_on_a_benign_target(
    model: str,
) -> None:
    decision = _reviewer(
        {"risk_tier": "low", "difficulty": "bounded"}, lens="security", model=model
    )
    assert decision.profile is reasoning_policy.ReasoningProfile.HIGH
    assert _applied_key(decision) == "high"
    assert "security_lens_review" in _reasons(decision)


def test_the_security_lens_is_matched_case_insensitively() -> None:
    decision = _reviewer({"risk_tier": "low"}, lens=" Security ")
    assert decision.profile is reasoning_policy.ReasoningProfile.HIGH


@pytest.mark.parametrize("model", CLAUDE_MODELS)
@pytest.mark.parametrize(
    ("target_card", "reason"),
    [
        ({"risk_tier": "high", "difficulty": "bounded"}, "high_risk_target_review"),
        ({"risk_tier": "low", "difficulty": "complex"}, "complex_difficulty"),
        (
            {"risk_tier": "low", "rework_predecessor": {"request_id": "R-prev"}},
            "complex_difficulty",
        ),
        (
            {"work_kind": "security", "risk_tier": "low", "difficulty": "bounded"},
            "security_work",
        ),
        (
            {"work_kind": "architecture", "risk_tier": "low", "difficulty": "bounded"},
            "architecture_work",
        ),
        (None, "high_risk_target_review"),
    ],
)
def test_high_risk_complex_security_or_unknown_target_lifts_the_reviewer_to_high(
    model: str, target_card: dict[str, object] | None, reason: str
) -> None:
    decision = _reviewer(target_card, model=model)
    assert decision.profile is reasoning_policy.ReasoningProfile.HIGH
    assert _applied_key(decision) == "high"
    assert reason in _reasons(decision)


@pytest.mark.parametrize("model", CLAUDE_MODELS)
def test_only_a_critical_target_lifts_the_reviewer_to_max(model: str) -> None:
    decision = _reviewer({"risk_tier": "critical", "difficulty": "bounded"}, model=model)
    assert decision.profile is reasoning_policy.ReasoningProfile.MAXIMUM
    assert _applied_key(decision) == "max"
    assert "critical_risk_tier" in _reasons(decision)


def test_reviewer_effort_comes_from_the_target_card_not_the_reviewer_card() -> None:
    benign_target = {"risk_tier": "low", "difficulty": "bounded"}
    loud_reviewer_card = {
        "risk_tier": "critical",
        "difficulty": "complex",
        "work_kind": "security",
    }
    decision = _reviewer(benign_target, reviewer_card=loud_reviewer_card)
    assert decision.profile is reasoning_policy.ReasoningProfile.MEDIUM
    assert _applied_key(decision) == "medium"

    quiet_reviewer_card = {"risk_tier": "low", "difficulty": "bounded"}
    lifted = _reviewer(
        {"risk_tier": "critical"}, reviewer_card=quiet_reviewer_card
    )
    assert lifted.profile is reasoning_policy.ReasoningProfile.MAXIMUM
    assert _applied_key(lifted) == "max"


def test_an_implementer_never_picks_up_a_review_lens() -> None:
    decision = runtime_adapters.resolve_adapter_reasoning(
        "claude_cli",
        {"risk_tier": "low", "difficulty": "bounded"},
        model="claude-opus-5-5",
        lens="security",
    )
    assert decision is not None
    assert decision.profile is reasoning_policy.ReasoningProfile.MEDIUM
    assert "security_lens_review" not in _reasons(decision)


# --- the VS Code LM bridge mirrors the rule ---------------------------------


def _vscode_profile(
    request_kind: str,
    card: dict[str, object] | None = None,
    **kwargs: object,
) -> reasoning_policy.ReasoningProfile:
    request = vscode_lm_bridge.vscode_lm_effort_request(
        request_kind=request_kind,
        card=card,
        model="claude-opus-5-5",
        **kwargs,
    )
    capability = reasoning_policy.RouteCapability(
        route_id="vscode_lm",
        provider_family=reasoning_policy.ProviderFamily.CLAUDE,
        supports_effort_control=True,
        effort_keys=("low", "medium", "high", "xhigh", "max"),
    )
    return reasoning_policy.resolve_reasoning_effort(request, capability).profile


def test_vscode_lm_review_baselines_at_medium_for_a_benign_target() -> None:
    profile = _vscode_profile(
        "quality_review",
        {"risk_tier": "critical"},
        target_card={"risk_tier": "low", "difficulty": "bounded"},
        lens="correctness",
    )
    assert profile is reasoning_policy.ReasoningProfile.MEDIUM


@pytest.mark.parametrize(
    ("target_card", "lens", "expected"),
    [
        ({"risk_tier": "low"}, "security", reasoning_policy.ReasoningProfile.HIGH),
        ({"risk_tier": "high"}, "correctness", reasoning_policy.ReasoningProfile.HIGH),
        (
            {"risk_tier": "critical"},
            "correctness",
            reasoning_policy.ReasoningProfile.MAXIMUM,
        ),
        (
            {"risk_tier": "low", "difficulty": "complex"},
            "correctness",
            reasoning_policy.ReasoningProfile.HIGH,
        ),
        (
            {"work_kind": "security", "risk_tier": "low"},
            "correctness",
            reasoning_policy.ReasoningProfile.HIGH,
        ),
        (None, "correctness", reasoning_policy.ReasoningProfile.HIGH),
    ],
)
def test_vscode_lm_review_lifts_like_the_cli_route(
    target_card: dict[str, object] | None,
    lens: str,
    expected: reasoning_policy.ReasoningProfile,
) -> None:
    profile = _vscode_profile(
        "quality_review", {}, target_card=target_card, lens=lens
    )
    assert profile is expected
    assert _reviewer(target_card, lens=lens).profile is expected


@pytest.mark.parametrize(
    "target_card",
    [
        {"risk_tier": "low", "rework_predecessor": {"request_id": "R-prev"}},
        {"difficulty": "bounded"},
        {"risk": "high", "difficulty": "bounded"},
        {"risk_tier": "low", "difficulty": "bounded"},
        {"work_kind": "security", "risk_tier": "low", "difficulty": "bounded"},
        {"work_kind": "architecture", "risk_tier": "low", "difficulty": "bounded"},
        {"risk_tier": "critical", "difficulty": "bounded"},
        None,
    ],
)
def test_cli_and_vscode_lm_reviewers_derive_the_same_profile(
    target_card: dict[str, object] | None,
) -> None:
    # Both routes receive the same review_target_card from launch_isolated.
    cli = _reviewer(target_card).profile
    vscode = _vscode_profile(
        "quality_review", {}, target_card=target_card, lens="correctness"
    )
    assert vscode is cli


@pytest.mark.parametrize(
    "card",
    [
        {"risk_tier": "low", "rework_predecessor": {"request_id": "R-prev"}},
        {"difficulty": "bounded"},
        {"risk_tier": "low", "difficulty": "bounded"},
        {"work_kind": "architecture", "risk_tier": "low", "difficulty": "bounded"},
        {"work_kind": "documentation", "risk_tier": "low", "difficulty": "bounded"},
        {"work_kind": "Security", "difficulty": "bounded"},
    ],
)
def test_cli_and_vscode_lm_implementers_derive_the_same_profile(
    card: dict[str, object],
) -> None:
    vscode = vscode_lm_bridge.vscode_lm_effort_request(request_kind="worker", card=card)
    cli = runtime_adapters._derive_reasoning_request("claude_cli", card, is_reviewer=False)
    assert vscode.work_kind is cli.work_kind
    assert _vscode_profile("worker", card) is _implementer(card).profile


def test_vscode_lm_review_lens_is_carried_on_the_request_only_for_reviews() -> None:
    review = vscode_lm_bridge.vscode_lm_effort_request(
        request_kind="quality_review",
        model="claude-opus-5-5",
        target_card={"risk_tier": "low"},
        lens="Security",
    )
    assert review.review_lens == "security"
    worker = vscode_lm_bridge.vscode_lm_effort_request(
        request_kind="worker",
        card={"risk_tier": "low", "difficulty": "bounded"},
        model="claude-opus-5-5",
        lens="security",
    )
    assert worker.review_lens is None


@pytest.mark.parametrize(
    ("card", "expected"),
    [
        (
            {"risk_tier": "low", "difficulty": "bounded"},
            reasoning_policy.ReasoningProfile.MEDIUM,
        ),
        (
            {"risk_tier": "low", "difficulty": "standard"},
            reasoning_policy.ReasoningProfile.HIGH,
        ),
        (
            {"risk_tier": "low", "difficulty": "complex"},
            reasoning_policy.ReasoningProfile.HIGH,
        ),
        (
            {"risk_tier": "critical", "difficulty": "bounded"},
            reasoning_policy.ReasoningProfile.MAXIMUM,
        ),
    ],
)
def test_vscode_lm_implementer_follows_the_same_tiers(
    card: dict[str, object], expected: reasoning_policy.ReasoningProfile
) -> None:
    assert _vscode_profile("worker", card) is expected


# --- Codex keeps an applied key for every rung ------------------------------


def test_codex_has_an_applied_key_for_every_rung_and_medium_maps_to_medium() -> None:
    capability = runtime_adapters.route_reasoning_capability("codex_cli", "gpt-5.5")
    assert capability is not None
    expected = {
        reasoning_policy.ReasoningProfile.MEDIUM: "medium",
        reasoning_policy.ReasoningProfile.MEDIUM_HIGH: "high",
        reasoning_policy.ReasoningProfile.HIGH: "high",
        reasoning_policy.ReasoningProfile.MAXIMUM: "xhigh",
    }
    for profile in reasoning_policy.ReasoningProfile:
        route = reasoning_policy.normalize_for_route(profile, capability)
        assert route.applied is True
        assert route.applied_key == expected[profile]


def test_codex_bounded_implementer_applies_medium() -> None:
    decision = runtime_adapters.resolve_adapter_reasoning(
        "codex_cli", {"risk_tier": "low", "difficulty": "bounded"}, model="gpt-5.5"
    )
    assert decision is not None
    assert decision.profile is reasoning_policy.ReasoningProfile.MEDIUM
    assert _applied_key(decision) == "medium"
    assert runtime_adapters._effort_flag_tokens("codex_cli", "medium") == [
        "-c",
        'model_reasoning_effort="medium"',
    ]


# --- every decision carries its reason code ---------------------------------


@pytest.mark.parametrize(
    "card",
    [
        {"risk_tier": "low", "difficulty": "bounded"},
        {"risk_tier": "low", "difficulty": "standard"},
        {"risk_tier": "high", "difficulty": "bounded"},
        {"risk_tier": "critical"},
        {"work_kind": "security", "risk_tier": "low"},
        {"work_kind": "architecture", "risk_tier": "low"},
        {"risk_tier": "low", "difficulty": "complex"},
    ],
)
def test_every_implementer_decision_carries_a_reason_code(
    card: dict[str, object],
) -> None:
    decision = _implementer(card)
    assert decision.rationale.baseline_reason
    assert decision.rationale.baseline_reason in _reasons(decision)
    if decision.profile is reasoning_policy.ReasoningProfile.MAXIMUM:
        assert decision.rationale.decisive_reason == "critical_risk_tier"
        assert decision.rationale.reason_codes
