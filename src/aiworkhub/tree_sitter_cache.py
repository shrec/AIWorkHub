"""Bounded tree-sitter-language-pack grammar cache discovery and mirroring.

Validation replaces the user profile, so the package's own cache-root variable
is the only authority forwarded. Discovery (moved out of worker_workspace) only
accepts a real, parser-complete cache; the mirror copies that cache's grammar
libraries into one host-owned repository directory that no container can
write, and the AppContainer validation lane reads it through a read-only grant.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from . import platform_io as _platform_io

if TYPE_CHECKING:
    from .windows_appcontainer import ContainerGrant

TREE_SITTER_CACHE_ROOT_ENV = "TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"
_TREE_SITTER_CACHE_DIRECTORY = "tree-sitter-language-pack"
_TREE_SITTER_GRAMMAR_SUFFIXES = frozenset({".dll", ".so", ".dylib"})
_TREE_SITTER_REQUIRED_GRAMMARS = frozenset({"javascript", "typescript"})
_MAX_CANDIDATES = 2
_MAX_VERSION_DIRECTORIES = 32
_MAX_LIBRARY_ENTRIES = 128
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _real_directory(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & _REPARSE
    )


def _version_libs(version: os.DirEntry[str]) -> Path | None:
    try:
        info = version.stat(follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE:
        return None
    libs = Path(version.path) / "libs"
    return libs if _real_directory(libs) else None


def _grammar_libraries(libs: Path) -> list[tuple[str, Path, os.stat_result]] | None:
    """Non-empty regular grammar files in ``libs``; None past the entry bound."""

    found: list[tuple[str, Path, os.stat_result]] = []
    with os.scandir(libs) as libraries:
        for index, library in enumerate(libraries):
            if index >= _MAX_LIBRARY_ENTRIES:
                return None
            try:
                info = library.stat(follow_symlinks=False)
            except OSError:
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & _REPARSE
                or info.st_size <= 0
                or Path(library.name).suffix.casefold()
                not in _TREE_SITTER_GRAMMAR_SUFFIXES
            ):
                continue
            found.append((library.name, Path(library.path), info))
    return found


def _parser_complete(root: Path) -> bool:
    if not _real_directory(root):
        return False
    pack = root / _TREE_SITTER_CACHE_DIRECTORY
    if not _real_directory(pack):
        return False
    try:
        with os.scandir(pack) as versions:
            for version_index, version in enumerate(versions):
                if version_index >= _MAX_VERSION_DIRECTORIES:
                    return False
                libs = _version_libs(version)
                if libs is None:
                    continue
                libraries = _grammar_libraries(libs)
                if libraries is None:
                    continue
                # The package loads one version, so grammars never accumulate
                # across version directories.
                grammars = {
                    grammar
                    for name, _path, _info in libraries
                    for grammar in _TREE_SITTER_REQUIRED_GRAMMARS
                    if grammar in name.casefold()
                }
                if _TREE_SITTER_REQUIRED_GRAMMARS <= grammars:
                    return True
    except OSError:
        return False
    return False


def trusted_tree_sitter_cache_root() -> Path | None:
    """Return a bounded, parser-complete cache root without exposing HOME.

    tree-sitter-language-pack derives its cache from USERPROFILE on Windows.
    Validation deliberately replaces that profile, so an already installed
    grammar otherwise becomes a network download attempt and the Source Graph
    silently falls back to positionless lexical extraction. The package's own
    cache-root variable is narrower than restoring any user profile variable.
    Only a real, parser-complete cache is forwarded.
    """

    candidates: list[Path] = []
    explicit = os.environ.get(TREE_SITTER_CACHE_ROOT_ENV, "").strip()
    if explicit:
        candidates.append(Path(explicit))
    if _platform_io.is_windows():
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data and local_app_data != explicit:
            candidates.append(Path(local_app_data))
    for candidate in candidates[:_MAX_CANDIDATES]:
        if not _parser_complete(candidate):
            continue
        try:
            return candidate.resolve(strict=True)
        except OSError:
            continue
    return None


def repo_mirror_root(repo: Path) -> Path:
    """The one shared grammar mirror of ``repo``.

    It sits beside ``runtime/worktrees``, never beneath a request-scoped grant
    path, so no container SID holds write access to it: only the host writes
    here and validation reads it through :func:`mirror_read_grants`.
    """

    return Path(repo).resolve() / ".aiworkhub" / "runtime" / "tree-sitter-cache"


def mirror_read_grants(repo: Path, env: Mapping[str, str]) -> list[ContainerGrant]:
    """One persistent read_execute grant exactly when ``env`` forwards the mirror."""

    from . import windows_appcontainer

    mirror = str(repo_mirror_root(repo))
    forwarded = env.get(TREE_SITTER_CACHE_ROOT_ENV, "")
    if not forwarded or os.path.normcase(os.path.normpath(forwarded)) != os.path.normcase(
        mirror
    ):
        return []
    return [windows_appcontainer.ContainerGrant(mirror, "read_execute", persistent=True)]


def _replace_library(source: Path, target: Path, info: os.stat_result) -> None:
    # Copy to a unique sibling, then swap it in atomically: a concurrent host
    # mirror, or a validation loading the grammar, never sees a half-written DLL.
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(fd)
    try:
        shutil.copyfile(source, temporary)
        os.utime(temporary, ns=(info.st_atime_ns, info.st_mtime_ns))
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def mirror_tree_sitter_cache(source_root: Path, destination_root: Path) -> Path | None:
    """Copy ``source_root``'s grammar libraries under ``destination_root``.

    An AppContainer cannot open the profile cache on C:, so validation reads a
    host-owned mirror (see :func:`repo_mirror_root`) instead. Only
    ``<version>/libs/<grammar library>`` is copied: subdirectories, bundles/
    and non-grammar files stay behind. manifest.json is deliberately NOT
    copied; the parser load path opens the library under libs/ directly. That
    was not re-measured inside the worker sandbox (it could not start a
    process), so a live contained parser load remains the proof. The equal
    size+mtime_ns skip is sound only because no container can write the
    destination. Returns the re-verified destination, or None on any failure
    -- never raises.
    """

    try:
        pack = Path(source_root) / _TREE_SITTER_CACHE_DIRECTORY
        if not _real_directory(pack):
            return None
        # Never write through a pre-existing link or junction at the destination.
        Path(destination_root).mkdir(parents=True, exist_ok=True)
        if not _real_directory(Path(destination_root)):
            return None
        with os.scandir(pack) as entries:
            versions = list(entries)[:_MAX_VERSION_DIRECTORIES]
        for version in versions:
            libs = _version_libs(version)
            libraries = None if libs is None else _grammar_libraries(libs)
            if not libraries:
                continue
            target_pack = Path(destination_root) / _TREE_SITTER_CACHE_DIRECTORY
            target_version = target_pack / version.name
            target_libs = target_version / "libs"
            # Defense in depth: create and verify one level at a time, never
            # traversing an unchecked link or junction with host privileges.
            for part in (target_pack, target_version, target_libs):
                part.mkdir(exist_ok=True)
                if not _real_directory(part):
                    return None
            for name, source, info in libraries:
                target = target_libs / name
                try:
                    existing = os.lstat(target)
                except FileNotFoundError:
                    existing = None
                if existing is not None:
                    if not stat.S_ISREG(existing.st_mode) or (
                        getattr(existing, "st_file_attributes", 0) & _REPARSE
                    ):
                        return None
                    if (
                        existing.st_size == info.st_size
                        and existing.st_mtime_ns == info.st_mtime_ns
                    ):
                        continue
                _replace_library(source, target, info)
        if not _parser_complete(Path(destination_root)):
            return None
        return Path(destination_root).resolve(strict=True)
    except OSError:
        return None
