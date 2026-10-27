"""Repository-level declaration of gitignored validation inputs (NF-2026-01396).

A tracked ``.aiworkhub/validation_inputs.json`` names globs of gitignored
canonical files (for example test fixtures) that every worker and combined
validation workspace must hydrate without a per-card declaration.
"""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath

SCHEMA_ID = "aiworkhub.validation_inputs.v1"
CONFIG_RELATIVE_PATH = ".aiworkhub/validation_inputs.json"
_MAX_CONFIG_BYTES = 1 << 20


def repo_validation_input_paths(source_root: Path) -> list[str]:
    """Return the files the repository declares, dot-prefixed matches skipped."""
    from . import worker_workspace as _ww

    config = source_root / CONFIG_RELATIVE_PATH
    if config.is_symlink():
        raise _ww.WorkspaceError("validation_inputs_symlink_refused")
    if not config.is_file():
        return []
    try:
        if config.stat().st_size > _MAX_CONFIG_BYTES:
            raise _ww.WorkspaceError("validation_inputs_too_large")
        payload = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _ww.WorkspaceError(f"validation_inputs_malformed:{exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_id") != SCHEMA_ID:
        raise _ww.WorkspaceError("validation_inputs_schema_id_invalid")
    include = payload.get("include")
    if not isinstance(include, list):
        raise _ww.WorkspaceError("validation_inputs_include_not_list")
    if len(include) > _ww.MAX_SEED_FILES:
        raise _ww.WorkspaceError(f"seed_file_limit_exceeded:{len(include)}")
    patterns: list[str] = []
    for entry in include:
        if not isinstance(entry, str):
            raise _ww.WorkspaceError("validation_inputs_entry_not_str")
        if PureWindowsPath(entry).is_absolute() or PureWindowsPath(entry).drive:
            raise _ww.WorkspaceError(f"validation_inputs_absolute_pattern:{entry}")
        pattern = _ww._relative_repo_path(entry)  # rejects absolute, empty and ``..``
        if _ww._is_private_repository_path(pattern):
            raise _ww.WorkspaceError(f"validation_inputs_private_pattern:{entry}")
        patterns.append(pattern)
    root = source_root.resolve()
    paths: list[str] = []
    for relative in _ww._expand_declared(source_root, patterns):
        # A dotfile under a declared tree (.gitkeep, .DS_Store) is private
        # state and never hydrated; refusing it instead would abort every
        # workspace this repository creates.
        if _ww._is_private_repository_path(relative):
            continue
        try:
            target = (source_root / relative).resolve().relative_to(root)
        except ValueError as exc:
            raise _ww.WorkspaceError(
                f"validation_inputs_escapes_repo:{relative}"
            ) from exc
        # An in-repo symlink can still point at private state (.git, .env).
        if _ww._is_private_repository_path(target.as_posix()):
            raise _ww.WorkspaceError(f"validation_inputs_private_target:{relative}")
        paths.append(relative)
    return paths
