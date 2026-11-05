"""Native build-driver support seeding for the sparse worker worktree.

NF-2026-02079: ``worker_workspace._declared_workspace_seed_closure`` follows
python imports, js requires, literal assets and npm support, none of which can
reach what a native build driver reads: ``CMakePresets.json``, the whole
``CMakeLists.txt`` tree, target sources no card names, ``third_party/``.  A
native build graph is defined by repository content the card never nominates,
so when a declared validation row runs one of :data:`NATIVE_BUILD_DRIVERS` as
its executable, the full *tracked* tree of ``source_root`` becomes immutable
support.  Untracked and ignored files stay out (the git index remains the only
authority for repository-owned source), and a card with no native driver row
never runs git here, so its seed set is byte-identical to before.

The row parser, the tracked-path helper and the error type are owned by
``worker_workspace``; they are imported lazily so this module defines only the
driver vocabulary and the tree expansion, never a second command parser.
"""

from __future__ import annotations

import ntpath
from collections.abc import Iterable
from pathlib import Path

# Executable basenames (case-insensitive, Windows launcher suffix stripped)
# that drive a native build or test graph.
NATIVE_BUILD_DRIVERS = frozenset(
    {
        "cargo",
        "cmake",
        "ctest",
        "dotnet",
        "go",
        "gradle",
        "make",
        "meson",
        "msbuild",
        "mvn",
        "ninja",
        "nmake",
    }
)
_LAUNCHER_SUFFIXES = (".exe", ".cmd", ".bat")


def native_build_driver(executable: str) -> str | None:
    """Return the driver name ``executable`` invokes, or ``None``.

    Only the exact basename counts: ``/usr/bin/cmake`` and ``CMake.exe`` are
    drivers, ``mycmake`` and ``cmake_helper`` are not.  ``ntpath.basename``
    splits on both separators, so POSIX and Windows spellings agree.
    """
    name = ntpath.basename(str(executable)).lower()
    for suffix in _LAUNCHER_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name if name in NATIVE_BUILD_DRIVERS else None


def invokes_native_build_driver(rows: Iterable[str]) -> bool:
    """Whether any validation row's executable is a native build driver.

    Rows go through ``worker_workspace``'s own validation parser, which
    already strips a supported leading env assignment and ``cd DIR &&``
    prefix and refuses every other shell form with ``WorkspaceError``.
    """
    from .worker_workspace import _parse_validation_command_detailed

    for row in rows:
        tokens = _parse_validation_command_detailed(row)[0]
        if tokens and native_build_driver(tokens[0]) is not None:
            return True
    return False


def native_build_support(source_root: Path, rows: Iterable[str]) -> tuple[str, ...]:
    """Tracked regular files of ``source_root`` when a row runs a native driver.

    Returns ``()`` without touching git for a card with no native driver row.
    Tracked symlinks, gitlinks and tracked-but-deleted paths are not regular
    files the seed copier accepts and are left out.  An empty tracked listing
    (git unavailable, not a repository) is refused by name rather than seeding
    a tree the declared driver cannot build, and the result is bounded by
    ``MAX_SEED_FILES`` like every other support expansion.
    """
    rows = tuple(rows)
    if not invokes_native_build_driver(rows):
        return ()
    from .worker_workspace import (
        MAX_SEED_FILES,
        WorkspaceError,
        _relative_repo_path,
        _tracked_repository_paths,
    )

    tracked = _tracked_repository_paths(source_root, (".",))
    if not tracked:
        raise WorkspaceError("validation_native_build_tree_unlisted")
    if len(tracked) > MAX_SEED_FILES:
        raise WorkspaceError(f"seed_file_limit_exceeded:{len(tracked)}")
    seeded: set[str] = set()
    # NF-2026-02079: a tracked path under a symlinked parent directory
    # resolves outside the repository, so only files inside source_root count.
    root = source_root.resolve()
    for row in sorted(tracked):
        relative = _relative_repo_path(row)
        candidate = source_root / relative
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if not candidate.resolve().is_relative_to(root):
            continue
        seeded.add(relative)
    return tuple(sorted(seeded))
