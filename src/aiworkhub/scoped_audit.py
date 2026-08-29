"""Graph-scoped audit packet contract for independent reviewers.

A :class:`ScopedAuditPacket` captures the bounded scope, evidence, prior
lessons, explicit review lens, explicit unknowns and validation expectations
that an independent reviewer needs to reach a truthful conclusion without
being shown an arbitrary repository dump. The contract is intentionally
strict: every collection is bounded and deterministically ordered; every
classifier field is checked against a controlled vocabulary; identifiers must
be unique within their section; only well-formed repo-relative paths are
accepted; and blocker-grade evidence must satisfy the review lens' required
evidence level.

This module is deliberately pure - it never queries Source Graph and never
launches reviewers.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from .evidence_levels import EvidenceLevel

__all__ = [
    "ALLOWED_CHANGE_KINDS",
    "ALLOWED_EVIDENCE_KINDS",
    "ALLOWED_REVIEW_LENSES",
    "ALLOWED_SYMBOL_KINDS",
    "ALLOWED_VALIDATION_KINDS",
    "AUDIT_COVERAGE_STATUSES",
    "AuditCoverageRecord",
    "AuditCoverageReport",
    "AuditModuleCoverage",
    "BLOCKER_MIN_EVIDENCE_LEVEL",
    "ChangedPath",
    "DuplicateIdentityError",
    "EvidenceLevel",
    "EvidenceReference",
    "KnownUnknown",
    "MAX_COLLECTION_SIZE",
    "MAX_AUDIT_FINDING_COUNT",
    "MAX_IDENTITY_LENGTH",
    "MAX_PATH_LENGTH",
    "MAX_RATIONALE_LENGTH",
    "MAX_TEXT_LENGTH",
    "MalformedPathError",
    "PriorLesson",
    "ReviewLens",
    "ScopedAuditPacket",
    "ScopedAuditValidationError",
    "TargetSymbol",
    "UnknownLensError",
    "UngroundedBlockerError",
    "ValidationExpectation",
    "canonical_json",
    "build_audit_coverage_report",
    "packet_fingerprint",
]


ALLOWED_REVIEW_LENSES: frozenset[str] = frozenset(
    {
        "correctness",
        "code_quality",
        "concurrency",
        "security",
        "data_loss",
        "api_contract",
        "test_adequacy",
        "performance",
        "migration",
        "ui_state",
        "tool_protocol",
    }
)
ALLOWED_CHANGE_KINDS: frozenset[str] = frozenset(
    {"added", "modified", "removed", "renamed"}
)
ALLOWED_SYMBOL_KINDS: frozenset[str] = frozenset(
    {
        "module",
        "class",
        "function",
        "method",
        "attribute",
        "type_alias",
        "protocol",
    }
)
ALLOWED_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {
        "call_graph",
        "control_flow",
        "data_flow",
        "callers",
        "callees",
        "persisted_state",
        "test_target",
        "contract",
    }
)
ALLOWED_VALIDATION_KINDS: frozenset[str] = frozenset(
    {"unit", "integration", "static_type", "linter", "build", "manual"}
)
AUDIT_COVERAGE_STATUSES: frozenset[str] = frozenset({"AUDITED", "UNKNOWN"})

MAX_PATH_LENGTH = 512
MAX_TEXT_LENGTH = 1024
MAX_COLLECTION_SIZE = 64
MAX_AUDIT_FINDING_COUNT = 1_000_000
MAX_RATIONALE_LENGTH = 256
MAX_IDENTITY_LENGTH = 200

BLOCKER_MIN_EVIDENCE_LEVEL: EvidenceLevel = EvidenceLevel.STATIC_EVIDENCE

_PATH_TRAVERSAL = re.compile(r"(?:^|/)\.\.(?:/|$)")
_PATH_ABSOLUTE = re.compile(
    r"^(?:[a-zA-Z]:[\\/]|[\\/]|[a-zA-Z][a-zA-Z0-9+.\-]*://)"
)
_PATH_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f\\]")


class ScopedAuditValidationError(ValueError):
    """Base class for scoped-audit validation failures."""


class UnknownLensError(ScopedAuditValidationError):
    """An unrecognised review lens was supplied."""


class DuplicateIdentityError(ScopedAuditValidationError):
    """Two or more entries share an identifier within a section."""


class MalformedPathError(ScopedAuditValidationError):
    """A path was not a valid bounded repo-relative path."""


class UngroundedBlockerError(ScopedAuditValidationError):
    """A blocker claim lacks evidence strong enough to support it."""


def _ensure_text(
    value: Any, name: str, *, max_len: int = MAX_TEXT_LENGTH
) -> str:
    if not isinstance(value, str):
        raise ScopedAuditValidationError(f"'{name}' must be a string")
    if value == "":
        raise ScopedAuditValidationError(f"'{name}' must not be empty")
    if len(value) > max_len:
        raise ScopedAuditValidationError(
            f"'{name}' exceeds maximum length {max_len}"
        )
    return value


def _ensure_identity(value: Any, *, name: str = "identity") -> str:
    text = _ensure_text(value, name, max_len=MAX_IDENTITY_LENGTH)
    if any(ch.isspace() for ch in text):
        raise ScopedAuditValidationError(
            f"'{name}' must not contain whitespace"
        )
    return text


def _validate_path(value: Any, *, name: str = "path") -> str:
    if not isinstance(value, str):
        raise MalformedPathError(f"'{name}' must be a string")
    if value == "":
        raise MalformedPathError(f"'{name}' must not be empty")
    if len(value) > MAX_PATH_LENGTH:
        raise MalformedPathError(
            f"'{name}' exceeds maximum length {MAX_PATH_LENGTH}"
        )
    if _PATH_ABSOLUTE.match(value):
        raise MalformedPathError(
            f"'{name}' must be repo-relative, not absolute: {value!r}"
        )
    if _PATH_TRAVERSAL.search(value):
        raise MalformedPathError(
            f"'{name}' must not escape the repository: {value!r}"
        )
    if _PATH_CONTROL_CHARS.search(value):
        raise MalformedPathError(
            f"'{name}' must not contain control or backslash characters"
        )
    return value


def _validate_vocab(
    value: Any,
    allowed: frozenset[str],
    *,
    name: str,
    error: type[ScopedAuditValidationError] = ScopedAuditValidationError,
) -> str:
    if value not in allowed:
        raise error(
            f"unrecognised {name}: {value!r}; "
            f"expected one of {sorted(allowed)}"
        )
    return value


def _ensure_tuple(
    value: Any, *, name: str, max_size: int = MAX_COLLECTION_SIZE
) -> tuple[Any, ...]:
    if not isinstance(value, tuple):
        raise ScopedAuditValidationError(
            f"'{name}' must be a tuple, got {type(value).__name__}"
        )
    if len(value) > max_size:
        raise ScopedAuditValidationError(
            f"'{name}' exceeds maximum size {max_size}"
        )
    return value


def _ensure_unique(items: tuple[Any, ...], *, section: str) -> None:
    seen: set[str] = set()
    for item in items:
        ident = item.identity
        if ident in seen:
            raise DuplicateIdentityError(
                f"duplicate identity {ident!r} in section {section!r}"
            )
        seen.add(ident)


def _check_line_span(line_start: Any, line_end: Any) -> None:
    if (
        not isinstance(line_start, int)
        or isinstance(line_start, bool)
        or line_start < 1
    ):
        raise ScopedAuditValidationError(
            "'line_start' must be a positive integer"
        )
    if not isinstance(line_end, int) or isinstance(line_end, bool):
        raise ScopedAuditValidationError("'line_end' must be an integer")
    if line_end < line_start:
        raise ScopedAuditValidationError(
            "'line_end' must be greater or equal to 'line_start'"
        )


T = TypeVar("T")


def _sorted(
    items: tuple[T, ...], key: Callable[[T], str]
) -> tuple[T, ...]:
    return tuple(sorted(items, key=key))


@dataclass(frozen=True, slots=True)
class TargetSymbol:
    """A bounded qualified graph symbol that defines the review scope."""

    qualified_name: str
    symbol_kind: str

    def __post_init__(self) -> None:
        _ensure_text(self.qualified_name, "qualified_name")
        _validate_vocab(
            self.symbol_kind, ALLOWED_SYMBOL_KINDS, name="symbol_kind"
        )

    @property
    def identity(self) -> str:
        return self.qualified_name

    @property
    def sort_key(self) -> str:
        return self.qualified_name

    def as_json(self) -> dict[str, str]:
        return {
            "qualified_name": self.qualified_name,
            "symbol_kind": self.symbol_kind,
        }


@dataclass(frozen=True, slots=True)
class ChangedPath:
    """A repository-relative path touched by the change under review."""

    path: str
    change_kind: str
    line_start: int = 1
    line_end: int = 1

    def __post_init__(self) -> None:
        _validate_path(self.path)
        _validate_vocab(
            self.change_kind, ALLOWED_CHANGE_KINDS, name="change_kind"
        )
        _check_line_span(self.line_start, self.line_end)

    @property
    def identity(self) -> str:
        return self.path

    @property
    def sort_key(self) -> str:
        return self.path

    def as_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "change_kind": self.change_kind,
            "line_start": self.line_start,
            "line_end": self.line_end,
        }


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """Bounded evidence attached to impact/test/contract sections."""

    identity: str
    evidence_kind: str
    evidence_level: EvidenceLevel
    path: str
    line_start: int
    line_end: int
    description: str
    supports_blocker: bool = False

    def __post_init__(self) -> None:
        _ensure_identity(self.identity)
        _validate_vocab(
            self.evidence_kind, ALLOWED_EVIDENCE_KINDS, name="evidence_kind"
        )
        if not isinstance(self.evidence_level, EvidenceLevel):
            raise ScopedAuditValidationError(
                "'evidence_level' must be an EvidenceLevel instance"
            )
        _validate_path(self.path)
        _check_line_span(self.line_start, self.line_end)
        _ensure_text(self.description, "description")
        if not isinstance(self.supports_blocker, bool):
            raise ScopedAuditValidationError(
                "'supports_blocker' must be a bool"
            )

    @property
    def sort_key(self) -> str:
        return self.identity

    def as_json(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "evidence_kind": self.evidence_kind,
            "evidence_level": self.evidence_level.name.lower(),
            "path": self.path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "description": self.description,
            "supports_blocker": self.supports_blocker,
        }


@dataclass(frozen=True, slots=True)
class PriorLesson:
    """A verified prior lesson reused as review evidence."""

    identity: str
    source: str
    evidence_level: EvidenceLevel
    summary: str

    def __post_init__(self) -> None:
        _ensure_identity(self.identity)
        _ensure_text(self.source, "source")
        if not isinstance(self.evidence_level, EvidenceLevel):
            raise ScopedAuditValidationError(
                "'evidence_level' must be an EvidenceLevel instance"
            )
        _ensure_text(self.summary, "summary")

    @property
    def sort_key(self) -> str:
        return self.identity

    def as_json(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "source": self.source,
            "evidence_level": self.evidence_level.name.lower(),
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class KnownUnknown:
    """An explicit open question the reviewer must consider."""

    identity: str
    question: str
    why_relevant: str

    def __post_init__(self) -> None:
        _ensure_identity(self.identity)
        _ensure_text(self.question, "question")
        _ensure_text(self.why_relevant, "why_relevant")

    @property
    def sort_key(self) -> str:
        return self.identity

    def as_json(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "question": self.question,
            "why_relevant": self.why_relevant,
        }


@dataclass(frozen=True, slots=True)
class ValidationExpectation:
    """A specific validation command the reviewer should expect to hold."""

    identity: str
    validation_kind: str
    command: str
    expected_outcome: str

    def __post_init__(self) -> None:
        _ensure_identity(self.identity)
        _validate_vocab(
            self.validation_kind,
            ALLOWED_VALIDATION_KINDS,
            name="validation_kind",
        )
        _ensure_text(self.command, "command")
        _ensure_text(self.expected_outcome, "expected_outcome")

    @property
    def sort_key(self) -> str:
        return self.identity

    def as_json(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "validation_kind": self.validation_kind,
            "command": self.command,
            "expected_outcome": self.expected_outcome,
        }


@dataclass(frozen=True, slots=True)
class ReviewLens:
    """The single explicit lens the reviewer must apply."""

    lens_kind: str
    rationale: str
    required_evidence_level: EvidenceLevel = EvidenceLevel.STATIC_EVIDENCE

    def __post_init__(self) -> None:
        _validate_vocab(
            self.lens_kind,
            ALLOWED_REVIEW_LENSES,
            name="review_lens_kind",
            error=UnknownLensError,
        )
        _ensure_text(self.rationale, "rationale", max_len=MAX_RATIONALE_LENGTH)
        if not isinstance(self.required_evidence_level, EvidenceLevel):
            raise ScopedAuditValidationError(
                "'required_evidence_level' must be an EvidenceLevel instance"
            )

    def as_json(self) -> dict[str, object]:
        return {
            "lens_kind": self.lens_kind,
            "rationale": self.rationale,
            "required_evidence_level": (
                self.required_evidence_level.name.lower()
            ),
        }


@dataclass(frozen=True, slots=True)
class ScopedAuditPacket:
    """The complete, fail-closed, deterministic graph-scoped audit packet."""

    packet_id: str
    task_id: str
    created_at: str
    target_symbols: tuple[TargetSymbol, ...]
    changed_paths: tuple[ChangedPath, ...]
    forbidden_changes: tuple[str, ...]
    invariants: tuple[str, ...]
    impact_evidence: tuple[EvidenceReference, ...]
    test_evidence: tuple[EvidenceReference, ...]
    contract_evidence: tuple[EvidenceReference, ...]
    prior_lessons: tuple[PriorLesson, ...]
    review_lens: ReviewLens
    unknowns: tuple[KnownUnknown, ...]
    validation_expectations: tuple[ValidationExpectation, ...]

    def __post_init__(self) -> None:
        _ensure_identity(self.packet_id, name="packet_id")
        _ensure_identity(self.task_id, name="task_id")
        _ensure_text(self.created_at, "created_at")

        for header in (
            "target_symbols",
            "changed_paths",
            "forbidden_changes",
            "invariants",
            "impact_evidence",
            "test_evidence",
            "contract_evidence",
            "prior_lessons",
            "unknowns",
            "validation_expectations",
        ):
            _ensure_tuple(getattr(self, header), name=header)

        for fc in self.forbidden_changes:
            _ensure_text(fc, "forbidden_change")
        for inv in self.invariants:
            _ensure_text(inv, "invariant")

        _ensure_unique(self.target_symbols, section="target_symbols")
        _ensure_unique(self.changed_paths, section="changed_paths")
        _ensure_unique(self.impact_evidence, section="impact_evidence")
        _ensure_unique(self.test_evidence, section="test_evidence")
        _ensure_unique(self.contract_evidence, section="contract_evidence")
        _ensure_unique(self.prior_lessons, section="prior_lessons")
        _ensure_unique(self.unknowns, section="unknowns")
        _ensure_unique(
            self.validation_expectations,
            section="validation_expectations",
        )

        # Identifiers must be globally unique across evidence sections.
        all_evidence = self._all_evidence()
        _ensure_unique(all_evidence, section="evidence")

        # Reject repository dumps: packet must declare scope, changes and
        # validation expectations explicitly.
        if not self.target_symbols:
            raise ScopedAuditValidationError(
                "packet must declare at least one target symbol"
            )
        if not self.changed_paths:
            raise ScopedAuditValidationError(
                "packet must declare at least one changed path"
            )
        if not self.validation_expectations:
            raise ScopedAuditValidationError(
                "packet must declare at least one validation expectation"
            )

        # Blocker-grade evidence must satisfy the lens-required level.
        for evidence in all_evidence:
            if (
                evidence.supports_blocker
                and evidence.evidence_level
                < self.review_lens.required_evidence_level
            ):
                raise UngroundedBlockerError(
                    f"evidence {evidence.identity!r} supports a blocker "
                    f"but its evidence level "
                    f"{evidence.evidence_level.name.lower()} is below the "
                    f"lens-required level "
                    f"{self.review_lens.required_evidence_level.name.lower()}"
                )

        self._apply(
            "target_symbols",
            _sorted(self.target_symbols, lambda s: s.sort_key),
        )
        self._apply(
            "changed_paths",
            _sorted(self.changed_paths, lambda s: s.sort_key),
        )
        self._apply(
            "forbidden_changes", tuple(sorted(self.forbidden_changes))
        )
        self._apply("invariants", tuple(sorted(self.invariants)))
        self._apply(
            "impact_evidence",
            _sorted(self.impact_evidence, lambda s: s.sort_key),
        )
        self._apply(
            "test_evidence",
            _sorted(self.test_evidence, lambda s: s.sort_key),
        )
        self._apply(
            "contract_evidence",
            _sorted(self.contract_evidence, lambda s: s.sort_key),
        )
        self._apply(
            "prior_lessons",
            _sorted(self.prior_lessons, lambda s: s.sort_key),
        )
        self._apply(
            "unknowns", _sorted(self.unknowns, lambda s: s.sort_key)
        )
        self._apply(
            "validation_expectations",
            _sorted(self.validation_expectations, lambda s: s.sort_key),
        )

    def _apply(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)

    def _all_evidence(self) -> tuple[EvidenceReference, ...]:
        return (
            self.impact_evidence
            + self.test_evidence
            + self.contract_evidence
        )

    def as_json(self) -> dict[str, object]:
        """Render the packet as a JSON-serialisable mapping."""
        return {
            "packet_id": self.packet_id,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "target_symbols": [s.as_json() for s in self.target_symbols],
            "changed_paths": [p.as_json() for p in self.changed_paths],
            "forbidden_changes": list(self.forbidden_changes),
            "invariants": list(self.invariants),
            "impact_evidence": [e.as_json() for e in self.impact_evidence],
            "test_evidence": [e.as_json() for e in self.test_evidence],
            "contract_evidence": [
                e.as_json() for e in self.contract_evidence
            ],
            "prior_lessons": [l.as_json() for l in self.prior_lessons],
            "review_lens": self.review_lens.as_json(),
            "unknowns": [u.as_json() for u in self.unknowns],
            "validation_expectations": [
                v.as_json() for v in self.validation_expectations
            ],
        }


def _validate_module_path(value: Any, *, name: str = "module_path") -> str:
    path = _validate_path(value, name=name)
    if any(part in {"", "."} for part in path.split("/")):
        raise MalformedPathError(f"'{name}' must be a normalized repo-relative path")
    if not path.endswith(".py"):
        raise MalformedPathError(f"'{name}' must identify a Python module")
    return path


@dataclass(frozen=True, slots=True)
class AuditCoverageRecord:
    """Immutable proof that one audit pass examined one module revision."""

    module_path: str
    source_revision: str
    audit_pass_identity: str
    successful: bool
    finding_count: int

    def __post_init__(self) -> None:
        _validate_module_path(self.module_path)
        _ensure_identity(self.source_revision, name="source_revision")
        _ensure_identity(self.audit_pass_identity, name="audit_pass_identity")
        if not isinstance(self.successful, bool):
            raise ScopedAuditValidationError("'successful' must be a bool")
        if (
            not isinstance(self.finding_count, int)
            or isinstance(self.finding_count, bool)
            or self.finding_count < 0
            or self.finding_count > MAX_AUDIT_FINDING_COUNT
        ):
            raise ScopedAuditValidationError(
                "'finding_count' must be a bounded nonnegative integer"
            )

    @property
    def identity(self) -> tuple[str, str, str]:
        return (
            self.module_path,
            self.source_revision,
            self.audit_pass_identity,
        )


@dataclass(frozen=True, slots=True)
class AuditModuleCoverage:
    """Deterministic coverage result for one expected module."""

    module_path: str
    source_revision: str
    status: str
    finding_count: int
    audit_pass_identities: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_module_path(self.module_path)
        _ensure_identity(self.source_revision, name="source_revision")
        _validate_vocab(
            self.status, AUDIT_COVERAGE_STATUSES, name="audit_coverage_status"
        )
        if (
            not isinstance(self.finding_count, int)
            or isinstance(self.finding_count, bool)
            or self.finding_count < 0
            or self.finding_count > MAX_AUDIT_FINDING_COUNT
        ):
            raise ScopedAuditValidationError(
                "'finding_count' must be a bounded nonnegative integer"
            )
        identities = _ensure_tuple(
            self.audit_pass_identities, name="audit_pass_identities"
        )
        for identity in identities:
            _ensure_identity(identity, name="audit_pass_identity")
        if len(set(identities)) != len(identities):
            raise DuplicateIdentityError("duplicate audit-pass identity")
        if self.status == "UNKNOWN" and (
            self.finding_count != 0 or identities
        ):
            raise ScopedAuditValidationError(
                "UNKNOWN coverage must not claim findings or audit passes"
            )
        if self.status == "AUDITED" and not identities:
            raise ScopedAuditValidationError(
                "AUDITED coverage requires a successful audit pass"
            )

    def as_json(self) -> dict[str, object]:
        return {
            "module_path": self.module_path,
            "source_revision": self.source_revision,
            "status": self.status,
            "finding_count": self.finding_count,
            "audit_pass_identities": list(self.audit_pass_identities),
        }


@dataclass(frozen=True, slots=True)
class AuditCoverageReport:
    """Bounded coverage report for an explicit module set and revision."""

    source_revision: str
    modules: tuple[AuditModuleCoverage, ...]

    def __post_init__(self) -> None:
        _ensure_identity(self.source_revision, name="source_revision")
        modules = _ensure_tuple(self.modules, name="modules")
        paths: set[str] = set()
        for module in modules:
            if not isinstance(module, AuditModuleCoverage):
                raise ScopedAuditValidationError(
                    "'modules' entries must be AuditModuleCoverage instances"
                )
            if module.source_revision != self.source_revision:
                raise ScopedAuditValidationError(
                    "module coverage revision must match report revision"
                )
            if module.module_path in paths:
                raise DuplicateIdentityError("duplicate module coverage")
            paths.add(module.module_path)
        object.__setattr__(
            self,
            "modules",
            _sorted(modules, lambda module: module.module_path),
        )

    def as_json(self) -> dict[str, object]:
        return {
            "source_revision": self.source_revision,
            "modules": [module.as_json() for module in self.modules],
        }


def build_audit_coverage_report(
    *,
    expected_modules: tuple[str, ...],
    source_revision: str,
    records: tuple[AuditCoverageRecord, ...],
) -> AuditCoverageReport:
    """Report only successful, exact-revision audit passes as ``AUDITED``."""
    _ensure_identity(source_revision, name="source_revision")
    _ensure_tuple(expected_modules, name="expected_modules")
    _ensure_tuple(records, name="records")
    if not expected_modules:
        raise ScopedAuditValidationError("'expected_modules' must not be empty")

    normalized_modules = tuple(
        _validate_module_path(path, name="expected_module")
        for path in expected_modules
    )
    if len(set(normalized_modules)) != len(normalized_modules):
        raise DuplicateIdentityError("duplicate expected module")

    seen_records: set[tuple[str, str, str]] = set()
    for record in records:
        if not isinstance(record, AuditCoverageRecord):
            raise ScopedAuditValidationError(
                "'records' entries must be AuditCoverageRecord instances"
            )
        if record.identity in seen_records:
            raise DuplicateIdentityError(
                "duplicate module/revision/audit-pass identity"
            )
        seen_records.add(record.identity)

    results: list[AuditModuleCoverage] = []
    for module_path in sorted(normalized_modules):
        matching = sorted(
            (
                record
                for record in records
                if record.module_path == module_path
                and record.source_revision == source_revision
                and record.successful
            ),
            key=lambda record: record.audit_pass_identity,
        )
        finding_count = sum(record.finding_count for record in matching)
        if finding_count > MAX_AUDIT_FINDING_COUNT:
            raise ScopedAuditValidationError(
                "aggregated finding count exceeds maximum "
                f"{MAX_AUDIT_FINDING_COUNT}"
            )
        results.append(
            AuditModuleCoverage(
                module_path=module_path,
                source_revision=source_revision,
                status="AUDITED" if matching else "UNKNOWN",
                finding_count=finding_count,
                audit_pass_identities=tuple(
                    record.audit_pass_identity for record in matching
                ),
            )
        )
    return AuditCoverageReport(source_revision, tuple(results))


def canonical_json(packet: ScopedAuditPacket) -> str:
    """Return the deterministic canonical JSON serialisation of ``packet``."""
    return json.dumps(
        packet.as_json(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def packet_fingerprint(packet: ScopedAuditPacket) -> str:
    """Return a stable SHA-256 fingerprint of ``packet``'s canonical JSON."""
    return hashlib.sha256(
        canonical_json(packet).encode("utf-8")
    ).hexdigest()
