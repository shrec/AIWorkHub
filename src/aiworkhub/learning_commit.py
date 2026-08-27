"""Learning Commit value contract for manager-adjudicated outcomes.

Defines a bounded distillation record containing task identity, repository area,
outcome, evidence identities, verified root cause/invariant/lesson candidates,
Context Graph edge candidates, and promotion eligibility. Only accepted
manager-verified evidence may mark AI Memory or causal-edge promotion eligible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from .evidence_levels import (
    InvalidReferenceSchemeError,
    EmptyReferencePathError,
    UnsafeFilePathError,
    ReferenceTooLongError,
)


class Outcome(Enum):
    """Manager-adjudicated outcome of a task."""
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class FailureCategory(Enum):
    """Canonical, closed taxonomy for a finalized task's failure signal.

    Classification (see :func:`classify_failure_category`) consults only the
    worker's own structured terminal substatus and a provider-sealed
    structured diagnostic -- never manager, assistant or provider free-form
    prose -- so a rejection reason or root-cause candidate written by a model
    can never spoof its own category.
    """
    CANDIDATE_CODE = "candidate_code"
    VALIDATION_ENVIRONMENT = "validation_environment"
    PROVIDER_RUNTIME = "provider_runtime"
    DEPENDENCY_OR_ROUTE = "dependency_or_route"
    POLICY_OR_SCOPE = "policy_or_scope"
    CANCELLATION_OR_TIMEOUT = "cancellation_or_timeout"
    INCONCLUSIVE = "inconclusive"


# Substatus vocabulary is the worker's own structured terminal report (never
# manager/assistant prose).  "review_ready" is included here because these
# groups are consulted only when the finalization outcome is *not* accepted:
# a manager-rejected task whose worker reported review_ready means the worker
# believed it succeeded but a human found a genuine candidate defect.
_CODE_QUALITY_TERMINAL_SUBSTATUSES: FrozenSet[str] = frozenset({
    "review_ready", "validation_failed",
})
_ENVIRONMENT_TERMINAL_SUBSTATUSES: FrozenSet[str] = frozenset({"finalize_failed"})
_PROVIDER_RUNTIME_TERMINAL_SUBSTATUSES: FrozenSet[str] = frozenset({
    "launch_failed", "worker_failed", "process_lost", "liveness_lost",
})
_CANCELLATION_TERMINAL_SUBSTATUSES: FrozenSet[str] = frozenset({"timed_out", "cancelled"})
_POLICY_TERMINAL_SUBSTATUSES: FrozenSet[str] = frozenset({"output_budget_exceeded"})

# Single canonical source of "this terminal substatus is an infrastructure
# failure, not a candidate-code failure" -- reused by workforce_catalog so
# code-quality rates and learning-commit classification never disagree.
INFRASTRUCTURE_TERMINAL_SUBSTATUSES: FrozenSet[str] = frozenset(
    _PROVIDER_RUNTIME_TERMINAL_SUBSTATUSES
    | _CANCELLATION_TERMINAL_SUBSTATUSES
    | _ENVIRONMENT_TERMINAL_SUBSTATUSES
)

INFRASTRUCTURE_FAILURE_CATEGORIES: FrozenSet[FailureCategory] = frozenset({
    FailureCategory.PROVIDER_RUNTIME,
    FailureCategory.DEPENDENCY_OR_ROUTE,
    FailureCategory.CANCELLATION_OR_TIMEOUT,
    FailureCategory.VALIDATION_ENVIRONMENT,
})
CODE_QUALITY_FAILURE_CATEGORIES: FrozenSet[FailureCategory] = frozenset({
    FailureCategory.CANDIDATE_CODE,
})

_SEALED_PROVIDER_QUOTA_CODES: FrozenSet[str] = frozenset({
    "insufficient_balance", "insufficient_quota",
    "balance_exhausted", "quota_exhausted",
})
_SEALED_PROVIDER_AUTH_CODES: FrozenSet[str] = frozenset({
    "invalid_grant", "unknown_refresh_token",
    "invalid_api_key", "unauthorized",
    "authentication_failed", "authorization_failed",
})
_SEALED_PROVIDER_DEPENDENCY_CODES: FrozenSet[str] = frozenset({
    "dependency_unavailable", "route_unavailable",
    "upstream_unavailable", "mcp_unavailable",
})


def _sealed_provider_diagnostic(value: Any) -> Optional[Dict[str, Any]]:
    """Return ``value`` only if the provider transport sealed it itself.

    ``owner`` must be exactly ``"provider"`` and ``sealed`` exactly ``True``.
    Any other shape -- including a structured-looking dict an assistant wrote
    about itself -- is untrusted and ignored, so substring/dict spoofing
    cannot forge a classification.
    """
    if not isinstance(value, dict):
        return None
    if str(value.get("owner") or "").strip().casefold() != "provider":
        return None
    if value.get("sealed") is not True:
        return None
    return value


def classify_failure_category(
    *,
    terminal_substatus: Optional[str] = None,
    sealed_diagnostics: Optional[Dict[str, Any]] = None,
) -> FailureCategory:
    """Classify one finalized task's failure into the canonical taxonomy.

    Only the worker's own structured ``terminal_substatus`` and a
    provider-sealed structured diagnostic are consulted.  Free-form prose (a
    manager's reject reason, a root-cause candidate, an assistant's
    self-reported error text) is never scanned.
    """
    substatus = str(terminal_substatus or "").strip().lower()
    sealed = _sealed_provider_diagnostic(sealed_diagnostics)
    if sealed is not None:
        code = str(sealed.get("code") or "").strip().casefold()
        status = sealed.get("http_status")
        status_code = status if isinstance(status, int) and not isinstance(status, bool) else 0
        if (
            status_code in (401, 402, 403)
            or code in _SEALED_PROVIDER_QUOTA_CODES
            or code in _SEALED_PROVIDER_AUTH_CODES
        ):
            return FailureCategory.PROVIDER_RUNTIME
        if code in _SEALED_PROVIDER_DEPENDENCY_CODES:
            return FailureCategory.DEPENDENCY_OR_ROUTE
    if substatus in _CODE_QUALITY_TERMINAL_SUBSTATUSES:
        return FailureCategory.CANDIDATE_CODE
    if substatus in _ENVIRONMENT_TERMINAL_SUBSTATUSES:
        return FailureCategory.VALIDATION_ENVIRONMENT
    if substatus in _PROVIDER_RUNTIME_TERMINAL_SUBSTATUSES:
        return FailureCategory.PROVIDER_RUNTIME
    if substatus in _POLICY_TERMINAL_SUBSTATUSES:
        return FailureCategory.POLICY_OR_SCOPE
    if substatus in _CANCELLATION_TERMINAL_SUBSTATUSES:
        return FailureCategory.CANCELLATION_OR_TIMEOUT
    return FailureCategory.INCONCLUSIVE


@dataclass(frozen=True)
class EdgeCandidate:
    """A proposed Context Graph edge connecting two nodes with a relation."""
    source: str
    target: str
    relation: str

    def __post_init__(self) -> None:
        _bounded_text(self.source, "EdgeCandidate.source", maximum=256)
        _bounded_text(self.target, "EdgeCandidate.target", maximum=256)
        _bounded_text(self.relation, "EdgeCandidate.relation", maximum=64)
        if not _RELATION_PATTERN.fullmatch(self.relation):
            raise ValueError("EdgeCandidate.relation has an invalid format")
        # Prevent self-loops (unbounded edges)
        if self.source == self.target:
            raise ValueError("EdgeCandidate source and target must differ")


ALLOWED_COMMIT_FIELDS: FrozenSet[str] = frozenset({
    "task_id",
    "repository_id",
    "repo_area",
    "outcome",
    "failure_category",
    "evidence_ids",
    "root_cause_candidate",
    "invariant_candidate",
    "lesson_candidate",
    "edge_candidates",
    "promotion_eligible_ai_memory",
    "promotion_eligible_context_graph",
    "promotion_eligible_kb",
})

_MAX_REF_LEN = 1024
_ALLOWED_SCHEMES: FrozenSet[str] = frozenset({ "file", "http", "https" })
_SCHEME_PATTERN = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*$')
_RELATION_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
_MAX_EVIDENCE_IDS = 32
_MAX_EDGE_CANDIDATES = 16
_MAX_TEXT_BYTES = 16 * 1024


def _bounded_text(value: str, field_name: str, *, maximum: int = _MAX_TEXT_BYTES) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if "\x00" in value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field_name} is invalid or too large")


def _validate_evidence_id(ref: str) -> None:
    """Validate a single evidence id string.

    Raises:
        InvalidReferenceSchemeError: missing or unrecognised scheme.
        EmptyReferencePathError: recognised scheme but empty rest.
        UnsafeFilePathError: ``file:`` scheme with ``..`` or backslash.
        ReferenceTooLongError: string longer than *_MAX_REF_LEN*.
    """
    if not isinstance(ref, str):
        raise ValueError("evidence id must be a string")
    if len(ref) > _MAX_REF_LEN:
        raise ReferenceTooLongError(
            f"reference length ({len(ref)}) exceeds maximum allowed ({_MAX_REF_LEN})"
        )
    if ':' not in ref:
        raise InvalidReferenceSchemeError("reference is missing a scheme")
    scheme, rest = ref.split(':', 1)
    if not _SCHEME_PATTERN.match(scheme):
        raise InvalidReferenceSchemeError(f"unrecognised scheme: {scheme}")
    if scheme not in _ALLOWED_SCHEMES:
        raise InvalidReferenceSchemeError(f"scheme not allowed: {scheme}")
    if rest == "":
        raise EmptyReferencePathError(f"empty path after scheme in reference: {ref!r}")
    if scheme == "file":
        # Reject backslashes.
        if '\\' in rest:
            raise UnsafeFilePathError(f"unsafe file path in reference: {ref!r}")
        # Reject absolute paths.
        if rest.startswith('/'):
            raise UnsafeFilePathError(f"unsafe file path in reference: {ref!r}")
        # Reject cross-repo / injection markers.
        if ':' in rest:
            raise UnsafeFilePathError(f"unsafe file path in reference: {ref!r}")
        # Reject malformed empty segments and traversal (segment exactly '..').
        segments = rest.split('/')
        for seg in segments:
            if seg == '':
                raise UnsafeFilePathError(f"unsafe file path in reference: {ref!r}")
            if seg == '..':
                raise UnsafeFilePathError(f"unsafe file path in reference: {ref!r}")


@dataclass(frozen=True)
class LearningCommit:
    """Immutable learning commit record produced after manager adjudication."""

    task_id: str
    repo_area: str
    outcome: Outcome
    failure_category: Optional[FailureCategory] = None
    evidence_ids: Tuple[str, ...] = field(default_factory=tuple)
    root_cause_candidate: Optional[str] = None
    invariant_candidate: Optional[str] = None
    lesson_candidate: Optional[str] = None
    edge_candidates: Tuple[EdgeCandidate, ...] = field(default_factory=tuple)
    promotion_eligible_ai_memory: bool = False
    promotion_eligible_context_graph: bool = False
    promotion_eligible_kb: bool = False
    repository_id: Optional[str] = None

    def __post_init__(self) -> None:
        _bounded_text(self.task_id, "task_id", maximum=256)
        _bounded_text(self.repo_area, "repo_area", maximum=256)
        if self.repository_id is not None:
            _bounded_text(self.repository_id, "repository_id", maximum=256)
        if not isinstance(self.outcome, Outcome):
            raise ValueError(f"outcome must be an Outcome, got {type(self.outcome).__name__}")
        if self.failure_category is not None:
            if not isinstance(self.failure_category, FailureCategory):
                raise ValueError(
                    "failure_category must be a FailureCategory, got "
                    f"{type(self.failure_category).__name__}"
                )
            if self.outcome == Outcome.ACCEPTED:
                raise ValueError("failure_category must be None for outcome ACCEPTED")

        evidence_ids = self.evidence_ids
        if not isinstance(evidence_ids, (list, tuple)):
            raise ValueError("evidence_ids must be a list or tuple")
        if len(evidence_ids) > _MAX_EVIDENCE_IDS:
            raise ValueError("too many evidence_ids")
        validated_evidence: list[str] = []
        for idx, eid in enumerate(evidence_ids):
            if not isinstance(eid, str):
                raise ValueError(
                    f"evidence_ids[{idx}] must be a string, got {type(eid).__name__}"
                )
            _validate_evidence_id(eid)
            validated_evidence.append(eid)
        object.__setattr__(self, 'evidence_ids', tuple(validated_evidence))

        for attr_name in (
            "root_cause_candidate", "invariant_candidate", "lesson_candidate"
        ):
            val = getattr(self, attr_name)
            if val is not None:
                _bounded_text(val, attr_name)

        edge_candidates = self.edge_candidates
        if not isinstance(edge_candidates, (list, tuple)):
            raise ValueError("edge_candidates must be a list or tuple")
        if len(edge_candidates) > _MAX_EDGE_CANDIDATES:
            raise ValueError("too many edge_candidates")
        validated_edges: list[EdgeCandidate] = []
        for idx, edge in enumerate(edge_candidates):
            if not isinstance(edge, EdgeCandidate):
                raise ValueError(
                    f"edge_candidates[{idx}] must be an EdgeCandidate, "
                    f"got {type(edge).__name__}"
                )
            validated_edges.append(edge)
        object.__setattr__(self, 'edge_candidates', tuple(validated_edges))

        if self.promotion_eligible_ai_memory:
            if self.outcome != Outcome.ACCEPTED:
                raise ValueError("promotion_eligible_ai_memory requires outcome ACCEPTED")
            if not self.evidence_ids:
                raise ValueError("promotion_eligible_ai_memory requires at least one evidence_id")
        if self.promotion_eligible_context_graph:
            if self.outcome != Outcome.ACCEPTED:
                raise ValueError("promotion_eligible_context_graph requires outcome ACCEPTED")
            if not self.evidence_ids:
                raise ValueError("promotion_eligible_context_graph requires at least one evidence_id")
        if self.promotion_eligible_kb:
            if self.outcome != Outcome.ACCEPTED:
                raise ValueError("promotion_eligible_kb requires outcome ACCEPTED")
            if not self.evidence_ids:
                raise ValueError("promotion_eligible_kb requires at least one evidence_id")


def validate_repo_match(commit: LearningCommit, repository_id: str) -> None:
    """Reject cross-repository commits."""
    if not isinstance(repository_id, str):
        raise TypeError("repository_id must be a string")
    asserted_repository_id = commit.repository_id or commit.repo_area
    if asserted_repository_id != repository_id:
        raise ValueError(
            f"Cross-repository commit: repository_id={asserted_repository_id!r} "
            f"does not match repository_id={repository_id!r}"
        )


def learning_commit_from_dict(data: Dict[str, Any]) -> LearningCommit:
    """Construct a LearningCommit from a dictionary, rejecting unknown fields."""
    if not isinstance(data, dict):
        raise TypeError("data must be a dictionary")

    extra = set(data.keys()) - ALLOWED_COMMIT_FIELDS
    if extra:
        raise ValueError(f"Unknown fields in commit data: {', '.join(sorted(extra))}")

    required = {"task_id", "repo_area", "outcome"}
    missing = required - set(data.keys())
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")

    outcome_raw = data["outcome"]
    if isinstance(outcome_raw, Outcome):
        outcome = outcome_raw
    elif isinstance(outcome_raw, str):
        try:
            outcome = Outcome(outcome_raw.lower())
        except ValueError:
            raise ValueError(
                f"Invalid outcome value: {outcome_raw!r}; "
                f"expected one of {[o.value for o in Outcome]}"
            )
    else:
        raise TypeError(
            f"'outcome' must be an Outcome or string, got {type(outcome_raw).__name__}"
        )

    category_raw = data.get("failure_category")
    if category_raw is None:
        failure_category: Optional[FailureCategory] = None
    elif isinstance(category_raw, FailureCategory):
        failure_category = category_raw
    elif isinstance(category_raw, str):
        try:
            failure_category = FailureCategory(category_raw.lower())
        except ValueError:
            raise ValueError(
                f"Invalid failure_category value: {category_raw!r}; "
                f"expected one of {[c.value for c in FailureCategory]}"
            )
    else:
        raise TypeError(
            "'failure_category' must be a FailureCategory or string, got "
            f"{type(category_raw).__name__}"
        )

    raw_edges = data.get("edge_candidates", [])
    if not isinstance(raw_edges, list):
        raise ValueError("edge_candidates must be a list")
    edges: List[EdgeCandidate] = []
    for idx, item in enumerate(raw_edges):
        if isinstance(item, EdgeCandidate):
            edges.append(item)
        elif isinstance(item, dict):
            try:
                edges.append(EdgeCandidate(**item))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid edge_candidates[{idx}]: {exc}"
                ) from exc
        else:
            raise ValueError(
                f"edge_candidates[{idx}] must be an EdgeCandidate or dict, "
                f"got {type(item).__name__}"
            )

    return LearningCommit(
        task_id=data["task_id"],
        repo_area=data["repo_area"],
        outcome=outcome,
        failure_category=failure_category,
        repository_id=data.get("repository_id"),
        evidence_ids=data.get("evidence_ids", []),
        root_cause_candidate=data.get("root_cause_candidate"),
        invariant_candidate=data.get("invariant_candidate"),
        lesson_candidate=data.get("lesson_candidate"),
        edge_candidates=tuple(edges),
        promotion_eligible_ai_memory=data.get("promotion_eligible_ai_memory", False),
        promotion_eligible_context_graph=data.get("promotion_eligible_context_graph", False),
        promotion_eligible_kb=data.get("promotion_eligible_kb", False),
    )
