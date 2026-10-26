"""Deterministic coverage of the reasoning-effort policy core (NF-2026-00897).

The suite walks the full declared input matrix rather than sampling it, so a
future branch that quietly lowers effort for cost cannot pass by living in an
untested corner of the product.
"""

from __future__ import annotations

import dataclasses
import itertools
import json

import pytest

from aiworkhub.reasoning_policy import (
    ABSOLUTE_FLOOR,
    POLICY_SCHEMA_ID,
    ControlStatus,
    Difficulty,
    EffortRequest,
    KeyMapping,
    ProviderFamily,
    ReasoningProfile,
    RiskTier,
    RouteCapability,
    TaskRole,
    WorkKind,
    normalize_for_route,
    provider_family_for_route,
    resolve_reasoning_effort,
    select_reasoning_profile,
)

ALL_REQUESTS = tuple(
    EffortRequest(role=role, risk_tier=risk, work_kind=kind, difficulty=difficulty,
                  provider_family=family)
    for role, risk, kind, difficulty, family in itertools.product(
        TaskRole, RiskTier, WorkKind, Difficulty, ProviderFamily
    )
)

NON_CLAUDE_FAMILIES = tuple(f for f in ProviderFamily if f is not ProviderFamily.CLAUDE)


def _request(**overrides) -> EffortRequest:
    base = {
        "role": TaskRole.IMPLEMENTER,
        "risk_tier": RiskTier.MEDIUM,
        "work_kind": WorkKind.REPOSITORY_CODING,
        "difficulty": Difficulty.STANDARD,
        "provider_family": ProviderFamily.OTHER,
    }
    base.update(overrides)
    return EffortRequest(**base)


def _is_mandatory_maximum(request: EffortRequest) -> bool:
    return request.risk_tier is RiskTier.CRITICAL


def _is_mandatory_high(request: EffortRequest) -> bool:
    return (
        request.difficulty is Difficulty.COMPLEX
        or request.work_kind in {WorkKind.SECURITY, WorkKind.ARCHITECTURE}
        or request.role in {TaskRole.SECURITY_AUDITOR, TaskRole.ARCHITECT}
        or (request.role is TaskRole.REVIEWER and request.risk_tier is RiskTier.HIGH)
    )


def _is_unlifted_review(request: EffortRequest) -> bool:
    return (
        request.role is TaskRole.REVIEWER
        and request.risk_tier in {RiskTier.LOW, RiskTier.MEDIUM}
        and request.work_kind not in {WorkKind.SECURITY, WorkKind.ARCHITECTURE}
        and request.difficulty is not Difficulty.COMPLEX
    )


def _is_bounded_medium_implementer(request: EffortRequest) -> bool:
    return (
        request.role is TaskRole.IMPLEMENTER
        and request.difficulty is Difficulty.BOUNDED
        and request.risk_tier in {RiskTier.LOW, RiskTier.MEDIUM}
    )


def _is_explicit_mechanical(request: EffortRequest) -> bool:
    return (
        request.work_kind is WorkKind.MECHANICAL_EDIT
        and request.difficulty is Difficulty.BOUNDED
        and request.risk_tier is RiskTier.LOW
        and request.role in {TaskRole.IMPLEMENTER, TaskRole.MECHANICAL_OPERATOR}
    )


# --- canonical ladder -------------------------------------------------------


def test_the_canonical_ladder_has_no_low_or_minimal_rung():
    names = {profile.name for profile in ReasoningProfile}
    assert names == {"MEDIUM", "MEDIUM_HIGH", "HIGH", "MAXIMUM"}
    values = " ".join(profile.value for profile in ReasoningProfile)
    assert "low" not in values and "minimal" not in values
    assert ReasoningProfile.MEDIUM.value == "canonical_medium"
    assert ABSOLUTE_FLOOR is ReasoningProfile.MEDIUM


def test_profiles_order_by_reasoning_quality():
    assert (
        ReasoningProfile.MEDIUM
        < ReasoningProfile.MEDIUM_HIGH
        < ReasoningProfile.HIGH
        < ReasoningProfile.MAXIMUM
    )
    assert max(ReasoningProfile) is ReasoningProfile.MAXIMUM
    assert sorted(ReasoningProfile) == [
        ReasoningProfile.MEDIUM,
        ReasoningProfile.MEDIUM_HIGH,
        ReasoningProfile.HIGH,
        ReasoningProfile.MAXIMUM,
    ]


def test_profiles_do_not_compare_against_foreign_types():
    with pytest.raises(TypeError):
        _ = ReasoningProfile.HIGH < 2


# --- policy matrix ----------------------------------------------------------


def test_every_matrix_cell_selects_at_or_above_the_absolute_floor():
    for request in ALL_REQUESTS:
        selected = select_reasoning_profile(request).selected
        assert selected >= ABSOLUTE_FLOOR, request


def test_only_bounded_implementer_work_and_unlifted_reviews_may_select_below_high():
    for request in ALL_REQUESTS:
        selected = select_reasoning_profile(request).selected
        if selected < ReasoningProfile.HIGH:
            assert selected is ReasoningProfile.MEDIUM, request
            assert (
                _is_bounded_medium_implementer(request)
                or _is_explicit_mechanical(request)
                or _is_unlifted_review(request)
            ), request


def test_a_reviewer_selects_medium_only_when_nothing_lifts_it():
    for request in ALL_REQUESTS:
        if request.role is not TaskRole.REVIEWER:
            continue
        selected = select_reasoning_profile(request).selected
        if _is_unlifted_review(request):
            assert selected is ReasoningProfile.MEDIUM, request
        else:
            assert selected >= ReasoningProfile.HIGH, request


def test_mandatory_escalations_reach_high_and_only_critical_reaches_maximum():
    for request in ALL_REQUESTS:
        selected = select_reasoning_profile(request).selected
        if _is_mandatory_maximum(request):
            assert selected is ReasoningProfile.MAXIMUM, request
        elif _is_mandatory_high(request):
            assert selected is ReasoningProfile.HIGH, request


def test_maximum_is_reached_only_through_the_critical_risk_escalation():
    for request in ALL_REQUESTS:
        rationale = select_reasoning_profile(request)
        assert rationale.baseline is not ReasoningProfile.MAXIMUM, request
        if rationale.selected is ReasoningProfile.MAXIMUM:
            assert request.risk_tier is RiskTier.CRITICAL, request
            assert rationale.escalations, request
            assert rationale.decisive_reason == "critical_risk_tier", request
        else:
            assert request.risk_tier is not RiskTier.CRITICAL, request


def test_selection_is_a_pure_deterministic_function_of_the_declared_inputs():
    for request in ALL_REQUESTS:
        first = select_reasoning_profile(request)
        second = select_reasoning_profile(request)
        assert first == second
        assert first.selected is second.selected


# --- implementer baseline ---------------------------------------------------

ALL_FAMILIES = tuple(ProviderFamily)
_CODING_KINDS = (WorkKind.REPOSITORY_CODING, WorkKind.MECHANICAL_EDIT, WorkKind.REWORK)


@pytest.mark.parametrize("family", ALL_FAMILIES)
@pytest.mark.parametrize("risk", [RiskTier.LOW, RiskTier.MEDIUM])
@pytest.mark.parametrize("kind", _CODING_KINDS)
def test_bounded_low_or_medium_risk_implementer_selects_medium_on_every_family(
    family, risk, kind
):
    rationale = select_reasoning_profile(
        _request(
            risk_tier=risk,
            work_kind=kind,
            difficulty=Difficulty.BOUNDED,
            provider_family=family,
        )
    )
    assert rationale.selected is ReasoningProfile.MEDIUM
    assert rationale.baseline is ReasoningProfile.MEDIUM
    assert rationale.escalations == ()
    if kind is WorkKind.MECHANICAL_EDIT and risk is RiskTier.LOW:
        assert rationale.decisive_reason == "explicit_mechanical_bounded_low_risk"
    else:
        assert rationale.decisive_reason == "bounded_implementer_medium"


@pytest.mark.parametrize("family", ALL_FAMILIES)
def test_explicit_mechanical_bounded_low_risk_work_selects_medium(family):
    rationale = select_reasoning_profile(
        _request(
            role=TaskRole.MECHANICAL_OPERATOR,
            risk_tier=RiskTier.LOW,
            work_kind=WorkKind.MECHANICAL_EDIT,
            difficulty=Difficulty.BOUNDED,
            provider_family=family,
        )
    )
    assert rationale.selected is ReasoningProfile.MEDIUM
    assert rationale.baseline_reason == "explicit_mechanical_bounded_low_risk"


@pytest.mark.parametrize("family", ALL_FAMILIES)
def test_bounded_high_risk_implementer_selects_high(family):
    rationale = select_reasoning_profile(
        _request(
            risk_tier=RiskTier.HIGH,
            difficulty=Difficulty.BOUNDED,
            provider_family=family,
        )
    )
    assert rationale.selected is ReasoningProfile.HIGH
    assert rationale.escalations == ()
    assert rationale.decisive_reason == "bounded_high_risk_implementer_high"


@pytest.mark.parametrize("family", ALL_FAMILIES)
@pytest.mark.parametrize("risk", [RiskTier.LOW, RiskTier.MEDIUM, RiskTier.HIGH])
@pytest.mark.parametrize("kind", [WorkKind.REPOSITORY_CODING, WorkKind.REWORK])
def test_standard_implementer_selects_high_never_the_old_claude_maximum(family, risk, kind):
    rationale = select_reasoning_profile(
        _request(
            risk_tier=risk,
            work_kind=kind,
            difficulty=Difficulty.STANDARD,
            provider_family=family,
        )
    )
    assert rationale.selected is ReasoningProfile.HIGH
    assert rationale.baseline is ReasoningProfile.HIGH
    assert rationale.escalations == ()
    assert rationale.decisive_reason == "standard_implementer_high"


@pytest.mark.parametrize("family", ALL_FAMILIES)
def test_complex_difficulty_lifts_a_coding_implementer_to_high_never_to_maximum(family):
    rationale = select_reasoning_profile(
        _request(difficulty=Difficulty.COMPLEX, provider_family=family)
    )
    assert rationale.selected is ReasoningProfile.HIGH
    assert rationale.baseline is ReasoningProfile.HIGH
    assert "complex_difficulty" in rationale.reason_codes
    assert rationale.decisive_reason == "repository_coding_quality_floor_high"


@pytest.mark.parametrize("family", ALL_FAMILIES)
def test_a_planner_keeps_the_high_quality_floor(family):
    rationale = select_reasoning_profile(
        _request(
            role=TaskRole.PLANNER,
            difficulty=Difficulty.BOUNDED,
            provider_family=family,
        )
    )
    assert rationale.selected is ReasoningProfile.HIGH
    assert rationale.baseline_reason == "repository_coding_quality_floor_high"


# --- reviewer ---------------------------------------------------------------


@pytest.mark.parametrize("family", ALL_FAMILIES)
@pytest.mark.parametrize("risk", [RiskTier.LOW, RiskTier.MEDIUM])
@pytest.mark.parametrize("difficulty", [Difficulty.BOUNDED, Difficulty.STANDARD])
@pytest.mark.parametrize("lens", [None, "correctness", "code_quality"])
def test_a_reviewer_of_a_low_or_medium_risk_non_complex_target_selects_medium(
    family, risk, difficulty, lens
):
    rationale = select_reasoning_profile(
        _request(
            role=TaskRole.REVIEWER,
            work_kind=WorkKind.CORRECTNESS_REVIEW,
            risk_tier=risk,
            difficulty=difficulty,
            provider_family=family,
            review_lens=lens,
        )
    )
    assert rationale.selected is ReasoningProfile.MEDIUM
    assert rationale.baseline is ReasoningProfile.MEDIUM
    assert rationale.escalations == ()
    assert rationale.decisive_reason == "reviewer_baseline_medium"


@pytest.mark.parametrize("family", ALL_FAMILIES)
@pytest.mark.parametrize(
    ("overrides", "reason", "expected"),
    [
        ({"review_lens": "security"}, "security_lens_review", ReasoningProfile.HIGH),
        ({"review_lens": " Security "}, "security_lens_review", ReasoningProfile.HIGH),
        ({"risk_tier": RiskTier.HIGH}, "high_risk_target_review", ReasoningProfile.HIGH),
        ({"work_kind": WorkKind.SECURITY}, "security_work", ReasoningProfile.HIGH),
        ({"work_kind": WorkKind.ARCHITECTURE}, "architecture_work", ReasoningProfile.HIGH),
        ({"difficulty": Difficulty.COMPLEX}, "complex_difficulty", ReasoningProfile.HIGH),
        (
            {"risk_tier": RiskTier.CRITICAL},
            "high_risk_target_review",
            ReasoningProfile.MAXIMUM,
        ),
        ({"risk_tier": RiskTier.CRITICAL}, "critical_risk_tier", ReasoningProfile.MAXIMUM),
    ],
)
def test_a_reviewer_lifts_to_high_and_only_a_critical_target_reaches_maximum(
    family, overrides, reason, expected
):
    shape = {
        "role": TaskRole.REVIEWER,
        "work_kind": WorkKind.CORRECTNESS_REVIEW,
        "risk_tier": RiskTier.LOW,
        "difficulty": Difficulty.BOUNDED,
        "provider_family": family,
    }
    shape.update(overrides)
    rationale = select_reasoning_profile(_request(**shape))
    assert rationale.selected is expected
    assert rationale.baseline is ReasoningProfile.MEDIUM
    assert reason in rationale.reason_codes


def test_correctness_review_work_kind_alone_no_longer_lifts_an_implementer():
    rationale = select_reasoning_profile(
        _request(
            work_kind=WorkKind.CORRECTNESS_REVIEW,
            risk_tier=RiskTier.LOW,
            difficulty=Difficulty.STANDARD,
        )
    )
    assert rationale.selected is ReasoningProfile.HIGH
    assert rationale.escalations == ()


@pytest.mark.parametrize("family", list(ProviderFamily))
@pytest.mark.parametrize(
    ("overrides", "reason", "expected"),
    [
        ({"risk_tier": RiskTier.CRITICAL}, "critical_risk_tier", ReasoningProfile.MAXIMUM),
        ({"work_kind": WorkKind.SECURITY}, "security_work", ReasoningProfile.HIGH),
        ({"role": TaskRole.SECURITY_AUDITOR}, "security_work", ReasoningProfile.HIGH),
        ({"work_kind": WorkKind.ARCHITECTURE}, "architecture_work", ReasoningProfile.HIGH),
        ({"role": TaskRole.ARCHITECT}, "architecture_work", ReasoningProfile.HIGH),
        (
            {"work_kind": WorkKind.REWORK, "difficulty": Difficulty.COMPLEX},
            "complex_difficulty",
            ReasoningProfile.HIGH,
        ),
        (
            {"role": TaskRole.REVIEWER, "review_lens": "security"},
            "security_lens_review",
            ReasoningProfile.HIGH,
        ),
        (
            {"role": TaskRole.REVIEWER, "risk_tier": RiskTier.HIGH},
            "high_risk_target_review",
            ReasoningProfile.HIGH,
        ),
    ],
)
def test_only_critical_reaches_maximum_while_the_other_escalations_reach_high(
    family, overrides, reason, expected
):
    # Start from the cheapest shape the policy can ever produce -- explicitly
    # mechanical, bounded, low risk -- so only the override under test can raise
    # the selection.
    shape = {
        "role": TaskRole.MECHANICAL_OPERATOR,
        "risk_tier": RiskTier.LOW,
        "work_kind": WorkKind.MECHANICAL_EDIT,
        "difficulty": Difficulty.BOUNDED,
        "provider_family": family,
    }
    shape.update(overrides)
    rationale = select_reasoning_profile(_request(**shape))
    assert rationale.selected is expected
    assert reason in rationale.reason_codes
    assert rationale.decisive_reason in rationale.reason_codes
    if expected is ReasoningProfile.MAXIMUM:
        assert rationale.decisive_reason == "critical_risk_tier"


# --- rationale --------------------------------------------------------------


def test_the_rationale_is_machine_readable_and_json_serializable():
    rationale = select_reasoning_profile(
        _request(risk_tier=RiskTier.CRITICAL, provider_family=ProviderFamily.CODEX)
    )
    payload = rationale.as_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["selected"] == "canonical_maximum"
    assert payload["decisive_reason"] == "critical_risk_tier"
    assert payload["never_below"] == "canonical_medium"
    assert "no_cost_based_downgrade" in payload["invariants"]
    assert payload["escalations"][0]["profile"] == "canonical_maximum"


def test_the_decisive_reason_is_the_baseline_when_nothing_escalates():
    rationale = select_reasoning_profile(_request(provider_family=ProviderFamily.GLM))
    assert rationale.escalations == ()
    assert rationale.decisive_reason == "standard_implementer_high"


def test_policy_inputs_and_rationales_are_immutable():
    request = _request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.risk_tier = RiskTier.LOW  # type: ignore[misc]
    rationale = select_reasoning_profile(request)
    with pytest.raises(dataclasses.FrozenInstanceError):
        rationale.selected = ReasoningProfile.MEDIUM_HIGH  # type: ignore[misc]


# --- provider family classification -----------------------------------------


@pytest.mark.parametrize(
    ("route_id", "family"),
    [
        ("claude_opus-5", ProviderFamily.CLAUDE),
        ("anthropic/claude-sonnet-5", ProviderFamily.CLAUDE),
        ("fable-5.1", ProviderFamily.CLAUDE),
        ("codex_gpt-5", ProviderFamily.CODEX),
        ("glm-5.2", ProviderFamily.GLM),
        ("deepseek-reasoner", ProviderFamily.DEEPSEEK),
        ("gemini-3-pro", ProviderFamily.GEMINI),
        ("some-internal-route", ProviderFamily.OTHER),
        ("", ProviderFamily.OTHER),
    ],
)
def test_route_identifiers_classify_into_provider_families(route_id, family):
    assert provider_family_for_route(route_id) is family


# --- provider capability normalization --------------------------------------


CLAUDE_ROUTE = RouteCapability(
    route_id="claude_opus-5",
    provider_family=ProviderFamily.CLAUDE,
    effort_keys=("low", "medium", "high", "max"),
)
XHIGH_ROUTE = RouteCapability(
    route_id="codex_gpt-5",
    provider_family=ProviderFamily.CODEX,
    effort_keys=("low", "medium", "high", "xhigh"),
)
HIGH_TOP_ROUTE = RouteCapability(
    route_id="glm-5.2",
    provider_family=ProviderFamily.GLM,
    effort_keys=("low", "medium", "high"),
)


@pytest.mark.parametrize(
    ("route", "ceiling"),
    [(CLAUDE_ROUTE, "max"), (XHIGH_ROUTE, "xhigh"), (HIGH_TOP_ROUTE, "high")],
)
def test_canonical_maximum_maps_to_the_routes_highest_declared_key(route, ceiling):
    effort = normalize_for_route(ReasoningProfile.MAXIMUM, route)
    assert effort.applied_key == ceiling
    assert effort.status is ControlStatus.APPLIED
    assert effort.applied is True
    assert effort.mapping is KeyMapping.SEMANTIC


@pytest.mark.parametrize(
    ("keys", "ceiling"),
    [
        (("minimal", "low", "medium", "high", "max"), "max"),
        (("medium", "high"), "high"),
        (("max",), "max"),
        (("high", "max", "medium"), "max"),
        (("low", "xhigh"), "xhigh"),
    ],
)
def test_canonical_maximum_maps_to_the_ceiling_for_every_declared_route_shape(keys, ceiling):
    route = RouteCapability("r", ProviderFamily.OTHER, effort_keys=keys)
    effort = normalize_for_route(ReasoningProfile.MAXIMUM, route)
    assert effort.applied_key == ceiling
    assert effort.applied is True


@pytest.mark.parametrize(
    ("keys", "ceiling"),
    [
        (("minimal", "low"), "low"),
        (("low", "medium"), "medium"),
        (("minimal",), "minimal"),
        (("low", "medium", "adaptive"), "medium"),
    ],
)
def test_canonical_maximum_is_never_applied_on_a_route_topping_out_below_high(keys, ceiling):
    # A ladder whose own ceiling is weaker than a real ``high`` cannot carry the
    # canonical maximum, so reporting APPLIED there would be a false claim that
    # the strongest request the policy can make was honored.
    route = RouteCapability("weak", ProviderFamily.OTHER, effort_keys=keys)
    effort = normalize_for_route(ReasoningProfile.MAXIMUM, route)
    assert effort.status is ControlStatus.CAPABILITY_CEILING
    assert effort.applied is False
    assert effort.applied_key == ceiling
    assert "not applied" in effort.detail


def test_one_unrecognized_key_does_not_disable_ranking_of_the_recognized_keys():
    route = RouteCapability(
        "mixed", ProviderFamily.OTHER, effort_keys=("low", "medium", "adaptive")
    )
    effort = normalize_for_route(ReasoningProfile.HIGH, route)
    assert effort.mapping is KeyMapping.SEMANTIC
    assert effort.status is ControlStatus.CAPABILITY_CEILING
    assert effort.applied is False
    assert effort.applied_key == "medium"
    assert "'adaptive'" in effort.detail


def test_recognized_keys_still_honor_a_request_alongside_an_unrecognized_one():
    route = RouteCapability(
        "mixed", ProviderFamily.OTHER, effort_keys=("adaptive", "medium", "high", "max")
    )
    high = normalize_for_route(ReasoningProfile.HIGH, route)
    assert high.mapping is KeyMapping.SEMANTIC
    assert high.applied_key == "high"
    assert high.applied is True
    top = normalize_for_route(ReasoningProfile.MAXIMUM, route)
    assert top.applied_key == "max"
    assert top.applied is True


def test_lower_rungs_pick_the_cheapest_key_that_still_honors_them():
    medium = normalize_for_route(ReasoningProfile.MEDIUM, CLAUDE_ROUTE)
    assert medium.applied_key == "medium"
    assert medium.applied is True
    high = normalize_for_route(ReasoningProfile.HIGH, CLAUDE_ROUTE)
    assert high.applied_key == "high"
    assert high.applied is True
    medium_high = normalize_for_route(ReasoningProfile.MEDIUM_HIGH, CLAUDE_ROUTE)
    assert medium_high.applied_key == "high"
    assert medium_high.applied is True


def test_a_declared_medium_high_key_is_used_instead_of_upgrading():
    route = RouteCapability(
        "r", ProviderFamily.OTHER, effort_keys=("low", "medium_high", "high", "max")
    )
    effort = normalize_for_route(ReasoningProfile.MEDIUM_HIGH, route)
    assert effort.applied_key == "medium_high"


def test_semantic_ranking_ignores_the_order_keys_were_declared_in():
    shuffled = RouteCapability(
        "r", ProviderFamily.OTHER, effort_keys=("max", "low", "high", "medium")
    )
    assert normalize_for_route(ReasoningProfile.MAXIMUM, shuffled).applied_key == "max"
    assert normalize_for_route(ReasoningProfile.HIGH, shuffled).applied_key == "high"


def test_normalization_never_selects_a_key_below_the_canonical_request():
    ladder = {
        "minimal": 10,
        "low": 20,
        "medium": 30,
        "medium_high": 40,
        "high": 50,
        "xhigh": 60,
        "max": 70,
    }
    required = {
        ReasoningProfile.MEDIUM: 30,
        ReasoningProfile.MEDIUM_HIGH: 40,
        ReasoningProfile.HIGH: 50,
        ReasoningProfile.MAXIMUM: 50,
    }
    for route in (CLAUDE_ROUTE, XHIGH_ROUTE, HIGH_TOP_ROUTE):
        for profile in ReasoningProfile:
            effort = normalize_for_route(profile, route)
            assert effort.status is ControlStatus.APPLIED
            assert ladder[effort.applied_key] >= required[profile]


# --- unsupported controls and ceilings --------------------------------------


def test_a_route_without_the_control_reports_unsupported_and_not_applied():
    route = RouteCapability("legacy", ProviderFamily.OTHER, supports_effort_control=False)
    effort = normalize_for_route(ReasoningProfile.MAXIMUM, route)
    assert effort.status is ControlStatus.UNSUPPORTED
    assert effort.applied is False
    assert effort.applied_key is None
    assert effort.mapping is KeyMapping.NONE


def test_a_route_with_the_control_but_no_keys_reports_provider_default():
    route = RouteCapability("opaque", ProviderFamily.OTHER, effort_keys=())
    effort = normalize_for_route(ReasoningProfile.MAXIMUM, route)
    assert effort.status is ControlStatus.PROVIDER_DEFAULT
    assert effort.applied is False
    assert effort.applied_key is None


def test_an_unsupported_control_is_never_reported_as_applied_for_any_profile():
    routes = [
        RouteCapability("legacy", ProviderFamily.OTHER, supports_effort_control=False),
        RouteCapability("opaque", ProviderFamily.OTHER, effort_keys=()),
    ]
    for route in routes:
        for profile in ReasoningProfile:
            effort = normalize_for_route(profile, route)
            assert effort.applied is False
            assert effort.applied_key is None
            assert effort.requested is profile


def test_a_route_ceiling_below_the_request_is_reported_not_silently_accepted():
    route = RouteCapability("tiny", ProviderFamily.OTHER, effort_keys=("minimal", "low", "medium"))
    effort = normalize_for_route(ReasoningProfile.HIGH, route)
    assert effort.status is ControlStatus.CAPABILITY_CEILING
    assert effort.applied is False
    assert effort.applied_key == "medium"
    assert "not applied" in effort.detail


UNRANKABLE_ROUTE = RouteCapability(
    "custom", ProviderFamily.OTHER, effort_keys=("think", "think_more", "think_hardest")
)


def test_an_unrankable_ladder_is_never_applied_from_positional_convention_alone():
    # Declaration order is a convention this route never attests, so reading a
    # ceiling out of it and reporting APPLIED would assert an ordering the
    # policy cannot prove -- the strongest request silently landing anywhere.
    for profile in ReasoningProfile:
        effort = normalize_for_route(profile, UNRANKABLE_ROUTE)
        assert effort.status is ControlStatus.UNVERIFIABLE, profile
        assert effort.applied is False, profile
        assert effort.applied_key is None, profile
        assert effort.mapping is KeyMapping.POSITIONAL, profile
        assert "not applied" in effort.detail, profile


def test_an_unrankable_ladder_still_explains_the_realized_positional_offset():
    top = normalize_for_route(ReasoningProfile.MAXIMUM, UNRANKABLE_ROUTE)
    assert "'think_hardest'" in top.detail
    assert "0 rung(s)" in top.detail
    mid = normalize_for_route(ReasoningProfile.HIGH, UNRANKABLE_ROUTE)
    assert "'think_more'" in mid.detail
    assert "1 rung(s)" in mid.detail


def test_a_single_key_route_reports_the_clamped_offset_without_claiming_applied():
    route = RouteCapability("one", ProviderFamily.OTHER, effort_keys=("only",))
    for profile in ReasoningProfile:
        effort = normalize_for_route(profile, route)
        assert effort.applied is False, profile
        assert effort.applied_key is None, profile
        # The rationale must name the offset realized after clamping -- every
        # profile lands on the one declared key -- not the requested offset.
        assert "'only'" in effort.detail, profile
        assert "0 rung(s)" in effort.detail, profile
        assert "1 rung(s)" not in effort.detail
        assert "2 rung(s)" not in effort.detail


@pytest.mark.parametrize(
    ("overrides", "profile"),
    [
        ({"risk_tier": RiskTier.CRITICAL}, ReasoningProfile.MAXIMUM),
        ({"work_kind": WorkKind.SECURITY}, ReasoningProfile.HIGH),
        ({"role": TaskRole.SECURITY_AUDITOR}, ReasoningProfile.HIGH),
    ],
)
def test_an_escalated_request_is_not_attested_on_an_unrankable_route(overrides, profile):
    # The escalated request is exactly the one a false APPLIED would hurt most.
    decision = resolve_reasoning_effort(_request(**overrides), UNRANKABLE_ROUTE)
    assert decision.profile is profile
    effort = decision.route_effort
    assert effort is not None
    assert effort.requested is profile
    assert effort.status is ControlStatus.UNVERIFIABLE
    assert effort.applied is False
    assert effort.applied_key is None
    payload = decision.as_dict()
    assert payload["route_effort"]["applied"] is False
    assert payload["route_effort"]["status"] == "unverifiable"
    assert json.loads(json.dumps(payload)) == payload


# --- separator-insensitive key ranking ---------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        "xhigh",
        "x-high",
        "x_high",
        "X High",
        "extra_high",
        "extra-high",
        "very high",
        "VeryHigh",
    ],
)
def test_maximum_resolves_a_stronger_separator_variant_rather_than_ignoring_it(variant):
    # Ranking the punctuation instead of the name would set the variant aside as
    # unknown and settle for the ``high`` beneath it -- a silent downgrade.
    route = RouteCapability("r", ProviderFamily.OTHER, effort_keys=("low", "high", variant))
    effort = normalize_for_route(ReasoningProfile.MAXIMUM, route)
    assert effort.status is ControlStatus.APPLIED
    assert effort.mapping is KeyMapping.SEMANTIC
    assert effort.applied_key == variant
    assert "ignored" not in effort.detail


@pytest.mark.parametrize(
    "variant", ["medium_high", "medium-high", "mediumhigh", "Medium High", "med_high"]
)
def test_separator_variants_of_medium_high_all_honor_the_medium_high_rung(variant):
    route = RouteCapability("r", ProviderFamily.OTHER, effort_keys=("low", variant, "high"))
    effort = normalize_for_route(ReasoningProfile.MEDIUM_HIGH, route)
    assert effort.applied_key == variant
    assert effort.applied is True


def test_equivalent_separator_spellings_rank_identically():
    spellings = ("xhigh", "x-high", "x_high", "X High", "extra-high", "very_high")
    resolved = set()
    for spelling in spellings:
        route = RouteCapability("r", ProviderFamily.OTHER, effort_keys=("high", spelling))
        effort = normalize_for_route(ReasoningProfile.MAXIMUM, route)
        # Every spelling must beat the declared ``high``, never tie with it.
        assert effort.applied_key == spelling
        resolved.add(effort.status)
    assert resolved == {ControlStatus.APPLIED}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"route_id": " ", "provider_family": ProviderFamily.OTHER},
        {"route_id": "r", "provider_family": ProviderFamily.OTHER, "effort_keys": ("high", "high")},
        {"route_id": "r", "provider_family": ProviderFamily.OTHER, "effort_keys": ("high", " ")},
        {"route_id": "r", "provider_family": ProviderFamily.OTHER, "effort_keys": ("high", "-")},
        {
            "route_id": "r",
            "provider_family": ProviderFamily.OTHER,
            # One rung spelled two ways is one key, not a two-rung ladder.
            "effort_keys": ("x-high", "x_high"),
        },
    ],
)
def test_malformed_route_capabilities_are_rejected(kwargs):
    with pytest.raises(ValueError):
        RouteCapability(**kwargs)


# --- combined resolution ----------------------------------------------------


def test_resolution_pairs_the_canonical_profile_with_the_route_key():
    decision = resolve_reasoning_effort(
        _request(provider_family=ProviderFamily.CLAUDE, risk_tier=RiskTier.CRITICAL),
        CLAUDE_ROUTE,
    )
    assert decision.profile is ReasoningProfile.MAXIMUM
    assert decision.route_effort is not None
    assert decision.route_effort.applied_key == "max"
    payload = decision.as_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["schema_id"] == POLICY_SCHEMA_ID


def test_an_unstated_provider_family_adopts_the_routes_family():
    decision = resolve_reasoning_effort(_request(), CLAUDE_ROUTE)
    assert decision.request.provider_family is ProviderFamily.CLAUDE
    assert decision.profile is ReasoningProfile.HIGH
    assert decision.route_effort is not None
    assert decision.route_effort.applied_key == "high"
    assert decision.rationale.baseline_reason == "standard_implementer_high"


def test_the_review_lens_survives_family_adoption_and_is_serialized():
    decision = resolve_reasoning_effort(
        _request(role=TaskRole.REVIEWER, review_lens="security"), CLAUDE_ROUTE
    )
    assert decision.request.review_lens == "security"
    assert decision.request.as_dict()["review_lens"] == "security"
    assert decision.profile is ReasoningProfile.HIGH
    assert decision.rationale.baseline is ReasoningProfile.MEDIUM
    assert decision.rationale.decisive_reason == "security_lens_review"
    assert decision.route_effort is not None
    assert decision.route_effort.applied_key == "high"


def test_every_rung_resolves_to_an_applied_key_on_the_codex_ladder():
    codex = RouteCapability(
        "codex_cli",
        ProviderFamily.CODEX,
        effort_keys=("minimal", "low", "medium", "high", "xhigh"),
    )
    expected = {
        ReasoningProfile.MEDIUM: "medium",
        ReasoningProfile.MEDIUM_HIGH: "high",
        ReasoningProfile.HIGH: "high",
        ReasoningProfile.MAXIMUM: "xhigh",
    }
    for profile in ReasoningProfile:
        effort = normalize_for_route(profile, codex)
        assert effort.applied is True, profile
        assert effort.applied_key == expected[profile], profile


def test_a_stated_family_that_contradicts_the_route_is_an_error():
    with pytest.raises(ValueError, match="provider family conflict"):
        resolve_reasoning_effort(_request(provider_family=ProviderFamily.CODEX), CLAUDE_ROUTE)


def test_resolution_without_a_route_reports_no_route_effort():
    decision = resolve_reasoning_effort(_request(provider_family=ProviderFamily.CLAUDE))
    assert decision.route_effort is None
    assert decision.as_dict()["route_effort"] is None


def test_the_whole_matrix_resolves_on_every_route_without_a_false_applied_claim():
    routes = [
        CLAUDE_ROUTE,
        XHIGH_ROUTE,
        HIGH_TOP_ROUTE,
        UNRANKABLE_ROUTE,
        RouteCapability("legacy", ProviderFamily.OTHER, supports_effort_control=False),
        RouteCapability("opaque", ProviderFamily.OTHER, effort_keys=()),
        RouteCapability("tiny", ProviderFamily.OTHER, effort_keys=("minimal", "low")),
    ]
    for base in ALL_REQUESTS:
        # Leave the family unstated so every request resolves on every route and
        # adopts that route's family, instead of skipping mismatched pairs.
        request = dataclasses.replace(base, provider_family=ProviderFamily.OTHER)
        for route in routes:
            decision = resolve_reasoning_effort(request, route)
            effort = decision.route_effort
            assert effort is not None
            assert decision.profile >= ABSOLUTE_FLOOR
            assert effort.requested is decision.profile
            if effort.applied:
                assert effort.applied_key is not None
                assert effort.status is ControlStatus.APPLIED
                # An applied claim may only ever come from a ranked key name;
                # no positional convention can ever produce one.
                assert effort.mapping is KeyMapping.SEMANTIC
            else:
                assert effort.status in {
                    ControlStatus.UNSUPPORTED,
                    ControlStatus.PROVIDER_DEFAULT,
                    ControlStatus.CAPABILITY_CEILING,
                    ControlStatus.UNVERIFIABLE,
                }
