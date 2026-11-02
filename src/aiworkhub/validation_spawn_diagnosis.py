"""Why a declared validation command could not be spawned (NF-2026-01936).

A worker's advisory validation that fails to launch used to come back as a bare
``tool_unavailable`` carrying only the raw ``OSError`` text, so nobody could
tell whether the head was missing from PATH, was a Windows batch wrapper, or
lived under an install root the operator never registered in the canonical
``.aiworkhub/config/toolchains.json``.  This module only EXPLAINS such a
failure: it never changes which command runs or how it is launched.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .toolchain_registry import (
    REGISTRY_RELATIVE_PATH,
    ToolchainRegistry,
    _under,
    load_registry,
)

SPAWN_DIAGNOSIS_REASONS = (
    "command_not_found",
    "windows_batch_wrapper",
    "toolchain_not_registered",
    "toolchain_registered_launch_failed",
    "unknown",
)
MAX_MESSAGE_CHARS = 300
MAX_PATH_ENTRIES = 256
_BATCH_SUFFIXES = frozenset({".cmd", ".bat"})
_ID_SANITIZE_RE = re.compile(r"[^a-z0-9_-]+")
_REGISTRY_PATH_TEXT = REGISTRY_RELATIVE_PATH.as_posix()


def _native_entry_point(name: str, wrapper: Path) -> str:
    """The native argv a batch wrapper stands for, as concrete as can be known."""
    directory = wrapper.parent
    stem = name.casefold()
    if stem in {"npm", "npx"}:
        node = directory / "node.exe"
        script = directory / "node_modules" / "npm" / "bin" / f"{stem}-cli.js"
        return f"declare `node {script}` (node: {node}) instead of `{name}`"
    if stem in {"flutter", "dart"} and directory.name.casefold() == "bin":
        dart = directory / "cache" / "dart-sdk" / "bin" / "dart.exe"
        if stem == "dart":
            return f"declare the native `{dart}` instead of `{name}`"
        snapshot = directory / "cache" / "flutter_tools.snapshot"
        return f"declare `{dart} {snapshot}` instead of `{name}`"
    return f"declare the native executable that {wrapper.name} invokes instead of `{name}`"


def _install_root(executable: Path) -> Path:
    directory = executable.parent
    return directory.parent if directory.name.casefold() == "bin" else directory


def _toolchain_id(name: str) -> str:
    cleaned = _ID_SANITIZE_RE.sub("-", name.casefold()).strip("-_")[:48]
    return cleaned or "toolchain"


def _candidates(head: str, extensions: tuple[str, ...]) -> list[str]:
    suffix = Path(head).suffix.casefold()
    if suffix and suffix in extensions:
        return [head]
    return [*(head + ext for ext in extensions), head]


def _resolve(head: str, path: str, pathext: str) -> str | None:
    extensions = tuple(
        ext.casefold() for ext in pathext.split(os.pathsep) if ext.strip()
    )
    candidates = _candidates(head, extensions)
    if os.path.isabs(head):
        return next((c for c in candidates if os.path.isfile(c)), None)
    directories = [d for d in path.split(os.pathsep) if d.strip()][:MAX_PATH_ENTRIES]
    # Directory-major, like the OS loader: an earlier PATH entry wins over a
    # better extension in a later one.
    for directory in directories:
        for candidate in candidates:
            found = shutil.which(candidate, path=directory)
            if found:
                return found
    return None


def diagnose_validation_spawn_failure(
    declared_head: str,
    launch_message: str,
    *,
    path: str,
    pathext: str,
    registry: ToolchainRegistry | None = None,
) -> dict[str, Any]:
    """Return ``{reason, resolved_path, remedy}`` for one failed validation launch.

    ``path``/``pathext`` are the host's PATH and PATHEXT; ``registry`` defaults
    to the canonical :func:`load_registry`.  Every input that cannot be
    classified maps to ``unknown`` with the raw message kept, never to success.
    """

    message = str(launch_message or "")[:MAX_MESSAGE_CHARS]
    head = str(declared_head or "").strip()
    if not head:
        return {
            "reason": "unknown",
            "resolved_path": None,
            "remedy": f"no declared command head to diagnose; launch error: {message}",
        }
    name = Path(head).stem if os.path.isabs(head) else head
    resolved = _resolve(head, path, pathext)
    if resolved is None:
        where = (
            "does not exist; install it there or declare its correct absolute path"
            if os.path.isabs(head)
            else "is not on the host PATH; install it, put its directory on PATH"
        )
        return {
            "reason": "command_not_found",
            "resolved_path": None,
            "remedy": (
                f"`{head}` {where} and register its install root in "
                f"{_REGISTRY_PATH_TEXT}"
            ),
        }
    resolved_path = Path(resolved)
    if resolved_path.suffix.casefold() in _BATCH_SUFFIXES:
        return {
            "reason": "windows_batch_wrapper",
            "resolved_path": str(resolved_path),
            "remedy": (
                f"`{head}` resolves to the batch wrapper {resolved_path}; batch files "
                "are never run with shell interpretation, so "
                f"{_native_entry_point(Path(name).stem, resolved_path)}"
            ),
        }
    if registry is None:
        try:
            registry = load_registry()
        except Exception as exc:  # a malformed registry is reported, never ignored
            return {
                "reason": "unknown",
                "resolved_path": str(resolved_path),
                "remedy": (
                    f"{_REGISTRY_PATH_TEXT} could not be read ({exc}); "
                    f"launch error: {message}"
                ),
            }
    owner = next(
        (tc for tc in registry.toolchains if _under(resolved_path, tc.directories())),
        None,
    )
    if owner is None:
        if resolved_path.suffix.casefold() not in {"", ".exe", ".com"}:
            return {
                "reason": "unknown",
                "resolved_path": str(resolved_path),
                "remedy": f"`{head}` resolves to {resolved_path}; launch error: {message}",
            }
        executable = Path(name).stem if Path(name).suffix else name
        entry: Mapping[str, Any] = {
            _toolchain_id(executable): {
                "executables": [executable],
                "roots": [str(_install_root(resolved_path))],
            }
        }
        return {
            "reason": "toolchain_not_registered",
            "resolved_path": str(resolved_path),
            "remedy": (
                f"add to \"toolchains\" in {_REGISTRY_PATH_TEXT}: "
                f"{json.dumps(entry, sort_keys=True)}"
            ),
        }
    return {
        "reason": "toolchain_registered_launch_failed",
        "resolved_path": str(resolved_path),
        "remedy": (
            f"`{head}` resolves under registered toolchain '{owner.id}' but the "
            f"launch still failed: {message}"
        ),
    }
