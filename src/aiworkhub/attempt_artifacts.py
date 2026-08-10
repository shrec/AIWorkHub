"""Deterministic, replay-verifiable attempt artifact bundles."""
from __future__ import annotations
import hashlib
import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .worker_workspace import write_json_0600

_ABSENT_SHA256_SENTINEL = "0000000000000000000000000000000000000000000000000000000000000000"
_EMPTY_FILE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_ALLOWED_ROLES: frozenset[str] = frozenset({"metadata","diff","validation","usage","review","sarif"})
_MAX_PATH_LEN = 4096
_MAX_ATTEMPT_ID_LEN = 256
_MAX_MEDIA_TYPE_LEN = 256
_TOP_LEVEL_FIELDS: frozenset[str] = frozenset({"attempt_id","artifacts"})
_ARTIFACT_FIELDS: frozenset[str] = frozenset({"path","sha256","byte_count","media_type","role","present","required"})
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_COMPONENT = re.compile(r"^[A-Za-z]:")
MANIFEST_FILENAME = "manifest.json"
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
ROLE_FILENAMES: dict[str, str] = {
    "metadata": "metadata.json",
    "diff": "diff.json",
    "validation": "validation.json",
    "usage": "usage.json",
    "review": "review.json",
    "sarif": "findings.sarif.json",
}
REQUIRED_BUNDLE_ROLES: frozenset[str] = frozenset(
    {"metadata", "diff", "validation", "usage", "review"}
)

class InvalidArtifactError(ValueError):
    """Raised when a single artifact entry fails structural validation."""
class InvalidManifestError(ValueError):
    """Raised when the manifest as a whole fails structural validation."""

def _is_control_code(codepoint: int) -> bool:
    return codepoint < 0x20 or 0x7F <= codepoint <= 0x9F

def _validate_safe_relative_path(path: str) -> None:
    if not path:
        raise InvalidArtifactError("artifact path must be non-empty")
    if len(path) > _MAX_PATH_LEN:
        raise InvalidArtifactError(f"artifact path exceeds maximum length of {_MAX_PATH_LEN} characters")
    if any(_is_control_code(ord(ch)) for ch in path):
        raise InvalidArtifactError("artifact path must not contain control characters")
    normalized = posixpath.normpath(path.replace("\\", "/"))
    if normalized == ".." or normalized.startswith("../"):
        raise InvalidArtifactError(f"directory traversal rejected: {path!r}")
    if normalized.startswith("/"):
        raise InvalidArtifactError(f"absolute path rejected: {path!r}")
    for component in normalized.split("/"):
        if _DRIVE_COMPONENT.match(component):
            raise InvalidArtifactError(f"drive-letter path rejected: {path!r}")
    if not normalized or normalized == ".":
        raise InvalidArtifactError(f"path resolves to nothing: {path!r}")

def _validate_sha256_hex(sha256: str) -> None:
    if not isinstance(sha256, str):
        raise InvalidArtifactError("sha256 must be a string")
    if not _SHA256_HEX.match(sha256):
        raise InvalidArtifactError("sha256 must be 64 lowercase hexadecimal characters")

@dataclass(frozen=True)
class ArtifactEntry:
    path: str
    sha256: str
    byte_count: int
    media_type: str
    role: str
    present: bool = True
    required: bool = True
    _ALLOWED_ROLES: ClassVar[frozenset[str]] = _ALLOWED_ROLES
    def __post_init__(self) -> None:
        _validate_safe_relative_path(self.path)
        _validate_sha256_hex(self.sha256)
        if isinstance(self.byte_count, bool) or not isinstance(self.byte_count, int):
            raise InvalidArtifactError("byte_count must be an integer")
        if self.byte_count < 0:
            raise InvalidArtifactError("byte_count must be non-negative")
        if not isinstance(self.media_type, str):
            raise InvalidArtifactError("media_type must be a string")
        if self.media_type != self.media_type.strip():
            raise InvalidArtifactError("media_type must not have leading or trailing whitespace")
        if not self.media_type:
            raise InvalidArtifactError("media_type must be non-empty")
        if len(self.media_type) > _MAX_MEDIA_TYPE_LEN:
            raise InvalidArtifactError(f"media_type exceeds maximum length")
        if any(_is_control_code(ord(ch)) for ch in self.media_type):
            raise InvalidArtifactError("media_type must not contain control characters")
        if self.role not in self._ALLOWED_ROLES:
            raise InvalidArtifactError(f"role must be one of {sorted(self._ALLOWED_ROLES)!r}")
        if not isinstance(self.present, bool):
            raise InvalidArtifactError("present must be a boolean")
        if not isinstance(self.required, bool):
            raise InvalidArtifactError("required must be a boolean")
        if self.present:
            if self.sha256 == _ABSENT_SHA256_SENTINEL:
                raise InvalidArtifactError("all-zero SHA-256 sentinel is reserved for present=False")
            if self.byte_count == 0 and self.sha256 != _EMPTY_FILE_SHA256:
                raise InvalidArtifactError("present empty artifact must use the SHA-256 digest of empty bytes")
            if self.byte_count > 0 and self.sha256 == _EMPTY_FILE_SHA256:
                raise InvalidArtifactError("non-empty artifact cannot have the SHA-256 digest of empty bytes")
        else:
            if self.sha256 != _ABSENT_SHA256_SENTINEL:
                raise InvalidArtifactError("absent artifact must use the all-zero SHA-256 sentinel")
            if self.byte_count != 0:
                raise InvalidArtifactError("absent artifact must have byte_count=0")
    def to_dict(self) -> dict[str, Any]:
        return {"byte_count":self.byte_count,"media_type":self.media_type,"path":self.path,"present":self.present,"required":self.required,"role":self.role,"sha256":self.sha256}

@dataclass
class AttemptArtifactManifest:
    attempt_id: str
    artifacts: list[ArtifactEntry] = field(default_factory=list)
    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str):
            raise InvalidManifestError("attempt_id must be a string")
        stripped_id = self.attempt_id.strip()
        if not stripped_id:
            raise InvalidManifestError("attempt_id must be non-empty")
        if len(stripped_id) > _MAX_ATTEMPT_ID_LEN:
            raise InvalidManifestError(f"attempt_id exceeds maximum length")
        if any(_is_control_code(ord(ch)) for ch in self.attempt_id):
            raise InvalidManifestError("attempt_id must not contain control characters")
        if "/" in stripped_id or chr(92) in stripped_id:
            raise InvalidManifestError("attempt_id must not contain path separators")
        if stripped_id in (".", ".."):
            raise InvalidManifestError("attempt_id must not be a traversal form")
        object.__setattr__(self, "attempt_id", stripped_id)
        if not isinstance(self.artifacts, list):
            raise InvalidManifestError("artifacts must be a list")
        validated = []
        for item in self.artifacts:
            if not isinstance(item, ArtifactEntry):
                raise InvalidManifestError(f"each artifact must be an ArtifactEntry")
            validated.append(item)
        seen: set[str] = set()
        for a in validated:
            if a.path in seen:
                raise InvalidManifestError(f"duplicate artifact path in manifest: {a.path!r}")
            seen.add(a.path)
        for a in validated:
            if a.required and not a.present:
                raise InvalidManifestError(f"required artifact {a.path!r} must be present")
        validated.sort(key=lambda a: a.path)
        self.artifacts = validated
    def to_dict(self) -> dict[str, Any]:
        return {"artifacts":[a.to_dict() for a in self.artifacts],"attempt_id":self.attempt_id}
    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, ensure_ascii=False, separators=(",",":") if indent is None else None)

def _duplicate_sensitive_object_pairs_hook(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidManifestError(f"duplicate key {key!r} in manifest JSON")
        result[key] = value
    return result

def _raw_to_artifact_entry(raw: Any) -> ArtifactEntry:
    if not isinstance(raw, dict):
        raise InvalidManifestError("each artifact must be a JSON object")
    extra = set(raw.keys()) - _ARTIFACT_FIELDS
    if extra:
        raise InvalidManifestError(f"unknown fields in artifact: {sorted(extra)!r}")
    path = raw.get("path")
    if path is None or not isinstance(path, str):
        raise InvalidManifestError(f"path must be a string")
    sha256 = raw.get("sha256")
    if sha256 is None or not isinstance(sha256, str):
        raise InvalidManifestError(f"sha256 must be a string")
    byte_count = raw.get("byte_count")
    if byte_count is None:
        raise InvalidManifestError("missing required field: byte_count")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int):
        raise InvalidManifestError(f"byte_count must be an integer")
    media_type = raw.get("media_type")
    if media_type is None or not isinstance(media_type, str):
        raise InvalidManifestError(f"media_type must be a string")
    role = raw.get("role")
    if role is None or not isinstance(role, str):
        raise InvalidManifestError(f"role must be a string")
    present = raw.get("present", True)
    if not isinstance(present, bool):
        raise InvalidManifestError(f"present must be a boolean")
    required = raw.get("required", True)
    if not isinstance(required, bool):
        raise InvalidManifestError(f"required must be a boolean")
    return ArtifactEntry(path=path,sha256=sha256,byte_count=byte_count,media_type=media_type,role=role,present=present,required=required)

def parse_manifest_json(json_str: str) -> AttemptArtifactManifest:
    if not isinstance(json_str, str):
        raise InvalidManifestError("manifest JSON must be a string")
    try:
        raw = json.loads(json_str, object_pairs_hook=_duplicate_sensitive_object_pairs_hook)
    except json.JSONDecodeError as exc:
        raise InvalidManifestError(f"invalid JSON: {exc}") from exc
    except InvalidManifestError:
        raise
    except Exception as exc:
        raise InvalidManifestError(f"failed to parse manifest JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise InvalidManifestError("manifest JSON must be a top-level object")
    extra = set(raw.keys()) - _TOP_LEVEL_FIELDS
    if extra:
        raise InvalidManifestError(f"unknown top-level fields: {sorted(extra)!r}")
    attempt_id = raw.get("attempt_id")
    if attempt_id is None or not isinstance(attempt_id, str):
        raise InvalidManifestError(f"attempt_id must be a string")
    raw_artifacts = raw.get("artifacts", [])
    if not isinstance(raw_artifacts, list):
        raise InvalidManifestError(f"artifacts must be a JSON array")
    entries = []
    for idx, item in enumerate(raw_artifacts):
        try:
            entries.append(_raw_to_artifact_entry(item))
        except (InvalidArtifactError, InvalidManifestError):
            raise
        except Exception as exc:
            raise InvalidManifestError(f"invalid artifact at index {idx}: {exc}") from exc
    return AttemptArtifactManifest(attempt_id=attempt_id, artifacts=entries)

def validate_artifact_path(path: str) -> bool:
    _validate_safe_relative_path(path)
    return True


def _canonical_json_bytes(payload: Any) -> bytes:
    try:
        data = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InvalidArtifactError(
            f"artifact payload is not deterministic JSON: {type(exc).__name__}"
        ) from exc
    if len(data) > MAX_ARTIFACT_BYTES:
        raise InvalidArtifactError("artifact payload exceeds bounded size")
    return data


def persist_json_bundle(
    bundle_dir: Path,
    *,
    attempt_id: str,
    payloads: dict[str, Any],
) -> dict[str, Any]:
    """Atomically persist and immediately verify one attempt bundle.

    Required roles are always present. SARIF is optional. Paths are fixed by
    role, never caller-controlled, and every manifest digest is computed from
    the exact bytes subsequently verified from disk.
    """

    extra_roles = set(payloads) - set(ROLE_FILENAMES)
    if extra_roles:
        raise InvalidManifestError(
            f"unknown artifact roles: {sorted(extra_roles)!r}"
        )
    missing = REQUIRED_BUNDLE_ROLES - set(payloads)
    if missing:
        raise InvalidManifestError(
            f"missing required artifact roles: {sorted(missing)!r}"
        )
    if bundle_dir.is_symlink():
        raise InvalidManifestError("attempt artifact bundle root is a symlink")
    bundle_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not bundle_dir.is_dir() or bundle_dir.is_symlink():
        raise InvalidManifestError("attempt artifact bundle root is not a directory")

    entries: list[ArtifactEntry] = []
    for role in sorted(payloads):
        filename = ROLE_FILENAMES[role]
        data = _canonical_json_bytes(payloads[role])
        # Reuse the cross-platform owner-only atomic JSON writer, then hash the
        # exact committed bytes rather than the pre-write Python object.
        write_json_0600(bundle_dir / filename, payloads[role])
        committed = (bundle_dir / filename).read_bytes()
        if committed != data:
            raise InvalidArtifactError(
                f"artifact byte fidelity mismatch for role {role!r}"
            )
        entries.append(
            ArtifactEntry(
                path=filename,
                sha256=hashlib.sha256(committed).hexdigest(),
                byte_count=len(committed),
                media_type=(
                    "application/sarif+json"
                    if role == "sarif"
                    else "application/json"
                ),
                role=role,
                present=True,
                required=role in REQUIRED_BUNDLE_ROLES,
            )
        )

    manifest = AttemptArtifactManifest(attempt_id=attempt_id, artifacts=entries)
    write_json_0600(bundle_dir / MANIFEST_FILENAME, manifest.to_dict())
    verified = verify_json_bundle(bundle_dir)
    return {
        "schema_id": "aiworkhub.attempt_artifact_bundle_receipt.v1",
        "attempt_id": attempt_id,
        "manifest_path": str(bundle_dir / MANIFEST_FILENAME),
        "manifest_sha256": hashlib.sha256(
            (bundle_dir / MANIFEST_FILENAME).read_bytes()
        ).hexdigest(),
        "artifact_count": len(entries),
        "roles": [entry.role for entry in manifest.artifacts],
        "verified": verified["verified"],
    }


def verify_json_bundle(bundle_dir: Path) -> dict[str, Any]:
    """Re-read and hash every manifest-bound artifact without following links."""

    manifest_path = bundle_dir / MANIFEST_FILENAME
    if bundle_dir.is_symlink() or manifest_path.is_symlink():
        raise InvalidManifestError("attempt artifact bundle identity is unsafe")
    try:
        raw_manifest = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidManifestError("attempt artifact manifest is unavailable") from exc
    manifest = parse_manifest_json(raw_manifest)
    observed_roles: set[str] = set()
    for entry in manifest.artifacts:
        artifact_path = bundle_dir / entry.path
        if artifact_path.parent != bundle_dir or artifact_path.is_symlink():
            raise InvalidArtifactError("artifact path escaped or became a symlink")
        try:
            data = artifact_path.read_bytes()
        except OSError as exc:
            raise InvalidArtifactError(
                f"artifact is unavailable for role {entry.role!r}"
            ) from exc
        if len(data) != entry.byte_count:
            raise InvalidArtifactError(
                f"artifact byte count mismatch for role {entry.role!r}"
            )
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            raise InvalidArtifactError(
                f"artifact digest mismatch for role {entry.role!r}"
            )
        observed_roles.add(entry.role)
    missing = REQUIRED_BUNDLE_ROLES - observed_roles
    if missing:
        raise InvalidManifestError(
            f"manifest omits required roles: {sorted(missing)!r}"
        )
    return {
        "schema_id": "aiworkhub.attempt_artifact_bundle_verification.v1",
        "attempt_id": manifest.attempt_id,
        "artifact_count": len(manifest.artifacts),
        "roles": sorted(observed_roles),
        "verified": True,
    }
