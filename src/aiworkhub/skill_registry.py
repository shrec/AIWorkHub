"""Standalone, typed, immutable Repository Skill Registry foundation.

This module is the audited route-successor to V1 for RM-2026-00021. It defines
versioned skill records and provides deterministic validation, digesting,
bounded ranking, and activation/retirement transition predicates over explicit
in-memory inputs only.

Design invariants
-----------------
* Pure and side-effect free: no filesystem, subprocess, network, database,
  task-store, model invocation, or executable payload. Everything operates on
  explicit in-memory inputs.
* Immutable historical versions: the registry indexes records by
  ``(identity, version)`` and by content digest. A version, once registered,
  can never be overwritten, and a digest can never be rebound to a different
  identity. Runtime state (lifecycle, evidence, counters) evolves only through
  derived snapshots whose *content digest never changes*.
* Deterministic: canonical serialization sorts mapping keys and rejects NaN and
  infinity, so identity/version/digest/normalized payload are identical across
  mapping order and platforms.
* Fail-closed: unknown keys, malformed identifiers/versions, unsafe paths,
  invalid lifecycle transitions, bool-as-int counters, non-finite confidence,
  and contradictory triggers raise :class:`SkillRegistryError` with a stable
  ``code``.
* One success never activates: activation requires a configurable minimum of
  independent accepted evidence (always >= 2) from distinct non-secret
  provenance identities and no unresolved negative safety evidence, plus
  explicit manager authority. Workers can only propose.
* Proposals are evidence-free: provenance evidence is appended exclusively
  through ``add_evidence``, bound to a single authenticated authority, so a
  worker can never forge independent acceptance by pre-populating records.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, NoReturn, TypeVar
_IDENTITY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_VERSION_RE = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_ACTOR_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")

_E = TypeVar("_E", bound=Enum)

_REQUIRED_FIELDS = frozenset(
    {
        "identity",
        "version",
        "scope",
        "task_family",
        "path_or_symbol",
        "risk",
        "stage",
        "triggers",
        "confidence",
    }
)
_OPTIONAL_FIELDS = frozenset(
    {
        "applicability",
        "procedure_steps",
        "avoid_rules",
        "preferred_tools",
        "evidence",
        "lifecycle_state",
        "accepted_count",
        "negative_count",
    }
)
_ALLOWED_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS

# Content fields are digested and immutable. Runtime state fields are not part
# of the digest and may evolve through derived snapshots.
_CONTENT_FIELDS = (
    "identity",
    "version",
    "scope",
    "task_family",
    "path_or_symbol",
    "risk",
    "stage",
    "triggers",
    "applicability",
    "procedure_steps",
    "avoid_rules",
    "preferred_tools",
    "confidence",
)

# Fail-closed lifecycle transition table. Only the two real transitions are
# legal: ``proposed -> active`` and ``active -> retired``. ``proposed ->
# retired`` is deliberately rejected (retirement is active-only, matching
# :meth:`SkillRegistry.retire` and :func:`can_retire`), and no self-transition
# is treated as a state change.
_ALLOWED_TRANSITIONS = frozenset(
    {
        ("proposed", "active"),
        ("active", "retired"),
    }
)

class SkillRegistryError(ValueError):
    """A fail-closed skill-registry rejection with a stable reason ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SkillScope(str, Enum):
    """Where a skill is authoritative."""

    REPOSITORY = "repository"
    GLOBAL = "global"


class RiskLevel(str, Enum):
    """Severity tier used by deterministic ranking."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class LifecycleState(str, Enum):
    """Legal lifecycle states of a versioned skill record."""

    PROPOSED = "proposed"
    ACTIVE = "active"
    RETIRED = "retired"


class EvidenceOutcome(str, Enum):
    """Outcome of a single provenance evidence record."""

    ACCEPTED = "accepted"
    NEGATIVE = "negative"


class AuthorityRole(str, Enum):
    """Role of the actor producing an action or evidence record."""

    WORKER = "worker"
    MANAGER = "manager"


_RISK_SEVERITY = {
    RiskLevel.CRITICAL: 4,
    RiskLevel.HIGH: 3,
    RiskLevel.MEDIUM: 2,
    RiskLevel.LOW: 1,
}


def _fail(code: str, message: str) -> NoReturn:
    raise SkillRegistryError(code, message)


@dataclass(frozen=True)
class EvidenceRecord:
    """A single provenance evidence entry bound to a skill version.

    ``actor_id`` is the stable, non-secret provenance identity of the actor who
    produced this evidence. The actor's secret credential is never stored here.
    """

    source: str
    outcome: EvidenceOutcome
    authority: AuthorityRole
    actor_id: str = ""
    resolved: bool = False
    note: str = ""


@dataclass(frozen=True)
class Authority:
    """The actor behind a proposal, evidence, or lifecycle transition.

    ``actor_id`` is a stable, non-secret provenance identity. ``token`` is the
    secret credential used only for the manager gate and never persisted.
    """

    role: AuthorityRole
    actor_id: str = ""
    token: str = ""

    @property
    def is_manager(self) -> bool:
        """True only for an explicit manager authority with a non-empty token."""
        return (
            isinstance(self.role, AuthorityRole)
            and self.role is AuthorityRole.MANAGER
            and isinstance(self.token, str)
            and bool(self.token)
        )


@dataclass(frozen=True)
class SkillRecord:
    """A typed, versioned skill definition plus its runtime state snapshot."""

    identity: str
    version: str
    scope: SkillScope
    task_family: str
    path_or_symbol: str
    risk: RiskLevel
    stage: str
    triggers: tuple[str, ...]
    confidence: float
    applicability: tuple[str, ...] = ()
    procedure_steps: tuple[str, ...] = ()
    avoid_rules: tuple[str, ...] = ()
    preferred_tools: tuple[str, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()
    lifecycle_state: LifecycleState = LifecycleState.PROPOSED
    accepted_count: int = 0
    negative_count: int = 0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SkillRecord":
        """Build a validated, canonical record from a plain mapping."""
        return cls(**normalize(data))


@dataclass(frozen=True)
class TransitionDecision:
    """Result of a lifecycle transition predicate.

    Truthiness mirrors :attr:`allowed`, so ``if can_activate(...)`` behaves as a
    plain boolean guard instead of an always-truthy container.
    """

    allowed: bool
    reason_code: str
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


def _require_nonempty_str(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        _fail("skill_registry.invalid_type", f"{field}: expected a non-empty string")
    stripped = value.strip()
    if not stripped:
        _fail("skill_registry.invalid_value", f"{field}: must not be blank")
    return stripped


def _validate_identity(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        _fail("skill_registry.invalid_identity", "identity must be a string")
    if not _IDENTITY_RE.match(value):
        _fail(
            "skill_registry.invalid_identity",
            f"identity {value!r} is malformed (expected [a-z][a-z0-9_.-]*)",
        )
    return value


def _validate_actor_id(value: Any, code: str) -> str:
    """Validate a stable, non-secret provenance identity (length and shape)."""
    if isinstance(value, bool) or not isinstance(value, str):
        _fail(code, "actor_id must be a string")
    if not value.strip():
        _fail(code, "actor_id must be a non-empty provenance identity")
    if not _ACTOR_ID_RE.match(value):
        _fail(
            code,
            f"actor_id {value!r} is malformed (expected [a-z][a-z0-9_.-]* up to 128 chars)",
        )
    return value


def _validate_version(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        _fail("skill_registry.invalid_version", "version must be a string")
    if not _VERSION_RE.match(value):
        _fail(
            "skill_registry.invalid_version",
            f"version {value!r} is malformed (expected a semver-like value)",
        )
    return value


def _validate_path_or_symbol(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        _fail("skill_registry.unsafe_path", "path_or_symbol must be a string")
    text = value
    if not text or text in (".", ".."):
        _fail("skill_registry.unsafe_path", "path_or_symbol must not be empty or a dot")
    if "\x00" in text:
        _fail("skill_registry.unsafe_path", "path_or_symbol contains a NUL byte")
    if text.startswith(("/", "\\")) or _ABSOLUTE_PATH_RE.match(text):
        _fail("skill_registry.unsafe_path", "path_or_symbol must be relative, not absolute")
    parts = re.split(r"[\\/]+", text)
    if any(part == ".." for part in parts):
        _fail("skill_registry.unsafe_path", "path_or_symbol must not traverse parents ('..')")
    return text


def _validate_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("skill_registry.invalid_confidence", "confidence must be a finite float in [0, 1]")
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        _fail("skill_registry.invalid_confidence", "confidence must be finite (not NaN/inf)")
    if not 0.0 <= number <= 1.0:
        _fail("skill_registry.invalid_confidence", "confidence must be within [0, 1]")
    return number


def _validate_counter(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("skill_registry.invalid_counter", f"{field} must be an int, not bool")
    if value < 0:
        _fail("skill_registry.invalid_counter", f"{field} must be non-negative")
    return value


def _coerce_enum(value: Any, enum_cls: type[_E], field: str, code: str) -> _E:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            _fail(code, f"{field}={value!r} is not a valid {enum_cls.__name__}")
    _fail(code, f"{field} must be a {enum_cls.__name__} or its string value")


def _validate_string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        _fail("skill_registry.invalid_type", f"{field} must be a list/tuple of strings")
    result: list[str] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, str):
            _fail("skill_registry.invalid_type", f"{field} entries must be strings")
        stripped = item.strip()
        if not stripped:
            _fail("skill_registry.invalid_value", f"{field} entries must not be blank")
        result.append(stripped)
    return tuple(result)


def _negation_of(trigger: str) -> str:
    return trigger[1:] if trigger.startswith("!") else "!" + trigger


def _check_contradictions(triggers: tuple[str, ...], avoid_rules: tuple[str, ...]) -> None:
    trigger_set = set(triggers)
    if len(trigger_set) != len(triggers):
        _fail("skill_registry.contradictory_triggers", "duplicate triggers are contradictory")
    overlap = trigger_set & set(avoid_rules)
    if overlap:
        _fail(
            "skill_registry.contradictory_triggers",
            f"trigger {sorted(overlap)[0]!r} also appears in avoid_rules",
        )
    avoid_set = set(avoid_rules)
    for trigger in triggers:
        negation = _negation_of(trigger)
        if negation in trigger_set:
            _fail(
                "skill_registry.contradictory_triggers",
                f"trigger {trigger!r} contradicts {negation!r}",
            )
        if negation in avoid_set:
            _fail(
                "skill_registry.contradictory_triggers",
                f"trigger {trigger!r} contradicts avoid rule {negation!r}",
            )


_EVIDENCE_KEYS = frozenset({"source", "outcome", "authority", "actor_id", "resolved", "note"})


def _validate_evidence_record(record: EvidenceRecord) -> EvidenceRecord:
    if (
        isinstance(record.source, bool)
        or not isinstance(record.source, str)
        or not record.source.strip()
    ):
        _fail("skill_registry.invalid_evidence", "evidence source must be a non-empty string")
    if not isinstance(record.outcome, EvidenceOutcome):
        _fail("skill_registry.invalid_evidence", "evidence outcome must be an EvidenceOutcome")
    if not isinstance(record.authority, AuthorityRole):
        _fail("skill_registry.invalid_evidence", "evidence authority must be an AuthorityRole")
    _validate_actor_id(record.actor_id, "skill_registry.invalid_evidence")
    if not isinstance(record.resolved, bool):
        _fail("skill_registry.invalid_evidence", "evidence resolved must be a bool")
    if not isinstance(record.note, str):
        _fail("skill_registry.invalid_evidence", "evidence note must be a string")
    return record


def _evidence_from_mapping(data: Mapping[str, Any]) -> EvidenceRecord:
    unknown = [key for key in data if key not in _EVIDENCE_KEYS]
    if unknown:
        _fail("skill_registry.unknown_key", f"unknown evidence key: {sorted(unknown)[0]}")
    source = data.get("source")
    if isinstance(source, bool) or not isinstance(source, str) or not source.strip():
        _fail("skill_registry.invalid_evidence", "evidence source must be a non-empty string")
    outcome = _coerce_enum(
        data.get("outcome"), EvidenceOutcome, "evidence.outcome", "skill_registry.invalid_evidence"
    )
    authority = _coerce_enum(
        data.get("authority"),
        AuthorityRole,
        "evidence.authority",
        "skill_registry.invalid_evidence",
    )
    actor_id = _validate_actor_id(data.get("actor_id", ""), "skill_registry.invalid_evidence")
    resolved = data.get("resolved", False)
    if not isinstance(resolved, bool):
        _fail("skill_registry.invalid_evidence", "evidence resolved must be a bool")
    note = data.get("note", "")
    if not isinstance(note, str):
        _fail("skill_registry.invalid_evidence", "evidence note must be a string")
    return EvidenceRecord(
        source=source.strip(),
        outcome=outcome,
        authority=authority,
        actor_id=actor_id,
        resolved=resolved,
        note=note,
    )


def _coerce_evidence_item(value: Any) -> EvidenceRecord:
    if isinstance(value, EvidenceRecord):
        return _validate_evidence_record(value)
    if isinstance(value, Mapping):
        return _evidence_from_mapping(value)
    _fail("skill_registry.invalid_type", "evidence must be an EvidenceRecord or mapping")


def _coerce_evidence(value: Any, field: str = "evidence") -> tuple[EvidenceRecord, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        _fail("skill_registry.invalid_type", f"{field} must be a list/tuple of evidence")
    return tuple(_coerce_evidence_item(item) for item in value)


def _bind_evidence(value: Any, authority: Authority) -> EvidenceRecord:
    """Parse submitted evidence and bind its provenance to ``authority``.

    The record's ``authority`` role and ``actor_id`` provenance identity are
    always taken from the authenticated ``authority`` argument; any
    caller-supplied values are overridden so one actor cannot forge independent
    evidence by choosing multiple free-text source labels.

    ``resolved`` is always forced to ``False`` for a newly appended evidence
    record, for both outcomes. Resolution is exclusively the manager-only
    :meth:`SkillRegistry.mark_negative_resolved` transition, so neither a worker
    nor a manager ``add_evidence`` append may pre-resolve a negative (or any
    other) evidence entry.
    """
    if isinstance(value, EvidenceRecord):
        return _validate_evidence_record(
            replace(
                value,
                authority=authority.role,
                actor_id=authority.actor_id,
                resolved=False,
            )
        )
    if isinstance(value, Mapping):
        unknown = [key for key in value if key not in _EVIDENCE_KEYS]
        if unknown:
            _fail("skill_registry.unknown_key", f"unknown evidence key: {sorted(unknown)[0]}")
        bound = dict(value)
        bound["authority"] = authority.role
        bound["actor_id"] = authority.actor_id
        bound["resolved"] = False
        return _evidence_from_mapping(bound)
    _fail("skill_registry.invalid_type", "evidence must be an EvidenceRecord or mapping")


def normalize(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a mapping and return its canonical, normalized record dict."""
    if not isinstance(data, Mapping):
        _fail("skill_registry.invalid_type", "skill data must be a mapping")

    unknown = [key for key in data if key not in _ALLOWED_FIELDS]
    if unknown:
        _fail("skill_registry.unknown_key", f"unknown skill field: {sorted(unknown)[0]}")
    missing = [key for key in _REQUIRED_FIELDS if key not in data]
    if missing:
        _fail("skill_registry.missing_field", f"missing required field: {sorted(missing)[0]}")

    identity = _validate_identity(data["identity"])
    version = _validate_version(data["version"])
    scope = _coerce_enum(data["scope"], SkillScope, "scope", "skill_registry.invalid_scope")
    task_family = _require_nonempty_str(data["task_family"], "task_family")
    path_or_symbol = _validate_path_or_symbol(data["path_or_symbol"])
    risk = _coerce_enum(data["risk"], RiskLevel, "risk", "skill_registry.invalid_risk")
    stage = _require_nonempty_str(data["stage"], "stage")
    triggers = _validate_string_tuple(data["triggers"], "triggers")
    confidence = _validate_confidence(data["confidence"])

    applicability = _validate_string_tuple(data.get("applicability", ()), "applicability")
    procedure_steps = _validate_string_tuple(data.get("procedure_steps", ()), "procedure_steps")
    avoid_rules = _validate_string_tuple(data.get("avoid_rules", ()), "avoid_rules")
    preferred_tools = _validate_string_tuple(data.get("preferred_tools", ()), "preferred_tools")

    _check_contradictions(triggers, avoid_rules)

    evidence = _coerce_evidence(data.get("evidence", ()))

    lifecycle_state = _coerce_enum(
        data.get("lifecycle_state", "proposed"),
        LifecycleState,
        "lifecycle_state",
        "skill_registry.invalid_lifecycle",
    )

    expected_accepted = sum(1 for item in evidence if item.outcome is EvidenceOutcome.ACCEPTED)
    expected_negative = sum(1 for item in evidence if item.outcome is EvidenceOutcome.NEGATIVE)

    if "accepted_count" in data:
        accepted_count = _validate_counter(data["accepted_count"], "accepted_count")
        if accepted_count != expected_accepted:
            _fail("skill_registry.counter_mismatch", "accepted_count does not match evidence")
    else:
        accepted_count = expected_accepted

    if "negative_count" in data:
        negative_count = _validate_counter(data["negative_count"], "negative_count")
        if negative_count != expected_negative:
            _fail("skill_registry.counter_mismatch", "negative_count does not match evidence")
    else:
        negative_count = expected_negative

    return {
        "identity": identity,
        "version": version,
        "scope": scope,
        "task_family": task_family,
        "path_or_symbol": path_or_symbol,
        "risk": risk,
        "stage": stage,
        "triggers": triggers,
        "confidence": confidence,
        "applicability": applicability,
        "procedure_steps": procedure_steps,
        "avoid_rules": avoid_rules,
        "preferred_tools": preferred_tools,
        "evidence": evidence,
        "lifecycle_state": lifecycle_state,
        "accepted_count": accepted_count,
        "negative_count": negative_count,
    }


def validate_record(record: SkillRecord) -> SkillRecord:
    """Re-validate an existing :class:`SkillRecord` and return a canonical copy.

    Raw field values are passed through to :func:`normalize` unchanged so the
    dedicated validators (``_validate_string_tuple`` for tuple fields and
    ``_coerce_evidence`` for evidence) own type validation. Coercing a crafted
    malformed container here (for example ``list(record.triggers)`` on a
    ``str``) would split it into single characters and misclassify the failure
    as ``contradictory_triggers`` instead of ``invalid_type``.
    """
    if not isinstance(record, SkillRecord):
        _fail("skill_registry.invalid_type", "expected a SkillRecord")
    data = {
        "identity": record.identity,
        "version": record.version,
        "scope": record.scope,
        "task_family": record.task_family,
        "path_or_symbol": record.path_or_symbol,
        "risk": record.risk,
        "stage": record.stage,
        "triggers": record.triggers,
        "confidence": record.confidence,
        "applicability": record.applicability,
        "procedure_steps": record.procedure_steps,
        "avoid_rules": record.avoid_rules,
        "preferred_tools": record.preferred_tools,
        "evidence": record.evidence,
        "lifecycle_state": record.lifecycle_state,
        "accepted_count": record.accepted_count,
        "negative_count": record.negative_count,
    }
    return SkillRecord(**normalize(data))


def _content_payload(record: SkillRecord) -> dict[str, Any]:
    """Build the canonical content payload from the authoritative field list.

    ``_CONTENT_FIELDS`` is the single source of truth for which fields enter the
    digest, so content fields cannot drift from the digest payload.
    """
    return {field: _canonical(getattr(record, field)) for field in _CONTENT_FIELDS}


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            _fail("skill_registry.invalid_confidence", "cannot serialize non-finite float")
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _canonical(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_canonical(item) for item in sorted(value, key=lambda item: canonical_json(item))]
    if isinstance(value, SkillRecord):
        return _canonical(_content_payload(value))
    if isinstance(value, EvidenceRecord):
        return _canonical(
            {
                "source": value.source,
                "outcome": value.outcome.value,
                "authority": value.authority.value,
                "actor_id": _validate_actor_id(value.actor_id, "skill_registry.invalid_evidence"),
                "resolved": value.resolved,
                "note": value.note,
            }
        )
    if isinstance(
        value, (SkillSelectionReceipt, SkillRuntimePacket, SkillRuntimePacketRow)
    ):
        return _canonical(value.as_mapping())
    if isinstance(value, SkillSelection):
        return _canonical(
            {
                "identity": value.identity,
                "version": value.version,
                "digest": value.digest,
                "reasons": list(value.reasons),
            }
        )
    _fail("skill_registry.invalid_type", f"cannot canonicalize {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return a deterministic, order-independent JSON string for ``value``."""
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_payload(record: SkillRecord) -> bytes:
    """Return the canonical, digestable content bytes of a validated record."""
    record = validate_record(record)
    return canonical_json(_content_payload(record)).encode("utf-8")


def skill_digest(record: SkillRecord) -> str:
    """Return the deterministic SHA-256 content digest of a record."""
    return hashlib.sha256(canonical_payload(record)).hexdigest()


def independent_accepted_evidence_count(record: SkillRecord) -> int:
    """Count distinct non-secret provenance identities with accepted evidence."""
    record = validate_record(record)
    return len(
        {item.actor_id for item in record.evidence if item.outcome is EvidenceOutcome.ACCEPTED}
    )


def unresolved_negative_evidence(record: SkillRecord) -> tuple[EvidenceRecord, ...]:
    """Return unresolved negative safety evidence, if any."""
    record = validate_record(record)
    return tuple(
        item
        for item in record.evidence
        if item.outcome is EvidenceOutcome.NEGATIVE and not item.resolved
    )


def transition_allowed(current: LifecycleState, target: LifecycleState) -> bool:
    """Return whether a raw lifecycle transition is legal."""
    current_state = _coerce_enum(
        current, LifecycleState, "current", "skill_registry.invalid_lifecycle"
    )
    target_state = _coerce_enum(
        target, LifecycleState, "target", "skill_registry.invalid_lifecycle"
    )
    return (current_state.value, target_state.value) in _ALLOWED_TRANSITIONS


def _validate_min_accepted_evidence(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("skill_registry.invalid_type", "min_accepted_evidence must be an int")
    if value < 2:
        _fail("skill_registry.invalid_value", "min_accepted_evidence must be at least 2")
    return value


def _deny(code: str, reason: str) -> TransitionDecision:
    return TransitionDecision(allowed=False, reason_code=code, reason=reason)


def _allow() -> TransitionDecision:
    return TransitionDecision(allowed=True, reason_code="skill_registry.ok", reason="")


def can_activate(record: SkillRecord, min_accepted_evidence: int) -> TransitionDecision:
    """Predicate for proposed -> active, gated by evidence and safety."""
    record = validate_record(record)
    min_accepted_evidence = _validate_min_accepted_evidence(min_accepted_evidence)
    if record.lifecycle_state is not LifecycleState.PROPOSED:
        return _deny(
            "skill_registry.invalid_transition",
            f"cannot activate from {record.lifecycle_state.value}",
        )
    if independent_accepted_evidence_count(record) < min_accepted_evidence:
        return _deny(
            "skill_registry.insufficient_evidence",
            "insufficient independent accepted evidence",
        )
    negative = unresolved_negative_evidence(record)
    if negative:
        sources = sorted({item.actor_id for item in negative})
        return _deny(
            "skill_registry.unresolved_negative_evidence",
            f"unresolved negative evidence from {sources[0]!r}",
        )
    return _allow()


def can_retire(record: SkillRecord) -> TransitionDecision:
    """Predicate for active -> retired; only active skills may retire."""
    record = validate_record(record)
    if record.lifecycle_state is not LifecycleState.ACTIVE:
        return _deny(
            "skill_registry.invalid_transition",
            f"cannot retire from {record.lifecycle_state.value}",
        )
    return _allow()


def can_promote(record: SkillRecord) -> TransitionDecision:
    """Predicate for repository -> global promotion; only active repository skills."""
    record = validate_record(record)
    if record.scope is not SkillScope.REPOSITORY:
        return _deny(
            "skill_registry.invalid_scope",
            "only repository-scoped skills can be promoted",
        )
    if record.lifecycle_state is not LifecycleState.ACTIVE:
        return _deny(
            "skill_registry.invalid_transition",
            f"cannot promote a {record.lifecycle_state.value} skill",
        )
    return _allow()


def _risk_severity(risk: Any) -> int:
    """Return the numeric severity of a risk tier, coercing string values."""
    if not isinstance(risk, RiskLevel):
        risk = _coerce_enum(risk, RiskLevel, "risk", "skill_registry.invalid_risk")
    return _RISK_SEVERITY[risk]


def _rank_key(record: SkillRecord) -> tuple[Any, ...]:
    return (
        record.task_family,
        record.path_or_symbol,
        -_risk_severity(record.risk),
        record.stage,
        -independent_accepted_evidence_count(record),
        record.identity,
        record.version,
    )


# Hard ceiling and default for bounded ranking. ``rank`` is always bounded to
# at most this many results; ``limit=None`` means this value and any larger
# requested limit is deterministically clamped to it.
MAX_RANK_LIMIT = 1000

# Hard ceiling and default for bounded task-aware selection. ``select`` requires
# a positive int (bools fail closed) and clamps any larger request to this cap.
MAX_SELECT_LIMIT = 32
DEFAULT_SELECT_LIMIT = 8
SELECT_WILDCARD = "*"
_SELECT_REASON_LIMIT = 8
_SELECT_CONTEXT_REQUIRED = ("task_family", "path_or_symbol", "risk", "stage")
_SELECT_CONTEXT_ALLOWED = frozenset((*_SELECT_CONTEXT_REQUIRED, "triggers", "applicability"))
_SELECT_REASON_FIELDS = (*_SELECT_CONTEXT_REQUIRED, "triggers", "applicability")
_SELECT_REASON_HEAD = "lifecycle:active"
_SELECT_REASON_KINDS = {
    "task_family": frozenset({"exact", "wildcard"}),
    "path_or_symbol": frozenset({"exact", "wildcard"}),
    "risk": frozenset({"exact", "wildcard"}),
    "stage": frozenset({"exact", "wildcard"}),
    "triggers": frozenset({"exact", "wildcard", "unconstrained"}),
    "applicability": frozenset({"exact", "wildcard", "unconstrained"}),
}

RUNTIME_PACKET_VERSION = "1.0.0"
MAX_PACKET_SELECTED = 32
MAX_PACKET_LIST_ITEMS = 16
MAX_PACKET_STRING_BYTES = 256
MAX_PACKET_BYTES = 8192
_PACKET_ROW_KEYS = frozenset(
    {
        "identity",
        "version",
        "digest",
        "reasons",
        "applicability",
        "procedure_steps",
        "avoid_rules",
        "preferred_tools",
    }
)
_PACKET_KEYS = frozenset({"version", "skills"})
_CONTROL_CHAR_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u00ad\u061c\u200b\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)
_SECRET_PROVIDER_PREFIXES: tuple[str, ...] = ("aws", "openai", "github")
_SECRET_KEY_FAMILIES: tuple[tuple[str, ...], ...] = (
    ("password",),
    ("passwd",),
    ("pwd",),
    ("secret",),
    ("token",),
    ("authorization",),
    ("credential",),
    ("bearer",),
    ("key",),
    ("private",),
    ("api", "key"),
    ("access", "key"),
    ("access", "id"),
    ("access", "key", "id"),
    ("private", "key"),
    ("secret", "key"),
    ("secret", "access", "key"),
    ("auth", "key"),
    ("auth", "token"),
    ("bearer", "token"),
    ("client", "secret"),
    ("api", "secret"),
    ("api", "token"),
    ("access", "token"),
    ("token", "id"),
    ("token", "value"),
    ("password", "hash"),
    ("client", "credential"),
    ("credential", "id"),
    ("credential", "key"),
    ("credential", "token"),
    ("credential", "secret"),
    ("credential", "hash"),
)


def _secret_family_catalog() -> tuple[frozenset[str], frozenset[tuple[str, ...]], frozenset[str]]:
    fused: set[str] = set()
    sequences: set[tuple[str, ...]] = set()
    atoms: set[str] = set(_SECRET_PROVIDER_PREFIXES)
    for family in _SECRET_KEY_FAMILIES:
        sequences.add(family)
        fused.add("".join(family))
        atoms.update(family)
        for prefix in _SECRET_PROVIDER_PREFIXES:
            sequences.add((prefix, *family))
            fused.add(f"{prefix}{''.join(family)}")
    return frozenset(fused), frozenset(sequences), frozenset(atoms)


_SECRET_FAMILY_FORMS, _SECRET_FAMILY_SEQUENCES, _SECRET_FAMILY_ATOMS = _secret_family_catalog()
_KEY_SEP_RE = re.compile(r"[._-]+")
_CAMEL_SPLIT_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_MAX_ASSIGNMENT_KEY_CHARS = 128
_BEARER_RE = re.compile(r"(?i)\b(?:authorization\s*:|bearer\s*(?::\s*|=\s*|\s+)\S+)")
_PRIVATE_KEY_RE = re.compile(r"(?i)-----BEGIN[ A-Z]*PRIVATE KEY(?: BLOCK)?-----")
_INSTRUCTION_ABS_PATH_RE = re.compile(
    r"(?i)(?:file:[\\/]+|(?<![A-Za-z0-9._~-])/[^\s]|"
    r"[A-Za-z]:[\\/]|\\\\|(?<![A-Za-z0-9])//[.?A-Za-z]|"
    r"(?<![A-Za-z0-9._~-])\\[^\s])"
)

def rank(candidates: Iterable[SkillRecord], limit: int | None = None) -> list[SkillRecord]:
    """Return a deterministic, bounded ranking of candidate records.

    Ranking sorts purely on the explicit content/runtime keys in
    :func:`_rank_key` (task family, path/symbol, risk, stage, evidence count)
    with a stable lexical tie-break, and is always bounded to at most
    ``MAX_RANK_LIMIT`` results.

    ``limit`` semantics (fail-closed then clamp):

    * ``None`` -> the default bound ``MAX_RANK_LIMIT``.
    * ``bool`` or non-``int`` -> :class:`SkillRegistryError` ``invalid_type``.
    * negative -> :class:`SkillRegistryError` ``invalid_value``.
    * greater than ``MAX_RANK_LIMIT`` -> clamped to ``MAX_RANK_LIMIT``.
    """
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Iterable):
        _fail("skill_registry.invalid_type", "candidates must be an iterable of SkillRecord")
    if limit is None:
        limit = MAX_RANK_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        _fail("skill_registry.invalid_type", "limit must be an int or None")
    if limit < 0:
        _fail("skill_registry.invalid_value", "limit must be non-negative")
    if limit > MAX_RANK_LIMIT:
        limit = MAX_RANK_LIMIT

    def _validated() -> Iterator[SkillRecord]:
        for record in candidates:
            if not isinstance(record, SkillRecord):
                _fail("skill_registry.invalid_type", "candidates must contain only SkillRecord")
            yield validate_record(record)

    if limit == 0:
        # Still validate every candidate even though nothing is retained.
        for _ in _validated():
            pass
        return []
    return heapq.nsmallest(limit, _validated(), key=_rank_key)


@dataclass(frozen=True)
class SkillSelection:
    """Compact selected-skill row: identity, version, digest, reason tokens."""

    identity: str
    version: str
    digest: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SkillSelectionReceipt:
    """Bounded read-only receipt. Never carries procedure bodies or secrets."""

    selected: tuple[SkillSelection, ...] = ()
    context: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    context_seal: str = ""

    def as_mapping(self) -> dict[str, Any]:
        return {
            "selected": [
                {
                    "identity": item.identity,
                    "version": item.version,
                    "digest": item.digest,
                    "reasons": list(item.reasons),
                }
                for item in self.selected
            ],
            "context": _receipt_context_as_mapping(self.context),
            "context_seal": self.context_seal,
        }


@dataclass(frozen=True)
class SkillRuntimePacketRow:
    """Bounded runtime instruction row. No evidence, counters, or host paths."""

    identity: str
    version: str
    digest: str
    reasons: tuple[str, ...]
    applicability: tuple[str, ...]
    procedure_steps: tuple[str, ...]
    avoid_rules: tuple[str, ...]
    preferred_tools: tuple[str, ...]

    def as_mapping(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "version": self.version,
            "digest": self.digest,
            "reasons": list(self.reasons),
            "applicability": list(self.applicability),
            "procedure_steps": list(self.procedure_steps),
            "avoid_rules": list(self.avoid_rules),
            "preferred_tools": list(self.preferred_tools),
        }


@dataclass(frozen=True)
class SkillRuntimePacket:
    """Versioned, bounded runtime instruction packet for later prompt injection."""

    version: str = RUNTIME_PACKET_VERSION
    skills: tuple[SkillRuntimePacketRow, ...] = ()

    def as_mapping(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "skills": [row.as_mapping() for row in self.skills],
        }


def _normalize_select_context(context: Any) -> dict[str, Any]:
    if not isinstance(context, Mapping):
        _fail("skill_registry.invalid_type", "selection context must be a mapping")
    unknown = [key for key in context if key not in _SELECT_CONTEXT_ALLOWED]
    if unknown:
        _fail("skill_registry.unknown_key", f"unknown selection field: {sorted(unknown)[0]}")
    missing = [key for key in _SELECT_CONTEXT_REQUIRED if key not in context]
    if missing:
        _fail("skill_registry.missing_field", f"missing required field: {sorted(missing)[0]}")
    task_family = _require_nonempty_str(context["task_family"], "task_family")
    raw_path = context["path_or_symbol"]
    path_or_symbol = (
        SELECT_WILDCARD if raw_path == SELECT_WILDCARD else _validate_path_or_symbol(raw_path)
    )
    raw_risk = context["risk"]
    risk: Any
    if raw_risk == SELECT_WILDCARD:
        risk = SELECT_WILDCARD
    else:
        risk = _coerce_enum(raw_risk, RiskLevel, "risk", "skill_registry.invalid_risk")
    stage = _require_nonempty_str(context["stage"], "stage")
    triggers = _validate_string_tuple(context.get("triggers", ()), "triggers")
    applicability = _validate_string_tuple(context.get("applicability", ()), "applicability")
    return {
        "task_family": task_family,
        "path_or_symbol": path_or_symbol,
        "risk": risk,
        "stage": stage,
        "triggers": triggers,
        "applicability": applicability,
    }


def _canonical_select_context(normalized: Mapping[str, Any]) -> dict[str, Any]:
    risk = normalized["risk"]
    return {
        "task_family": normalized["task_family"],
        "path_or_symbol": normalized["path_or_symbol"],
        "risk": risk.value if isinstance(risk, RiskLevel) else risk,
        "stage": normalized["stage"],
        "triggers": tuple(normalized["triggers"]),
        "applicability": tuple(normalized["applicability"]),
    }


def _receipt_context_as_mapping(context: Any) -> dict[str, Any]:
    if not isinstance(context, Mapping):
        _fail("skill_registry.invalid_type", "receipt context must be a mapping")
    if not context:
        return {}
    payload: dict[str, Any] = {}
    for key, value in context.items():
        if isinstance(value, tuple):
            payload[key] = list(value)
        elif isinstance(value, RiskLevel):
            payload[key] = value.value
        else:
            payload[key] = value
    return payload


def _bind_receipt_context(context: Any, *, require: bool) -> dict[str, Any]:
    if not isinstance(context, Mapping):
        _fail("skill_registry.invalid_type", "receipt context must be a mapping")
    if not context:
        if require:
            _fail("skill_registry.missing_field", "missing required field: context")
        return {}
    return _normalize_select_context(context)


def _receipt_context_seal(
    context: Any, selected: tuple[SkillSelection, ...] = ()
) -> str:
    payload = {
        "context": _receipt_context_as_mapping(context),
        "selected": [
            {
                "identity": item.identity,
                "version": item.version,
                "digest": item.digest,
            }
            for item in selected
        ],
    }
    if not payload["context"] and not payload["selected"]:
        return ""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _verify_receipt_context_seal(receipt: SkillSelectionReceipt) -> None:
    seal = receipt.context_seal
    if isinstance(seal, bool) or not isinstance(seal, str):
        _fail("skill_registry.invalid_type", "receipt context_seal must be a string")
    expected = _receipt_context_seal(receipt.context, receipt.selected)
    if seal != expected:
        _fail("skill_registry.invalid_value", "receipt context seal does not match selection")


def _scalar_match(pattern: str, value: str) -> str | None:
    if pattern == SELECT_WILDCARD or value == SELECT_WILDCARD:
        return "wildcard"
    if pattern == value:
        return "exact"
    return None


def _path_match(pattern: str, value: str) -> str | None:
    kind = _scalar_match(pattern, value)
    if kind is not None:
        return kind
    if pattern.endswith("/*"):
        prefix = pattern[:-2]
        if prefix and (value == prefix or value.startswith(prefix + "/")):
            return "wildcard"
    return None


def _tuple_match(patterns: tuple[str, ...], values: tuple[str, ...]) -> str | None:
    if not patterns:
        return "unconstrained"
    if SELECT_WILDCARD in patterns or SELECT_WILDCARD in values:
        return "wildcard"
    if not values:
        return None
    pattern_set = set(patterns)
    if any(item in pattern_set for item in values):
        return "exact"
    return None


def _selection_reasons(record: SkillRecord, context: Mapping[str, Any]) -> tuple[str, ...] | None:
    checks = (
        ("task_family", _scalar_match(record.task_family, context["task_family"])),
        ("path_or_symbol", _path_match(record.path_or_symbol, context["path_or_symbol"])),
        (
            "risk",
            "wildcard"
            if context["risk"] == SELECT_WILDCARD
            else ("exact" if record.risk == context["risk"] else None),
        ),
        ("stage", _scalar_match(record.stage, context["stage"])),
        ("triggers", _tuple_match(record.triggers, context["triggers"])),
        ("applicability", _tuple_match(record.applicability, context["applicability"])),
    )
    reasons: list[str] = ["lifecycle:active"]
    for field, kind in checks:
        if kind is None:
            return None
        reasons.append(f"{field}:{kind}")
    return tuple(reasons[:_SELECT_REASON_LIMIT])


def _validate_select_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        _fail("skill_registry.invalid_type", "limit must be a positive int")
    if limit < 1:
        _fail("skill_registry.invalid_value", "limit must be a positive int")
    if limit > MAX_SELECT_LIMIT:
        return MAX_SELECT_LIMIT
    return limit


def select(
    candidates: Iterable[SkillRecord],
    context: Mapping[str, Any],
    limit: int = DEFAULT_SELECT_LIMIT,
) -> SkillSelectionReceipt:
    """Return a deterministic, bounded, read-only ACTIVE skill selection receipt.

    Only ``LifecycleState.ACTIVE`` records are eligible. Matching is exact after
    normalization, plus the explicit wildcard ``*`` (and a trailing ``/*``
    path prefix). Substring and fuzzy matching are not used. Ties use
    :func:`_rank_key`. Candidate input order cannot change selected identities
    or receipt ordering. ``limit`` must be a positive ``int`` and is clamped to
    ``MAX_SELECT_LIMIT``. Zero matches yield an empty receipt. This function
    never mutates records, registry state, files, or network state.
    """
    limit = _validate_select_limit(limit)
    normalized = _normalize_select_context(context)
    matched: list[tuple[tuple[Any, ...], SkillSelection]] = []
    for record in _index_packet_candidates(candidates).values():
        if record.lifecycle_state is not LifecycleState.ACTIVE:
            continue
        reasons = _selection_reasons(record, normalized)
        if reasons is None:
            continue
        matched.append(
            (
                _rank_key(record),
                SkillSelection(
                    identity=record.identity,
                    version=record.version,
                    digest=skill_digest(record),
                    reasons=reasons,
                ),
            )
        )
    matched.sort(key=lambda item: item[0])
    selected = tuple(item[1] for item in matched[:limit])
    bound_context = _canonical_select_context(normalized)
    return SkillSelectionReceipt(
        selected=selected,
        context=MappingProxyType(bound_context),
        context_seal=_receipt_context_seal(bound_context, selected),
    )


def _validate_positive_limit(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("skill_registry.invalid_type", f"{field} must be a positive int")
    if value < 1:
        _fail("skill_registry.invalid_value", f"{field} must be a positive int")
    return value


_ASSIGNMENT_DELIMITERS = frozenset("=:\uff1a\uff1d")
_ASSIGNMENT_LINE_BREAKS = frozenset("\n\r\v\f\u0085\u2028\u2029")


def _is_assignment_padding(char: str) -> bool:
    return char.isspace() and char not in _ASSIGNMENT_LINE_BREAKS


def _is_assignment_key_char(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char in "._-")


def _assignment_keys(text: str) -> Iterator[str]:
    length = len(text)
    index = 0
    while index < length:
        if text[index] not in _ASSIGNMENT_DELIMITERS:
            index += 1
            continue
        value_at = index + 1
        while value_at < length and _is_assignment_padding(text[value_at]):
            value_at += 1
        if value_at >= length or text[value_at].isspace():
            index += 1
            continue
        cursor = index - 1
        while cursor >= 0 and _is_assignment_padding(text[cursor]):
            cursor -= 1
        if cursor >= 0 and text[cursor] in "'\"":
            cursor -= 1
        end = cursor + 1
        while cursor >= 0 and _is_assignment_key_char(text[cursor]):
            cursor -= 1
        start = cursor + 1
        key_len = end - start
        if 1 <= key_len <= _MAX_ASSIGNMENT_KEY_CHARS:
            first = text[start]
            if first.isascii() and (first.isalpha() or first == "_"):
                yield text[start:end]
        index += 1


def _assignment_key_tokens(key: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for chunk in _KEY_SEP_RE.split(key):
        if not chunk:
            continue
        parts = _CAMEL_SPLIT_RE.findall(chunk)
        if not parts:
            tokens.append(chunk.lower())
            continue
        tokens.extend(part.lower() for part in parts)
    return tuple(tokens)


def _canonical_family_token(token: str) -> str:
    if token in _SECRET_FAMILY_ATOMS:
        return token
    if token.endswith("s"):
        stem = token[:-1]
        if stem in _SECRET_FAMILY_ATOMS:
            return stem
    return token


def _token_is_secret_bearing(token: str) -> bool:
    if token in _SECRET_FAMILY_FORMS:
        return True
    if not token.endswith("s"):
        return False
    return token[:-1] in _SECRET_FAMILY_FORMS


def _assignment_tokens_are_secret(tokens: tuple[str, ...]) -> bool:
    if any(_token_is_secret_bearing(token) for token in tokens):
        return True
    canonical = tuple(_canonical_family_token(token) for token in tokens)
    length = len(canonical)
    for start in range(length):
        for end in range(start + 1, length + 1):
            if canonical[start:end] in _SECRET_FAMILY_SEQUENCES:
                return True
    return False


def _has_secret_assignment(text: str) -> bool:
    for key in _assignment_keys(text):
        if _assignment_tokens_are_secret(_assignment_key_tokens(key)):
            return True
    return False


def _reject_instruction_string(text: str, field: str) -> None:
    if _CONTROL_CHAR_RE.search(text):
        _fail("skill_registry.invalid_value", f"{field} contains a control character")
    if _PRIVATE_KEY_RE.search(text) or _BEARER_RE.search(text) or _has_secret_assignment(text):
        _fail("skill_registry.secret_rejected", f"{field} contains credential material")
    if _INSTRUCTION_ABS_PATH_RE.search(text):
        _fail("skill_registry.unsafe_path", f"{field} contains an absolute host path")


def _bounded_packet_strings(
    values: tuple[str, ...],
    field: str,
    max_list_items: int,
    max_string_bytes: int,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        _fail("skill_registry.invalid_type", f"{field} must be a tuple of strings")
    if len(values) > max_list_items:
        _fail("skill_registry.packet_limit", f"{field} exceeds max list items")
    for item in values:
        if isinstance(item, bool) or not isinstance(item, str):
            _fail("skill_registry.invalid_type", f"{field} entries must be strings")
        if len(item.encode("utf-8")) > max_string_bytes:
            _fail("skill_registry.packet_limit", f"{field} entry exceeds max string bytes")
        _reject_instruction_string(item, field)
    return values


def _bounded_packet_scalar(value: str, field: str, max_string_bytes: int) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        _fail("skill_registry.invalid_type", f"{field} must be a string")
    if len(value.encode("utf-8")) > max_string_bytes:
        _fail("skill_registry.packet_limit", f"{field} exceeds max string bytes")
    _reject_instruction_string(value, field)
    return value


def _validate_packet_reasons(reasons: tuple[str, ...]) -> tuple[str, ...]:
    expected = 1 + len(_SELECT_REASON_FIELDS)
    if len(reasons) != expected:
        _fail("skill_registry.invalid_value", "reasons violate selector grammar")
    if reasons[0] != _SELECT_REASON_HEAD:
        _fail("skill_registry.invalid_value", "reasons violate selector grammar")
    seen: set[str] = set()
    for index, token in enumerate(reasons[1:]):
        field, sep, kind = token.partition(":")
        allowed = _SELECT_REASON_KINDS.get(field)
        if sep != ":" or not field or ":" in kind or allowed is None or kind not in allowed:
            _fail("skill_registry.invalid_value", "reasons violate selector grammar")
        if field in seen:
            _fail("skill_registry.invalid_value", "reasons violate selector grammar")
        if field != _SELECT_REASON_FIELDS[index]:
            _fail("skill_registry.invalid_value", "reasons violate selector grammar")
        seen.add(field)
    return reasons


def _bind_canonical_packet_reasons(
    reasons: tuple[str, ...], record: SkillRecord, context: Mapping[str, Any]
) -> tuple[str, ...]:
    derived = _selection_reasons(record, context)
    if derived is None or reasons != derived:
        _fail("skill_registry.invalid_value", "reasons do not match selected record")
    return derived


def _index_packet_candidates(candidates: Iterable[SkillRecord]) -> dict[tuple[str, str], SkillRecord]:
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Iterable):
        _fail("skill_registry.invalid_type", "candidates must be an iterable of SkillRecord")
    index: dict[tuple[str, str], SkillRecord] = {}
    for record in candidates:
        if not isinstance(record, SkillRecord):
            _fail("skill_registry.invalid_type", "candidates must contain only SkillRecord")
        record = validate_record(record)
        key = (record.identity, record.version)
        if key in index:
            _fail(
                "skill_registry.duplicate_selection",
                f"duplicate candidate {record.identity!r}@{record.version!r}",
            )
        index[key] = record
    return index


def _authenticate_selection_receipt(receipt: Any) -> SkillSelectionReceipt:
    if type(receipt) is not SkillSelectionReceipt:
        if isinstance(receipt, SkillSelectionReceipt):
            _fail("skill_registry.receipt_extended", "receipt type must be exactly SkillSelectionReceipt")
        _fail("skill_registry.invalid_type", "receipt must be a SkillSelectionReceipt")
    receipt_map = receipt.as_mapping()
    if set(receipt_map) != {"selected", "context", "context_seal"}:
        _fail("skill_registry.receipt_extended", "receipt mapping has unexpected keys")
    if set(vars(receipt)) != {"selected", "context", "context_seal"}:
        _fail("skill_registry.receipt_extended", "receipt has unexpected fields")
    if not isinstance(receipt.context, Mapping):
        _fail("skill_registry.invalid_type", "receipt context must be a mapping")
    if receipt_map["context"] and set(receipt_map["context"]) != set(_SELECT_REASON_FIELDS):
        _fail("skill_registry.receipt_extended", "receipt context has unexpected keys")
    if not isinstance(receipt.selected, tuple):
        _fail("skill_registry.invalid_type", "receipt.selected must be a tuple")
    return receipt


def _resolve_selected_records(
    candidates: Iterable[SkillRecord],
    receipt: SkillSelectionReceipt,
) -> list[tuple[SkillSelection, SkillRecord]]:
    index = _index_packet_candidates(candidates)
    resolved: list[tuple[SkillSelection, SkillRecord]] = []
    seen: set[tuple[str, str]] = set()
    for item in receipt.selected:
        if type(item) is not SkillSelection:
            if isinstance(item, SkillSelection):
                _fail("skill_registry.receipt_extended", "selection row type must be exactly SkillSelection")
            _fail("skill_registry.invalid_type", "receipt rows must be SkillSelection")
        row_keys = {"identity", "version", "digest", "reasons"}
        if set(vars(item)) != row_keys:
            _fail("skill_registry.receipt_extended", "selection row has unexpected fields")
        key = (item.identity, item.version)
        if key in seen:
            _fail(
                "skill_registry.duplicate_selection",
                f"duplicate selection {item.identity!r}@{item.version!r}",
            )
        seen.add(key)
        match = index.get(key)
        if match is None:
            _fail(
                "skill_registry.not_found",
                f"no candidate for selected {item.identity!r}@{item.version!r}",
            )
        digest = skill_digest(match)
        if digest != item.digest:
            _fail(
                "skill_registry.digest_mismatch",
                f"digest mismatch for {item.identity!r}@{item.version!r}",
            )
        if match.lifecycle_state is not LifecycleState.ACTIVE:
            _fail(
                "skill_registry.invalid_lifecycle",
                f"selected {item.identity!r}@{item.version!r} is not ACTIVE",
            )
        resolved.append((item, match))
    rank_keys = [_rank_key(record) for _, record in resolved]
    if rank_keys != sorted(rank_keys):
        _fail("skill_registry.selection_spoofed", "receipt order does not match deterministic rank order")
    _verify_receipt_context_seal(receipt)
    return resolved


def _emit_runtime_packet(
    resolved: list[tuple[SkillSelection, SkillRecord]],
    bound_context: Mapping[str, Any],
    *,
    max_list_items: int,
    max_string_bytes: int,
    max_packet_bytes: int,
) -> SkillRuntimePacket:
    rows: list[SkillRuntimePacketRow] = []
    for item, record in resolved:
        identity = _bounded_packet_scalar(item.identity, "identity", max_string_bytes)
        version = _bounded_packet_scalar(item.version, "version", max_string_bytes)
        digest = _bounded_packet_scalar(item.digest, "digest", max_string_bytes)
        claimed_reasons = _validate_packet_reasons(
            _bounded_packet_strings(item.reasons, "reasons", max_list_items, max_string_bytes)
        )
        applicability = _bounded_packet_strings(
            record.applicability, "applicability", max_list_items, max_string_bytes
        )
        procedure_steps = _bounded_packet_strings(
            record.procedure_steps, "procedure_steps", max_list_items, max_string_bytes
        )
        avoid_rules = _bounded_packet_strings(
            record.avoid_rules, "avoid_rules", max_list_items, max_string_bytes
        )
        preferred_tools = _bounded_packet_strings(
            record.preferred_tools, "preferred_tools", max_list_items, max_string_bytes
        )
        row = SkillRuntimePacketRow(
            identity=identity,
            version=version,
            digest=digest,
            reasons=_bind_canonical_packet_reasons(claimed_reasons, record, bound_context),
            applicability=applicability,
            procedure_steps=procedure_steps,
            avoid_rules=avoid_rules,
            preferred_tools=preferred_tools,
        )
        if set(row.as_mapping()) != _PACKET_ROW_KEYS:
            _fail("skill_registry.receipt_extended", "packet row has unexpected keys")
        rows.append(row)
    packet = SkillRuntimePacket(version=RUNTIME_PACKET_VERSION, skills=tuple(rows))
    payload = packet.as_mapping()
    if set(payload) != _PACKET_KEYS:
        _fail("skill_registry.receipt_extended", "packet has unexpected keys")
    encoded = canonical_json(payload).encode("utf-8")
    if len(encoded) > max_packet_bytes:
        _fail("skill_registry.packet_limit", "canonical packet exceeds max_packet_bytes")
    return packet


def build_runtime_packet(
    candidates: Iterable[SkillRecord],
    receipt: SkillSelectionReceipt,
    *,
    max_selected: int = MAX_PACKET_SELECTED,
    max_list_items: int = MAX_PACKET_LIST_ITEMS,
    max_string_bytes: int = MAX_PACKET_STRING_BYTES,
    max_packet_bytes: int = MAX_PACKET_BYTES,
) -> SkillRuntimePacket:
    """Resolve a selection receipt to a bounded, safe runtime instruction packet.

    Every selected identity/version/digest must bind to exactly one ACTIVE
    candidate. Packet order follows the receipt and is independent of candidate
    input order. Construction is pure: it never mutates inputs or I/O state.
    """
    receipt = _authenticate_selection_receipt(receipt)
    max_selected = min(_validate_positive_limit(max_selected, "max_selected"), MAX_PACKET_SELECTED)
    max_list_items = min(_validate_positive_limit(max_list_items, "max_list_items"), MAX_PACKET_LIST_ITEMS)
    max_string_bytes = min(
        _validate_positive_limit(max_string_bytes, "max_string_bytes"), MAX_PACKET_STRING_BYTES
    )
    max_packet_bytes = min(_validate_positive_limit(max_packet_bytes, "max_packet_bytes"), MAX_PACKET_BYTES)
    if len(receipt.selected) > max_selected:
        _fail("skill_registry.packet_limit", "selected rows exceed max_selected")
    resolved = _resolve_selected_records(candidates, receipt)
    bound_context = _bind_receipt_context(receipt.context, require=bool(resolved))
    return _emit_runtime_packet(
        resolved,
        bound_context,
        max_list_items=max_list_items,
        max_string_bytes=max_string_bytes,
        max_packet_bytes=max_packet_bytes,
    )


def _validate_authority(authority: Any) -> Authority:
    if not isinstance(authority, Authority):
        _fail("skill_registry.unauthorized", "an authority actor is required")
    if not isinstance(authority.role, AuthorityRole):
        _fail("skill_registry.unauthorized", "authority role must be a valid AuthorityRole")
    _validate_actor_id(authority.actor_id, "skill_registry.unauthorized")
    if not isinstance(authority.token, str):
        _fail("skill_registry.unauthorized", "authority token must be a string")
    return authority


def _require_manager(authority: Any) -> Authority:
    authority = _validate_authority(authority)
    if not authority.is_manager:
        _fail("skill_registry.unauthorized", "explicit manager authority is required")
    return authority


class SkillRegistry:
    """Immutable, append-only in-memory store of versioned skill records."""

    def __init__(self, min_accepted_evidence: int = 2) -> None:
        self._min_accepted_evidence = _validate_min_accepted_evidence(min_accepted_evidence)
        self._entries: dict[tuple[str, str], SkillRecord] = {}
        self._digest_index: dict[str, tuple[str, str]] = {}

    @property
    def min_accepted_evidence(self) -> int:
        return self._min_accepted_evidence

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def __iter__(self) -> Iterator[SkillRecord]:
        return iter(self._entries.values())

    def records(self) -> list[SkillRecord]:
        """Return all registered records (insertion order of identity/version)."""
        return list(self._entries.values())

    def get(self, identity: str, version: str) -> SkillRecord | None:
        return self._entries.get((identity, version))

    def _require_entry(self, identity: str, version: str) -> tuple[tuple[str, str], SkillRecord]:
        key = (identity, version)
        entry = self._entries.get(key)
        if entry is None:
            _fail("skill_registry.not_found", f"no skill registered as {identity!r}@{version!r}")
        return key, entry

    def propose(self, record: SkillRecord, authority: Authority) -> SkillRecord:
        """Register a new proposed record; workers may propose but never activate.

        Proposals must be evidence-free with zero counters. Provenance evidence is
        appended only through :meth:`add_evidence`, which binds it to the single
        authenticated authority, so one actor cannot forge independence.
        """
        record = validate_record(record)
        _validate_authority(authority)
        if record.lifecycle_state is not LifecycleState.PROPOSED:
            _fail(
                "skill_registry.invalid_transition",
                "workers may only propose records in the proposed state",
            )
        if record.evidence or record.accepted_count or record.negative_count:
            _fail(
                "skill_registry.proposal_evidence_forbidden",
                "proposals must be evidence-free with zero counters; append authenticated evidence via add_evidence",
            )
        digest = skill_digest(record)
        key = (record.identity, record.version)
        if digest in self._digest_index:
            _fail("skill_registry.immutable_digest", f"digest {digest} is already registered")
        if key in self._entries:
            _fail(
                "skill_registry.immutable_identity",
                f"identity/version {record.identity!r}@{record.version!r} is already registered",
            )
        self._entries[key] = record
        self._digest_index[digest] = key
        return record

    def add_evidence(
        self,
        identity: str,
        version: str,
        evidence: EvidenceRecord | Mapping[str, Any],
        authority: Authority,
    ) -> SkillRecord:
        """Append a worker- or manager-produced evidence entry to a version.

        Provenance (``authority`` role and ``actor_id``) is bound to the
        authenticated ``authority`` argument, never the submitted payload.
        """
        authority = _validate_authority(authority)
        key, entry = self._require_entry(identity, version)
        evidence_record = _bind_evidence(evidence, authority)
        accepted = entry.accepted_count + (
            1 if evidence_record.outcome is EvidenceOutcome.ACCEPTED else 0
        )
        negative = entry.negative_count + (
            1 if evidence_record.outcome is EvidenceOutcome.NEGATIVE else 0
        )
        updated = replace(
            entry,
            evidence=entry.evidence + (evidence_record,),
            accepted_count=accepted,
            negative_count=negative,
        )
        self._entries[key] = updated
        return updated

    def mark_negative_resolved(
        self, identity: str, version: str, authority: Authority
    ) -> SkillRecord:
        """Resolve all unresolved negative evidence (manager only)."""
        _require_manager(authority)
        key, entry = self._require_entry(identity, version)
        updated_evidence = tuple(
            replace(item, resolved=True)
            if item.outcome is EvidenceOutcome.NEGATIVE and not item.resolved
            else item
            for item in entry.evidence
        )
        updated = replace(entry, evidence=updated_evidence)
        self._entries[key] = updated
        return updated

    def activate(self, identity: str, version: str, authority: Authority) -> SkillRecord:
        """Activate a proposed skill (manager only, evidence-gated)."""
        _require_manager(authority)
        key, entry = self._require_entry(identity, version)
        decision = can_activate(entry, self._min_accepted_evidence)
        if not decision.allowed:
            _fail(decision.reason_code, decision.reason)
        updated = replace(entry, lifecycle_state=LifecycleState.ACTIVE)
        self._entries[key] = updated
        return updated

    def retire(self, identity: str, version: str, authority: Authority) -> SkillRecord:
        """Retire an active skill (manager only)."""
        _require_manager(authority)
        key, entry = self._require_entry(identity, version)
        decision = can_retire(entry)
        if not decision.allowed:
            _fail(decision.reason_code, decision.reason)
        updated = replace(entry, lifecycle_state=LifecycleState.RETIRED)
        self._entries[key] = updated
        return updated

    def promote(
        self, identity: str, version: str, new_version: str, authority: Authority
    ) -> SkillRecord:
        """Promote an active repository skill to global by creating a new version.

        Manager-only. The source record (and its content digest) is left
        unchanged; a new GLOBAL-scoped record carrying the validated
        ``new_version`` is digested and registered atomically. Version and
        digest collisions are rejected.
        """
        _require_manager(authority)
        _, entry = self._require_entry(identity, version)
        decision = can_promote(entry)
        if not decision.allowed:
            _fail(decision.reason_code, decision.reason)
        new_version_value = _validate_version(new_version)
        promoted = replace(entry, scope=SkillScope.GLOBAL, version=new_version_value)
        promoted = validate_record(promoted)
        digest = skill_digest(promoted)
        new_key = (identity, new_version_value)
        if digest in self._digest_index:
            _fail("skill_registry.immutable_digest", f"digest {digest} is already registered")
        if new_key in self._entries:
            _fail(
                "skill_registry.immutable_identity",
                f"identity/version {identity!r}@{new_version_value!r} is already registered",
            )
        self._entries[new_key] = promoted
        self._digest_index[digest] = new_key
        return promoted

    def select(
        self, context: Mapping[str, Any], limit: int = DEFAULT_SELECT_LIMIT
    ) -> SkillSelectionReceipt:
        """Read-only ACTIVE skill selection. Never mutates registry state."""
        return select(self.records(), context, limit=limit)

    def build_runtime_packet(
        self,
        receipt: SkillSelectionReceipt,
        *,
        max_selected: int = MAX_PACKET_SELECTED,
        max_list_items: int = MAX_PACKET_LIST_ITEMS,
        max_string_bytes: int = MAX_PACKET_STRING_BYTES,
        max_packet_bytes: int = MAX_PACKET_BYTES,
    ) -> SkillRuntimePacket:
        """Read-only runtime packet build. Never mutates registry state."""
        return build_runtime_packet(
            self.records(),
            receipt,
            max_selected=max_selected,
            max_list_items=max_list_items,
            max_string_bytes=max_string_bytes,
            max_packet_bytes=max_packet_bytes,
        )


__all__ = [
    "Authority",
    "AuthorityRole",
    "DEFAULT_SELECT_LIMIT",
    "EvidenceOutcome",
    "EvidenceRecord",
    "LifecycleState",
    "MAX_PACKET_BYTES",
    "MAX_PACKET_LIST_ITEMS",
    "MAX_PACKET_SELECTED",
    "MAX_PACKET_STRING_BYTES",
    "MAX_RANK_LIMIT",
    "MAX_SELECT_LIMIT",
    "RUNTIME_PACKET_VERSION",
    "RiskLevel",
    "SELECT_WILDCARD",
    "SkillRecord",
    "SkillRegistry",
    "SkillRegistryError",
    "SkillRuntimePacket",
    "SkillRuntimePacketRow",
    "SkillScope",
    "SkillSelection",
    "SkillSelectionReceipt",
    "TransitionDecision",
    "build_runtime_packet",
    "can_activate",
    "can_promote",
    "can_retire",
    "canonical_json",
    "canonical_payload",
    "independent_accepted_evidence_count",
    "normalize",
    "rank",
    "select",
    "skill_digest",
    "transition_allowed",
    "unresolved_negative_evidence",
    "validate_record",
]
