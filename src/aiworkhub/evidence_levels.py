"""Evidence-level contract for AIWorkHub findings and outcomes.

This module defines canonical evidence levels, strict parsing/normalization,
minimum-level comparison, and deterministic JSON-safe record validation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict, Optional, Set, Union

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_REF_LEN: int = 2048

ALLOWED_SEVERITIES: Set[str] = {"BLOCKER", "CRITICAL", "HIGH", "MEDIUM", "LOW", "NONE"}
ALLOWED_CONFIDENCES: Set[str] = {"HIGH", "MEDIUM", "LOW", "NONE"}
ALLOWED_SCHEMES: Set[str] = {"file", "http", "https"}
ALLOWED_FIELDS: Set[str] = {"evidence_level", "severity", "confidence", "reference", "verified_by", "message"}

_SCHEME_PATTERN = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*$')


# ---------------------------------------------------------------------------
# Evidence levels
# ---------------------------------------------------------------------------

class EvidenceLevel(IntEnum):
    """Ordered evidence levels from weakest to strongest.

    ``INCONCLUSIVE`` is a special sentinel that cannot satisfy any minimum
    positive level.
    """
    INCONCLUSIVE = 0
    CLAIMED = 1
    OBSERVATION = 2
    STATIC_EVIDENCE = 3
    TESTED = 4
    REPRODUCED = 5
    FIXED_AND_VERIFIED = 6

    def __str__(self) -> str:
        return self.name.lower()


def meets_evidence_level(level: EvidenceLevel, minimum: EvidenceLevel) -> bool:
    """Return True if *level* is at least as strong as *minimum*."""
    if not isinstance(level, EvidenceLevel):
        raise TypeError(f"'level' must be an EvidenceLevel, not {type(level).__name__}")
    if not isinstance(minimum, EvidenceLevel):
        raise TypeError(f"'minimum' must be an EvidenceLevel, not {type(minimum).__name__}")
    if level == EvidenceLevel.INCONCLUSIVE or minimum == EvidenceLevel.INCONCLUSIVE:
        return False
    return level >= minimum


# ---------------------------------------------------------------------------
# Validation exceptions
# ---------------------------------------------------------------------------

class EvidenceValidationError(Exception):
    """Base exception for all evidence-level validation failures."""


class UnknownFieldError(EvidenceValidationError):
    """An unexpected key was present in the evidence record."""


class InvalidSeverityError(EvidenceValidationError):
    """The *severity* value is not recognised."""


class InvalidConfidenceError(EvidenceValidationError):
    """The *confidence* value is not recognised."""


class InvalidEvidenceLevelError(EvidenceValidationError):
    """The *evidence_level* value is not recognised."""


class InvalidReferenceError(EvidenceValidationError):
    """Base for reference-specific errors."""


class InvalidReferenceSchemeError(InvalidReferenceError):
    """The reference does not contain a recognised scheme."""


class EmptyReferencePathError(InvalidReferenceError):
    """A recognised scheme was supplied but the scheme-specific part is empty."""


class UnsafeFilePathError(InvalidReferenceError):
    """A ``file:`` reference contains unsafe characters (traversal or backslashes)."""


class ReferenceTooLongError(InvalidReferenceError):
    """The reference exceeds the maximum allowed length."""


class MissingRequiredFieldsError(EvidenceValidationError):
    """Required fields for the requested evidence level or severity are absent."""


class FabricatedVerificationError(EvidenceValidationError):
    """Verification-related fields are present, but the record is not in a verified state."""


# ---------------------------------------------------------------------------
# Reference validation
# ---------------------------------------------------------------------------

def _validate_reference(ref: str) -> None:
    """Validate a single reference string according to the contract.

    Raises:
        InvalidReferenceSchemeError: missing or unrecognised scheme.
        EmptyReferencePathError: recognised scheme but empty rest.
        UnsafeFilePathError: ``file:`` scheme with ``..`` or backslash.
        ReferenceTooLongError: string longer than *MAX_REF_LEN*.
    """
    if not isinstance(ref, str):
        raise InvalidReferenceError("reference must be a string")
    if len(ref) > MAX_REF_LEN:
        raise ReferenceTooLongError(
            f"reference length ({len(ref)}) exceeds maximum allowed ({MAX_REF_LEN})"
        )
    if ':' not in ref:
        raise InvalidReferenceSchemeError("reference is missing a scheme")
    scheme, rest = ref.split(':', 1)
    if not _SCHEME_PATTERN.match(scheme):
        raise InvalidReferenceSchemeError(f"unrecognised scheme: {scheme}")
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidReferenceSchemeError(f"scheme not allowed: {scheme}")
    if rest == "":
        raise EmptyReferencePathError(f"empty path after scheme in reference: {ref!r}")
    if scheme == "file":
        if '..' in rest or '\\' in rest:
            raise UnsafeFilePathError(f"unsafe file path in reference: {ref!r}")


# ---------------------------------------------------------------------------
# Evidence record
# ---------------------------------------------------------------------------
def _validate_evidence_fields(
    evidence_level: EvidenceLevel,
    severity: str,
    confidence: str,
    reference: Optional[str],
    verified_by: Optional[str],
    message: Optional[str],
) -> None:
    """Validate the fields of an EvidenceRecord for direct construction.

    Raises the same canonical exceptions as ``validate_evidence_record``.
    """
    if not isinstance(evidence_level, EvidenceLevel):
        raise InvalidEvidenceLevelError(
            f"'evidence_level' must be an EvidenceLevel instance, not {type(evidence_level).__name__}"
        )
    if not isinstance(severity, str):
        raise InvalidSeverityError("'severity' must be a string")
    if severity not in ALLOWED_SEVERITIES:
        raise InvalidSeverityError(f"unrecognised severity: {severity!r}")
    if not isinstance(confidence, str):
        raise InvalidConfidenceError("'confidence' must be a string")
    if confidence not in ALLOWED_CONFIDENCES:
        raise InvalidConfidenceError(f"unrecognised confidence: {confidence!r}")
    if reference is not None:
        if not isinstance(reference, str):
            raise InvalidReferenceError("reference must be a string or None")
        if reference == "":
            raise InvalidReferenceError("reference must not be empty")
        _validate_reference(reference)
    if verified_by is not None:
        if not isinstance(verified_by, str):
            raise EvidenceValidationError("'verified_by' must be a string or None")
        if verified_by == "":
            raise EvidenceValidationError("'verified_by' must not be empty")
    if message is not None and not isinstance(message, str):
        raise EvidenceValidationError("'message' must be a string or None")
    if evidence_level == EvidenceLevel.FIXED_AND_VERIFIED:
        if not reference:
            raise MissingRequiredFieldsError(
                "'reference' is required for fixed_and_verified evidence"
            )
        if not verified_by:
            raise MissingRequiredFieldsError(
                "'verified_by' is required for fixed_and_verified evidence"
            )
    else:
        if verified_by is not None:
            raise FabricatedVerificationError(
                "'verified_by' is only allowed for fixed_and_verified evidence"
            )

@dataclass(frozen=True)
class EvidenceRecord:
    """Immutable, validated evidence record."""
    evidence_level: EvidenceLevel
    severity: str
    confidence: str
    reference: Optional[str]
    verified_by: Optional[str]
    message: Optional[str] = None
    def __post_init__(self) -> None:
        _validate_evidence_fields(
            evidence_level=self.evidence_level,
            severity=self.severity,
            confidence=self.confidence,
            reference=self.reference,
            verified_by=self.verified_by,
            message=self.message,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a deterministic, JSON-safe dictionary representation."""
        return {
            'evidence_level': self.evidence_level.name.lower(),
            'severity': self.severity,
            'confidence': self.confidence,
            'reference': self.reference,
            'verified_by': self.verified_by,
            'message': self.message,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> EvidenceRecord:
        """Strictly parse and validate *raw*, returning an ``EvidenceRecord``."""
        return validate_evidence_record(raw)


def _check_is_string(value: object, field_name: str, exc_class: type[EvidenceValidationError] = EvidenceValidationError) -> str:
    """Ensure *value* is a ``str`` (not ``bool``) and return it, raising *exc_class* on failure."""
    if isinstance(value, bool):
        raise exc_class(f"'{field_name}' must be a string, not boolean")
    if not isinstance(value, str):
        raise exc_class(f"'{field_name}' must be a string")
    return value


def validate_evidence_record(raw: Dict[str, Any]) -> EvidenceRecord:
    """Parse *raw* into an :class:`EvidenceRecord`, enforcing every contract rule.

    The function rejects:
    * non-dict containers
    * booleans where strings are expected
    * unknown top-level keys
    * invalid *evidence_level*, *severity*, or *confidence* values
    * references that are too long, missing a scheme, have an empty path for
      recognised schemes, or contain unsafe file-path characters
    * ``fixed_and_verified`` records that are missing *reference* or
      *verified_by*
    """
    if not isinstance(raw, dict):
        raise EvidenceValidationError("evidence record must be a dictionary")

    # Unknown keys ----------------------------------------------------------
    extra = set(raw.keys()) - ALLOWED_FIELDS
    if extra:
        raise UnknownFieldError(f"unknown fields: {sorted(extra)}")

    # evidence_level --------------------------------------------------------
    level_raw = raw.get('evidence_level')
    if level_raw is None:
        raise MissingRequiredFieldsError("'evidence_level' is required")
    level_str = _check_is_string(level_raw, 'evidence_level', InvalidEvidenceLevelError)
    try:
        level = EvidenceLevel[level_str.upper()]
    except KeyError:
        raise InvalidEvidenceLevelError(f"unrecognised evidence_level: {level_raw!r}")

    # severity --------------------------------------------------------------
    severity_raw = raw.get('severity')
    if severity_raw is None:
        raise MissingRequiredFieldsError("'severity' is required")
    sev_str = _check_is_string(severity_raw, 'severity', InvalidSeverityError)
    severity = sev_str.upper()
    if severity not in ALLOWED_SEVERITIES:
        raise InvalidSeverityError(f"unrecognised severity: {severity_raw!r}")

    # confidence ------------------------------------------------------------
    confidence_raw = raw.get('confidence')
    if confidence_raw is None:
        raise MissingRequiredFieldsError("'confidence' is required")
    conf_str = _check_is_string(confidence_raw, 'confidence', InvalidConfidenceError)
    confidence = conf_str.upper()
    if confidence not in ALLOWED_CONFIDENCES:
        raise InvalidConfidenceError(f"unrecognised confidence: {confidence_raw!r}")

    # reference -------------------------------------------------------------
    reference_raw = raw.get('reference')
    reference: Optional[str] = None
    if reference_raw is not None:
        ref_str = _check_is_string(reference_raw, 'reference', InvalidReferenceError)
        if ref_str == '':
            reference = None
        else:
            _validate_reference(ref_str)
            reference = ref_str

    # verified_by -----------------------------------------------------------
    verified_by_raw = raw.get('verified_by')
    verified_by: Optional[str] = None
    if verified_by_raw is not None:
        verified_by_str = _check_is_string(verified_by_raw, 'verified_by', EvidenceValidationError)
        if verified_by_str == '':
            verified_by = None
        else:
            verified_by = verified_by_str

    # message ---------------------------------------------------------------
    message_raw = raw.get('message')
    message: Optional[str] = None
    if message_raw is not None:
        message = _check_is_string(message_raw, 'message', EvidenceValidationError)

    # Verification-specific checks -----------------------------------------
    if level == EvidenceLevel.FIXED_AND_VERIFIED:
        if not reference:
            raise MissingRequiredFieldsError(
                "'reference' is required for fixed_and_verified evidence"
            )
        if not verified_by:
            raise MissingRequiredFieldsError(
                "'verified_by' is required for fixed_and_verified evidence"
            )
    else:
        if verified_by is not None:
            raise FabricatedVerificationError(
                "'verified_by' is only allowed for fixed_and_verified evidence"
            )

    return EvidenceRecord(
        evidence_level=level,
        severity=severity,
        confidence=confidence,
        reference=reference,
        verified_by=verified_by,
        message=message,
    )
