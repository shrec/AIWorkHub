"""Deterministic attempt artifact manifest contract."""
from __future__ import annotations
import json, posixpath, re
from dataclasses import dataclass, field
from typing import Any, ClassVar

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
