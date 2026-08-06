"""Standalone SARIF 2.1.0 contract primitives.

This module provides deterministic, prose-independent SARIF document
construction and stable structural fingerprints for findings, intended for
later integration with known_bug_scanner.to_sarif and
quality_evidence.adapt_sarif. It intentionally has no dependency on those
modules and does not modify them.

Design goals:
- Deterministic output: identical structural identity -> identical SARIF and
  identical fingerprint, regardless of free-text prose ordering/wording, and
  independent of mutable severity classification.
- Safe by default: path traversal and unsafe absolute paths are rejected,
  including during fingerprint computation (fail-closed).
- Explicit provenance: emitted SARIF documents carry scope and VCS metadata.
- No sensitive payloads: PoC, secret, and raw exploit fields are never
  serialized into the SARIF output, even if present on input findings.
- Stable identity: results are sorted by structural fingerprint so
  equivalent finding sets produce byte-equivalent documents regardless of
  input order.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA_URI = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/"
    "sarif-schema-2.1.0.json"
)

# Fields that must never be serialized into SARIF output, even if present on
# an input finding mapping.
_SENSITIVE_FIELDS = frozenset({"poc", "secret", "raw_exploit", "exploit", "payload"})

_SYNTHETIC_LOCATION_MARKER = "synthetic:no-location"

_MAX_RULE_ID_LEN = 256


class UnsafePathError(ValueError):
    """Raised when a finding references a path outside the analyzed scope."""


class InvalidFindingError(ValueError):
    """Raised when a finding fails structural validation."""


@dataclass(frozen=True)
class VcsProvenance:
    """VCS provenance metadata attached to a SARIF run.

    Optional fields, when present, must be bounded non-empty strings free of
    control characters. No scheme allowlist is imposed so legitimate URI or
    plain reference forms (e.g. commit hashes, branch names) are accepted.
    """

    repository_uri: str | None = None
    revision_id: str | None = None
    branch: str | None = None

    _MAX_FIELD_LEN = 2048

    def __post_init__(self) -> None:
        for field_name in ("repository_uri", "revision_id", "branch"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise InvalidFindingError(
                    f"VcsProvenance.{field_name} must be a string when present"
                )
            if not value.strip():
                raise InvalidFindingError(
                    f"VcsProvenance.{field_name} must be non-empty when present"
                )
            if len(value) > self._MAX_FIELD_LEN:
                raise InvalidFindingError(
                    f"VcsProvenance.{field_name} exceeds maximum length of "
                    f"{self._MAX_FIELD_LEN} characters"
                )
            if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
                raise InvalidFindingError(
                    f"VcsProvenance.{field_name} must not contain control characters"
                )

    def to_sarif_version_control(self) -> list[dict[str, Any]] | None:
        if not (self.repository_uri or self.revision_id or self.branch):
            return None
        entry: dict[str, Any] = {}
        if self.repository_uri:
            entry["repositoryUri"] = self.repository_uri
        if self.revision_id:
            entry["revisionId"] = self.revision_id
        if self.branch:
            entry["branch"] = self.branch
        return [entry]


@dataclass(frozen=True)
class ScopeProvenance:
    """Explicit metadata describing the analyzed scope of a run."""

    scope_root: str
    tool_name: str = "aiworkhub-sarif-contract"
    tool_version: str = "1.0.0"
    vcs: VcsProvenance = field(default_factory=VcsProvenance)

    def __post_init__(self) -> None:
        if not is_safe_scope_root(self.scope_root):
            raise UnsafePathError(f"unsafe scope_root rejected: {self.scope_root!r}")


def is_safe_scope_root(scope_root: str) -> bool:
    """Return True if ``scope_root`` is ``.`` or a safe repository-relative path."""
    if scope_root == ".":
        return True
    return is_safe_relative_path(scope_root)


def is_safe_relative_path(path: str) -> bool:
    """Return True if ``path`` is a safe, scope-relative path.

    Rejects absolute paths, empty paths, and any path containing a ``..``
    traversal segment after normalization.
    """
    if not path:
        return False
    if path.startswith("/") or path.startswith("\\"):
        return False
    # Reject drive-letter style absolute paths (e.g. C:\...) defensively.
    if len(path) >= 2 and path[1] == ":":
        return False
    normalized = posixpath.normpath(path.replace("\\", "/"))
    if normalized == ".." or normalized.startswith("../"):
        return False
    if normalized.startswith("/"):
        return False
    return True


def _require_safe_path(path: str) -> str:
    if not is_safe_relative_path(path):
        raise UnsafePathError(f"unsafe or traversal path rejected: {path!r}")
    return posixpath.normpath(path.replace("\\", "/"))


def _strip_sensitive(finding: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in finding.items() if k.lower() not in _SENSITIVE_FIELDS}


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidFindingError(f"{field_name} must be a positive integer")
    if value <= 0:
        raise InvalidFindingError(f"{field_name} must be a positive integer")
    return value


def _require_rule_id(finding: Mapping[str, Any]) -> str:
    rule_id = finding.get("rule_id", finding.get("id"))
    if not isinstance(rule_id, str):
        raise InvalidFindingError("rule_id must be a non-empty string")
    rule_id_str = rule_id.strip()
    if not rule_id_str:
        raise InvalidFindingError("rule_id is required and must be non-empty")
    if len(rule_id_str) > _MAX_RULE_ID_LEN:
        raise InvalidFindingError(
            f"rule_id exceeds maximum length of {_MAX_RULE_ID_LEN} characters"
        )
    return rule_id_str


def normalize_structural_identity(finding: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the prose- and severity-independent structural identity.

    Only stable, semantic fields participate in fingerprinting: rule_id,
    normalized path, start line/column, and an optional explicit
    finding_id/id identity component. Free-text message prose and mutable
    severity are deliberately excluded so that rewording or reclassifying
    severity does not change the fingerprint or alert identity.

    Raises ``UnsafePathError`` if the path is unsafe/traversal, and
    ``InvalidFindingError`` if rule_id is missing/empty or line/column are
    not positive integers when present.
    """
    rule_id = _require_rule_id(finding)
    path = finding.get("path")
    line = finding.get("line")
    column = finding.get("column")
    finding_id = finding.get("finding_id")

    if path is not None and not isinstance(path, str):
        raise InvalidFindingError("path must be a string when present")
    normalized_path = _require_safe_path(path) if path else None
    normalized_line = _require_positive_int(line, "line") if line is not None else None
    normalized_column = (
        _require_positive_int(column, "column") if column is not None else None
    )

    if finding_id is not None:
        if not isinstance(finding_id, str):
            raise InvalidFindingError("finding_id must be a string when present")
        finding_id = finding_id.strip()
        if not finding_id or len(finding_id) > _MAX_RULE_ID_LEN:
            raise InvalidFindingError(
                "finding_id must be a non-empty bounded string when present"
            )

    identity: dict[str, Any] = {
        "rule_id": rule_id,
        "path": normalized_path,
        "line": normalized_line,
        "column": normalized_column,
        "finding_id": finding_id if finding_id not in (None, "") else None,
    }
    return identity


def compute_fingerprint(finding: Mapping[str, Any]) -> str:
    """Compute a deterministic, prose- and severity-independent fingerprint.

    The fingerprint is a stable sha256 hex digest of the finding's
    normalized structural identity (rule id, path, location, optional
    finding_id). Reordering or rewording the message/description, or
    changing severity, does not affect it. Fails closed (raises) on unsafe
    paths or invalid structural fields rather than silently fingerprinting
    unsafe input.
    """
    identity = normalize_structural_identity(finding)
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_location(finding: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Build a SARIF location list; returns (locations, is_synthetic).

    If the finding has no path, an explicit synthetic anchor location is
    emitted so that locationless findings remain observable rather than
    silently dropped.
    """
    path = finding.get("path")
    if not path:
        return (
            [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": _SYNTHETIC_LOCATION_MARKER},
                    },
                    "message": {"text": "no source location available"},
                }
            ],
            True,
        )

    if not isinstance(path, str):
        raise InvalidFindingError("path must be a string when present")

    safe_path = _require_safe_path(path)
    region: dict[str, Any] = {}
    line = finding.get("line")
    column = finding.get("column")
    if line is not None:
        region["startLine"] = _require_positive_int(line, "line")
    if column is not None:
        region["startColumn"] = _require_positive_int(column, "column")

    physical_location: dict[str, Any] = {"artifactLocation": {"uri": safe_path}}
    if region:
        physical_location["region"] = region

    return [{"physicalLocation": physical_location}], False


def finding_to_sarif_result(finding: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a single finding mapping into a SARIF result object.

    Sensitive fields (poc, secret, raw_exploit, exploit, payload) are
    stripped and never serialized. Fails closed on unsafe paths and invalid
    structural fields (missing/empty rule_id, non-positive line/column).
    """
    clean = _strip_sensitive(finding)
    normalized_rule_id = _require_rule_id(clean)  # fail closed before building anything else
    locations, is_synthetic = _build_location(clean)
    fingerprint = compute_fingerprint(clean)

    result: dict[str, Any] = {
        "ruleId": normalized_rule_id,
        "level": _severity_to_level(str(clean.get("severity", ""))),
        "message": {"text": str(clean.get("message", clean.get("description", "")))},
        "locations": locations,
        "partialFingerprints": {"structuralIdentity/v1": fingerprint},
        "properties": {"syntheticLocation": is_synthetic},
    }
    return result


def _severity_to_level(severity: str) -> str:
    mapping = {
        "critical": "error",
        "high": "error",
        "medium": "warning",
        "low": "note",
        "info": "note",
    }
    return mapping.get(severity.lower(), "warning")


def build_sarif_log(
    findings: Sequence[Mapping[str, Any]],
    scope: ScopeProvenance,
) -> dict[str, Any]:
    """Build a deterministic SARIF 2.1.0 log document.

    An empty ``findings`` sequence produces a valid run with an empty
    ``results`` array and explicit scope/provenance metadata, suitable for
    clean-run alert resolution. Results are sorted by structural fingerprint
    so that equivalent finding sets produce byte-equivalent documents
    regardless of input order.
    """
    results_with_fp = [
        (compute_fingerprint(_strip_sensitive(f)), finding_to_sarif_result(f))
        for f in findings
    ]
    results_with_fp.sort(
        key=lambda pair: (
            pair[0],
            json.dumps(pair[1], sort_keys=True, separators=(",", ":")),
        )
    )
    results = [r for _, r in results_with_fp]

    run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": scope.tool_name,
                "version": scope.tool_version,
            }
        },
        "results": results,
        "properties": {
            "scopeRoot": scope.scope_root,
            "analyzedFindingCount": len(results),
        },
    }

    version_control = scope.vcs.to_sarif_version_control()
    if version_control is not None:
        run["versionControlProvenance"] = version_control

    return {
        "$schema": SARIF_SCHEMA_URI,
        "version": SARIF_VERSION,
        "runs": [run],
    }
