"""Pure Learning Commit contract for manager-adjudicated outcomes.

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


@dataclass(frozen=True)
class EdgeCandidate:
    """A proposed Context Graph edge connecting two nodes with a relation."""
    source: str
    target: str
    relation: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("EdgeCandidate.source must be a non-empty string")
        if not isinstance(self.target, str) or not self.target.strip():
            raise ValueError("EdgeCandidate.target must be a non-empty string")
        if not isinstance(self.relation, str) or not self.relation.strip():
            raise ValueError("EdgeCandidate.relation must be a non-empty string")
        # Prevent self-loops (unbounded edges)
        if self.source == self.target:
            raise ValueError("EdgeCandidate source and target must differ")


ALLOWED_COMMIT_FIELDS: FrozenSet[str] = frozenset({
    "task_id",
    "repo_area",
    "outcome",
    "evidence_ids",
    "root_cause_candidate",
    "invariant_candidate",
    "lesson_candidate",
    "edge_candidates",
    "promotion_eligible_ai_memory",
    "promotion_eligible_context_graph",
})

_MAX_REF_LEN = 1024
_ALLOWED_SCHEMES: FrozenSet[str] = frozenset({ "file", "http", "https" })
_SCHEME_PATTERN = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*$')


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
    evidence_ids: Tuple[str, ...] = field(default_factory=tuple)
    root_cause_candidate: Optional[str] = None
    invariant_candidate: Optional[str] = None
    lesson_candidate: Optional[str] = None
    edge_candidates: Tuple[EdgeCandidate, ...] = field(default_factory=tuple)
    promotion_eligible_ai_memory: bool = False
    promotion_eligible_context_graph: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(self.repo_area, str) or not self.repo_area.strip():
            raise ValueError("repo_area must be a non-empty string")
        if not isinstance(self.outcome, Outcome):
            raise ValueError(f"outcome must be an Outcome, got {type(self.outcome).__name__}")

        evidence_ids = self.evidence_ids
        if not isinstance(evidence_ids, (list, tuple)):
            raise ValueError("evidence_ids must be a list or tuple")
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
            if val is not None and (
                not isinstance(val, str) or not val.strip()
            ):
                raise ValueError(f"{attr_name} must be None or a non-empty string")

        edge_candidates = self.edge_candidates
        if not isinstance(edge_candidates, (list, tuple)):
            raise ValueError("edge_candidates must be a list or tuple")
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


def validate_repo_match(commit: LearningCommit, repository_id: str) -> None:
    """Reject cross-repository commits."""
    if not isinstance(repository_id, str):
        raise TypeError("repository_id must be a string")
    if commit.repo_area != repository_id:
        raise ValueError(
            f"Cross-repository commit: repo_area={commit.repo_area!r} "
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
        evidence_ids=data.get("evidence_ids", []),
        root_cause_candidate=data.get("root_cause_candidate"),
        invariant_candidate=data.get("invariant_candidate"),
        lesson_candidate=data.get("lesson_candidate"),
        edge_candidates=tuple(edges),
        promotion_eligible_ai_memory=data.get("promotion_eligible_ai_memory", False),
        promotion_eligible_context_graph=data.get("promotion_eligible_context_graph", False),
    )
