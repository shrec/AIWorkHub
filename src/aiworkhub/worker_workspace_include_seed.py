"""Local ``#include`` dependency preflight for C/C++/CUDA sources (B664).

Extracted from ``worker_workspace.py`` to keep that module under the
module-size ratchet (NF-2026-01156); behaviour is unchanged.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Iterable

_HEADER_FILE_SUFFIXES = frozenset(
    {".h", ".hpp", ".hxx", ".hh", ".inl", ".cuh", ".c", ".cpp", ".cu", ".cc", ".cxx"}
)
_QUOTED_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+"([^"]+)"')
_ANGLE_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+<([^>]+)>')
_DEFAULT_INCLUDE_ROOTS: tuple[str, ...] = (".",)


def _resolve_local_quoted_includes(
    repo: Path,
    seeded: list[str],
    include_roots: tuple[str, ...] = _DEFAULT_INCLUDE_ROOTS,
) -> list[str]:
    """Resolve repository-local ``#include`` dependencies for C/CUDA files.

    For every C / CUDA / header file already in *seeded*, scan for ``#include
    "..."`` (quoted) and ``#include <...>`` (angle-bracket) directives, and
    recursively collect the transitive closure of real regular files reachable
    from the declared inputs.

    Quoted includes use compiler-style lookup: the including file's directory
    first, then each configured repository include root.  A target neither
    rule finds is looked up among the repository's tracked files at
    ``<dir>/<target>`` -- the compiler's view with the project's own
    ``-I<dir>``, whatever build system declares it.

    Angle-bracket includes are resolved ONLY against the normalized repository
    include roots -- never the including file's directory, and never the
    tracked-file fallback, since ``<...>`` is a project/SDK search, not a
    same-directory one.  A target that resolves under no include root (an SDK
    or system header such as ``<vector>``) is simply not seeded.

    This only seeds headers the worker may want to read; the compiler resolves
    includes itself.  An include that still resolves nowhere (a generated,
    system or build-provided header) is skipped, never a launch refusal, and
    an escaping target (absolute, ``..``, symlink) is never seeded.

    Returns the augmented, sorted, deduplicated seed list.  Callers must still
    respect ``MAX_SEED_FILES``, symlink rejection, and beneath-root checks.
    """
    from . import worker_workspace as _ww

    if not seeded:
        return []

    normalized_roots = _normalize_include_roots(repo, include_roots)

    resolved: dict[str, str] = {}  # repo-relative path -> including relative (provenance)
    pending: list[str] = list(seeded)
    seen: set[str] = set()
    tracked: list[str] | None = None  # loaded on the first quoted include the roots miss

    while pending:
        relative = pending.pop()
        if relative in seen:
            continue
        seen.add(relative)

        full_path = repo / relative
        suffix = full_path.suffix.lower()
        if suffix not in _HEADER_FILE_SUFFIXES:
            continue
        if full_path.is_symlink() or not full_path.is_file():
            continue

        try:
            text = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        including_dir = (repo / relative).parent
        for line in text.splitlines():
            quoted = _QUOTED_INCLUDE_RE.match(line)
            if quoted is not None:
                include_target = quoted.group(1)
                # Resolve: current-file directory first, then each include root.
                candidate = _resolve_one_quoted_include(
                    repo, including_dir, include_target, normalized_roots
                )
                if candidate is not None:
                    matches = [candidate.relative_to(repo).as_posix()]
                else:
                    if tracked is None:
                        tracked = _tracked_repository_files(repo)
                    matches = _tracked_include_matches(tracked, include_target)
            else:
                angle = _ANGLE_INCLUDE_RE.match(line)
                if angle is None:
                    continue
                # Angle includes: include roots only -- no current-directory
                # rule, no tracked-file fallback.
                candidate = _resolve_include_against_roots(
                    repo, angle.group(1), normalized_roots
                )
                matches = (
                    [candidate.relative_to(repo).as_posix()]
                    if candidate is not None
                    else []
                )
            for match_relative in matches:
                match_path = repo / match_relative
                if match_path.is_symlink() or not match_path.is_file():
                    continue
                if match_relative not in resolved:
                    resolved[match_relative] = relative
                    if match_relative not in seen:
                        pending.append(match_relative)

    augmented = sorted(set(seeded) | set(resolved.keys()))
    if len(augmented) > _ww.MAX_SEED_FILES:
        raise _ww.WorkspaceError(f"seed_file_limit_exceeded:{len(augmented)}")
    return augmented


def _normalize_include_roots(repo: Path, roots: Iterable[str]) -> tuple[str, ...]:
    from . import worker_workspace as _ww

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in roots:
        if raw == ".":
            norm = "."
            candidate = repo
        else:
            candidate = _safe_include_candidate(repo, repo, raw)
            if candidate is None:
                raise _ww.WorkspaceError(f"include_root_not_directory:{raw}")
            norm = candidate.relative_to(repo).as_posix()
        if candidate.is_symlink() or not candidate.is_dir():
            raise _ww.WorkspaceError(f"include_root_not_directory:{raw}")
        key = "." if norm == "." else norm
        if key not in seen:
            seen.add(key)
            normalized.append(key)
    return tuple(normalized)


def _resolve_include_against_roots(
    repo: Path,
    target: str,
    include_roots: tuple[str, ...],
) -> Path | None:
    """Try to find *target* relative to each configured repository include root."""
    for root_raw in include_roots:
        base = repo if root_raw == "." else repo / root_raw
        candidate = _safe_include_candidate(repo, base, target)
        if candidate is None:
            continue
        if candidate.exists():
            return candidate
    return None


def _resolve_one_quoted_include(
    repo: Path,
    including_dir: Path,
    target: str,
    include_roots: tuple[str, ...],
) -> Path | None:
    """Try to find *target* using compiler-style lookup.

    1. Relative to the including file's directory (``including_dir / target``).
    2. Relative to each configured repository include root.
    """
    # Rule 1: current-file directory.
    direct = _safe_include_candidate(repo, including_dir, target)
    if direct is not None and direct.exists():
        return direct

    # Rule 2: configured include roots.
    return _resolve_include_against_roots(repo, target, include_roots)


_MAX_TRACKED_INCLUDE_MATCHES = 8


def _tracked_repository_files(repo: Path) -> list[str]:
    """Every git-tracked file, repo-relative; any git failure yields []."""
    from . import worker_workspace as _ww

    try:
        completed = _ww._run(
            ["git", "ls-files", "-z", "--full-name"],
            cwd=repo,
            timeout=_ww._LITERAL_ASSET_TRACKED_TIMEOUT_SECONDS,
            phase="workspace_provision",
        )
    except (_ww.GitCommandTimeout, subprocess.SubprocessError, OSError, ValueError):
        return []
    if completed.returncode != 0:
        return []
    return [row for row in completed.stdout.split("\x00") if row]


def _tracked_include_matches(tracked: list[str], target: str) -> list[str]:
    """Tracked files at ``<dir>/<target>``: only tracked, never ``..`` or absolute."""
    norm = target.replace("\\", "/")
    if not norm or norm.startswith("/") or ":" in norm or ".." in norm.split("/"):
        return []
    suffix = "/" + norm
    return sorted(row for row in tracked if row.endswith(suffix))[:_MAX_TRACKED_INCLUDE_MATCHES]


def _safe_include_candidate(repo: Path, base: Path, target: str) -> Path | None:
    from . import worker_workspace as _ww

    target_value = target.strip().replace("\\", "/")
    if (
        not target_value
        or target_value.startswith("/")
        or "\x00" in target_value
    ):
        return None
    candidate = Path(os.path.abspath(base / PurePosixPath(target_value)))
    try:
        _ww._require_beneath(repo, candidate.parent)
        resolved = candidate.resolve(strict=False)
        _ww._require_beneath(repo, resolved)
    except _ww.WorkspaceError:
        return None
    return candidate
