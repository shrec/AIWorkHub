"""NF-2026-01703: MAXIMUM reasoning effort is very rare and an explicit decision.

Only a critical risk tier selects ``ReasoningProfile.MAXIMUM`` (Claude ``max``,
Codex and VS Code LM ``xhigh``). Security and architecture work, complex
difficulty, the reviewer security lens and a high-risk review target keep their
reason codes but lift to HIGH. A reviewer baselines at MEDIUM and takes its risk
tier, work kind and difficulty from the TARGET card, so an unknown target fails
closed to HIGH and a reviewer card's own fields never lift a review.
"""

from __future__ import annotations

import itertools
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

from aiworkhub import reasoning_policy, runtime_adapters, vscode_lm_bridge

Profile = reasoning_policy.ReasoningProfile
Role = reasoning_policy.TaskRole
Risk = reasoning_policy.RiskTier
Kind = reasoning_policy.WorkKind
Difficulty = reasoning_policy.Difficulty
Family = reasoning_policy.ProviderFamily

CLAUDE_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5")
REWORK_PREDECESSOR: dict[str, object] = {"request_id": "R-prev"}
QUIET_REVIEWER_CARD: dict[str, object] = {"risk_tier": "low"}
LOUD_REVIEWER_CARD: dict[str, object] = {
    "risk_tier": "critical",
    "difficulty": "complex",
    "work_kind": "security",
}
VSCODE_CAPABILITY = reasoning_policy.RouteCapability(
    route_id="vscode_lm",
    provider_family=Family.CLAUDE,
    supports_effort_control=True,
    effort_keys=("low", "medium", "high", "xhigh"),
)


def _request(
    *,
    role: Role = Role.IMPLEMENTER,
    risk: Risk = Risk.LOW,
    kind: Kind = Kind.REPOSITORY_CODING,
    difficulty: Difficulty = Difficulty.STANDARD,
    lens: str | None = None,
    family: Family = Family.CLAUDE,
) -> reasoning_policy.EffortRequest:
    return reasoning_policy.EffortRequest(
        role=role,
        risk_tier=risk,
        work_kind=kind,
        difficulty=difficulty,
        provider_family=family,
        review_lens=lens,
    )


def _cli_implementer(
    card: dict[str, object], model: str = "claude-opus-5-5"
) -> reasoning_policy.ReasoningDecision:
    decision = runtime_adapters.resolve_adapter_reasoning("claude_cli", card, model=model)
    assert decision is not None
    return decision


def _cli_reviewer(
    target_card: dict[str, object] | None,
    lens: str | None = "correctness",
    *,
    reviewer_card: dict[str, object] | None = None,
    model: str = "claude-opus-5-5",
) -> reasoning_policy.ReasoningDecision:
    decision = runtime_adapters.resolve_adapter_reasoning(
        "claude_cli",
        QUIET_REVIEWER_CARD if reviewer_card is None else reviewer_card,
        is_reviewer=True,
        model=model,
        target_card=target_card,
        lens=lens,
    )
    assert decision is not None
    return decision


def _vscode_decision(
    request_kind: str,
    card: dict[str, object] | None = None,
    *,
    target_card: dict[str, object] | None = None,
    lens: str | None = None,
) -> reasoning_policy.ReasoningDecision:
    effort_request = vscode_lm_bridge.vscode_lm_effort_request(
        request_kind=request_kind,
        card=card,
        model="claude-opus-5-5",
        target_card=target_card,
        lens=lens,
    )
    return reasoning_policy.resolve_reasoning_effort(effort_request, VSCODE_CAPABILITY)


def _applied_key(decision: reasoning_policy.ReasoningDecision) -> str | None:
    assert decision.route_effort is not None
    assert decision.route_effort.applied is True
    return decision.route_effort.applied_key


def _reasons(decision: reasoning_policy.ReasoningDecision) -> set[str]:
    return set(decision.rationale.reason_codes)


def _shape(effort_request: reasoning_policy.EffortRequest) -> tuple[object, ...]:
    return (
        effort_request.role,
        effort_request.risk_tier,
        effort_request.work_kind,
        effort_request.difficulty,
        effort_request.review_lens,
    )


# --- policy core: reason codes, baselines and the lifts ---------------------


def test_reason_codes_survive_and_the_reviewer_baseline_constant_is_renamed() -> None:
    assert reasoning_policy.REASON_CRITICAL_RISK == "critical_risk_tier"
    assert reasoning_policy.REASON_SECURITY_WORK == "security_work"
    assert reasoning_policy.REASON_ARCHITECTURE_WORK == "architecture_work"
    assert reasoning_policy.REASON_SECURITY_LENS_REVIEW == "security_lens_review"
    assert reasoning_policy.REASON_HIGH_RISK_TARGET_REVIEW == "high_risk_target_review"
    assert reasoning_policy.REASON_COMPLEX_DIFFICULTY == "complex_difficulty"
    assert reasoning_policy.REASON_REVIEWER_BASELINE_MEDIUM == "reviewer_baseline_medium"
    assert not hasattr(reasoning_policy, "REASON_REVIEWER_BASELINE_HIGH")


@pytest.mark.parametrize(
    ("effort_request", "reason"),
    [
        pytest.param(
            _request(kind=Kind.SECURITY),
            reasoning_policy.REASON_SECURITY_WORK,
            id="security-work",
        ),
        pytest.param(
            _request(role=Role.SECURITY_AUDITOR, kind=Kind.RESEARCH),
            reasoning_policy.REASON_SECURITY_WORK,
            id="security-auditor",
        ),
        pytest.param(
            _request(kind=Kind.ARCHITECTURE),
            reasoning_policy.REASON_ARCHITECTURE_WORK,
            id="architecture-work",
        ),
        pytest.param(
            _request(role=Role.ARCHITECT, kind=Kind.RESEARCH),
            reasoning_policy.REASON_ARCHITECTURE_WORK,
            id="architect",
        ),
        pytest.param(
            _request(role=Role.REVIEWER, kind=Kind.CORRECTNESS_REVIEW, lens="security"),
            reasoning_policy.REASON_SECURITY_LENS_REVIEW,
            id="security-lens",
        ),
        pytest.param(
            _request(role=Role.REVIEWER, risk=Risk.HIGH, kind=Kind.CORRECTNESS_REVIEW),
            reasoning_policy.REASON_HIGH_RISK_TARGET_REVIEW,
            id="high-risk-target",
        ),
        pytest.param(
            _request(difficulty=Difficulty.COMPLEX),
            reasoning_policy.REASON_COMPLEX_DIFFICULTY,
            id="complex-difficulty",
        ),
    ],
)
def test_every_lift_except_critical_risk_lifts_to_high_and_is_still_recorded(
    effort_request: reasoning_policy.EffortRequest, reason: str
) -> None:
    rationale = reasoning_policy.select_reasoning_profile(effort_request)
    lifts = {lift.reason: lift.profile for lift in rationale.escalations}
    assert lifts[reason] is Profile.HIGH
    assert Profile.MAXIMUM not in lifts.values()
    assert reason in rationale.reason_codes
    assert rationale.selected is Profile.HIGH


@pytest.mark.parametrize("role", list(Role))
def test_critical_risk_is_the_only_lift_that_selects_maximum(role: Role) -> None:
    rationale = reasoning_policy.select_reasoning_profile(_request(role=role, risk=Risk.CRITICAL))
    assert rationale.selected is Profile.MAXIMUM
    assert rationale.decisive_reason == reasoning_policy.REASON_CRITICAL_RISK
    maximum_reasons = [
        lift.reason for lift in rationale.escalations if lift.profile is Profile.MAXIMUM
    ]
    assert maximum_reasons == [reasoning_policy.REASON_CRITICAL_RISK]


@pytest.mark.parametrize(
    ("effort_request", "baseline", "reason"),
    [
        pytest.param(
            _request(role=Role.REVIEWER, risk=Risk.LOW, kind=Kind.CORRECTNESS_REVIEW),
            Profile.MEDIUM,
            "reviewer_baseline_medium",
            id="reviewer-low",
        ),
        pytest.param(
            _request(role=Role.REVIEWER, risk=Risk.MEDIUM, kind=Kind.CORRECTNESS_REVIEW),
            Profile.MEDIUM,
            "reviewer_baseline_medium",
            id="reviewer-medium",
        ),
        pytest.param(
            _request(role=Role.REVIEWER, risk=Risk.HIGH, kind=Kind.CORRECTNESS_REVIEW),
            Profile.MEDIUM,
            "reviewer_baseline_medium",
            id="reviewer-high-lifts-later",
        ),
        pytest.param(
            _request(risk=Risk.LOW, difficulty=Difficulty.BOUNDED),
            Profile.MEDIUM,
            "bounded_implementer_medium",
            id="bounded-low",
        ),
        pytest.param(
            _request(risk=Risk.MEDIUM, difficulty=Difficulty.BOUNDED),
            Profile.MEDIUM,
            "bounded_implementer_medium",
            id="bounded-medium",
        ),
        pytest.param(
            _request(risk=Risk.HIGH, difficulty=Difficulty.BOUNDED),
            Profile.HIGH,
            "bounded_high_risk_implementer_high",
            id="bounded-high-risk",
        ),
        pytest.param(
            _request(risk=Risk.LOW, difficulty=Difficulty.STANDARD),
            Profile.HIGH,
            "standard_implementer_high",
            id="standard",
        ),
        pytest.param(
            _request(kind=Kind.MECHANICAL_EDIT, risk=Risk.LOW, difficulty=Difficulty.BOUNDED),
            Profile.MEDIUM,
            "explicit_mechanical_bounded_low_risk",
            id="explicit-mechanical",
        ),
    ],
)
def test_the_reviewer_baseline_is_medium_and_the_implementer_baselines_are_unchanged(
    effort_request: reasoning_policy.EffortRequest, baseline: Profile, reason: str
) -> None:
    rationale = reasoning_policy.select_reasoning_profile(effort_request)
    assert rationale.baseline is baseline
    assert rationale.baseline_reason == reason


# --- (a) critical risk is the only road to MAXIMUM --------------------------


@pytest.mark.parametrize("model", CLAUDE_MODELS)
@pytest.mark.parametrize("difficulty", ["bounded", "standard", "complex"])
@pytest.mark.parametrize("work_kind", ["generic", "security", "architecture"])
def test_a_critical_implementer_resolves_to_maximum(
    model: str, difficulty: str, work_kind: str
) -> None:
    decision = _cli_implementer(
        {"risk_tier": "critical", "work_kind": work_kind, "difficulty": difficulty}, model
    )
    assert decision.profile is Profile.MAXIMUM
    assert _applied_key(decision) == "max"
    assert decision.rationale.decisive_reason == "critical_risk_tier"


@pytest.mark.parametrize("model", CLAUDE_MODELS)
@pytest.mark.parametrize("lens", [None, "correctness", "security", "architecture"])
def test_a_reviewer_of_a_critical_target_resolves_to_maximum(
    model: str, lens: str | None
) -> None:
    decision = _cli_reviewer({"risk_tier": "critical", "difficulty": "bounded"}, lens, model=model)
    assert decision.profile is Profile.MAXIMUM
    assert _applied_key(decision) == "max"
    assert decision.rationale.decisive_reason == "critical_risk_tier"
    assert {"critical_risk_tier", "high_risk_target_review"} <= _reasons(decision)


# --- (b) security / architecture / complex implementers stop at HIGH --------

HIGH_LIFT_CARDS = [
    pytest.param(
        {"risk_tier": risk, "work_kind": kind, "difficulty": difficulty},
        reason,
        id=f"{risk}-{kind}-{difficulty}",
    )
    for risk in ("low", "medium", "high")
    for kind, reason in (("security", "security_work"), ("architecture", "architecture_work"))
    for difficulty in ("bounded", "standard", "complex")
] + [
    pytest.param(
        {"risk_tier": risk, "difficulty": "complex"},
        "complex_difficulty",
        id=f"{risk}-complex",
    )
    for risk in ("low", "medium", "high")
]


@pytest.mark.parametrize("model", CLAUDE_MODELS)
@pytest.mark.parametrize(("card", "reason"), HIGH_LIFT_CARDS)
def test_security_architecture_and_complex_implementers_resolve_to_high_never_max(
    model: str, card: dict[str, object], reason: str
) -> None:
    decision = _cli_implementer(card, model)
    assert decision.profile is Profile.HIGH
    assert _applied_key(decision) == "high"
    assert reason in _reasons(decision)
    assert "critical_risk_tier" not in _reasons(decision)
    assert Profile.MAXIMUM not in {lift.profile for lift in decision.rationale.escalations}


# --- (c) a reviewer of a high-risk target is HIGH, a benign one MEDIUM ------


@pytest.mark.parametrize("model", CLAUDE_MODELS)
@pytest.mark.parametrize("difficulty", ["bounded", "standard", "complex"])
@pytest.mark.parametrize("lens", [None, "correctness", "performance", "security", " Security "])
def test_a_reviewer_of_a_high_risk_target_resolves_to_high_with_any_lens(
    model: str, difficulty: str, lens: str | None
) -> None:
    decision = _cli_reviewer({"risk_tier": "high", "difficulty": difficulty}, lens, model=model)
    assert decision.profile is Profile.HIGH
    assert _applied_key(decision) == "high"
    assert decision.rationale.baseline is Profile.MEDIUM
    assert "high_risk_target_review" in _reasons(decision)


@pytest.mark.parametrize("model", CLAUDE_MODELS)
@pytest.mark.parametrize("lens", [None, "correctness", "performance"])
def test_a_reviewer_of_a_medium_risk_standard_non_security_target_resolves_to_medium(
    model: str, lens: str | None
) -> None:
    decision = _cli_reviewer({"risk_tier": "medium", "difficulty": "standard"}, lens, model=model)
    assert decision.profile is Profile.MEDIUM
    assert _applied_key(decision) == "medium"
    assert decision.rationale.baseline_reason == "reviewer_baseline_medium"
    assert decision.rationale.escalations == ()


@pytest.mark.parametrize("lens", ["security", " Security "])
def test_the_security_lens_lifts_a_medium_risk_target_to_high_not_max(lens: str) -> None:
    decision = _cli_reviewer({"risk_tier": "medium", "difficulty": "standard"}, lens)
    assert decision.profile is Profile.HIGH
    assert _applied_key(decision) == "high"
    assert _reasons(decision) == {"reviewer_baseline_medium", "security_lens_review"}


# --- (d) the reviewer's work kind comes from the TARGET card ----------------


@pytest.mark.parametrize("model", CLAUDE_MODELS)
@pytest.mark.parametrize(
    ("work_kind", "expected_kind", "reason"),
    [
        ("security", Kind.SECURITY, "security_work"),
        ("architecture", Kind.ARCHITECTURE, "architecture_work"),
    ],
)
def test_a_reviewer_of_a_security_or_architecture_target_resolves_to_high(
    model: str, work_kind: str, expected_kind: Kind, reason: str
) -> None:
    decision = _cli_reviewer(
        {"risk_tier": "medium", "work_kind": work_kind, "difficulty": "standard"},
        "correctness",
        model=model,
    )
    assert decision.request.work_kind is expected_kind
    assert decision.request.risk_tier is Risk.MEDIUM
    assert decision.profile is Profile.HIGH
    assert _applied_key(decision) == "high"
    assert reason in _reasons(decision)


def test_a_reviewer_cards_own_work_kind_never_lifts_the_review() -> None:
    decision = _cli_reviewer(
        {"risk_tier": "medium", "difficulty": "standard"},
        "correctness",
        reviewer_card={"risk_tier": "low", "work_kind": "security"},
    )
    assert decision.request.work_kind is Kind.CORRECTNESS_REVIEW
    assert decision.profile is Profile.MEDIUM
    assert _applied_key(decision) == "medium"


@pytest.mark.parametrize(
    ("target_work_kind", "expected_kind"),
    [
        ("security", Kind.SECURITY),
        (" Architecture ", Kind.ARCHITECTURE),
        ("generic", Kind.CORRECTNESS_REVIEW),
        (None, Kind.CORRECTNESS_REVIEW),
    ],
)
def test_the_derived_reviewer_work_kind_is_the_target_work_kind(
    target_work_kind: str | None, expected_kind: Kind
) -> None:
    target: dict[str, object] = {"risk_tier": "medium"}
    if target_work_kind is not None:
        target["work_kind"] = target_work_kind
    derived = runtime_adapters._derive_reasoning_request(
        "claude_cli",
        {"risk_tier": "low", "work_kind": "architecture"},
        is_reviewer=True,
        target_card=target,
    )
    assert derived.role is Role.REVIEWER
    assert derived.work_kind is expected_kind


# --- (e) an unknown target fails closed to HIGH -----------------------------


@pytest.mark.parametrize("model", CLAUDE_MODELS)
@pytest.mark.parametrize("lens", [None, "correctness", "security"])
@pytest.mark.parametrize(
    "reviewer_card", [QUIET_REVIEWER_CARD, LOUD_REVIEWER_CARD], ids=["quiet", "loud"]
)
def test_a_reviewer_with_an_unknown_target_resolves_to_high_never_max(
    model: str, lens: str | None, reviewer_card: dict[str, object]
) -> None:
    decision = _cli_reviewer(None, lens, reviewer_card=reviewer_card, model=model)
    assert decision.request.risk_tier is Risk.HIGH
    assert decision.request.work_kind is Kind.CORRECTNESS_REVIEW
    assert decision.request.difficulty is Difficulty.STANDARD
    assert decision.profile is Profile.HIGH
    assert _applied_key(decision) == "high"
    assert "high_risk_target_review" in _reasons(decision)
    assert "critical_risk_tier" not in _reasons(decision)


# --- (f) a rework card without a declared difficulty is COMPLEX -> HIGH -----


@pytest.mark.parametrize("model", CLAUDE_MODELS)
def test_a_rework_card_without_a_declared_difficulty_at_high_risk_resolves_to_high(
    model: str,
) -> None:
    decision = _cli_implementer(
        {"risk_tier": "high", "rework_predecessor": dict(REWORK_PREDECESSOR)}, model
    )
    assert decision.request.difficulty is Difficulty.COMPLEX
    assert decision.profile is Profile.HIGH
    assert _applied_key(decision) == "high"
    assert "complex_difficulty" in _reasons(decision)


@pytest.mark.parametrize("model", CLAUDE_MODELS)
def test_a_declared_difficulty_still_wins_on_a_rework_card(model: str) -> None:
    decision = _cli_implementer(
        {
            "risk_tier": "high",
            "difficulty": "bounded",
            "rework_predecessor": dict(REWORK_PREDECESSOR),
        },
        model,
    )
    assert decision.request.difficulty is Difficulty.BOUNDED
    assert decision.profile is Profile.HIGH
    assert decision.rationale.baseline_reason == "bounded_high_risk_implementer_high"
    assert "complex_difficulty" not in _reasons(decision)


def test_only_a_critical_rework_card_reaches_max() -> None:
    decision = _cli_implementer(
        {"risk_tier": "critical", "rework_predecessor": dict(REWORK_PREDECESSOR)}
    )
    assert decision.request.difficulty is Difficulty.COMPLEX
    assert decision.profile is Profile.MAXIMUM
    assert _applied_key(decision) == "max"


def test_a_reviewer_of_a_high_risk_rework_target_resolves_to_high() -> None:
    decision = _cli_reviewer(
        {"risk_tier": "high", "rework_predecessor": dict(REWORK_PREDECESSOR)}
    )
    assert decision.request.difficulty is Difficulty.COMPLEX
    assert decision.profile is Profile.HIGH
    assert _applied_key(decision) == "high"


# --- (g) exhaustive: no non-critical request ever selects MAXIMUM -----------

NON_CRITICAL_RISKS = tuple(tier for tier in Risk if tier is not Risk.CRITICAL)
LENSES = (None, "correctness", "security", " Security ", "architecture")


def _all_requests(risks: Iterable[Risk]) -> Iterator[reasoning_policy.EffortRequest]:
    for role, risk, kind, difficulty, family, lens in itertools.product(
        Role, risks, Kind, Difficulty, Family, LENSES
    ):
        yield _request(
            role=role, risk=risk, kind=kind, difficulty=difficulty, lens=lens, family=family
        )


def _is_mandatory_high(effort_request: reasoning_policy.EffortRequest) -> bool:
    reviewer = effort_request.role is Role.REVIEWER
    return (
        effort_request.work_kind in {Kind.SECURITY, Kind.ARCHITECTURE}
        or effort_request.role in {Role.SECURITY_AUDITOR, Role.ARCHITECT}
        or effort_request.difficulty is Difficulty.COMPLEX
        or (reviewer and effort_request.risk_tier in {Risk.HIGH, Risk.CRITICAL})
        or (reviewer and (effort_request.review_lens or "").strip().lower() == "security")
    )


def test_no_non_critical_request_ever_selects_or_records_maximum() -> None:
    offenders = []
    for effort_request in _all_requests(NON_CRITICAL_RISKS):
        rationale = reasoning_policy.select_reasoning_profile(effort_request)
        records_maximum = any(lift.profile is Profile.MAXIMUM for lift in rationale.escalations)
        if rationale.selected is Profile.MAXIMUM or records_maximum:
            offenders.append(effort_request)
    assert not offenders, offenders[:3]


def test_non_critical_requests_reach_exactly_high_when_a_lift_applies() -> None:
    offenders = []
    for effort_request in _all_requests(NON_CRITICAL_RISKS):
        rationale = reasoning_policy.select_reasoning_profile(effort_request)
        expected = Profile.HIGH if _is_mandatory_high(effort_request) else rationale.baseline
        if rationale.selected is not expected:
            offenders.append(effort_request)
    assert not offenders, offenders[:3]


def test_every_critical_request_selects_maximum_through_the_critical_lift() -> None:
    offenders = []
    for effort_request in _all_requests((Risk.CRITICAL,)):
        rationale = reasoning_policy.select_reasoning_profile(effort_request)
        if (
            rationale.selected is not Profile.MAXIMUM
            or rationale.decisive_reason != reasoning_policy.REASON_CRITICAL_RISK
        ):
            offenders.append(effort_request)
    assert not offenders, offenders[:3]


def _target_cards() -> Iterator[dict[str, object]]:
    for risk, kind, difficulty, rework in itertools.product(
        (None, "low", "medium", "high", "unrecognised"),
        (None, "generic", "security", "architecture", "documentation"),
        (None, "bounded", "standard", "complex", "unrecognised"),
        (False, True),
    ):
        card: dict[str, object] = {}
        if risk is not None:
            card["risk_tier"] = risk
        if kind is not None:
            card["work_kind"] = kind
        if difficulty is not None:
            card["difficulty"] = difficulty
        if rework:
            card["rework_predecessor"] = dict(REWORK_PREDECESSOR)
        yield card


NON_CRITICAL_CARDS = tuple(_target_cards())
REVIEW_LENSES = (None, "correctness", "security")
REVIEWER_CARDS = (QUIET_REVIEWER_CARD, LOUD_REVIEWER_CARD)


@pytest.mark.parametrize("model", CLAUDE_MODELS)
def test_no_non_critical_card_reaches_max_on_the_cli_route(model: str) -> None:
    offenders = []
    for card in NON_CRITICAL_CARDS:
        decisions = [("implementer", _cli_implementer(card, model))]
        for lens, reviewer_card in itertools.product(REVIEW_LENSES, REVIEWER_CARDS):
            decisions.append(
                (
                    f"reviewer:{lens}",
                    _cli_reviewer(card, lens, reviewer_card=reviewer_card, model=model),
                )
            )
        for label, decision in decisions:
            if decision.profile is Profile.MAXIMUM or _applied_key(decision) == "max":
                offenders.append((label, card))
    assert not offenders, offenders[:3]


def test_no_non_critical_card_reaches_xhigh_on_the_vscode_route() -> None:
    offenders = []
    for card in NON_CRITICAL_CARDS:
        decisions = [("worker", _vscode_decision("worker", card))]
        for lens, reviewer_card in itertools.product(REVIEW_LENSES, REVIEWER_CARDS):
            decisions.append(
                (
                    f"review:{lens}",
                    _vscode_decision(
                        "quality_review", reviewer_card, target_card=card, lens=lens
                    ),
                )
            )
        for label, decision in decisions:
            if decision.profile is Profile.MAXIMUM or _applied_key(decision) == "xhigh":
                offenders.append((label, card))
    assert not offenders, offenders[:3]


# --- (h) the CLI and vscode_lm routes derive the same profile ---------------

PARITY_TARGETS: tuple[dict[str, object] | None, ...] = (
    *NON_CRITICAL_CARDS,
    {"risk_tier": "critical"},
    {"risk_tier": "critical", "work_kind": "security", "difficulty": "complex"},
    None,
)


@pytest.mark.parametrize("lens", [None, "correctness", "security", " Security "])
def test_cli_and_vscode_lm_reviewers_derive_the_same_profile_for_every_target(
    lens: str | None,
) -> None:
    for target in PARITY_TARGETS:
        cli = _cli_reviewer(target, lens)
        vscode = _vscode_decision("quality_review", {}, target_card=target, lens=lens)
        assert _shape(vscode.request) == _shape(cli.request), target
        assert vscode.profile is cli.profile, target
        assert vscode.rationale.reason_codes == cli.rationale.reason_codes, target


@pytest.mark.parametrize(
    "reviewer_card", [QUIET_REVIEWER_CARD, LOUD_REVIEWER_CARD], ids=["quiet", "loud"]
)
@pytest.mark.parametrize(
    ("target_card", "expected"),
    [
        ({"risk_tier": "critical"}, Profile.MAXIMUM),
        ({"risk_tier": "high"}, Profile.HIGH),
        ({"risk_tier": "medium", "work_kind": "security"}, Profile.HIGH),
        ({"risk_tier": "medium", "difficulty": "standard"}, Profile.MEDIUM),
        (None, Profile.HIGH),
    ],
)
def test_both_reviewer_routes_ignore_the_reviewer_card_and_follow_the_target(
    reviewer_card: dict[str, object],
    target_card: dict[str, object] | None,
    expected: Profile,
) -> None:
    cli = _cli_reviewer(target_card, "correctness", reviewer_card=reviewer_card)
    vscode = _vscode_decision(
        "quality_review", reviewer_card, target_card=target_card, lens="correctness"
    )
    assert cli.profile is expected
    assert vscode.profile is expected


@pytest.mark.parametrize(
    ("card", "expected"),
    [
        ({"risk_tier": "critical"}, Profile.MAXIMUM),
        ({"risk_tier": "high", "work_kind": "security"}, Profile.HIGH),
        ({"risk_tier": "high", "work_kind": "architecture"}, Profile.HIGH),
        ({"risk_tier": "high", "difficulty": "complex"}, Profile.HIGH),
        ({"risk_tier": "high", "rework_predecessor": dict(REWORK_PREDECESSOR)}, Profile.HIGH),
    ],
)
def test_cli_and_vscode_lm_implementers_derive_the_same_profile(
    card: dict[str, object], expected: Profile
) -> None:
    cli = _cli_implementer(card)
    vscode = _vscode_decision("worker", card)
    assert _shape(vscode.request) == _shape(cli.request)
    assert cli.profile is expected
    assert vscode.profile is expected


# --- the launch path, provider mapping and receipt shape --------------------


def _claude_effort_argv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    decision: reasoning_policy.ReasoningDecision,
) -> list[str]:
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _name: sys.executable)
    plan = runtime_adapters.build_runtime_command(
        "claude_cli",
        "write tests",
        tmp_path,
        model="claude-opus-5",
        reasoning_decision=decision,
        context_capacity=1_000_000,
    )
    assert plan.launchable is True
    return plan.argv[-2:]


@pytest.mark.parametrize(
    ("card", "expected_effort"),
    [
        pytest.param({"risk_tier": "high", "work_kind": "security"}, "high", id="security"),
        pytest.param(
            {"risk_tier": "high", "work_kind": "architecture"}, "high", id="architecture"
        ),
        pytest.param({"risk_tier": "high", "difficulty": "complex"}, "high", id="complex"),
        pytest.param(
            {"risk_tier": "high", "rework_predecessor": dict(REWORK_PREDECESSOR)},
            "high",
            id="undeclared-rework",
        ),
        pytest.param({"risk_tier": "critical"}, "max", id="critical"),
    ],
)
def test_claude_worker_launch_passes_effort_max_only_for_a_critical_card(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    card: dict[str, object],
    expected_effort: str,
) -> None:
    decision = runtime_adapters.resolve_adapter_reasoning("claude_cli", card, model="claude-opus-5")
    assert decision is not None
    assert _claude_effort_argv(monkeypatch, tmp_path, decision) == ["--effort", expected_effort]


@pytest.mark.parametrize(
    ("target_card", "lens", "expected_effort"),
    [
        pytest.param({"risk_tier": "high"}, "correctness", "high", id="high-risk-target"),
        pytest.param(
            {"risk_tier": "medium", "work_kind": "security"}, "correctness", "high", id="security"
        ),
        pytest.param({"risk_tier": "low"}, "security", "high", id="security-lens"),
        pytest.param(None, "correctness", "high", id="unknown-target"),
        pytest.param(
            {"risk_tier": "medium", "difficulty": "standard"},
            "correctness",
            "medium",
            id="benign-target",
        ),
        pytest.param({"risk_tier": "critical"}, "correctness", "max", id="critical-target"),
    ],
)
def test_claude_review_launch_passes_effort_max_only_for_a_critical_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target_card: dict[str, object] | None,
    lens: str,
    expected_effort: str,
) -> None:
    decision = runtime_adapters.resolve_adapter_reasoning(
        "claude_cli",
        QUIET_REVIEWER_CARD,
        is_reviewer=True,
        model="claude-opus-5",
        target_card=target_card,
        lens=lens,
    )
    assert decision is not None
    assert _claude_effort_argv(monkeypatch, tmp_path, decision) == ["--effort", expected_effort]


@pytest.mark.parametrize(
    ("card", "expected_key"),
    [
        ({"risk_tier": "high", "work_kind": "security"}, "high"),
        ({"risk_tier": "high", "work_kind": "architecture"}, "high"),
        ({"risk_tier": "high", "difficulty": "complex"}, "high"),
        ({"risk_tier": "critical"}, "xhigh"),
    ],
)
def test_codex_reaches_xhigh_only_for_a_critical_card(
    card: dict[str, object], expected_key: str
) -> None:
    decision = runtime_adapters.resolve_adapter_reasoning("codex_cli", card, model="gpt-5.5")
    assert decision is not None
    assert _applied_key(decision) == expected_key


def test_provider_key_tables_are_unchanged() -> None:
    expected = {
        ("claude_cli", "claude-opus-5-5"): {
            Profile.MEDIUM: "medium",
            Profile.HIGH: "high",
            Profile.MAXIMUM: "max",
        },
        ("claude_cli", "claude-haiku-4.5"): {
            Profile.HIGH: "high",
            Profile.MAXIMUM: "high",
        },
        ("codex_cli", "gpt-5.5"): {
            Profile.MEDIUM: "medium",
            Profile.HIGH: "high",
            Profile.MAXIMUM: "xhigh",
        },
    }
    for (adapter_id, model), keys in expected.items():
        capability = runtime_adapters.route_reasoning_capability(adapter_id, model)
        assert capability is not None
        for profile, key in keys.items():
            route = reasoning_policy.normalize_for_route(profile, capability)
            assert route.applied is True, (adapter_id, model, profile)
            assert route.applied_key == key, (adapter_id, model, profile)


def test_the_decision_and_rationale_shape_is_unchanged() -> None:
    payload = _cli_implementer({"risk_tier": "high", "work_kind": "security"}).as_dict()
    assert set(payload) == {"schema_id", "request", "profile", "rationale", "route_effort"}
    assert payload["profile"] == "canonical_high"
    assert payload["route_effort"]["applied_key"] == "high"
    rationale = payload["rationale"]
    assert set(rationale) == {
        "baseline",
        "baseline_reason",
        "escalations",
        "selected",
        "decisive_reason",
        "never_below",
        "invariants",
        "reason_codes",
    }
    assert rationale["escalations"] == [{"reason": "security_work", "profile": "canonical_high"}]
    assert rationale["decisive_reason"] == "standard_implementer_high"
    assert rationale["reason_codes"] == ["standard_implementer_high", "security_work"]
