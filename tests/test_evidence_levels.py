"""Tests for the evidence-level contract."""

from __future__ import annotations

import json

import pytest

from aiworkhub.evidence_levels import (
    ALLOWED_CONFIDENCES,
    ALLOWED_FIELDS,
    ALLOWED_SCHEMES,
    ALLOWED_SEVERITIES,
    MAX_REF_LEN,
    EmptyReferencePathError,
    EvidenceLevel,
    EvidenceRecord,
    EvidenceValidationError,
    FabricatedVerificationError,
    InvalidConfidenceError,
    InvalidEvidenceLevelError,
    InvalidReferenceError,
    InvalidReferenceSchemeError,
    InvalidSeverityError,
    MissingRequiredFieldsError,
    ReferenceTooLongError,
    UnknownFieldError,
    UnsafeFilePathError,
    meets_evidence_level,
    validate_evidence_record,
)


# -----------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------

def _record(**overrides: object) -> dict:
    """Build a minimal valid record dict with *overrides*."""
    data: dict = {
        "evidence_level": "claimed",
        "severity": "medium",
        "confidence": "high",
    }
    data.update(overrides)
    return data


# -----------------------------------------------------------------------
# EvidenceLevel ordering
# -----------------------------------------------------------------------

class TestEvidenceLevelOrdering:
    @pytest.mark.parametrize("level,minimum,expected", [
        (EvidenceLevel.CLAIMED, EvidenceLevel.CLAIMED, True),
        (EvidenceLevel.OBSERVATION, EvidenceLevel.CLAIMED, True),
        (EvidenceLevel.STATIC_EVIDENCE, EvidenceLevel.STATIC_EVIDENCE, True),
        (EvidenceLevel.TESTED, EvidenceLevel.TESTED, True),
        (EvidenceLevel.REPRODUCED, EvidenceLevel.REPRODUCED, True),
        (EvidenceLevel.FIXED_AND_VERIFIED, EvidenceLevel.FIXED_AND_VERIFIED, True),
        (EvidenceLevel.CLAIMED, EvidenceLevel.OBSERVATION, False),
        (EvidenceLevel.INCONCLUSIVE, EvidenceLevel.CLAIMED, False),
        (EvidenceLevel.FIXED_AND_VERIFIED, EvidenceLevel.INCONCLUSIVE, False),
    ])
    def test_meets(self, level, minimum, expected):
        assert meets_evidence_level(level, minimum) == expected

    def test_canonical_order(self):
        levels = [
            EvidenceLevel.INCONCLUSIVE,
            EvidenceLevel.CLAIMED,
            EvidenceLevel.OBSERVATION,
            EvidenceLevel.STATIC_EVIDENCE,
            EvidenceLevel.TESTED,
            EvidenceLevel.REPRODUCED,
            EvidenceLevel.FIXED_AND_VERIFIED,
        ]
        assert sorted(levels) == levels

    def test_all_positive_combinations(self):
        positive = [
            EvidenceLevel.CLAIMED,
            EvidenceLevel.OBSERVATION,
            EvidenceLevel.STATIC_EVIDENCE,
            EvidenceLevel.TESTED,
            EvidenceLevel.REPRODUCED,
            EvidenceLevel.FIXED_AND_VERIFIED,
        ]
        for i, a in enumerate(positive):
            for j, b in enumerate(positive):
                if i >= j:
                    assert meets_evidence_level(a, b)
                else:
                    assert not meets_evidence_level(a, b)


# -----------------------------------------------------------------------
# Positive record validation
# -----------------------------------------------------------------------

class TestPositiveValidation:
    def test_minimal_valid_record(self):
        rec = validate_evidence_record(_record())
        assert rec.evidence_level == EvidenceLevel.CLAIMED
        assert rec.severity == "MEDIUM"
        assert rec.confidence == "HIGH"
        assert rec.reference is None
        assert rec.verified_by is None
        assert rec.message is None

    @pytest.mark.parametrize("sev", sorted(ALLOWED_SEVERITIES))
    def test_all_severities_accepted(self, sev):
        rec = validate_evidence_record(_record(severity=sev.lower()))
        assert rec.severity == sev

    @pytest.mark.parametrize("conf", sorted(ALLOWED_CONFIDENCES))
    def test_all_confidences_accepted(self, conf):
        rec = validate_evidence_record(_record(confidence=conf.lower()))
        assert rec.confidence == conf

    @pytest.mark.parametrize("level", EvidenceLevel)
    def test_all_evidence_level_names(self, level):
        kwargs = {"evidence_level": level.name.lower()}
        if level == EvidenceLevel.FIXED_AND_VERIFIED:
            kwargs["reference"] = "http://fix"
            kwargs["verified_by"] = "tester"
        rec = validate_evidence_record(_record(**kwargs))
        assert rec.evidence_level == level

    def test_fixed_and_verified_valid(self):
        rec = validate_evidence_record(_record(
            evidence_level="fixed_and_verified",
            severity="high",
            confidence="high",
            reference="http://example.com/fix",
            verified_by="tester",
        ))
        assert rec.evidence_level == EvidenceLevel.FIXED_AND_VERIFIED
        assert rec.reference == "http://example.com/fix"
        assert rec.verified_by == "tester"

    # --- max-length reference (rework #1) ---
    def test_reference_exactly_max_length_valid(self):
        scheme = "http://"
        payload_len = MAX_REF_LEN - len(scheme)
        ref = scheme + "x" * payload_len
        assert len(ref) == MAX_REF_LEN
        rec = validate_evidence_record(_record(
            evidence_level="fixed_and_verified",
            severity="medium",
            confidence="high",
            reference=ref,
            verified_by="tester",
        ))
        assert rec.reference == ref


# -----------------------------------------------------------------------
# Round-trip serialization
# -----------------------------------------------------------------------

class TestRoundTrip:
    def test_dict_roundtrip(self):
        original = {
            "evidence_level": "fixed_and_verified",
            "severity": "critical",
            "confidence": "low",
            "reference": "https://example.com",
            "verified_by": "verifier",
            "message": "roundtrip",
        }
        rec = validate_evidence_record(original)
        out = rec.to_dict()
        rec2 = validate_evidence_record(out)
        assert rec == rec2

    def test_to_dict_deterministic_order(self):
        rec = validate_evidence_record(_record(
            evidence_level="tested",
            severity="blocker",
            confidence="medium",
            reference="file:path",
        ))
        d1 = rec.to_dict()
        d2 = rec.to_dict()
        assert d1 == d2
        expected_keys = ["evidence_level", "severity", "confidence", "reference", "verified_by", "message"]
        assert list(d1.keys()) == expected_keys

    def test_json_roundtrip(self):
        rec = validate_evidence_record(_record(
            evidence_level="static_evidence",
            severity="low",
            confidence="none",
        ))
        json_str = json.dumps(rec.to_dict(), sort_keys=True, indent=2)
        reloaded = json.loads(json_str)
        rec2 = validate_evidence_record(reloaded)
        assert rec == rec2


# -----------------------------------------------------------------------
# Reference validation negatives
# -----------------------------------------------------------------------

class TestReferenceNegatives:
    def test_reference_too_long_raises(self):
        ref = "http://" + "a" * (MAX_REF_LEN - len("http://") + 1)
        assert len(ref) > MAX_REF_LEN
        with pytest.raises(ReferenceTooLongError):
            validate_evidence_record(_record(reference=ref))

    def test_reference_missing_scheme_raises(self):
        with pytest.raises(InvalidReferenceSchemeError):
            validate_evidence_record(_record(reference="noscheme/path"))

    def test_untyped_max_length_rejected(self):
        # rework #1: raw 'x'*2048 must stay rejected
        ref = "x" * MAX_REF_LEN
        with pytest.raises(InvalidReferenceSchemeError):
            validate_evidence_record(_record(reference=ref))

    @pytest.mark.parametrize("scheme", ["file", "http", "https"])
    def test_empty_payload_raises_empty_path_error(self, scheme):
        # rework #2: recognized scheme with empty payload -> EmptyReferencePathError
        ref = f"{scheme}:"
        with pytest.raises(EmptyReferencePathError):
            validate_evidence_record(_record(reference=ref))

    def test_unknown_scheme_raises_invalid_scheme(self):
        with pytest.raises(InvalidReferenceSchemeError):
            validate_evidence_record(_record(reference="unknown:path"))

    def test_file_scheme_traversal_raises_unsafe(self):
        # rework #3: unsafe-file-path class
        with pytest.raises(UnsafeFilePathError):
            validate_evidence_record(_record(reference="file:../etc/passwd"))

    def test_file_scheme_backslash_raises_unsafe(self):
        # rework #3: same error identity for backslash
        with pytest.raises(UnsafeFilePathError):
            validate_evidence_record(_record(reference="file:..\\evil"))

    def test_file_scheme_normal_path_ok(self):
        rec = validate_evidence_record(_record(reference="file:normal/path"))
        assert rec.reference == "file:normal/path"

    def test_reference_boolean_rejected(self):
        with pytest.raises(InvalidReferenceError):
            validate_evidence_record(_record(reference=True))

    def test_reference_invalid_scheme_pattern(self):
        with pytest.raises(InvalidReferenceSchemeError):
            validate_evidence_record(_record(reference="123bad:path"))


# -----------------------------------------------------------------------
# Fabricated verification / missing required fields
# -----------------------------------------------------------------------

class TestVerificationRequirements:
    def test_fixed_and_verified_requires_reference(self):
        with pytest.raises(MissingRequiredFieldsError):
            validate_evidence_record(_record(
                evidence_level="fixed_and_verified",
                verified_by="someone",
            ))

    def test_fixed_and_verified_requires_verified_by(self):
        with pytest.raises(MissingRequiredFieldsError):
            validate_evidence_record(_record(
                evidence_level="fixed_and_verified",
                reference="http://fix",
            ))

    def test_non_verified_with_verified_by_raises_fabrication(self):
        with pytest.raises(FabricatedVerificationError):
            validate_evidence_record(_record(
                evidence_level="tested",
                verified_by="someone",
            ))

    def test_non_verified_reference_allowed(self):
        rec = validate_evidence_record(_record(
            evidence_level="observation",
            reference="http://example.com",
        ))
        assert rec.reference == "http://example.com"

    def test_high_severity_blocker_fixed_and_verified_valid(self):
        validate_evidence_record(_record(
            evidence_level="fixed_and_verified",
            severity="blocker",
            confidence="high",
            reference="http://blocker.fix",
            verified_by="qa",
        ))

    def test_high_severity_blocker_missing_ref_raises(self):
        with pytest.raises(MissingRequiredFieldsError):
            validate_evidence_record(_record(
                evidence_level="fixed_and_verified",
                severity="blocker",
                verified_by="qa",
            ))


# -----------------------------------------------------------------------
# Unknown fields / booleans / malformed containers
# -----------------------------------------------------------------------

class TestStrictness:
    def test_unknown_field_raises(self):
        with pytest.raises(UnknownFieldError):
            validate_evidence_record(_record(extra_field="value"))

    @pytest.mark.parametrize("field", ["evidence_level", "severity", "confidence"])
    def test_boolean_rejected(self, field):
        exc_map = {
            "evidence_level": InvalidEvidenceLevelError,
            "severity": InvalidSeverityError,
            "confidence": InvalidConfidenceError,
        }
        with pytest.raises(exc_map[field]):
            validate_evidence_record(_record(**{field: True}))

    def test_non_dict_input_rejected(self):
        with pytest.raises(EvidenceValidationError):
            validate_evidence_record([])

    def test_nested_dict_in_field_rejected(self):
        with pytest.raises((InvalidReferenceError, EvidenceValidationError)):
            validate_evidence_record(_record(reference={"url": "http://x"}))

    def test_invalid_evidence_level_string(self):
        with pytest.raises(InvalidEvidenceLevelError):
            validate_evidence_record(_record(evidence_level="super_verified"))

    def test_invalid_severity(self):
        with pytest.raises(InvalidSeverityError):
            validate_evidence_record(_record(severity="extreme"))

    def test_invalid_confidence(self):
        with pytest.raises(InvalidConfidenceError):
            validate_evidence_record(_record(confidence="sure"))

    def test_message_boolean_rejected(self):
        with pytest.raises(EvidenceValidationError):
            validate_evidence_record(_record(message=True))


# -----------------------------------------------------------------------
# Boundary / edge cases
# -----------------------------------------------------------------------

class TestBoundary:
    def test_empty_record_raises_missing(self):
        with pytest.raises(MissingRequiredFieldsError):
            validate_evidence_record({})

    def test_none_values_required_fields(self):
        with pytest.raises((MissingRequiredFieldsError, EvidenceValidationError)):
            validate_evidence_record(_record(severity=None))

    def test_empty_string_confidence_rejected(self):
        with pytest.raises(InvalidConfidenceError):
            validate_evidence_record(_record(confidence=""))

    def test_empty_string_reference_for_verified_rejected(self):
        with pytest.raises(MissingRequiredFieldsError):
            validate_evidence_record(_record(
                evidence_level="fixed_and_verified",
                reference="",
                verified_by="someone",
            ))

    def test_empty_string_verified_by_for_verified_rejected(self):
        with pytest.raises(MissingRequiredFieldsError):
            validate_evidence_record(_record(
                evidence_level="fixed_and_verified",
                reference="http://x",
                verified_by="",
            ))

    def test_max_ref_len_boundary(self):
        scheme = "http://"
        payload_len = MAX_REF_LEN - len(scheme)
        ref_ok = scheme + "a" * payload_len
        ref_bad = ref_ok + "b"
        assert len(ref_ok) == MAX_REF_LEN
        validate_evidence_record(_record(reference=ref_ok))
        with pytest.raises(ReferenceTooLongError):
            validate_evidence_record(_record(reference=ref_bad))


# -----------------------------------------------------------------------
# Immutability
# -----------------------------------------------------------------------

def test_record_is_immutable():
    rec = validate_evidence_record(_record())
    with pytest.raises(AttributeError):
        rec.evidence_level = EvidenceLevel.TESTED  # type: ignore[misc]


# -----------------------------------------------------------------------
# Allowed fields exhaustiveness
# -----------------------------------------------------------------------

def test_known_allowed_fields():
    # Ensure the documented set matches the dataclass fields.
    expected = {"evidence_level", "severity", "confidence", "reference", "verified_by", "message"}
    assert ALLOWED_FIELDS == expected


# -----------------------------------------------------------------------
# Canonical severity/confidence sets
# -----------------------------------------------------------------------

def test_severity_set_stable():
    assert ALLOWED_SEVERITIES == {"BLOCKER", "CRITICAL", "HIGH", "MEDIUM", "LOW", "NONE"}

def test_confidence_set_stable():
    assert ALLOWED_CONFIDENCES == {"HIGH", "MEDIUM", "LOW", "NONE"}

def test_scheme_set_stable():
    assert ALLOWED_SCHEMES == {"file", "http", "https"}


# -----------------------------------------------------------------------
# Extended matrix tests (predecessor protection rebuild)
# -----------------------------------------------------------------------

class TestExtendedMatrix:
    @pytest.mark.parametrize("level", [
        EvidenceLevel.CLAIMED,
        EvidenceLevel.OBSERVATION,
        EvidenceLevel.STATIC_EVIDENCE,
        EvidenceLevel.TESTED,
        EvidenceLevel.REPRODUCED,
    ])
    @pytest.mark.parametrize("severity", sorted(ALLOWED_SEVERITIES))
    @pytest.mark.parametrize("confidence", sorted(ALLOWED_CONFIDENCES))
    def test_non_verified_levels_all_combos(self, level, severity, confidence):
        rec = validate_evidence_record(_record(
            evidence_level=level.name.lower(),
            severity=severity.lower(),
            confidence=confidence.lower(),
        ))
        assert rec.evidence_level == level
        assert rec.severity == severity
        assert rec.confidence == confidence

    @pytest.mark.parametrize("severity", sorted(ALLOWED_SEVERITIES))
    @pytest.mark.parametrize("confidence", sorted(ALLOWED_CONFIDENCES))
    def test_fixed_and_verified_all_combos(self, severity, confidence):
        rec = validate_evidence_record(_record(
            evidence_level="fixed_and_verified",
            severity=severity.lower(),
            confidence=confidence.lower(),
            reference="http://x",
            verified_by="tester",
        ))
        assert rec.evidence_level == EvidenceLevel.FIXED_AND_VERIFIED

    def test_meets_evidence_level_type_guard_level(self):
        with pytest.raises(TypeError):
            meets_evidence_level(1, EvidenceLevel.CLAIMED)

    def test_meets_evidence_level_type_guard_minimum(self):
        with pytest.raises(TypeError):
            meets_evidence_level(EvidenceLevel.CLAIMED, "INCONCLUSIVE")

    def test_meets_evidence_level_type_guard_bool_level(self):
        with pytest.raises(TypeError):
            meets_evidence_level(True, EvidenceLevel.CLAIMED)

    def test_meets_evidence_level_type_guard_bool_minimum(self):
        with pytest.raises(TypeError):
            meets_evidence_level(EvidenceLevel.CLAIMED, False)

    def test_inconclusive_with_verified_by_raises_fabrication(self):
        with pytest.raises(FabricatedVerificationError):
            validate_evidence_record(_record(
                evidence_level="inconclusive",
                verified_by="someone",
            ))

    def test_file_reference_traversal_deep(self):
        with pytest.raises(UnsafeFilePathError):
            validate_evidence_record(_record(reference="file:../../etc/passwd"))

    def test_file_reference_traversal_mixed(self):
        with pytest.raises(UnsafeFilePathError):
            validate_evidence_record(_record(reference="file:..\\windows"))

    def test_reference_scheme_with_hyphen_invalid(self):
        with pytest.raises(InvalidReferenceSchemeError):
            validate_evidence_record(_record(reference="bad-scheme:path"))

    def test_severity_case_insensitive(self):
        rec = validate_evidence_record(_record(severity="blocker"))
        assert rec.severity == "BLOCKER"

    def test_confidence_case_insensitive(self):
        rec = validate_evidence_record(_record(confidence="high"))
        assert rec.confidence == "HIGH"

    def test_to_dict_all_fields_present(self):
        rec = validate_evidence_record(_record(
            evidence_level="fixed_and_verified",
            severity="low",
            confidence="medium",
            reference="http://x",
            verified_by="u",
            message="m",
        ))
        d = rec.to_dict()
        assert set(d.keys()) == {"evidence_level", "severity", "confidence", "reference", "verified_by", "message"}

    def test_verified_by_boolean_rejected(self):
        with pytest.raises(EvidenceValidationError):
            validate_evidence_record(_record(verified_by=True))

    def test_reference_none_allowed_for_non_verified(self):
        rec = validate_evidence_record(_record(
            evidence_level="tested",
            reference=None,
        ))
        assert rec.reference is None

    def test_message_none_allowed(self):
        rec = validate_evidence_record(_record(message=None))
        assert rec.message is None

# -----------------------------------------------------------------------
# Direct constructor adversarial tests (rework)
# -----------------------------------------------------------------------

class TestDirectConstructorValidation:
    """Ensure EvidenceRecord direct construction cannot bypass validation."""

    def test_valid_direct_construction(self):
        rec = EvidenceRecord(
            evidence_level=EvidenceLevel.TESTED,
            severity="MEDIUM",
            confidence="HIGH",
            reference=None,
            verified_by=None,
            message=None,
        )
        assert rec.evidence_level == EvidenceLevel.TESTED
        assert rec.severity == "MEDIUM"
        assert rec.confidence == "HIGH"

    def test_valid_fixed_and_verified(self):
        rec = EvidenceRecord(
            evidence_level=EvidenceLevel.FIXED_AND_VERIFIED,
            severity="BLOCKER",
            confidence="HIGH",
            reference="http://fixed.example.com",
            verified_by="qa-team",
        )
        assert rec.reference == "http://fixed.example.com"
        assert rec.verified_by == "qa-team"

    # --- level type ---
    def test_evidence_level_must_be_enum_instance(self):
        with pytest.raises(InvalidEvidenceLevelError, match="EvidenceLevel instance"):
            EvidenceRecord(
                evidence_level="claimed",  # type: ignore[arg-type]
                severity="MEDIUM",
                confidence="HIGH",
                reference=None,
                verified_by=None,
            )

    # --- severity ---
    def test_severity_must_be_string(self):
        with pytest.raises(InvalidSeverityError, match="must be a string"):
            EvidenceRecord(
                evidence_level=EvidenceLevel.CLAIMED,
                severity=True,  # type: ignore[arg-type]
                confidence="HIGH",
                reference=None,
                verified_by=None,
            )

    def test_severity_must_be_recognised(self):
        with pytest.raises(InvalidSeverityError, match="unrecognised severity"):
            EvidenceRecord(
                evidence_level=EvidenceLevel.CLAIMED,
                severity="EXTREME",
                confidence="HIGH",
                reference=None,
                verified_by=None,
            )

    def test_severity_lowercase_rejected(self):
        # parser normalises, direct constructor must reject
        with pytest.raises(InvalidSeverityError):
            EvidenceRecord(
                evidence_level=EvidenceLevel.CLAIMED,
                severity="medium",
                confidence="HIGH",
                reference=None,
                verified_by=None,
            )

    # --- confidence ---
    def test_confidence_must_be_string(self):
        with pytest.raises(InvalidConfidenceError, match="must be a string"):
            EvidenceRecord(
                evidence_level=EvidenceLevel.CLAIMED,
                severity="MEDIUM",
                confidence=False,  # type: ignore[arg-type]
                reference=None,
                verified_by=None,
            )

    def test_confidence_must_be_recognised(self):
        with pytest.raises(InvalidConfidenceError, match="unrecognised confidence"):
            EvidenceRecord(
                evidence_level=EvidenceLevel.CLAIMED,
                severity="MEDIUM",
                confidence="SURE",
                reference=None,
                verified_by=None,
            )

    def test_confidence_lowercase_rejected(self):
        with pytest.raises(InvalidConfidenceError):
            EvidenceRecord(
                evidence_level=EvidenceLevel.CLAIMED,
                severity="MEDIUM",
                confidence="high",
                reference=None,
                verified_by=None,
            )

    # --- reference ---
    def test_reference_must_be_valid(self):
        with pytest.raises(InvalidReferenceSchemeError):
            EvidenceRecord(
                evidence_level=EvidenceLevel.CLAIMED,
                severity="MEDIUM",
                confidence="HIGH",
                reference="no-scheme",
                verified_by=None,
            )

    def test_reference_empty_string_rejected(self):
        with pytest.raises(InvalidReferenceError, match="must not be empty"):
            EvidenceRecord(
                evidence_level=EvidenceLevel.CLAIMED,
                severity="MEDIUM",
                confidence="HIGH",
                reference="",
                verified_by=None,
            )

    def test_reference_bool_rejected(self):
        with pytest.raises(InvalidReferenceError, match="must be a string or None"):
            EvidenceRecord(
                evidence_level=EvidenceLevel.CLAIMED,
                severity="MEDIUM",
                confidence="HIGH",
                reference=True,  # type: ignore[arg-type]
                verified_by=None,
            )

    # --- verified_by ---
    def test_verified_by_empty_string_rejected(self):
        with pytest.raises(EvidenceValidationError, match="must not be empty"):
            EvidenceRecord(
                evidence_level=EvidenceLevel.CLAIMED,
                severity="MEDIUM",
                confidence="HIGH",
                reference=None,
                verified_by="",
            )

    def test_verified_by_bool_rejected(self):
        with pytest.raises(EvidenceValidationError, match="must be a string or None"):
            EvidenceRecord(
                evidence_level=EvidenceLevel.CLAIMED,
                severity="MEDIUM",
                confidence="HIGH",
                reference=None,
                verified_by=True,  # type: ignore[arg-type]
            )

    # --- fixed_and_verified requirements ---
    def test_fixed_and_verified_missing_reference_direct(self):
        with pytest.raises(MissingRequiredFieldsError, match="reference"):
            EvidenceRecord(
                evidence_level=EvidenceLevel.FIXED_AND_VERIFIED,
                severity="HIGH",
                confidence="HIGH",
                reference=None,
                verified_by="qa",
            )

    def test_fixed_and_verified_missing_verified_by_direct(self):
        with pytest.raises(MissingRequiredFieldsError, match="verified_by"):
            EvidenceRecord(
                evidence_level=EvidenceLevel.FIXED_AND_VERIFIED,
                severity="HIGH",
                confidence="HIGH",
                reference="http://fix",
                verified_by=None,
            )

    # --- fabrication ---
    def test_non_verified_with_verified_by_raises_fabrication(self):
        with pytest.raises(FabricatedVerificationError, match="verified_by"):
            EvidenceRecord(
                evidence_level=EvidenceLevel.TESTED,
                severity="MEDIUM",
                confidence="HIGH",
                reference=None,
                verified_by="attacker",
            )

    def test_inconclusive_with_verified_by_raises_fabrication(self):
        with pytest.raises(FabricatedVerificationError):
            EvidenceRecord(
                evidence_level=EvidenceLevel.INCONCLUSIVE,
                severity="NONE",
                confidence="NONE",
                reference=None,
                verified_by="should-not-be-here",
            )

    # --- message ---
    def test_message_must_be_string_or_none(self):
        with pytest.raises(EvidenceValidationError, match="must be a string or None"):
            EvidenceRecord(
                evidence_level=EvidenceLevel.CLAIMED,
                severity="MEDIUM",
                confidence="HIGH",
                reference=None,
                verified_by=None,
                message=42,  # type: ignore[arg-type]
            )
