"""Deterministic, provider-neutral reasoning-effort policy core (NF-2026-00897).

This module is pure: no I/O, no provider calls, no token-budget arithmetic. It
answers one question -- *how much reasoning effort does this task deserve* --
from four declared inputs (role, risk tier, work kind, difficulty), and then
normalizes that canonical answer onto whatever key a concrete route actually
declares.

Two invariants are structural here rather than advisory:

* The canonical ladder has no ``low``/``minimal`` rung, so no branch can select
  one to save cost. The cheap rung is unrepresentable, not merely unselected.
* ``applied`` is ``True`` only where a declared key of the route provably honors
  the canonical request. A route with no effort control reports ``unsupported``
  / ``provider_default``; a route whose ceiling sits below the request reports
  ``capability_ceiling``; a route whose declared key names cannot be ranked at
  all reports ``unverifiable``, because declaration order alone proves no
  ordering. In every one of those cases ``applied`` is ``False``: the policy
  never claims an effort value was applied that the route cannot be shown to
  accept.

Runtime wiring (shared runtime adapters, process launcher) is deliberately out
of scope for this foundation; callers consume :func:`resolve_reasoning_effort`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from functools import total_ordering
from typing import Any

__all__ = [
    "ABSOLUTE_FLOOR",
    "POLICY_SCHEMA_ID",
    "ControlStatus",
    "Difficulty",
    "EffortRequest",
    "Escalation",
    "KeyMapping",
    "ProviderFamily",
    "Rationale",
    "ReasoningDecision",
    "ReasoningProfile",
    "RiskTier",
    "RouteCapability",
    "RouteEffort",
    "TaskRole",
    "WorkKind",
    "normalize_for_route",
    "provider_family_for_route",
    "resolve_reasoning_effort",
    "select_reasoning_profile",
]

POLICY_SCHEMA_ID = "aiworkhub.reasoning_policy.decision.v1"


# --- canonical ladder -------------------------------------------------------


@total_ordering
class ReasoningProfile(Enum):
    """Canonical, provider-neutral reasoning-effort rungs, ascending.

    There is deliberately no ``LOW`` or ``MINIMAL`` member: this policy core
    never trades reasoning quality for cost, so those rungs cannot be named.
    """

    MEDIUM_HIGH = "canonical_medium_high"
    HIGH = "canonical_high"
    MAXIMUM = "canonical_maximum"

    @property
    def rank(self) -> int:
        """Ascending quality rank; higher means more reasoning effort."""
        return _PROFILE_RANK[self]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ReasoningProfile):
            return NotImplemented
        return self.rank < other.rank


_PROFILE_RANK: dict[ReasoningProfile, int] = {
    ReasoningProfile.MEDIUM_HIGH: 1,
    ReasoningProfile.HIGH: 2,
    ReasoningProfile.MAXIMUM: 3,
}

#: The lowest rung any branch of this policy may ever select.
ABSOLUTE_FLOOR = ReasoningProfile.MEDIUM_HIGH


# --- declared inputs --------------------------------------------------------


class TaskRole(Enum):
    """Who the work is being done as."""

    IMPLEMENTER = "implementer"
    MECHANICAL_OPERATOR = "mechanical_operator"
    PLANNER = "planner"
    REVIEWER = "reviewer"
    ARCHITECT = "architect"
    SECURITY_AUDITOR = "security_auditor"


class RiskTier(Enum):
    """Blast radius of getting the work wrong."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class WorkKind(Enum):
    """What kind of work the task is."""

    REPOSITORY_CODING = "repository_coding"
    MECHANICAL_EDIT = "mechanical_edit"
    REWORK = "rework"
    ARCHITECTURE = "architecture"
    CORRECTNESS_REVIEW = "correctness_review"
    SECURITY = "security"
    RESEARCH = "research"
    DOCUMENTATION = "documentation"


class Difficulty(Enum):
    """How much of the problem the task statement already pins down."""

    BOUNDED = "bounded"
    STANDARD = "standard"
    COMPLEX = "complex"


class ProviderFamily(Enum):
    """Coarse provider family of the route that will run the task."""

    CLAUDE = "claude"
    CODEX = "codex"
    GEMINI = "gemini"
    GLM = "glm"
    DEEPSEEK = "deepseek"
    OTHER = "other"


_FAMILY_TOKENS: tuple[tuple[ProviderFamily, frozenset[str]], ...] = (
    (
        ProviderFamily.CLAUDE,
        frozenset({"claude", "anthropic", "opus", "sonnet", "haiku", "fable"}),
    ),
    (ProviderFamily.CODEX, frozenset({"codex", "openai", "gpt", "o3", "o4"})),
    (ProviderFamily.GEMINI, frozenset({"gemini", "google"})),
    (ProviderFamily.GLM, frozenset({"glm", "zhipu"})),
    (ProviderFamily.DEEPSEEK, frozenset({"deepseek"})),
)

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def provider_family_for_route(route_id: str) -> ProviderFamily:
    """Classify a runner/route identifier into a :class:`ProviderFamily`.

    Matching is token based and order deterministic; an unrecognized route is
    ``OTHER`` rather than a guess. ``OTHER`` never unlocks a higher default, so
    a misclassification can only cost quality-neutral defaults, never silently
    lower effort below the repository floor.
    """
    tokens = {token for token in _TOKEN_SPLIT.split(route_id.strip().lower()) if token}
    for family, markers in _FAMILY_TOKENS:
        if tokens & markers:
            return family
    return ProviderFamily.OTHER


@dataclass(frozen=True)
class EffortRequest:
    """The four declared policy inputs plus the provider family they run on."""

    role: TaskRole
    risk_tier: RiskTier
    work_kind: WorkKind
    difficulty: Difficulty
    provider_family: ProviderFamily = ProviderFamily.OTHER

    def as_dict(self) -> dict[str, str]:
        return {
            "role": self.role.value,
            "risk_tier": self.risk_tier.value,
            "work_kind": self.work_kind.value,
            "difficulty": self.difficulty.value,
            "provider_family": self.provider_family.value,
        }


# --- rationale --------------------------------------------------------------

REASON_CRITICAL_RISK = "critical_risk_tier"
REASON_SECURITY_WORK = "security_work"
REASON_ARCHITECTURE_WORK = "architecture_work"
REASON_CORRECTNESS_REVIEW_WORK = "correctness_review_work"
REASON_COMPLEX_DIFFICULTY = "complex_difficulty"
REASON_CLAUDE_REPOSITORY_DEFAULT = "claude_repository_coding_default_maximum"
REASON_CLAUDE_MECHANICAL_FLOOR = "claude_explicit_mechanical_floor_high"
REASON_EXPLICIT_MECHANICAL = "explicit_mechanical_bounded_low_risk"
REASON_REPOSITORY_QUALITY_FLOOR = "repository_coding_quality_floor_high"
REASON_GENERAL_QUALITY_FLOOR = "general_quality_floor_high"

INVARIANT_NO_COST_DOWNGRADE = "no_cost_based_downgrade"
INVARIANT_NO_LOW_OR_MINIMAL = "canonical_ladder_excludes_low_and_minimal"


@dataclass(frozen=True)
class Escalation:
    """One mandatory lift of the baseline profile, with its reason code."""

    reason: str
    profile: ReasoningProfile

    def as_dict(self) -> dict[str, str]:
        return {"reason": self.reason, "profile": self.profile.value}


@dataclass(frozen=True)
class Rationale:
    """Machine-readable account of why a profile was selected."""

    baseline: ReasoningProfile
    baseline_reason: str
    escalations: tuple[Escalation, ...]
    selected: ReasoningProfile
    decisive_reason: str
    never_below: ReasoningProfile = ABSOLUTE_FLOOR
    invariants: tuple[str, ...] = (
        INVARIANT_NO_COST_DOWNGRADE,
        INVARIANT_NO_LOW_OR_MINIMAL,
    )

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return (self.baseline_reason, *(esc.reason for esc in self.escalations))

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline.value,
            "baseline_reason": self.baseline_reason,
            "escalations": [esc.as_dict() for esc in self.escalations],
            "selected": self.selected.value,
            "decisive_reason": self.decisive_reason,
            "never_below": self.never_below.value,
            "invariants": list(self.invariants),
            "reason_codes": list(self.reason_codes),
        }


# --- selection --------------------------------------------------------------

_REPOSITORY_CODING_KINDS = frozenset(
    {WorkKind.REPOSITORY_CODING, WorkKind.MECHANICAL_EDIT, WorkKind.REWORK}
)
_MECHANICAL_ROLES = frozenset({TaskRole.IMPLEMENTER, TaskRole.MECHANICAL_OPERATOR})


def _is_explicitly_mechanical(request: EffortRequest) -> bool:
    """True only when the task is explicitly mechanical *and* bounded *and* low risk."""
    return (
        request.work_kind is WorkKind.MECHANICAL_EDIT
        and request.difficulty is Difficulty.BOUNDED
        and request.risk_tier is RiskTier.LOW
        and request.role in _MECHANICAL_ROLES
    )


def _escalations(request: EffortRequest) -> tuple[Escalation, ...]:
    """Mandatory lifts to canonical maximum, applied for every provider family."""
    lifts: list[Escalation] = []
    if request.risk_tier is RiskTier.CRITICAL:
        lifts.append(Escalation(REASON_CRITICAL_RISK, ReasoningProfile.MAXIMUM))
    if request.work_kind is WorkKind.SECURITY or request.role is TaskRole.SECURITY_AUDITOR:
        lifts.append(Escalation(REASON_SECURITY_WORK, ReasoningProfile.MAXIMUM))
    if request.work_kind is WorkKind.ARCHITECTURE or request.role is TaskRole.ARCHITECT:
        lifts.append(Escalation(REASON_ARCHITECTURE_WORK, ReasoningProfile.MAXIMUM))
    if request.work_kind is WorkKind.CORRECTNESS_REVIEW or request.role is TaskRole.REVIEWER:
        lifts.append(Escalation(REASON_CORRECTNESS_REVIEW_WORK, ReasoningProfile.MAXIMUM))
    if request.difficulty is Difficulty.COMPLEX:
        lifts.append(Escalation(REASON_COMPLEX_DIFFICULTY, ReasoningProfile.MAXIMUM))
    return tuple(lifts)


def _baseline(request: EffortRequest) -> tuple[ReasoningProfile, str]:
    """The profile before mandatory escalations, never below :data:`ABSOLUTE_FLOOR`."""
    is_claude = request.provider_family is ProviderFamily.CLAUDE
    if _is_explicitly_mechanical(request):
        if is_claude:
            return ReasoningProfile.HIGH, REASON_CLAUDE_MECHANICAL_FLOOR
        return ReasoningProfile.MEDIUM_HIGH, REASON_EXPLICIT_MECHANICAL
    if request.work_kind in _REPOSITORY_CODING_KINDS:
        if is_claude:
            return ReasoningProfile.MAXIMUM, REASON_CLAUDE_REPOSITORY_DEFAULT
        return ReasoningProfile.HIGH, REASON_REPOSITORY_QUALITY_FLOOR
    return ReasoningProfile.HIGH, REASON_GENERAL_QUALITY_FLOOR


def select_reasoning_profile(request: EffortRequest) -> Rationale:
    """Select the canonical profile for ``request`` and explain the selection.

    The result is a pure function of the declared inputs: the baseline is lifted
    by any mandatory escalation, and nothing ever lowers it.
    """
    baseline, baseline_reason = _baseline(request)
    lifts = _escalations(request)
    selected = baseline
    decisive_reason = baseline_reason
    for lift in lifts:
        if lift.profile > selected:
            selected = lift.profile
            decisive_reason = lift.reason
    return Rationale(
        baseline=baseline,
        baseline_reason=baseline_reason,
        escalations=lifts,
        selected=selected,
        decisive_reason=decisive_reason,
    )


# --- provider capability normalization --------------------------------------


class ControlStatus(Enum):
    """What actually happened to the reasoning-effort control on a route.

    ``APPLIED`` is the only status that asserts the route accepted a key
    honoring the canonical request; every other member is an explicit,
    machine-readable refusal to make that claim.
    """

    APPLIED = "applied"
    CAPABILITY_CEILING = "capability_ceiling"
    PROVIDER_DEFAULT = "provider_default"
    UNSUPPORTED = "unsupported"
    #: The route declares keys, but none of their names can be ranked, so no
    #: ordering over them is proven and no key can be attested as applied.
    UNVERIFIABLE = "unverifiable"


class KeyMapping(Enum):
    """How the canonical profile was mapped onto the route's declared keys.

    ``POSITIONAL`` records that declaration order was the only reading
    available; it always accompanies a non-applied ``UNVERIFIABLE`` status and
    never an applied one.
    """

    SEMANTIC = "semantic"
    POSITIONAL = "positional"
    NONE = "none"


_KEY_SEPARATORS = re.compile(r"[^a-z0-9]+")


def _canonical_key(name: str) -> str:
    """Reduce a declared effort-key name to its separator-free comparison form.

    Providers punctuate the same rung differently (``x-high``, ``x_high``,
    ``X High``, ``xhigh``). Comparing the punctuation would rank one spelling
    and set its twin aside as unknown, which is how a route's real ceiling gets
    ignored, so every name is compared on letters and digits alone.
    """
    return _KEY_SEPARATORS.sub("", name.strip().lower())


#: Rank by canonical (separator-free) key name; every spelling of a rung shares
#: the rung's rank, so a stronger declared key can never be missed.
_KEY_ALIAS_RANK: dict[str, int] = {
    "minimal": 10,
    "none": 10,
    "off": 10,
    "low": 20,
    "med": 30,
    "medium": 30,
    "standard": 30,
    "medhigh": 40,
    "mediumhigh": 40,
    "high": 50,
    "extrahigh": 60,
    "veryhigh": 60,
    "xhigh": 60,
    "highest": 70,
    "max": 70,
    "maximum": 70,
    "ultra": 70,
    "ultrahigh": 70,
}

#: The weakest declared key rank that still honors each canonical profile.
#: Canonical maximum needs a ceiling that is at least a real ``high``; a ladder
#: topping out at ``medium`` cannot carry it and must not report ``applied``.
_REQUIRED_KEY_RANK: dict[ReasoningProfile, int] = {
    ReasoningProfile.MEDIUM_HIGH: 40,
    ReasoningProfile.HIGH: 50,
    ReasoningProfile.MAXIMUM: 50,
}

#: Offsets from the route's top key used to *describe* -- never to apply -- the
#: positional reading of a ladder whose key names cannot be ranked at all.
_POSITIONAL_OFFSET: dict[ReasoningProfile, int] = {
    ReasoningProfile.MAXIMUM: 0,
    ReasoningProfile.HIGH: 1,
    ReasoningProfile.MEDIUM_HIGH: 2,
}


@dataclass(frozen=True)
class RouteCapability:
    """What one concrete route declares about its reasoning-effort control.

    ``effort_keys`` holds the route's own key names. Every recognized name is
    ranked semantically and separator-insensitively, so declaration order
    carries no weight; a ladder whose names cannot be ranked at all is reported
    as ``unverifiable`` rather than read positionally, because the route never
    attests that its keys were declared ascending.
    """

    route_id: str
    provider_family: ProviderFamily
    supports_effort_control: bool = True
    effort_keys: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.route_id.strip():
            raise ValueError("route_id must be a non-empty identifier")
        if any(not _canonical_key(key) for key in self.effort_keys):
            raise ValueError(f"{self.route_id}: effort keys must be non-empty names")
        canonical = self.canonical_keys
        # Separator variants of one name are one key: declaring both would make
        # the ladder's ceiling depend on declaration order rather than meaning.
        if len(set(canonical)) != len(canonical):
            raise ValueError(f"{self.route_id}: duplicate effort keys declared")

    @property
    def canonical_keys(self) -> tuple[str, ...]:
        """Declared keys reduced to their separator-free comparison form."""
        return tuple(_canonical_key(key) for key in self.effort_keys)


@dataclass(frozen=True)
class RouteEffort:
    """The canonical profile as resolved against one route's declared keys."""

    route_id: str
    provider_family: ProviderFamily
    requested: ReasoningProfile
    status: ControlStatus
    applied_key: str | None
    mapping: KeyMapping
    detail: str

    @property
    def applied(self) -> bool:
        """True only when the route accepted a key honoring the canonical request."""
        return self.status is ControlStatus.APPLIED

    def as_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "provider_family": self.provider_family.value,
            "requested": self.requested.value,
            "status": self.status.value,
            "applied": self.applied,
            "applied_key": self.applied_key,
            "mapping": self.mapping.value,
            "detail": self.detail,
        }


def _ranked_keys(
    route: RouteCapability,
) -> tuple[list[tuple[int, int, str]], tuple[str, ...]]:
    """Split declared keys into ranked ``(rank, declared_index, key)`` entries.

    Ranking is separator-insensitive: ``x-high``, ``x_high``, ``X High`` and
    ``xhigh`` all canonicalize to one name and therefore to one rank, so a
    stronger declared rung is never set aside as unknown merely because of its
    punctuation.

    One genuinely unrecognized key name must not blind the ranking of its
    recognized neighbours, so an unknown name is set aside and reported rather
    than discarding the whole ladder: ``("low", "medium", "adaptive")`` still
    ranks ``low`` and ``medium``. The second element lists the names set aside.
    """
    ranked: list[tuple[int, int, str]] = []
    ignored: list[str] = []
    declared = zip(route.effort_keys, route.canonical_keys)
    for index, (raw, canonical) in enumerate(declared):
        rank = _KEY_ALIAS_RANK.get(canonical)
        if rank is None:
            ignored.append(raw)
            continue
        ranked.append((rank, index, raw))
    return ranked, tuple(ignored)


def normalize_for_route(profile: ReasoningProfile, route: RouteCapability) -> RouteEffort:
    """Map a canonical profile onto ``route``'s own highest honoring key.

    Canonical maximum resolves to the route's highest declared key -- the
    ``max``/``xhigh``/``high`` the route actually exposes. Separator variants of
    one name (``x-high``, ``x_high``, ``xhigh``) rank identically, so a stronger
    declared rung is never overlooked because of how it was punctuated. Lower
    rungs resolve to the cheapest declared key that still honors them, which may
    be an upgrade but is never a downgrade. When the route's whole ladder sits
    below the request -- including a ladder topping out below a real ``high``
    when canonical maximum is requested -- the result is ``capability_ceiling``
    and ``applied`` is ``False``. When no declared key name can be ranked at all
    the ladder's ordering is unproven, so the result is ``unverifiable`` and
    ``applied`` is ``False``: the positional reading is reported as a candidate
    in the rationale, never as an applied value.
    """
    if not route.supports_effort_control:
        return RouteEffort(
            route_id=route.route_id,
            provider_family=route.provider_family,
            requested=profile,
            status=ControlStatus.UNSUPPORTED,
            applied_key=None,
            mapping=KeyMapping.NONE,
            detail="route declares no reasoning-effort control",
        )
    if not route.effort_keys:
        return RouteEffort(
            route_id=route.route_id,
            provider_family=route.provider_family,
            requested=profile,
            status=ControlStatus.PROVIDER_DEFAULT,
            applied_key=None,
            mapping=KeyMapping.NONE,
            detail="route exposes the control but declares no selectable keys",
        )

    ranked, ignored = _ranked_keys(route)
    if not ranked:
        # Nothing here ranks, so the only ordering available is the declaration
        # order -- a convention the route never attests. Reporting APPLIED from
        # it would claim an effort value that cannot be shown to be the one the
        # route accepts, so the candidate is explained and withheld instead.
        index = max(0, len(route.effort_keys) - 1 - _POSITIONAL_OFFSET[profile])
        # Report the rung offset actually realized after clamping, not the
        # requested one: on a one-key route every profile lands on the ceiling.
        realized = len(route.effort_keys) - 1 - index
        return RouteEffort(
            route_id=route.route_id,
            provider_family=route.provider_family,
            requested=profile,
            status=ControlStatus.UNVERIFIABLE,
            applied_key=None,
            mapping=KeyMapping.POSITIONAL,
            detail=(
                f"no rankable key names; {profile.value} would sit {realized} "
                f"rung(s) below the ceiling at candidate key "
                f"{route.effort_keys[index]!r} only under an assumed "
                f"declared-ascending order the route does not attest, so the "
                f"canonical request was not applied"
            ),
        )

    ceiling = max(ranked)
    required = _REQUIRED_KEY_RANK[profile]
    if profile is ReasoningProfile.MAXIMUM:
        chosen = ceiling
    else:
        honoring = [entry for entry in ranked if entry[0] >= required]
        chosen = min(honoring) if honoring else ceiling
    ignored_note = (
        f"; unrankable key(s) {', '.join(repr(key) for key in ignored)} were ignored"
        if ignored
        else ""
    )
    if chosen[0] < required:
        return RouteEffort(
            route_id=route.route_id,
            provider_family=route.provider_family,
            requested=profile,
            status=ControlStatus.CAPABILITY_CEILING,
            applied_key=ceiling[2],
            mapping=KeyMapping.SEMANTIC,
            detail=(
                f"route ceiling {ceiling[2]!r} sits below {profile.value}; "
                f"the canonical request was not applied{ignored_note}"
            ),
        )
    honored = (
        f"canonical maximum mapped to the route ceiling {chosen[2]!r}"
        if profile is ReasoningProfile.MAXIMUM
        else f"{profile.value} honored by the route key {chosen[2]!r}"
    )
    return RouteEffort(
        route_id=route.route_id,
        provider_family=route.provider_family,
        requested=profile,
        status=ControlStatus.APPLIED,
        applied_key=chosen[2],
        mapping=KeyMapping.SEMANTIC,
        detail=f"{honored}{ignored_note}",
    )


# --- combined decision ------------------------------------------------------


@dataclass(frozen=True)
class ReasoningDecision:
    """A selected canonical profile plus, optionally, its route resolution."""

    request: EffortRequest
    profile: ReasoningProfile
    rationale: Rationale
    route_effort: RouteEffort | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_id": POLICY_SCHEMA_ID,
            "request": self.request.as_dict(),
            "profile": self.profile.value,
            "rationale": self.rationale.as_dict(),
            "route_effort": None if self.route_effort is None else self.route_effort.as_dict(),
        }


def resolve_reasoning_effort(
    request: EffortRequest,
    route: RouteCapability | None = None,
) -> ReasoningDecision:
    """Select a canonical profile and, when a route is given, normalize it.

    A request left at :data:`ProviderFamily.OTHER` adopts the route's family, so
    a Claude route never misses the Claude repository default merely because the
    caller omitted the family. A stated family that contradicts the route is an
    error rather than a silent reinterpretation.
    """
    resolved = request
    if route is not None:
        if request.provider_family is ProviderFamily.OTHER:
            resolved = EffortRequest(
                role=request.role,
                risk_tier=request.risk_tier,
                work_kind=request.work_kind,
                difficulty=request.difficulty,
                provider_family=route.provider_family,
            )
        elif request.provider_family is not route.provider_family:
            raise ValueError(
                f"provider family conflict: request declares "
                f"{request.provider_family.value!r} but route {route.route_id!r} is "
                f"{route.provider_family.value!r}"
            )
    rationale = select_reasoning_profile(resolved)
    route_effort = None if route is None else normalize_for_route(rationale.selected, route)
    return ReasoningDecision(
        request=resolved,
        profile=rationale.selected,
        rationale=rationale,
        route_effort=route_effort,
    )
