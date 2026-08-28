"""Typed, immutable, deterministic Development Rules Manifest foundation.

Pure parse/validate/resolve APIs over explicit mappings or UTF-8 JSON bytes.
No file I/O, subprocess, network, database, task-store or model invocation.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Union

SCHEMA_ID = "aiworkhub.development_rules_manifest"
SCHEMA_VERSION = "1.0.0"

_DIGEST_DOMAIN = b"aiworkhub.development_rules_manifest.digest.v1\n"

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
_MAX_IDENTIFIER_LENGTH = 64

_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

_PATH_SELECTOR_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-./*?\[\]]*$")
_MAX_PATH_SELECTOR_LENGTH = 256

MAX_ALLOCATIONS_PER_ITERATION = 1_000_000
MAX_EVIDENCE_MIN_COUNT = 1_000


class ReasonCode(Enum):
    UNKNOWN_SCHEMA = "unknown_schema"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    MALFORMED_VERSION = "malformed_version"
    MALFORMED_JSON = "malformed_json"
    WRONG_TYPE = "wrong_type"
    BOOL_AS_INT = "bool_as_int"
    UNKNOWN_FIELD = "unknown_field"
    MISSING_FIELD = "missing_field"
    MALFORMED_IDENTIFIER = "malformed_identifier"
    DUPLICATE_IDENTIFIER = "duplicate_identifier"
    INVALID_ENUM_VALUE = "invalid_enum_value"
    UNSAFE_PATH_SELECTOR = "unsafe_path_selector"
    OUT_OF_RANGE = "out_of_range"
    UNBOUNDED_LIMIT = "unbounded_limit"
    CONTRADICTORY_RULE = "contradictory_rule"
    UNKNOWN_LANGUAGE_REFERENCE = "unknown_language_reference"
    EMPTY_COLLECTION = "empty_collection"
    AMBIGUOUS_RULE_PRECEDENCE = "ambiguous_rule_precedence"


class ManifestValidationError(ValueError):
    """Fail-closed validation error carrying a stable typed reason."""

    def __init__(self, reason: ReasonCode, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}")


class RuleKind(Enum):
    CODING_CONVENTION = "coding_convention"
    FORBIDDEN_PATTERN = "forbidden_pattern"
    PERFORMANCE = "performance"
    OWNERSHIP_LIFETIME = "ownership_lifetime"
    CONCURRENCY = "concurrency"
    EVIDENCE = "evidence"


class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


class Severity(Enum):
    WARNING = "warning"
    ERROR = "error"


class ComplexityBound(Enum):
    O_1 = "o_1"
    O_LOG_N = "o_log_n"
    O_N = "o_n"
    O_N_LOG_N = "o_n_log_n"
    O_N2 = "o_n2"
    O_N3 = "o_n3"


class ConcurrencyModel(Enum):
    THREADS_IO_BOUND = "threads_io_bound"
    PROCESSES_CPU_BOUND = "processes_cpu_bound"
    SEQUENTIAL = "sequential"


@dataclass(frozen=True, slots=True)
class Applicability:
    languages: tuple[str, ...]
    paths: tuple[str, ...]
    tasks: tuple[str, ...]
    risk_levels: tuple[RiskLevel, ...]


@dataclass(frozen=True, slots=True)
class CodingConventionRequirement:
    guideline_ids: tuple[str, ...]
    severity: Severity


@dataclass(frozen=True, slots=True)
class ForbiddenPatternRequirement:
    severity: Severity
    rationale_id: str


@dataclass(frozen=True, slots=True)
class PerformanceRequirement:
    max_allocations_per_iteration: int
    max_time_complexity: ComplexityBound
    max_space_complexity: ComplexityBound
    min_concurrency_headroom_fraction: float
    measurement_required: bool
    measurement_evidence_id: str


@dataclass(frozen=True, slots=True)
class OwnershipLifetimeRequirement:
    requires_raii: bool
    requires_context_manager: bool
    requires_explicit_cleanup: bool
    forbidden_raw_ownership_patterns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConcurrencyPolicy:
    model: ConcurrencyModel
    min_headroom_fraction: float


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    required_evidence_types: tuple[str, ...]
    min_count: int


RulePayload = Union[
    CodingConventionRequirement,
    ForbiddenPatternRequirement,
    PerformanceRequirement,
    OwnershipLifetimeRequirement,
    ConcurrencyPolicy,
    EvidenceRequirement,
]


@dataclass(frozen=True, slots=True)
class DevelopmentRule:
    id: str
    topic: str
    kind: RuleKind
    applicability: Applicability
    allow: tuple[str, ...]
    forbid: tuple[str, ...]
    payload: RulePayload


@dataclass(frozen=True, slots=True)
class DevelopmentRulesManifest:
    schema: str
    schema_version: str
    languages: tuple[str, ...]
    rules: tuple[DevelopmentRule, ...]


@dataclass(frozen=True, slots=True)
class ResolvedRuleSet:
    language: str | None
    path: str | None
    task: str | None
    risk: RiskLevel | None
    rules: tuple[DevelopmentRule, ...]
    manifest_digest: str


_SAFE_REPR_TYPES = (str, int, float, bool, type(None), bytes)


def _safe_repr(value: object) -> str:
    """Render a bounded, type-only representation without invoking a value's own __repr__."""
    value_type = type(value)
    if value_type in _SAFE_REPR_TYPES:
        return repr(value)
    return f"<{value_type.__name__} object>"


def _stable_diagnostic_key(value: object) -> tuple[str, str]:
    return (type(value).__name__, _safe_repr(value))


def _check_keys(mapping: object, allowed: frozenset[str], required: frozenset[str], *, path: str) -> dict:
    if not isinstance(mapping, dict):
        raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"{path}: expected object, got {type(mapping).__name__}")
    keys = set(mapping.keys())
    unknown = keys - allowed
    if unknown:
        sorted_unknown = sorted(unknown, key=_stable_diagnostic_key)
        rendered_unknown = [_safe_repr(key) for key in sorted_unknown]
        raise ManifestValidationError(ReasonCode.UNKNOWN_FIELD, f"{path}: unknown field(s) {rendered_unknown}")
    missing = required - keys
    if missing:
        raise ManifestValidationError(ReasonCode.MISSING_FIELD, f"{path}: missing field(s) {sorted(missing)}")
    return mapping


def _expect_bool(value: object, *, path: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"{path}: expected bool, got {type(value).__name__}")
    return value


def _expect_int_strict(value: object, *, path: str) -> int:
    if isinstance(value, bool):
        raise ManifestValidationError(ReasonCode.BOOL_AS_INT, f"{path}: bool given where int expected")
    if not isinstance(value, int):
        raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"{path}: expected int, got {type(value).__name__}")
    return value


def _expect_float_strict(value: object, *, path: str) -> float:
    if isinstance(value, bool):
        raise ManifestValidationError(ReasonCode.BOOL_AS_INT, f"{path}: bool given where numeric expected")
    if not isinstance(value, (int, float)):
        raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"{path}: expected number, got {type(value).__name__}")
    result = float(value)
    if not math.isfinite(result):
        raise ManifestValidationError(ReasonCode.UNBOUNDED_LIMIT, f"{path}: value must be finite, got {value!r}")
    return result


def _validate_bounded_int(value: object, *, path: str, minimum: int, maximum: int) -> int:
    parsed = _expect_int_strict(value, path=path)
    if not (minimum <= parsed <= maximum):
        raise ManifestValidationError(
            ReasonCode.OUT_OF_RANGE, f"{path}: {parsed} out of bounds [{minimum}, {maximum}]"
        )
    return parsed


def _validate_fraction(value: object, *, path: str) -> float:
    parsed = _expect_float_strict(value, path=path)
    if not (0.0 < parsed <= 1.0):
        raise ManifestValidationError(ReasonCode.OUT_OF_RANGE, f"{path}: fraction must be within (0, 1], got {parsed}")
    return parsed


def _validate_identifier(value: object, *, path: str) -> str:
    if not isinstance(value, str):
        raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"{path}: expected str identifier, got {type(value).__name__}")
    if len(value) > _MAX_IDENTIFIER_LENGTH or not _IDENTIFIER_RE.match(value):
        raise ManifestValidationError(ReasonCode.MALFORMED_IDENTIFIER, f"{path}: malformed identifier {value!r}")
    return value


def _validate_identifier_tuple(values: object, *, path: str, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"{path}: expected array, got {type(values).__name__}")
    if not values and not allow_empty:
        raise ManifestValidationError(ReasonCode.EMPTY_COLLECTION, f"{path}: must not be empty")
    parsed = [_validate_identifier(v, path=f"{path}[{i}]") for i, v in enumerate(values)]
    seen: set[str] = set()
    for ident in parsed:
        if ident in seen:
            raise ManifestValidationError(ReasonCode.DUPLICATE_IDENTIFIER, f"{path}: duplicate identifier {ident!r}")
        seen.add(ident)
    return tuple(sorted(parsed))


def _validate_path_selector(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"{path}: expected non-empty str, got {value!r}")
    if len(value) > _MAX_PATH_SELECTOR_LENGTH:
        raise ManifestValidationError(ReasonCode.UNSAFE_PATH_SELECTOR, f"{path}: selector exceeds max length")
    if value.startswith("/") or ".." in value or "\\" in value or "\x00" in value:
        raise ManifestValidationError(ReasonCode.UNSAFE_PATH_SELECTOR, f"{path}: unsafe selector {value!r}")
    if not _PATH_SELECTOR_RE.match(value):
        raise ManifestValidationError(ReasonCode.UNSAFE_PATH_SELECTOR, f"{path}: unsafe selector {value!r}")
    return value


def _validate_path_selectors(values: object, *, path: str) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"{path}: expected array, got {type(values).__name__}")
    parsed = [_validate_path_selector(v, path=f"{path}[{i}]") for i, v in enumerate(values)]
    seen: set[str] = set()
    for selector in parsed:
        if selector in seen:
            raise ManifestValidationError(ReasonCode.DUPLICATE_IDENTIFIER, f"{path}: duplicate selector {selector!r}")
        seen.add(selector)
    return tuple(sorted(parsed))


def _parse_enum(value: object, enum_cls: type[Enum], *, path: str) -> Enum:
    if not isinstance(value, str):
        raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"{path}: expected str, got {type(value).__name__}")
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise ManifestValidationError(
            ReasonCode.INVALID_ENUM_VALUE, f"{path}: invalid value {value!r} for {enum_cls.__name__}"
        ) from exc


def _parse_applicability(mapping: object, *, path: str, known_languages: frozenset[str]) -> Applicability:
    if mapping is None:
        return Applicability((), (), (), ())
    allowed = frozenset({"languages", "paths", "tasks", "risk_levels"})
    _check_keys(mapping, allowed, required=frozenset(), path=path)
    languages = _validate_identifier_tuple(mapping.get("languages", []), path=f"{path}.languages", allow_empty=True)
    for language in languages:
        if language not in known_languages:
            raise ManifestValidationError(
                ReasonCode.UNKNOWN_LANGUAGE_REFERENCE, f"{path}.languages: {language!r} not declared in $.languages"
            )
    paths = _validate_path_selectors(mapping.get("paths", []), path=f"{path}.paths")
    tasks = _validate_identifier_tuple(mapping.get("tasks", []), path=f"{path}.tasks", allow_empty=True)
    risk_raw = mapping.get("risk_levels", [])
    if not isinstance(risk_raw, list):
        raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"{path}.risk_levels: expected array")
    risk_values = [_parse_enum(v, RiskLevel, path=f"{path}.risk_levels[{i}]") for i, v in enumerate(risk_raw)]
    if len(set(risk_values)) != len(risk_values):
        raise ManifestValidationError(ReasonCode.DUPLICATE_IDENTIFIER, f"{path}.risk_levels: duplicate risk level")
    risk_levels = tuple(sorted(risk_values, key=lambda r: _RISK_ORDER[r]))
    return Applicability(languages, paths, tasks, risk_levels)


def _parse_coding_convention_payload(mapping: object, *, path: str) -> CodingConventionRequirement:
    _check_keys(mapping, frozenset({"guideline_ids", "severity"}), required=frozenset({"guideline_ids", "severity"}), path=path)
    guideline_ids = _validate_identifier_tuple(mapping["guideline_ids"], path=f"{path}.guideline_ids", allow_empty=False)
    severity = _parse_enum(mapping["severity"], Severity, path=f"{path}.severity")
    return CodingConventionRequirement(guideline_ids=guideline_ids, severity=severity)


def _parse_forbidden_pattern_payload(mapping: object, *, path: str) -> ForbiddenPatternRequirement:
    _check_keys(mapping, frozenset({"severity", "rationale_id"}), required=frozenset({"severity", "rationale_id"}), path=path)
    severity = _parse_enum(mapping["severity"], Severity, path=f"{path}.severity")
    rationale_id = _validate_identifier(mapping["rationale_id"], path=f"{path}.rationale_id")
    return ForbiddenPatternRequirement(severity=severity, rationale_id=rationale_id)


_PERFORMANCE_PAYLOAD_KEYS = frozenset(
    {
        "max_allocations_per_iteration",
        "max_time_complexity",
        "max_space_complexity",
        "min_concurrency_headroom_fraction",
        "measurement_required",
        "measurement_evidence_id",
    }
)


def _parse_performance_payload(mapping: object, *, path: str) -> PerformanceRequirement:
    _check_keys(mapping, _PERFORMANCE_PAYLOAD_KEYS, required=_PERFORMANCE_PAYLOAD_KEYS, path=path)
    max_allocations = _validate_bounded_int(
        mapping["max_allocations_per_iteration"],
        path=f"{path}.max_allocations_per_iteration",
        minimum=0,
        maximum=MAX_ALLOCATIONS_PER_ITERATION,
    )
    max_time_complexity = _parse_enum(mapping["max_time_complexity"], ComplexityBound, path=f"{path}.max_time_complexity")
    max_space_complexity = _parse_enum(mapping["max_space_complexity"], ComplexityBound, path=f"{path}.max_space_complexity")
    headroom = _validate_fraction(mapping["min_concurrency_headroom_fraction"], path=f"{path}.min_concurrency_headroom_fraction")
    measurement_required = _expect_bool(mapping["measurement_required"], path=f"{path}.measurement_required")
    measurement_evidence_id = _validate_identifier(mapping["measurement_evidence_id"], path=f"{path}.measurement_evidence_id")
    return PerformanceRequirement(
        max_allocations_per_iteration=max_allocations,
        max_time_complexity=max_time_complexity,
        max_space_complexity=max_space_complexity,
        min_concurrency_headroom_fraction=headroom,
        measurement_required=measurement_required,
        measurement_evidence_id=measurement_evidence_id,
    )


_OWNERSHIP_PAYLOAD_KEYS = frozenset(
    {"requires_raii", "requires_context_manager", "requires_explicit_cleanup", "forbidden_raw_ownership_patterns"}
)


def _parse_ownership_payload(mapping: object, *, path: str) -> OwnershipLifetimeRequirement:
    _check_keys(mapping, _OWNERSHIP_PAYLOAD_KEYS, required=_OWNERSHIP_PAYLOAD_KEYS, path=path)
    requires_raii = _expect_bool(mapping["requires_raii"], path=f"{path}.requires_raii")
    requires_context_manager = _expect_bool(mapping["requires_context_manager"], path=f"{path}.requires_context_manager")
    requires_explicit_cleanup = _expect_bool(mapping["requires_explicit_cleanup"], path=f"{path}.requires_explicit_cleanup")
    forbidden_raw_ownership_patterns = _validate_identifier_tuple(
        mapping["forbidden_raw_ownership_patterns"], path=f"{path}.forbidden_raw_ownership_patterns", allow_empty=True
    )
    return OwnershipLifetimeRequirement(
        requires_raii=requires_raii,
        requires_context_manager=requires_context_manager,
        requires_explicit_cleanup=requires_explicit_cleanup,
        forbidden_raw_ownership_patterns=forbidden_raw_ownership_patterns,
    )


def _parse_concurrency_payload(mapping: object, *, path: str) -> ConcurrencyPolicy:
    _check_keys(mapping, frozenset({"model", "min_headroom_fraction"}), required=frozenset({"model", "min_headroom_fraction"}), path=path)
    model = _parse_enum(mapping["model"], ConcurrencyModel, path=f"{path}.model")
    min_headroom_fraction = _validate_fraction(mapping["min_headroom_fraction"], path=f"{path}.min_headroom_fraction")
    return ConcurrencyPolicy(model=model, min_headroom_fraction=min_headroom_fraction)


def _parse_evidence_payload(mapping: object, *, path: str) -> EvidenceRequirement:
    _check_keys(mapping, frozenset({"required_evidence_types", "min_count"}), required=frozenset({"required_evidence_types", "min_count"}), path=path)
    required_evidence_types = _validate_identifier_tuple(
        mapping["required_evidence_types"], path=f"{path}.required_evidence_types", allow_empty=False
    )
    min_count = _validate_bounded_int(mapping["min_count"], path=f"{path}.min_count", minimum=1, maximum=MAX_EVIDENCE_MIN_COUNT)
    return EvidenceRequirement(required_evidence_types=required_evidence_types, min_count=min_count)


_PAYLOAD_PARSERS = {
    RuleKind.CODING_CONVENTION: _parse_coding_convention_payload,
    RuleKind.FORBIDDEN_PATTERN: _parse_forbidden_pattern_payload,
    RuleKind.PERFORMANCE: _parse_performance_payload,
    RuleKind.OWNERSHIP_LIFETIME: _parse_ownership_payload,
    RuleKind.CONCURRENCY: _parse_concurrency_payload,
    RuleKind.EVIDENCE: _parse_evidence_payload,
}

_RULE_ALLOWED_KEYS = frozenset({"id", "topic", "kind", "applicability", "allow", "forbid", "payload"})
_RULE_REQUIRED_KEYS = frozenset({"id", "kind", "payload"})


def _parse_rule(mapping: object, *, path: str, known_languages: frozenset[str]) -> DevelopmentRule:
    _check_keys(mapping, _RULE_ALLOWED_KEYS, required=_RULE_REQUIRED_KEYS, path=path)
    rule_id = _validate_identifier(mapping["id"], path=f"{path}.id")
    topic = _validate_identifier(mapping["topic"], path=f"{path}.topic") if "topic" in mapping else rule_id
    kind = _parse_enum(mapping["kind"], RuleKind, path=f"{path}.kind")
    applicability = _parse_applicability(mapping.get("applicability"), path=f"{path}.applicability", known_languages=known_languages)
    allow = _validate_identifier_tuple(mapping.get("allow", []), path=f"{path}.allow", allow_empty=True)
    forbid = _validate_identifier_tuple(mapping.get("forbid", []), path=f"{path}.forbid", allow_empty=True)
    overlap = set(allow) & set(forbid)
    if overlap:
        raise ManifestValidationError(
            ReasonCode.CONTRADICTORY_RULE, f"{path}: identifiers in both allow and forbid: {sorted(overlap)}"
        )
    payload_mapping = mapping["payload"]
    payload = _PAYLOAD_PARSERS[kind](payload_mapping, path=f"{path}.payload")
    return DevelopmentRule(
        id=rule_id, topic=topic, kind=kind, applicability=applicability, allow=allow, forbid=forbid, payload=payload
    )


_TOP_LEVEL_KEYS = frozenset({"schema", "schema_version", "languages", "rules"})


def parse_manifest(mapping: object) -> DevelopmentRulesManifest:
    """Parse and validate an explicit mapping into an immutable manifest. Fails closed."""
    _check_keys(mapping, _TOP_LEVEL_KEYS, required=_TOP_LEVEL_KEYS, path="$")
    schema = mapping["schema"]
    if not isinstance(schema, str) or schema != SCHEMA_ID:
        raise ManifestValidationError(ReasonCode.UNKNOWN_SCHEMA, f"$.schema: expected {SCHEMA_ID!r}, got {schema!r}")
    schema_version = mapping["schema_version"]
    if not isinstance(schema_version, str) or not _VERSION_RE.match(schema_version):
        raise ManifestValidationError(ReasonCode.MALFORMED_VERSION, f"$.schema_version: malformed version {schema_version!r}")
    if schema_version != SCHEMA_VERSION:
        raise ManifestValidationError(
            ReasonCode.UNSUPPORTED_SCHEMA_VERSION,
            f"$.schema_version: unsupported version {schema_version!r}, expected {SCHEMA_VERSION!r}",
        )
    languages = _validate_identifier_tuple(mapping["languages"], path="$.languages", allow_empty=False)
    rules_raw = mapping["rules"]
    if not isinstance(rules_raw, list):
        raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"$.rules: expected array, got {type(rules_raw).__name__}")
    if not rules_raw:
        raise ManifestValidationError(ReasonCode.EMPTY_COLLECTION, "$.rules: must not be empty")
    known_languages = frozenset(languages)
    rules: list[DevelopmentRule] = []
    seen_ids: set[str] = set()
    for index, rule_mapping in enumerate(rules_raw):
        rule = _parse_rule(rule_mapping, path=f"$.rules[{index}]", known_languages=known_languages)
        if rule.id in seen_ids:
            raise ManifestValidationError(ReasonCode.DUPLICATE_IDENTIFIER, f"$.rules[{index}]: duplicate rule id {rule.id!r}")
        seen_ids.add(rule.id)
        rules.append(rule)
    rules_sorted = tuple(sorted(rules, key=lambda r: r.id))
    return DevelopmentRulesManifest(schema=schema, schema_version=schema_version, languages=languages, rules=rules_sorted)


def parse_manifest_bytes(data: bytes) -> DevelopmentRulesManifest:
    """Parse and validate UTF-8 JSON bytes into an immutable manifest. Fails closed."""
    if not isinstance(data, (bytes, bytearray)):
        raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"$: expected bytes, got {type(data).__name__}")
    try:
        decoded = bytes(data).decode("utf-8")
        obj = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ManifestValidationError(ReasonCode.MALFORMED_JSON, str(exc)) from exc
    return parse_manifest(obj)


def _to_canonical(obj: object) -> object:
    if isinstance(obj, DevelopmentRulesManifest):
        return {
            "schema": obj.schema,
            "schema_version": obj.schema_version,
            "languages": list(obj.languages),
            "rules": [_to_canonical(rule) for rule in obj.rules],
        }
    if isinstance(obj, DevelopmentRule):
        return {
            "id": obj.id,
            "topic": obj.topic,
            "kind": obj.kind.value,
            "applicability": _to_canonical(obj.applicability),
            "allow": list(obj.allow),
            "forbid": list(obj.forbid),
            "payload": _to_canonical(obj.payload),
        }
    if isinstance(obj, Applicability):
        return {
            "languages": list(obj.languages),
            "paths": list(obj.paths),
            "tasks": list(obj.tasks),
            "risk_levels": [r.value for r in obj.risk_levels],
        }
    if isinstance(obj, CodingConventionRequirement):
        return {"guideline_ids": list(obj.guideline_ids), "severity": obj.severity.value}
    if isinstance(obj, ForbiddenPatternRequirement):
        return {"severity": obj.severity.value, "rationale_id": obj.rationale_id}
    if isinstance(obj, PerformanceRequirement):
        return {
            "max_allocations_per_iteration": obj.max_allocations_per_iteration,
            "max_time_complexity": obj.max_time_complexity.value,
            "max_space_complexity": obj.max_space_complexity.value,
            "min_concurrency_headroom_fraction": obj.min_concurrency_headroom_fraction,
            "measurement_required": obj.measurement_required,
            "measurement_evidence_id": obj.measurement_evidence_id,
        }
    if isinstance(obj, OwnershipLifetimeRequirement):
        return {
            "requires_raii": obj.requires_raii,
            "requires_context_manager": obj.requires_context_manager,
            "requires_explicit_cleanup": obj.requires_explicit_cleanup,
            "forbidden_raw_ownership_patterns": list(obj.forbidden_raw_ownership_patterns),
        }
    if isinstance(obj, ConcurrencyPolicy):
        return {"model": obj.model.value, "min_headroom_fraction": obj.min_headroom_fraction}
    if isinstance(obj, EvidenceRequirement):
        return {"required_evidence_types": list(obj.required_evidence_types), "min_count": obj.min_count}
    raise TypeError(f"unsupported type for canonicalization: {type(obj)!r}")


def canonical_digest(manifest: DevelopmentRulesManifest) -> str:
    """Return the canonical SHA-256 hex digest, stable across mapping order and platform."""
    canonical = _to_canonical(manifest)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(_DIGEST_DOMAIN + encoded).hexdigest()


def _coerce_risk(value: object) -> RiskLevel | None:
    if value is None or isinstance(value, RiskLevel):
        return value
    if isinstance(value, str):
        try:
            return RiskLevel(value)
        except ValueError as exc:
            raise ManifestValidationError(ReasonCode.INVALID_ENUM_VALUE, f"risk: invalid value {value!r}") from exc
    raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"risk: expected RiskLevel or str, got {type(value).__name__}")


def _dimension_matches(rule_values: tuple, context_value: object) -> bool:
    if not rule_values:
        return True
    if context_value is None:
        return False
    return context_value in rule_values


def _applies(applicability: Applicability, language: str | None, path: str | None, task: str | None, risk: RiskLevel | None) -> bool:
    if not _dimension_matches(applicability.languages, language):
        return False
    if not _dimension_matches(applicability.tasks, task):
        return False
    if not _dimension_matches(applicability.risk_levels, risk):
        return False
    if applicability.paths:
        if path is None:
            return False
        if not any(fnmatch.fnmatchcase(path, selector) for selector in applicability.paths):
            return False
    return True


def _is_exact_path_selector(selector: str) -> bool:
    return not any(ch in selector for ch in "*?[")


def _path_dimension_specificity(paths: tuple[str, ...], path: str | None) -> int:
    if not paths or path is None:
        return 0
    matched_scores = [
        2 if _is_exact_path_selector(selector) else 1 for selector in paths if fnmatch.fnmatchcase(path, selector)
    ]
    return max(matched_scores, default=0)


def _specificity(applicability: Applicability, *, path: str | None) -> int:
    language_score = 1 if applicability.languages else 0
    task_score = 1 if applicability.tasks else 0
    risk_score = 1 if applicability.risk_levels else 0
    path_score = _path_dimension_specificity(applicability.paths, path)
    return language_score + task_score + risk_score + path_score


def _validate_resolve_language(value: object, *, known_languages: frozenset[str]) -> str | None:
    if value is None:
        return None
    validated = _validate_identifier(value, path="language")
    if validated not in known_languages:
        raise ManifestValidationError(
            ReasonCode.UNKNOWN_LANGUAGE_REFERENCE, f"language: {validated!r} not declared in manifest languages"
        )
    return validated


def _validate_resolve_task(value: object, *, path: str = "task") -> str | None:
    if value is None:
        return None
    return _validate_identifier(value, path=path)


def _validate_resolve_path(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"path: expected non-empty str, got {value!r}")
    if len(value) > _MAX_PATH_SELECTOR_LENGTH:
        raise ManifestValidationError(ReasonCode.UNSAFE_PATH_SELECTOR, "path: exceeds max length")
    if value.startswith("/") or ".." in value or "\\" in value or "\x00" in value:
        raise ManifestValidationError(ReasonCode.UNSAFE_PATH_SELECTOR, f"path: unsafe path {value!r}")
    if any(ch in value for ch in "*?[]"):
        raise ManifestValidationError(ReasonCode.UNSAFE_PATH_SELECTOR, f"path: unsafe path {value!r}")
    if not _PATH_SELECTOR_RE.match(value):
        raise ManifestValidationError(ReasonCode.UNSAFE_PATH_SELECTOR, f"path: unsafe path {value!r}")
    return value


def _reject_ambiguous_ties(winning_rules: tuple[DevelopmentRule, ...]) -> None:
    by_topic: dict[str, list[DevelopmentRule]] = {}
    for rule in winning_rules:
        by_topic.setdefault(rule.topic, []).append(rule)
    for topic, tied_rules in by_topic.items():
        if len(tied_rules) < 2:
            continue
        allow_union: set[str] = set()
        forbid_union: set[str] = set()
        for rule in tied_rules:
            allow_union |= set(rule.allow)
            forbid_union |= set(rule.forbid)
        conflict = allow_union & forbid_union
        if conflict:
            raise ManifestValidationError(
                ReasonCode.AMBIGUOUS_RULE_PRECEDENCE,
                f"topic {topic!r}: tied rules {[rule.id for rule in tied_rules]} contradict on {sorted(conflict)}",
            )


def resolve(
    manifest: DevelopmentRulesManifest,
    *,
    language: str | None = None,
    path: str | None = None,
    task: str | None = None,
    risk: RiskLevel | str | None = None,
) -> ResolvedRuleSet:
    """Resolve the immutable, deterministic rule subset applicable to a context.

    Context inputs are validated and fail closed: an unknown language, an
    unsafe/malformed path, or a malformed task identifier raises rather than
    silently falling back to unscoped rules. Only narrows the manifest's
    declared rules; never broadens authority beyond what is explicitly present
    in the manifest. Among rules sharing a topic, only the most specific
    applicable match(es) are kept, with exact path selectors outranking glob
    selectors; a tie between rules of equal specificity that contradict on
    allow/forbid fails closed instead of returning both.
    """
    known_languages = frozenset(manifest.languages)
    validated_language = _validate_resolve_language(language, known_languages=known_languages)
    validated_path = _validate_resolve_path(path)
    validated_task = _validate_resolve_task(task)
    resolved_risk = _coerce_risk(risk)
    matches = [
        rule
        for rule in manifest.rules
        if _applies(rule.applicability, validated_language, validated_path, validated_task, resolved_risk)
    ]
    best_specificity_by_topic: dict[str, int] = {}
    for rule in matches:
        specificity = _specificity(rule.applicability, path=validated_path)
        current_best = best_specificity_by_topic.get(rule.topic)
        if current_best is None or specificity > current_best:
            best_specificity_by_topic[rule.topic] = specificity
    winning_rules = tuple(
        sorted(
            (
                rule
                for rule in matches
                if _specificity(rule.applicability, path=validated_path) == best_specificity_by_topic[rule.topic]
            ),
            key=lambda rule: rule.id,
        )
    )
    _reject_ambiguous_ties(winning_rules)
    return ResolvedRuleSet(
        language=validated_language,
        path=validated_path,
        task=validated_task,
        risk=resolved_risk,
        rules=winning_rules,
        manifest_digest=canonical_digest(manifest),
    )


# --- inert construction-template registry ----------------------------------

CONSTRUCTION_TEMPLATE_REGISTRY_SCHEMA_ID = "aiworkhub.construction_template_registry"
CONSTRUCTION_TEMPLATE_REGISTRY_SCHEMA_VERSION = "1.0.0"

_TEMPLATE_DIGEST_DOMAIN = b"aiworkhub.construction_template.digest.v1\n"
_REGISTRY_DIGEST_DOMAIN = b"aiworkhub.construction_template_registry.digest.v1\n"


class LogicSlotType(Enum):
    EXPRESSION = "expression"
    STATEMENT = "statement"
    IDENTIFIER = "identifier"
    TYPE = "type"


@dataclass(frozen=True, slots=True)
class ConstructionTemplateSlot:
    id: str
    type: LogicSlotType
    required: bool


@dataclass(frozen=True, slots=True)
class LiteralPatternNode:
    source: str


@dataclass(frozen=True, slots=True)
class LogicSlotReferenceNode:
    slot_id: str


PatternNode = Union[LiteralPatternNode, LogicSlotReferenceNode]


@dataclass(frozen=True, slots=True)
class ConstructionTemplate:
    id: str
    applicability: "TemplateApplicability"
    slots: tuple[ConstructionTemplateSlot, ...]
    pattern: tuple[PatternNode, ...]


@dataclass(frozen=True, slots=True)
class TemplateApplicability:
    languages: tuple[str, ...]
    symbol_kinds: tuple[str, ...]
    task_kinds: tuple[str, ...]
    risk_levels: tuple[RiskLevel, ...]


@dataclass(frozen=True, slots=True)
class ConstructionTemplateRegistry:
    schema: str
    schema_version: str
    languages: tuple[str, ...]
    templates: tuple[ConstructionTemplate, ...]


@dataclass(frozen=True, slots=True)
class TemplateSelectionReceipt:
    language: str | None
    symbol_kind: str | None
    task_kind: str | None
    risk: RiskLevel | None
    template_ids: tuple[str, ...]
    template_digests: tuple[str, ...]
    registry_digest: str


def _parse_template_applicability(mapping: object, *, path: str, known_languages: frozenset[str]) -> TemplateApplicability:
    if mapping is None:
        return TemplateApplicability((), (), (), ())
    allowed = frozenset({"languages", "symbol_kinds", "task_kinds", "risk_levels"})
    _check_keys(mapping, allowed, required=frozenset(), path=path)
    languages = _validate_identifier_tuple(mapping.get("languages", []), path=f"{path}.languages", allow_empty=True)
    for language in languages:
        if language not in known_languages:
            raise ManifestValidationError(
                ReasonCode.UNKNOWN_LANGUAGE_REFERENCE, f"{path}.languages: {language!r} not declared in $.languages"
            )
    symbol_kinds = _validate_identifier_tuple(mapping.get("symbol_kinds", []), path=f"{path}.symbol_kinds", allow_empty=True)
    task_kinds = _validate_identifier_tuple(mapping.get("task_kinds", []), path=f"{path}.task_kinds", allow_empty=True)
    risk_raw = mapping.get("risk_levels", [])
    if not isinstance(risk_raw, list):
        raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"{path}.risk_levels: expected array")
    risk_levels = [
        _parse_enum(value, RiskLevel, path=f"{path}.risk_levels[{index}]")
        for index, value in enumerate(risk_raw)
    ]
    if len(set(risk_levels)) != len(risk_levels):
        raise ManifestValidationError(ReasonCode.DUPLICATE_IDENTIFIER, f"{path}.risk_levels: duplicate risk level")
    return TemplateApplicability(
        languages,
        symbol_kinds,
        task_kinds,
        tuple(sorted(risk_levels, key=lambda value: _RISK_ORDER[value])),
    )


def _parse_template_slot(mapping: object, *, path: str) -> ConstructionTemplateSlot:
    keys = frozenset({"id", "type", "required"})
    _check_keys(mapping, keys, required=keys, path=path)
    return ConstructionTemplateSlot(
        id=_validate_identifier(mapping["id"], path=f"{path}.id"),
        type=_parse_enum(mapping["type"], LogicSlotType, path=f"{path}.type"),
        required=_expect_bool(mapping["required"], path=f"{path}.required"),
    )


def _parse_pattern_node(mapping: object, *, path: str) -> PatternNode:
    _check_keys(mapping, frozenset({"literal", "logic_slot"}), required=frozenset(), path=path)
    has_literal = "literal" in mapping
    has_slot = "logic_slot" in mapping
    if has_literal == has_slot:
        raise ManifestValidationError(ReasonCode.CONTRADICTORY_RULE, f"{path}: exactly one pattern node kind is required")
    if has_literal:
        source = mapping["literal"]
        if not isinstance(source, str):
            raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"{path}.literal: expected str")
        return LiteralPatternNode(source=source)
    return LogicSlotReferenceNode(slot_id=_validate_identifier(mapping["logic_slot"], path=f"{path}.logic_slot"))


def _parse_construction_template(mapping: object, *, path: str, known_languages: frozenset[str]) -> ConstructionTemplate:
    _check_keys(
        mapping,
        frozenset({"id", "applicability", "slots", "pattern"}),
        required=frozenset({"id", "slots", "pattern"}),
        path=path,
    )
    slots_raw = mapping["slots"]
    if not isinstance(slots_raw, list):
        raise ManifestValidationError(ReasonCode.WRONG_TYPE, f"{path}.slots: expected array")
    slots = tuple(
        _parse_template_slot(slot, path=f"{path}.slots[{index}]") for index, slot in enumerate(slots_raw)
    )
    slot_ids = tuple(slot.id for slot in slots)
    if len(set(slot_ids)) != len(slot_ids):
        raise ManifestValidationError(ReasonCode.DUPLICATE_IDENTIFIER, f"{path}.slots: duplicate slot id")
    pattern_raw = mapping["pattern"]
    if not isinstance(pattern_raw, list) or not pattern_raw:
        raise ManifestValidationError(ReasonCode.EMPTY_COLLECTION, f"{path}.pattern: must be a non-empty array")
    pattern = tuple(
        _parse_pattern_node(node, path=f"{path}.pattern[{index}]") for index, node in enumerate(pattern_raw)
    )
    referenced = {node.slot_id for node in pattern if isinstance(node, LogicSlotReferenceNode)}
    unknown_slots = referenced - set(slot_ids)
    if unknown_slots:
        raise ManifestValidationError(ReasonCode.MALFORMED_IDENTIFIER, f"{path}.pattern: dangling slot reference {sorted(unknown_slots)}")
    unreferenced_required = {slot.id for slot in slots if slot.required} - referenced
    if unreferenced_required:
        raise ManifestValidationError(ReasonCode.CONTRADICTORY_RULE, f"{path}.slots: unreferenced required slot {sorted(unreferenced_required)}")
    return ConstructionTemplate(
        id=_validate_identifier(mapping["id"], path=f"{path}.id"),
        applicability=_parse_template_applicability(
            mapping.get("applicability"), path=f"{path}.applicability", known_languages=known_languages
        ),
        slots=tuple(sorted(slots, key=lambda slot: slot.id)),
        pattern=pattern,
    )


def parse_construction_template_registry(mapping: object) -> ConstructionTemplateRegistry:
    """Parse inert construction-template data without rendering or executing it."""
    required = frozenset({"schema", "schema_version", "languages", "templates"})
    _check_keys(mapping, required, required=required, path="$")
    if mapping["schema"] != CONSTRUCTION_TEMPLATE_REGISTRY_SCHEMA_ID:
        raise ManifestValidationError(ReasonCode.UNKNOWN_SCHEMA, "$.schema: unsupported construction-template registry schema")
    version = mapping["schema_version"]
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise ManifestValidationError(ReasonCode.MALFORMED_VERSION, "$.schema_version: malformed version")
    if version != CONSTRUCTION_TEMPLATE_REGISTRY_SCHEMA_VERSION:
        raise ManifestValidationError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION, "$.schema_version: unsupported version")
    languages = _validate_identifier_tuple(mapping["languages"], path="$.languages", allow_empty=False)
    templates_raw = mapping["templates"]
    if not isinstance(templates_raw, list) or not templates_raw:
        raise ManifestValidationError(ReasonCode.EMPTY_COLLECTION, "$.templates: must be a non-empty array")
    templates = tuple(
        _parse_construction_template(
            template, path=f"$.templates[{index}]", known_languages=frozenset(languages)
        )
        for index, template in enumerate(templates_raw)
    )
    if len({template.id for template in templates}) != len(templates):
        raise ManifestValidationError(ReasonCode.DUPLICATE_IDENTIFIER, "$.templates: duplicate template id")
    return ConstructionTemplateRegistry(
        schema=CONSTRUCTION_TEMPLATE_REGISTRY_SCHEMA_ID,
        schema_version=version,
        languages=languages,
        templates=tuple(sorted(templates, key=lambda template: template.id)),
    )


def _template_canonical(template: ConstructionTemplate) -> dict[str, object]:
    return {
        "id": template.id,
        "applicability": {
            "languages": list(template.applicability.languages),
            "symbol_kinds": list(template.applicability.symbol_kinds),
            "task_kinds": list(template.applicability.task_kinds),
            "risk_levels": [risk.value for risk in template.applicability.risk_levels],
        },
        "slots": [{"id": slot.id, "type": slot.type.value, "required": slot.required} for slot in template.slots],
        "pattern": [
            {"literal": node.source} if isinstance(node, LiteralPatternNode) else {"logic_slot": node.slot_id}
            for node in template.pattern
        ],
    }


def construction_template_digest(template: ConstructionTemplate) -> str:
    encoded = json.dumps(
        _template_canonical(template), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(_TEMPLATE_DIGEST_DOMAIN + encoded).hexdigest()


def construction_template_registry_digest(registry: ConstructionTemplateRegistry) -> str:
    canonical = {
        "schema": registry.schema,
        "schema_version": registry.schema_version,
        "languages": list(registry.languages),
        "templates": [_template_canonical(template) for template in registry.templates],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(_REGISTRY_DIGEST_DOMAIN + encoded).hexdigest()


def select_construction_templates(
    registry: ConstructionTemplateRegistry,
    *,
    language: str | None = None,
    symbol_kind: str | None = None,
    task_kind: str | None = None, risk: RiskLevel | str | None = None,
) -> TemplateSelectionReceipt:
    """Select declared inert templates only; this never renders or expands authority."""
    validated_language = _validate_resolve_language(language, known_languages=frozenset(registry.languages))
    validated_symbol_kind = _validate_resolve_task(symbol_kind, path="symbol_kind")
    validated_task_kind = _validate_resolve_task(task_kind, path="task_kind")
    resolved_risk = _coerce_risk(risk)
    selected = tuple(
        template for template in registry.templates
        if _dimension_matches(template.applicability.languages, validated_language)
        and _dimension_matches(template.applicability.symbol_kinds, validated_symbol_kind)
        and _dimension_matches(template.applicability.task_kinds, validated_task_kind)
        and _dimension_matches(template.applicability.risk_levels, resolved_risk)
    )
    return TemplateSelectionReceipt(
        language=validated_language,
        symbol_kind=validated_symbol_kind,
        task_kind=validated_task_kind,
        risk=resolved_risk,
        template_ids=tuple(template.id for template in selected),
        template_digests=tuple(construction_template_digest(template) for template in selected),
        registry_digest=construction_template_registry_digest(registry),
    )


__all__ = [
    "SCHEMA_ID",
    "SCHEMA_VERSION",
    "MAX_ALLOCATIONS_PER_ITERATION",
    "MAX_EVIDENCE_MIN_COUNT",
    "ReasonCode",
    "ManifestValidationError",
    "RuleKind",
    "RiskLevel",
    "Severity",
    "ComplexityBound",
    "ConcurrencyModel",
    "Applicability",
    "CodingConventionRequirement",
    "ForbiddenPatternRequirement",
    "PerformanceRequirement",
    "OwnershipLifetimeRequirement",
    "ConcurrencyPolicy",
    "EvidenceRequirement",
    "DevelopmentRule",
    "DevelopmentRulesManifest",
    "ResolvedRuleSet",
    "CONSTRUCTION_TEMPLATE_REGISTRY_SCHEMA_ID",
    "CONSTRUCTION_TEMPLATE_REGISTRY_SCHEMA_VERSION",
    "LogicSlotType",
    "ConstructionTemplateSlot",
    "LiteralPatternNode",
    "LogicSlotReferenceNode",
    "TemplateApplicability",
    "ConstructionTemplate",
    "ConstructionTemplateRegistry",
    "TemplateSelectionReceipt",
    "parse_manifest",
    "parse_manifest_bytes",
    "canonical_digest",
    "resolve",
    "parse_construction_template_registry",
    "construction_template_digest",
    "construction_template_registry_digest",
    "select_construction_templates",
]
