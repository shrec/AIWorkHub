"""Repo-owned executable registration for runtime adapter plans.

NF01250: an owner (the manager, only after artifact qualification) registers
a qualified private executable for one adapter by writing a small JSON
manifest under ``.aiworkhub/runtime/`` inside the repository.  This module
never writes, auto-discovers or falls back: it reads the one declared
manifest, validates it strictly, and returns adapter executable overrides
for the shared command builder in :mod:`aiworkhub.runtime_adapters`.  Any
malformed, unsafe, platform-mismatched or SHA-tampered registration fails
closed with a concrete reason instead of deferring to the adapter's
default PATH resolution.

The manifest shape is intentionally strict and repo-local only::

    {
      "schema": "aiworkhub.executable_registration.v1",
      "registrations": {
        "opencode_cli": {
          "platform": "linux",
          "artifact_version": "2.0.16+aiworkhub.winfix.1",
          "source_tag": "v2.0.16",
          "source_commit": "3a103fe0aff726a4edc7492f03f7b88195d9e4c9",
          "fixture_kind": "host_tested_only",
          "executable_relative": "artifacts/opencode-2.0.16-winfix.1/bin/opencode",
          "sha256": "545c62afcfb70baff923127bed854f31d9d044d54715510c29fee71ea3714ca9"
        }
      }
    }

No PATH, global, profile, admin, drive-letter or parent-ancestor grant is
ever accepted.  Explicit adapter ``executable_overrides`` supplied by a
caller keep priority over any registration; the shared builder in
``runtime_adapters`` consumes the selected registration BEFORE
``resolve_executable``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import platform_io

MANIFEST_SCHEMA_ID = "aiworkhub.executable_registration.v1"
REGISTRY_DIR_NAME = ".aiworkhub"
REGISTRY_FILENAME = "executable_registration.json"
_EXPECTED_REGISTRATION_KEYS = frozenset(
    {
        "platform",
        "artifact_version",
        "source_tag",
        "source_commit",
        "executable_relative",
        "sha256",
        "fixture_kind",
    }
)
_UNSAFE_RELATIVE_SUFFIXES = ("/", "\\", "/..", "\\..")


class ExecutableRegistrationError(Exception):
    """A registration is malformed, unsafe, mismatched, or missing."""

    def __init__(self, adapter_id: str, reason: str) -> None:
        self.adapter_id = adapter_id
        self.reason = reason
        super().__init__(f"executable_registration:{adapter_id}:{reason}")


def _is_registration_schema(document: Mapping[str, Any] | None) -> bool:
    return (
        isinstance(document, Mapping)
        and set(document) == {"schema", "registrations"}
        and document.get("schema") == MANIFEST_SCHEMA_ID
        and isinstance(document.get("registrations"), Mapping)
    )


def _read_registration_manifest(
    repo: Path,
) -> tuple[Mapping[str, Any] | None, str | None]:
    """Read the declared manifest; failure surfaces a concrete reason."""

    manifest_path = (
        repo / REGISTRY_DIR_NAME / "runtime" / REGISTRY_FILENAME
    )
    if not manifest_path.exists() and not manifest_path.is_symlink():
        # Genuinely absent manifest: siblings and unregistered defaults
        # must keep resolving exactly as they did before this feature.
        return None, None
    if not manifest_path.is_file():
        # A selected path that exists but is a directory, dangling symlink,
        # or other non-regular file is an invalid explicit registration,
        # not an absent one: fail closed with a concrete reason.
        return None, "manifest_present_but_not_a_file"
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return None, f"manifest_unreadable:{type(exc).__name__}"
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return None, "manifest_json_malformed"
    if not isinstance(document, Mapping):
        return None, "manifest_schema_strict_violation"
    if document.get("schema") != MANIFEST_SCHEMA_ID:
        return None, "manifest_schema_unsupported"
    if not _is_registration_schema(document):
        return None, "manifest_schema_strict_violation"
    return document, None


def _registration_platform_id() -> str | None:
    """Platform identity stays delegated to the shared PlatformIO helper."""

    if platform_io.is_windows():
        return "win32"
    if platform_io.is_linux():
        return "linux"
    if platform_io.is_darwin():
        return "darwin"
    return None


def _validate_registration(
    adapter_id: str,
    entry: Any,
    repo: Path,
) -> tuple[str | None, str | None]:
    """Strict additive validation; registration for other adapters is ignored."""

    if not isinstance(entry, Mapping):
        return None, "registration_malformed:not_object"
    unknown_keys = set(entry) - _EXPECTED_REGISTRATION_KEYS
    if unknown_keys:
        return None, "registry_strictness:unexpected_fields"

    platform_id = _registration_platform_id()
    if platform_id is None:
        return None, "registration_platform_unknown"
    if entry.get("platform") != platform_id:
        return None, "registration_platform_mismatch"

    for key_name in ("artifact_version", "source_tag", "source_commit"):
        value = entry.get(key_name)
        if not isinstance(value, str) or not value.strip():
            return None, f"registration_{key_name}_missing"
        if "\x00" in value:
            return None, f"registration_{key_name}_unsafe"

    sha_value = entry.get("sha256")
    if not isinstance(sha_value, str):
        return None, "registration_sha256_missing"
    if len(sha_value) != 64:
        return None, "registration_sha256_invalid"
    try:
        bytes.fromhex(sha_value)
    except ValueError:
        return None, "registration_sha256_invalid"

    fixture_kind = entry.get("fixture_kind")
    if fixture_kind is not None and fixture_kind not in {
        "host_tested_only",
        "native_qualified",
    }:
        return None, "registration_fixture_kind_invalid"

    executable_relative = entry.get("executable_relative")
    if (
        not isinstance(executable_relative, str)
        or not executable_relative
        or "\x00" in executable_relative
    ):
        return None, "registration_executable_relative_invalid"
    normalized = os.path.normpath(executable_relative)
    if (
        os.path.isabs(executable_relative)
        or os.path.isabs(normalized)
        or normalized == ".."
        or normalized.startswith("../")
        or normalized.startswith("..\\")
        or executable_relative.endswith(_UNSAFE_RELATIVE_SUFFIXES)
        or normalized in {".", ".."}
    ):
        return None, "registration_executable_unsafe"
    executable_path = repo / normalized
    resolved_repo = Path(os.path.abspath(repo)).resolve()
    resolved_executable = executable_path.resolve()
    try:
        resolved_executable.relative_to(resolved_repo)
    except ValueError:
        return None, "registration_executable_escapes_repo"
    try:
        if not resolved_executable.is_file():
            return None, "registration_executable_missing"
        file_size = resolved_executable.stat().st_size
        with resolved_executable.open("rb") as file_handle:
            if file_size > 256 * 1024 * 1024:
                return None, "registration_sha256_refused_large_file"
            digest = hashlib.sha256()
            for block in iter(
                lambda: file_handle.read(1024 * 1024), b""
            ):
                digest.update(block)
        computed_sha = digest.hexdigest()
    except (OSError, ValueError) as exc:
        return None, f"registration_executable_unreadable:{type(exc).__name__}"
    if computed_sha != sha_value:
        return None, "registration_sha256_mismatch"
    return str(resolved_executable), None


def select_executable_overrides(
    adapter_id: str,
    repo: Path,
    explicit_overrides: Mapping[str, object] | None,
) -> tuple[dict[str, str] | None, str | None]:
    """Resolve registration for the requested adapter into verified overrides.

    Explicit caller ``explicit_overrides`` wins for the requested adapter.
    An owned registration for a different adapter is never applied to this
    adapter.  A selected registration that is malformed, unsafe, wrong for
    the currently running platform, or whose registered SHA256 hash does not
    match the actual executable bytes fails closed.  A VALID registration
    entry is independently merged into the output so that the platform value
    recorded by the qualified provider is honored by the eventual exact-check
    in the adapter command builder.
    """

    repo = Path(repo)
    if (
        explicit_overrides is not None
        and isinstance(explicit_overrides, Mapping)
        and adapter_id in explicit_overrides
    ):
        # Caller authority stays first; never let repo bytes shadow an
        # explicit override.  Its own validation re-runs through the
        # shared builder's existing checks.
        return dict(explicit_overrides), None

    document, read_error = _read_registration_manifest(repo)
    if read_error:
        return None, read_error
    if document is None:
        # Unregistered adapters and default routes are preserved unchanged.
        return None, None
    registrations = document["registrations"]
    if adapter_id not in registrations:
        # No selection for this adapter: keep the existing default route
        # and never import another adapter's registration.
        return None, None

    selected_path, validation_error = _validate_registration(
        adapter_id,
        registrations[adapter_id],
        repo,
    )
    if validation_error:
        # A selected-but-invalid registration is nonlaunchable: there is
        # no fallback that could quietly substitute a PATH/default binary
        # for a qualified-and-tampered one.
        return None, validation_error
    if selected_path is None:
        return None, "registration_selection_unavailable"
    return {adapter_id: selected_path}, None
