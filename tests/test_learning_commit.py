"""Tests for the Learning Commit contract."""

from __future__ import annotations

import pytest

from aiworkhub.learning_commit import (
    LearningCommit,
    Outcome,
    EdgeCandidate,
    validate_repo_match,
    learning_commit_from_dict,
)
from aiworkhub.evidence_levels import (
    InvalidReferenceSchemeError,
    EmptyReferencePathError,
    UnsafeFilePathError,
    ReferenceTooLongError,
)

@pytest.fixture
def valid_accepted_commit() -> LearningCommit:
    return LearningCommit(
        task_id="task-123",
        repo_area="repo-abc",
        outcome=Outcome.ACCEPTED,
        evidence_ids=["file:evidence/42.json"],
        root_cause_candidate="null pointer",
        invariant_candidate="all pointers checked",
        lesson_candidate="add null checks",
        edge_candidates=[
            EdgeCandidate(source="bug-1", target="fix-1", relation="fixes")
        ],
        promotion_eligible_ai_memory=True,
        promotion_eligible_context_graph=True,
    )


@pytest.fixture
def minimal_valid_commit() -> LearningCommit:
    return LearningCommit(
        task_id="task-2",
        repo_area="repo-xyz",
        outcome=Outcome.REJECTED,
    )


class TestLearningCommitConstruction:
    def test_valid_accepted_commit(self, valid_accepted_commit):
        assert valid_accepted_commit.outcome == Outcome.ACCEPTED
        assert valid_accepted_commit.promotion_eligible_ai_memory is True
        assert valid_accepted_commit.promotion_eligible_context_graph is True

    def test_minimal_valid_commit(self, minimal_valid_commit):
        assert minimal_valid_commit.task_id == "task-2"
        assert minimal_valid_commit.evidence_ids == ()
        assert minimal_valid_commit.edge_candidates == ()

    def test_defaults(self):
        c = LearningCommit(
            task_id="t", repo_area="r", outcome=Outcome.INCONCLUSIVE
        )
        assert c.evidence_ids == ()
        assert c.root_cause_candidate is None
        assert c.lesson_candidate is None
        assert c.promotion_eligible_ai_memory is False

    def test_frozen(self, valid_accepted_commit):
        with pytest.raises(Exception):
            valid_accepted_commit.task_id = "mutated"  # type: ignore[misc]

    def test_rejects_missing_task_id(self):
        with pytest.raises(ValueError, match="task_id"):
            LearningCommit(task_id="", repo_area="r", outcome=Outcome.ACCEPTED)

    def test_rejects_missing_repo_area(self):
        with pytest.raises(ValueError, match="repo_area"):
            LearningCommit(task_id="t", repo_area="", outcome=Outcome.ACCEPTED)

    def test_rejects_invalid_outcome_type(self):
        with pytest.raises(ValueError, match="outcome"):
            LearningCommit(task_id="t", repo_area="r", outcome="accepted")  # type: ignore[arg-type]

    def test_none_candidates_ok(self):
        c = LearningCommit(
            task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
            root_cause_candidate=None,
            invariant_candidate=None,
            lesson_candidate=None,
        )
        assert c.root_cause_candidate is None

    def test_whitespace_only_candidates_rejected(self):
        with pytest.raises(ValueError):
            LearningCommit(
                task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
                root_cause_candidate="   ",
            )

    def test_edge_candidate_list_not_list(self):
        with pytest.raises(ValueError):
            LearningCommit(
                task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
                edge_candidates="not-a-list",  # type: ignore[arg-type]
            )

    def test_edge_candidate_invalid_type(self):
        with pytest.raises(ValueError):
            LearningCommit(
                task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
                edge_candidates=["invalid"],  # type: ignore[list-item]
            )


class TestEvidenceIdValidation:
    def test_valid_file_reference(self):
        LearningCommit(
            task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
            evidence_ids=["file:evidence/1.json"],
        )

    def test_valid_http_reference(self):
        LearningCommit(
            task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
            evidence_ids=["https://example.com/evidence/1"],
        )

    def test_rejects_missing_scheme(self):
        with pytest.raises(InvalidReferenceSchemeError):
            LearningCommit(
                task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
                evidence_ids=["no-scheme"],
            )

    def test_rejects_unknown_scheme(self):
        with pytest.raises(InvalidReferenceSchemeError):
            LearningCommit(
                task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
                evidence_ids=["ftp://example.com"],
            )

    def test_rejects_empty_path(self):
        with pytest.raises(EmptyReferencePathError):
            LearningCommit(
                task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
                evidence_ids=["file:"],
            )

    def test_rejects_unsafe_file_path(self):
        with pytest.raises(UnsafeFilePathError):
            LearningCommit(
                task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
                evidence_ids=["file:../../../etc/passwd"],
            )

    def test_rejects_too_long_reference(self):
        long_ref = "file:" + "a" * 3000
        with pytest.raises(ReferenceTooLongError):
            LearningCommit(
                task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
                evidence_ids=[long_ref],
            )

    def test_rejects_non_string_evidence(self):
        with pytest.raises(ValueError):
            LearningCommit(
                task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
                evidence_ids=[123],  # type: ignore[list-item]
            )

    def test_safe_embedded_dots_allowed(self):
        LearningCommit(
            task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
            evidence_ids=["file:docs/version..notes.md"],
        )

    def test_segment_exactly_dots_is_traversal(self):
        with pytest.raises(UnsafeFilePathError):
            LearningCommit(
                task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
                evidence_ids=["file:docs/../etc/passwd"],
            )

    def test_absolute_file_path_rejected(self):
        with pytest.raises(UnsafeFilePathError):
            LearningCommit(
                task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
                evidence_ids=["file:/etc/passwd"],
            )

    def test_malformed_empty_segment_rejected(self):
        with pytest.raises(UnsafeFilePathError):
            LearningCommit(
                task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
                evidence_ids=["file:docs//malformed"],
            )

class TestPromotionEligibility:
    def test_accepted_with_evidence_allows_promotion(self):
        c = LearningCommit(
            task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
            evidence_ids=["file:ev.json"],
            promotion_eligible_ai_memory=True,
        )
        assert c.promotion_eligible_ai_memory is True

    def test_rejected_disallows_promotion(self):
        with pytest.raises(ValueError, match="requires outcome ACCEPTED"):
            LearningCommit(
                task_id="t", repo_area="r", outcome=Outcome.REJECTED,
                evidence_ids=["file:ev.json"],
                promotion_eligible_ai_memory=True,
            )

    def test_inconclusive_disallows_promotion(self):
        with pytest.raises(ValueError, match="requires outcome ACCEPTED"):
            LearningCommit(
                task_id="t", repo_area="r", outcome=Outcome.INCONCLUSIVE,
                evidence_ids=["file:ev.json"],
                promotion_eligible_context_graph=True,
            )

    def test_promotion_without_evidence_rejected(self):
        with pytest.raises(ValueError, match="requires at least one evidence_id"):
            LearningCommit(
                task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
                promotion_eligible_ai_memory=True,
            )

    def test_accepted_no_promotion_ok_without_evidence(self):
        c = LearningCommit(
            task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
            evidence_ids=[],
        )
        assert c.outcome == Outcome.ACCEPTED

    def test_context_graph_promotion_same_gating(self):
        with pytest.raises(ValueError):
            LearningCommit(
                task_id="t", repo_area="r", outcome=Outcome.REJECTED,
                evidence_ids=["file:x"],
                promotion_eligible_context_graph=True,
            )


class TestCrossRepoValidation:
    def test_matching_repo_passes(self, valid_accepted_commit):
        validate_repo_match(valid_accepted_commit, "repo-abc")

    def test_non_matching_repo_raises(self, valid_accepted_commit):
        with pytest.raises(ValueError, match="Cross-repository commit"):
            validate_repo_match(valid_accepted_commit, "other-repo")

    def test_repo_id_type_error(self, minimal_valid_commit):
        with pytest.raises(TypeError):
            validate_repo_match(minimal_valid_commit, 123)  # type: ignore[arg-type]


class TestFromDict:
    def test_basic_accepted(self):
        d = {
            "task_id": "t1",
            "repo_area": "repo-a",
            "outcome": "accepted",
            "evidence_ids": ["file:ev.json"],
            "root_cause_candidate": "rc",
            "invariant_candidate": "inv",
            "lesson_candidate": "les",
            "edge_candidates": [
                {"source": "n1", "target": "n2", "relation": "fixes"}
            ],
            "promotion_eligible_ai_memory": True,
            "promotion_eligible_context_graph": True,
        }
        c = learning_commit_from_dict(d)
        assert c.task_id == "t1"
        assert c.outcome == Outcome.ACCEPTED
        assert len(c.edge_candidates) == 1
        assert c.edge_candidates[0].relation == "fixes"

    def test_outcome_enum_accepted(self):
        d = {"task_id": "t", "repo_area": "r", "outcome": Outcome.ACCEPTED}
        c = learning_commit_from_dict(d)
        assert c.outcome == Outcome.ACCEPTED

    def test_minimal_dict(self):
        d = {"task_id": "t", "repo_area": "r", "outcome": "rejected"}
        c = learning_commit_from_dict(d)
        assert c.evidence_ids == ()

    def test_rejects_unknown_fields(self):
        d = {"task_id": "t", "repo_area": "r", "outcome": "accepted", "foo": "bar"}
        with pytest.raises(ValueError, match="Unknown fields"):
            learning_commit_from_dict(d)

    def test_missing_outcome_raises(self):
        with pytest.raises(ValueError, match="Missing required fields"):
            learning_commit_from_dict({"task_id": "t", "repo_area": "r"})

    def test_invalid_outcome_string(self):
        with pytest.raises(ValueError, match="Invalid outcome"):
            learning_commit_from_dict(
                {"task_id": "t", "repo_area": "r", "outcome": "maybe"}
            )

    def test_outcome_wrong_type(self):
        with pytest.raises(TypeError):
            learning_commit_from_dict(
                {"task_id": "t", "repo_area": "r", "outcome": 123}
            )

    def test_edge_candidates_bad_dict(self):
        d = {
            "task_id": "t", "repo_area": "r", "outcome": "accepted",
            "edge_candidates": [{"source": "s", "relation": "r"}],
        }
        with pytest.raises(ValueError, match="Invalid edge_candidates"):
            learning_commit_from_dict(d)

    def test_edge_candidates_not_list(self):
        d = {
            "task_id": "t", "repo_area": "r", "outcome": "accepted",
            "edge_candidates": "not-list",
        }
        with pytest.raises(ValueError, match="edge_candidates must be a list"):
            learning_commit_from_dict(d)

    def test_data_not_dict(self):
        with pytest.raises(TypeError):
            learning_commit_from_dict("string")  # type: ignore[arg-type]

    def test_from_dict_yields_tuples(self):
        d = {
            "task_id": "t", "repo_area": "r", "outcome": "accepted",
            "evidence_ids": ["file:ev.json"],
            "edge_candidates": [{"source": "a", "target": "b", "relation": "fixes"}],
        }
        c = learning_commit_from_dict(d)
        assert isinstance(c.evidence_ids, tuple)
        assert isinstance(c.edge_candidates, tuple)


class TestEdgeCandidate:
    def test_valid_edge(self):
        e = EdgeCandidate(source="a", target="b", relation="depends_on")
        assert e.source == "a"

    def test_self_loop_rejected(self):
        with pytest.raises(ValueError):
            EdgeCandidate(source="x", target="x", relation="r")

    def test_empty_source(self):
        with pytest.raises(ValueError, match="source"):
            EdgeCandidate(source="", target="b", relation="r")

    def test_whitespace_source(self):
        with pytest.raises(ValueError):
            EdgeCandidate(source="  ", target="b", relation="r")

    def test_empty_target(self):
        with pytest.raises(ValueError, match="target"):
            EdgeCandidate(source="a", target="", relation="r")

    def test_empty_relation(self):
        with pytest.raises(ValueError, match="relation"):
            EdgeCandidate(source="a", target="b", relation="")


class TestImmutableCollections:
    def test_evidence_ids_is_tuple(self):
        c = LearningCommit(
            task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
            evidence_ids=["file:ev.json"],
        )
        assert isinstance(c.evidence_ids, tuple)
        assert c.evidence_ids == ("file:ev.json",)

    def test_edge_candidates_is_tuple(self):
        c = LearningCommit(
            task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
            edge_candidates=[EdgeCandidate(source="a", target="b", relation="r")],
        )
        assert isinstance(c.edge_candidates, tuple)
        assert len(c.edge_candidates) == 1

    def test_cannot_mutate_evidence_ids(self):
        c = LearningCommit(
            task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
            evidence_ids=["file:ev.json"],
        )
        with pytest.raises(TypeError):
            c.evidence_ids[0] = "mutated"  # type: ignore[index]

    def test_cannot_mutate_edge_candidates(self):
        c = LearningCommit(
            task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
            edge_candidates=[EdgeCandidate(source="a", target="b", relation="r")],
        )
        with pytest.raises(TypeError):
            c.edge_candidates[0] = EdgeCandidate(source="x", target="y", relation="z")  # type: ignore[index]

    def test_constructor_normalizes_evidence_ids_to_tuple(self):
        c = LearningCommit(
            task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
            evidence_ids=["file:a", "file:b"],
        )
        assert c.evidence_ids == ("file:a", "file:b")

    def test_constructor_normalizes_edge_candidates_to_tuple(self):
        edges = [EdgeCandidate(source="a", target="b", relation="r")]
        c = LearningCommit(
            task_id="t", repo_area="r", outcome=Outcome.ACCEPTED,
            edge_candidates=edges,
        )
        assert c.edge_candidates == tuple(edges)

    def test_from_dict_yields_tuples(self):
        d = {
            "task_id": "t", "repo_area": "r", "outcome": "accepted",
            "evidence_ids": ["file:ev.json"],
            "edge_candidates": [{"source": "a", "target": "b", "relation": "fixes"}],
        }
        c = learning_commit_from_dict(d)
        assert isinstance(c.evidence_ids, tuple)
        assert isinstance(c.edge_candidates, tuple)
