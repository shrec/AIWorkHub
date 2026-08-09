"""Adversarial tests for the graph-scoped audit packet contract.

Covers correctness (sorted collections, controlled vocabularies, evidence
levels), security (path traversal, absolute paths, control/backslash
characters, oversized fields, duplicate or whitespace identifiers),
concurrency (deterministic canonical JSON for equivalent inputs), and
inconclusive packets (explicit unknowns are valid; ungrounded blocker claims
fail closed).
"""

from __future__ import annotations

from typing import Any

import pytest

from aiworkhub.scoped_audit import (
    ALLOWED_REVIEW_LENSES,
    BLOCKER_MIN_EVIDENCE_LEVEL,
    ChangedPath,
    DuplicateIdentityError,
    EvidenceLevel,
    EvidenceReference,
    KnownUnknown,
    MalformedPathError,
    PriorLesson,
    ReviewLens,
    ScopedAuditPacket,
    ScopedAuditValidationError,
    TargetSymbol,
    UngroundedBlockerError,
    UnknownLensError,
    ValidationExpectation,
    canonical_json,
    packet_fingerprint,
)


def _target(name: str = "src/mod.Foo", kind: str = "function") -> TargetSymbol:
    return TargetSymbol(qualified_name=name, symbol_kind=kind)


def _change(path: str = "src/mod.py", kind: str = "modified") -> ChangedPath:
    return ChangedPath(path=path, change_kind=kind, line_start=10, line_end=20)


def _evidence(
    ident: str = "ev-1",
    *,
    kind: str = "call_graph",
    level: EvidenceLevel = EvidenceLevel.STATIC_EVIDENCE,
    path: str = "src/mod.py",
    supports_blocker: bool = False,
) -> EvidenceReference:
    return EvidenceReference(
        identity=ident,
        evidence_kind=kind,
        evidence_level=level,
        path=path,
        line_start=10,
        line_end=20,
        description="explanation",
        supports_blocker=supports_blocker,
    )


def _lesson(ident: str = "lesson-1") -> PriorLesson:
    return PriorLesson(
        identity=ident,
        source="reviews/2025#L10",
        evidence_level=EvidenceLevel.TESTED,
        summary="summary",
    )


def _unknown(ident: str = "unk-1") -> KnownUnknown:
    return KnownUnknown(
        identity=ident,
        question="???",
        why_relevant="reasons",
    )


def _expectation(ident: str = "v-1") -> ValidationExpectation:
    return ValidationExpectation(
        identity=ident,
        validation_kind="unit",
        command="pytest -q",
        expected_outcome="pass",
    )


def _lens(
    kind: str = "correctness",
    required: EvidenceLevel = EvidenceLevel.STATIC_EVIDENCE,
) -> ReviewLens:
    return ReviewLens(
        lens_kind=kind,
        rationale="why",
        required_evidence_level=required,
    )


def _packet_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "packet_id": "pkt-1",
        "task_id": "task-1",
        "created_at": "2026-08-09T10:00:00Z",
        "target_symbols": (_target(),),
        "changed_paths": (_change(),),
        "forbidden_changes": ("do not edit configuration",),
        "invariants": ("output ordering is stable",),
        "impact_evidence": (_evidence("impact-1"),),
        "test_evidence": (_evidence("test-1"),),
        "contract_evidence": (_evidence("contract-1"),),
        "prior_lessons": (_lesson(),),
        "review_lens": _lens(),
        "unknowns": (_unknown(),),
        "validation_expectations": (_expectation(),),
    }
    base.update(overrides)
    return base


def _make_packet(**overrides: Any) -> ScopedAuditPacket:
    return ScopedAuditPacket(**_packet_kwargs(**overrides))


class TestCorrectness:
    def test_minimal_packet_constructs(self) -> None:
        packet = _make_packet()
        assert packet.packet_id == "pkt-1"
        assert len(packet.target_symbols) == 1
        assert canonical_json(packet)

    def test_target_symbols_sorted(self) -> None:
        packet = _make_packet(
            target_symbols=(
                _target("src/z.py.Z"),
                _target("src/a.py.A"),
                _target("src/m.py.M"),
            )
        )
        names = [t.qualified_name for t in packet.target_symbols]
        assert names == ["src/a.py.A", "src/m.py.M", "src/z.py.Z"]

    def test_validation_expectations_sorted(self) -> None:
        packet = _make_packet(
            validation_expectations=(_expectation("v-3"), _expectation("v-1"))
        )
        ids = [v.identity for v in packet.validation_expectations]
        assert ids == ["v-1", "v-3"]

    def test_unknowns_may_be_empty(self) -> None:
        packet = _make_packet(unknowns=())
        assert packet.unknowns == ()

    @pytest.mark.parametrize("kind", sorted(ALLOWED_REVIEW_LENSES))
    def test_allowed_lens_kinds_accepted(self, kind: str) -> None:
        packet = _make_packet(review_lens=_lens(kind=kind))
        assert packet.review_lens.lens_kind == kind

    def test_code_quality_lens_accepted_unknown_still_fails(self) -> None:
        assert "code_quality" in ALLOWED_REVIEW_LENSES
        packet = _make_packet(review_lens=_lens(kind="code_quality"))
        assert packet.review_lens.lens_kind == "code_quality"
        with pytest.raises(UnknownLensError):
            _make_packet(review_lens=_lens(kind="speculative"))

    @pytest.mark.parametrize("level", list(EvidenceLevel))
    def test_evidence_level_serialises_lowercase(
        self, level: EvidenceLevel
    ) -> None:
        evidence = _evidence(level=level)
        assert evidence.as_json()["evidence_level"] == level.name.lower()

    def test_canonical_json_keys_sorted(self) -> None:
        payload = canonical_json(_make_packet())
        assert payload.find('"changed_paths"') < payload.find('"packet_id"')


class TestSecurity:
    @pytest.mark.parametrize(
        "bad_path",
        [
            "/etc/passwd",
            "../escape",
            "src/../../etc/x",
            "C:/windows/system32",
            "file:///etc/shadow",
            "src\x00module.py",
            "src/module.py\x1f",
            "src\\windows\\style.py",
        ],
    )
    def test_invalid_change_paths_rejected(self, bad_path: str) -> None:
        with pytest.raises(MalformedPathError):
            _make_packet(changed_paths=(_change(path=bad_path),))

    @pytest.mark.parametrize(
        "bad_path",
        ["/absolute", "../../lol", "scheme://x", "with\\backslash"],
    )
    def test_invalid_evidence_paths_rejected(self, bad_path: str) -> None:
        with pytest.raises(MalformedPathError):
            EvidenceReference(
                identity="x",
                evidence_kind="call_graph",
                evidence_level=EvidenceLevel.STATIC_EVIDENCE,
                path=bad_path,
                line_start=1,
                line_end=2,
                description="d",
            )

    def test_unknown_lens_rejected(self) -> None:
        with pytest.raises(UnknownLensError):
            ReviewLens(lens_kind="speculative", rationale="no")

    def test_oversized_text_field_rejected(self) -> None:
        with pytest.raises(ScopedAuditValidationError):
            _make_packet(created_at="x" * 5000)

    def test_oversized_collection_rejected(self) -> None:
        with pytest.raises(ScopedAuditValidationError):
            _make_packet(
                changed_paths=tuple(
                    _change(path=f"src/file_{i}.py") for i in range(100)
                )
            )

    def test_invalid_symbol_kind_rejected(self) -> None:
        with pytest.raises(ScopedAuditValidationError):
            TargetSymbol(qualified_name="x", symbol_kind="weird")

    def test_invalid_change_kind_rejected(self) -> None:
        with pytest.raises(ScopedAuditValidationError):
            ChangedPath(path="src/x.py", change_kind="exploded")

    def test_duplicate_identity_within_section_fails(self) -> None:
        with pytest.raises(DuplicateIdentityError):
            _make_packet(target_symbols=(_target("a"), _target("a")))

    def test_duplicate_identity_across_evidence_sections_fails(self) -> None:
        with pytest.raises(DuplicateIdentityError):
            _make_packet(
                impact_evidence=(_evidence("dup"),),
                test_evidence=(_evidence("dup", kind="test_target"),),
            )

    def test_whitespace_identity_rejected(self) -> None:
        with pytest.raises(ScopedAuditValidationError):
            _make_packet(packet_id="pkt 1")

    def test_invalid_evidence_value_type_rejected(self) -> None:
        with pytest.raises(ScopedAuditValidationError):
            EvidenceReference(
                identity="x",
                evidence_kind="call_graph",
                evidence_level="static",  # type: ignore[arg-type]
                path="src/x.py",
                line_start=1,
                line_end=2,
                description="d",
            )

    def test_line_span_inverted_rejected(self) -> None:
        with pytest.raises(ScopedAuditValidationError):
            ChangedPath(
                path="src/x.py",
                change_kind="modified",
                line_start=10,
                line_end=5,
            )


class TestConcurrencyDeterminism:
    def test_target_reordering_yields_identical_json(self) -> None:
        a = _make_packet(
            target_symbols=(_target("a"), _target("b"), _target("c"))
        )
        b = _make_packet(
            target_symbols=(_target("c"), _target("a"), _target("b"))
        )
        assert canonical_json(a) == canonical_json(b)
        assert packet_fingerprint(a) == packet_fingerprint(b)

    def test_evidence_reordering_yields_identical_json(self) -> None:
        ev_a = (_evidence("a"), _evidence("b"), _evidence("c"))
        ev_b = (_evidence("c"), _evidence("a"), _evidence("b"))
        a = _make_packet(impact_evidence=ev_a)
        b = _make_packet(impact_evidence=ev_b)
        assert canonical_json(a) == canonical_json(b)

    def test_fingerprint_stable_across_invocations(self) -> None:
        packet = _make_packet()
        assert packet_fingerprint(packet) == packet_fingerprint(packet)


class TestInconclusiveAndBlockers:
    def test_packet_with_multiple_unknowns_is_valid(self) -> None:
        packet = _make_packet(unknowns=(_unknown("u-1"), _unknown("u-2")))
        assert len(packet.unknowns) == 2

    def test_ungrounded_blocker_rejected(self) -> None:
        with pytest.raises(UngroundedBlockerError):
            _make_packet(
                impact_evidence=(
                    _evidence(
                        "blocker-1",
                        level=EvidenceLevel.CLAIMED,
                        supports_blocker=True,
                    ),
                )
            )

    def test_grounded_blocker_accepted(self) -> None:
        packet = _make_packet(
            impact_evidence=(
                _evidence(
                    "blocker-1",
                    level=BLOCKER_MIN_EVIDENCE_LEVEL,
                    supports_blocker=True,
                ),
            )
        )
        assert packet.impact_evidence[0].supports_blocker

    def test_lens_can_raise_required_evidence(self) -> None:
        with pytest.raises(UngroundedBlockerError):
            _make_packet(
                review_lens=_lens(required=EvidenceLevel.REPRODUCED),
                impact_evidence=(
                    _evidence(
                        "blocker-1",
                        level=EvidenceLevel.STATIC_EVIDENCE,
                        supports_blocker=True,
                    ),
                ),
            )

    def test_non_blocker_low_evidence_ok(self) -> None:
        packet = _make_packet(
            impact_evidence=(
                _evidence(
                    "low-1",
                    level=EvidenceLevel.CLAIMED,
                    supports_blocker=False,
                ),
            )
        )
        assert packet.impact_evidence[0].evidence_level == EvidenceLevel.CLAIMED


class TestRejectDump:
    def test_no_target_symbols_rejected(self) -> None:
        with pytest.raises(ScopedAuditValidationError):
            _make_packet(target_symbols=())

    def test_no_changed_paths_rejected(self) -> None:
        with pytest.raises(ScopedAuditValidationError):
            _make_packet(changed_paths=())

    def test_no_validation_expectations_rejected(self) -> None:
        with pytest.raises(ScopedAuditValidationError):
            _make_packet(validation_expectations=())
