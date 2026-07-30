"""Repository-local feature switches for AIWorkHub.

The settings file is deliberately small, non-executable JSON below the
repository's canonical ``.aiworkhub/config`` authority.  Reads are bounded
and fail closed on malformed or symlinked state.  Updates use an optimistic
revision so two VS Code windows cannot silently overwrite one another.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA_ID = "aiworkhub.feature_settings.v1"
SETTINGS_RELATIVE_PATH = Path(".aiworkhub/config/features.json")
MAX_SETTINGS_BYTES = 16 * 1024
FEATURE_KEYS: tuple[str, ...] = (
    "source_graph",
    "session_manager",
    "ai_memory",
    "knowledge_base",
    "context_graph",
)
DEFAULT_FEATURES: dict[str, bool] = {
    "source_graph": True,
    "session_manager": True,
    "ai_memory": True,
    "knowledge_base": True,
    # The transcript-backed Context Graph runtime is introduced separately.
    # Persist the opt-in now without claiming that an unavailable runtime is
    # active.
    "context_graph": False,
}


class FeatureSettingsError(RuntimeError):
    """Malformed, stale or unsafe repository feature settings."""


def settings_path(repo_root: Path | str) -> Path:
    return Path(repo_root).resolve() / SETTINGS_RELATIVE_PATH


def _public_payload(*, features: Mapping[str, bool], revision: int, configured: bool,
                    updated_at: str = "") -> dict[str, Any]:
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "revision": revision,
        "configured": configured,
        "features": {key: bool(features[key]) for key in FEATURE_KEYS},
        "capabilities": {
            "context_graph_runtime": False,
            "task_orchestration_locked_on": True,
            "callback_routing_locked_on": True,
        },
        "updated_at": updated_at,
    }


def _validate(value: Any) -> tuple[dict[str, bool], int, str]:
    if not isinstance(value, Mapping) or value.get("schema_id") != SCHEMA_ID:
        raise FeatureSettingsError("feature_settings_schema_invalid")
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise FeatureSettingsError("feature_settings_revision_invalid")
    features = value.get("features")
    if not isinstance(features, Mapping) or set(features) != set(FEATURE_KEYS):
        raise FeatureSettingsError("feature_settings_keys_invalid")
    normalized: dict[str, bool] = {}
    for key in FEATURE_KEYS:
        if not isinstance(features.get(key), bool):
            raise FeatureSettingsError(f"feature_setting_not_boolean:{key}")
        normalized[key] = features[key]
    updated_at = str(value.get("updated_at") or "")[:64]
    return normalized, revision, updated_at


def load(repo_root: Path | str) -> dict[str, Any]:
    path = settings_path(repo_root)
    if not path.exists():
        return _public_payload(features=DEFAULT_FEATURES, revision=0, configured=False)
    try:
        info = path.lstat()
    except OSError as exc:
        raise FeatureSettingsError(f"feature_settings_unreadable:{type(exc).__name__}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise FeatureSettingsError("feature_settings_must_be_regular_file")
    if info.st_size > MAX_SETTINGS_BYTES:
        raise FeatureSettingsError("feature_settings_too_large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeatureSettingsError(f"feature_settings_invalid_json:{type(exc).__name__}") from exc
    features, revision, updated_at = _validate(value)
    return _public_payload(
        features=features, revision=revision, configured=True, updated_at=updated_at
    )


def enabled(repo_root: Path | str, feature: str) -> bool:
    if feature not in FEATURE_KEYS:
        raise FeatureSettingsError(f"feature_setting_unknown:{feature}")
    return bool(load(repo_root)["features"][feature])


def update(
    repo_root: Path | str,
    *,
    changes: Mapping[str, Any],
    expected_revision: int,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    if not (root / ".aiworkhub/project.json").is_file():
        raise FeatureSettingsError("repository_not_initialized")
    if not isinstance(changes, Mapping) or not changes:
        raise FeatureSettingsError("feature_settings_changes_required")
    if set(changes) - set(FEATURE_KEYS):
        raise FeatureSettingsError("feature_settings_unknown_key")
    if any(not isinstance(value, bool) for value in changes.values()):
        raise FeatureSettingsError("feature_settings_changes_must_be_boolean")

    current = load(root)
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision != current["revision"]
    ):
        raise FeatureSettingsError("feature_settings_revision_conflict")
    features = dict(current["features"])
    features.update({str(key): bool(value) for key, value in changes.items()})
    revision = int(current["revision"]) + 1
    updated_at = datetime.now(timezone.utc).isoformat()
    stored = {
        "schema_id": SCHEMA_ID,
        "revision": revision,
        "updated_at": updated_at,
        "features": features,
    }

    path = settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise FeatureSettingsError("feature_settings_must_be_regular_file")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = (json.dumps(stored, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return _public_payload(
        features=features, revision=revision, configured=True, updated_at=updated_at
    )


def disabled_result(feature: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "disabled",
        "error": f"feature_disabled:{feature}",
        "feature": feature,
    }


__all__ = [
    "DEFAULT_FEATURES",
    "FEATURE_KEYS",
    "FeatureSettingsError",
    "SCHEMA_ID",
    "disabled_result",
    "enabled",
    "load",
    "settings_path",
    "update",
]
