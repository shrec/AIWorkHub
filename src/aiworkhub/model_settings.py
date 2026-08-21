"""Repository-local model routing policy for AIWorkHub.

The settings file is deliberately small, non-executable JSON below the
repository's canonical ``.aiworkhub/config`` authority.  It stores only
provider/adapter/model identity strings and boolean enablement switches,
so it accepts no credentials, commands, executable code or file paths.
Reads are bounded and fail closed on malformed, oversized, symlinked or
duplicate-bearing state. Updates use an optimistic revision so two VS Code
windows cannot silently overwrite one another. Evaluation defaults to
enabled, while provider and adapter disables remain hard parent gates for
every child route.
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from aiworkhub.platform_io import AdvisoryLockTimeout, lock_fd, unlock_fd

SCHEMA_ID = "aiworkhub.model_settings.v1"
SETTINGS_RELATIVE_PATH = Path(".aiworkhub/config/models.json")
MAX_SETTINGS_BYTES = 64 * 1024
MAX_IDENTITY_CHARS = 128
DEFAULT_ENABLED = True
_STORED_FIELDS = frozenset(
    {"schema_id", "revision", "updated_at", "providers", "adapters", "models"}
)


class ModelSettingsError(RuntimeError):
    """Malformed, stale or unsafe repository model settings."""


def settings_path(repo_root: Path | str) -> Path:
    return Path(repo_root).resolve() / SETTINGS_RELATIVE_PATH


def _public_payload(
    *,
    providers: Mapping[str, bool],
    adapters: Mapping[str, Mapping[str, bool]],
    models: Mapping[str, Mapping[str, Mapping[str, bool]]],
    revision: int,
    configured: bool,
    updated_at: str = "",
) -> dict[str, Any]:
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "revision": revision,
        "configured": configured,
        "default_enabled": DEFAULT_ENABLED,
        "providers": {key: bool(value) for key, value in providers.items()},
        "adapters": {
            provider: {key: bool(value) for key, value in transport.items()}
            for provider, transport in adapters.items()
        },
        "models": {
            provider: {
                adapter: {key: bool(value) for key, value in leaves.items()}
                for adapter, leaves in transport.items()
            }
            for provider, transport in models.items()
        },
        "updated_at": updated_at,
    }


def _validate_identity(value: Any, kind: str) -> str:
    if not isinstance(value, str) or not value:
        raise ModelSettingsError(f"model_settings_identity_invalid:{kind}")
    if len(value) > MAX_IDENTITY_CHARS:
        raise ModelSettingsError(f"model_settings_identity_too_long:{kind}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ModelSettingsError(f"model_settings_identity_control_characters:{kind}")
    return value


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, value in pairs:
        if key in decoded:
            raise ModelSettingsError("model_settings_duplicate_key")
        decoded[key] = value
    return decoded


def _section(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = value.get(name)
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise ModelSettingsError(f"model_settings_section_invalid:{name}")
    return section


def _bool_leaves(section: Any, kind: str) -> dict[str, bool]:
    if not isinstance(section, Mapping):
        raise ModelSettingsError(f"model_settings_section_invalid:{kind}")
    normalized: dict[str, bool] = {}
    for key, value in section.items():
        identity = _validate_identity(key, kind)
        if not isinstance(value, bool):
            raise ModelSettingsError(f"model_settings_not_boolean:{kind}")
        normalized[identity] = value
    return normalized


def _validate(
    value: Any,
) -> tuple[
    dict[str, bool],
    dict[str, dict[str, bool]],
    dict[str, dict[str, dict[str, bool]]],
    int,
    str,
]:
    if not isinstance(value, Mapping) or value.get("schema_id") != SCHEMA_ID:
        raise ModelSettingsError("model_settings_schema_invalid")
    if set(value) - _STORED_FIELDS:
        raise ModelSettingsError("model_settings_unknown_field")
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ModelSettingsError("model_settings_revision_invalid")

    providers = _bool_leaves(_section(value, "providers"), "provider")
    adapters: dict[str, dict[str, bool]] = {}
    for provider_key, transport_section in _section(value, "adapters").items():
        provider = _validate_identity(provider_key, "provider")
        adapters[provider] = _bool_leaves(transport_section, "adapter")
    models: dict[str, dict[str, dict[str, bool]]] = {}
    for provider_key, transport_section in _section(value, "models").items():
        provider = _validate_identity(provider_key, "provider")
        if not isinstance(transport_section, Mapping):
            raise ModelSettingsError("model_settings_section_invalid:adapter")
        transports: dict[str, dict[str, bool]] = {}
        for adapter_key, leaves in transport_section.items():
            adapter = _validate_identity(adapter_key, "adapter")
            transports[adapter] = _bool_leaves(leaves, "model")
        models[provider] = transports
    updated_at = str(value.get("updated_at") or "")[:64]
    return providers, adapters, models, revision, updated_at


def load(repo_root: Path | str) -> dict[str, Any]:
    path = settings_path(repo_root)
    if not path.exists():
        return _public_payload(
            providers={}, adapters={}, models={}, revision=0, configured=False
        )
    try:
        info = path.lstat()
    except OSError as exc:
        raise ModelSettingsError(
            f"model_settings_unreadable:{type(exc).__name__}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ModelSettingsError("model_settings_must_be_regular_file")
    if info.st_size > MAX_SETTINGS_BYTES:
        raise ModelSettingsError("model_settings_too_large")
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(text, object_pairs_hook=_object_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelSettingsError(
            f"model_settings_invalid_json:{type(exc).__name__}"
        ) from exc
    providers, adapters, models, revision, updated_at = _validate(value)
    return _public_payload(
        providers=providers,
        adapters=adapters,
        models=models,
        revision=revision,
        configured=True,
        updated_at=updated_at,
    )


def evaluate(
    repo_root: Path | str,
    *,
    provider: str,
    adapter: str | None = None,
    model: str | None = None,
) -> bool:
    """Effective enablement for one observed route.

    Parent switches are hard gates. An exact model switch refines only a
    provider and adapter that remain enabled. Identities never collide
    because every level keeps its own key namespace.
    """
    _validate_identity(provider, "provider")
    if model is not None and adapter is None:
        raise ModelSettingsError("model_settings_model_requires_adapter")
    if adapter is not None:
        _validate_identity(adapter, "adapter")
    if model is not None:
        _validate_identity(model, "model")

    return evaluate_state(
        load(repo_root), provider=provider, adapter=adapter, model=model
    )


def evaluate_state(
    state: Mapping[str, Any],
    *,
    provider: str,
    adapter: str | None = None,
    model: str | None = None,
) -> bool:
    """Evaluate one route against an already loaded policy snapshot."""
    _validate_identity(provider, "provider")
    if model is not None and adapter is None:
        raise ModelSettingsError("model_settings_model_requires_adapter")
    if adapter is not None:
        _validate_identity(adapter, "adapter")
    if model is not None:
        _validate_identity(model, "model")
    # Parent switches are hard gates.  An exact child override can refine an
    # enabled route, but it must never resurrect a provider or adapter that
    # the repository owner disabled as a group.
    if not bool(state["providers"].get(provider, DEFAULT_ENABLED)):
        return False
    if adapter is not None and not bool(
        state["adapters"].get(provider, {}).get(adapter, DEFAULT_ENABLED)
    ):
        return False
    if model is not None:
        override = state["models"].get(provider, {}).get(adapter, {})
        if model in override:
            return bool(override[model])
    return True


@contextmanager
def _update_lock(path: Path):
    """Serialize revision compare-and-swap across editor host processes."""

    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ModelSettingsError(
            f"model_settings_lock_unavailable:{type(exc).__name__}"
        ) from exc
    acquired = False
    try:
        try:
            lock_fd(fd, blocking=True)
            acquired = True
        except (AdvisoryLockTimeout, OSError) as exc:
            raise ModelSettingsError("model_settings_update_busy") from exc
        yield
    finally:
        if acquired:
            unlock_fd(fd)
        os.close(fd)


def update(
    repo_root: Path | str,
    *,
    provider: str,
    adapter: str | None = None,
    model: str | None = None,
    enabled: bool,
    expected_revision: int,
) -> dict[str, Any]:
    """Set exactly one provider, adapter or exact-model switch."""
    root = Path(repo_root).resolve()
    if not (root / ".aiworkhub/project.json").is_file():
        raise ModelSettingsError("repository_not_initialized")
    _validate_identity(provider, "provider")
    if model is not None and adapter is None:
        raise ModelSettingsError("model_settings_model_requires_adapter")
    if adapter is not None:
        _validate_identity(adapter, "adapter")
    if model is not None:
        _validate_identity(model, "model")
    if not isinstance(enabled, bool):
        raise ModelSettingsError("model_settings_enabled_must_be_bool")

    path = settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _update_lock(path):
        # The authoritative revision check and replacement share one advisory
        # lock, so two VS Code processes cannot both commit the same revision.
        current = load(root)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision != current["revision"]
        ):
            raise ModelSettingsError("model_settings_revision_conflict")

        providers = dict(current["providers"])
        adapters = {
            key: dict(transport) for key, transport in current["adapters"].items()
        }
        models = {
            provider_key: {
                adapter_key: dict(leaves)
                for adapter_key, leaves in transport.items()
            }
            for provider_key, transport in current["models"].items()
        }
        if model is not None:
            models.setdefault(provider, {}).setdefault(adapter, {})[model] = enabled
        elif adapter is not None:
            adapters.setdefault(provider, {})[adapter] = enabled
        else:
            providers[provider] = enabled

        revision = int(current["revision"]) + 1
        updated_at = datetime.now(timezone.utc).isoformat()
        stored = {
            "schema_id": SCHEMA_ID,
            "revision": revision,
            "updated_at": updated_at,
            "providers": providers,
            "adapters": adapters,
            "models": models,
        }
        payload = (
            json.dumps(stored, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        if len(payload) > MAX_SETTINGS_BYTES:
            raise ModelSettingsError("model_settings_too_large")

        if path.exists() and path.is_symlink():
            raise ModelSettingsError("model_settings_must_be_regular_file")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
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
            providers=providers,
            adapters=adapters,
            models=models,
            revision=revision,
            configured=True,
            updated_at=updated_at,
        )
