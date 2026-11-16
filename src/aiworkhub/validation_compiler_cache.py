"""Shared content-addressed compiler cache for validation lanes (NF-2026-02097).

Every validation command runs in a fresh, request-scoped scratch, so native
C/C++ builds recompiled every translation unit in every lane.  This module
points ccache at one in-repo directory beneath the already gitignored runtime
tree, keyed by the candidate's base commit, so identical compiler inputs hit
across lanes and across candidate worktrees of the same base.

Deliberately NOT a shared build/target directory: different candidate trees
must never share build outputs, and cmake would serialise on one.  ccache keys
each object by its full compiler inputs, so two candidate trees can only ever
share byte-identical compilations.  sccache is deliberately unsupported: a
running sccache server ignores ``SCCACHE_DIR`` and the first lane would leave
a daemon behind.

Residual risk: lanes that share one base commit share one cache directory, so
a hostile candidate could plant entries a sibling of the same base later hits.

Fail open: when ccache is not resolvable to an absolute path, the base commit
is unknown, the backend cannot write the cache directory, or any component is
(or cannot be proven not to be) a symlink/junction, the returned mapping is
empty and validation behaves exactly as before.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from .platform_io import is_windows

#: In-repo cache root, relative to the canonical repository (owner rule:
#: nothing outside the repo; ``.aiworkhub/runtime`` is already gitignored).
COMPILER_CACHE_RELATIVE = Path(".aiworkhub") / "runtime" / "compiler-cache"
#: Upper bound handed to ccache per base commit; it evicts least-recently-used
#: entries beyond it.
COMPILER_CACHE_MAX_SIZE = "5G"
#: A base commit's cache unused this long is deleted, so the total across
#: bases stays bounded (ccache caps each base, nothing else prunes them).
COMPILER_CACHE_STALE_SECONDS = 7 * 24 * 3600
#: A full SHA-1 or SHA-256 Git object id; anything else disables the cache.
_BASE_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

_WINDOWS = is_windows()
_lstat = os.lstat

Which = Callable[..., "str | None"]


def _sandbox_can_write_cache(backend: str) -> bool:
    # Only the editor-hosted route runs validation without an OS sandbox, so
    # only it can write a directory outside the request's exec scratch.
    # Landlock, bubblewrap and AppContainer grant exactly one writable
    # validation root (the per-request exec scratch); sandbox_argv and
    # _run_appcontainer_validation have no existing extra-writable-root list
    # to extend with one line, so per the NF-2026-02097 card the cache stays
    # off for those backends rather than growing a new ACL mechanism here.
    from .worker_workspace import VSCODE_LM_IN_PROCESS_BACKEND

    return backend == VSCODE_LM_IN_PROCESS_BACKEND


def _link_state(path: Path) -> bool | None:
    """``True`` for a symlink/junction, ``False`` when provably neither.

    ``None`` means the check could not be made; callers must refuse then,
    never assume "not a link".
    """
    try:
        info = _lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            return bool(is_junction())
        except OSError:
            return None
    if not _WINDOWS:
        return False  # POSIX has no junctions; S_ISLNK above is the whole check.
    attributes = getattr(info, "st_file_attributes", None)
    if not isinstance(attributes, int):
        return None
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _real_cache_dir(repo_root: Path, base_oid: str) -> Path | None:
    """Create ``<repo>/.aiworkhub/runtime/compiler-cache/<base_oid>/ccache``.

    Every component below the repository root is created one at a time and
    must be a real directory, never a symlink or junction, so the cache can
    not be redirected outside the repository.
    """
    current = repo_root
    for part in (*COMPILER_CACHE_RELATIVE.parts, base_oid, "ccache"):
        current = current / part
        if _link_state(current) is not False:
            return None
        try:
            current.mkdir(exist_ok=True)
            info = _lstat(current)
        except OSError:
            return None
        if not stat.S_ISDIR(info.st_mode) or _link_state(current) is not False:
            return None
    if not os.access(current, os.W_OK | os.X_OK):
        return None
    return current


def _prune_stale_bases(base_dir: Path) -> None:
    """Mark ``base_dir`` used and delete sibling bases unused for a week.

    Best effort: links and non-commit names are never touched, errors are
    ignored.  ponytail: mtime-based age, a size-based global cap if a week of
    bases ever outgrows the disk.
    """
    try:
        os.utime(base_dir)
        siblings = list(os.scandir(base_dir.parent))
    except OSError:
        return
    cutoff = time.time() - COMPILER_CACHE_STALE_SECONDS
    for entry in siblings:
        path = Path(entry.path)
        if entry.name == base_dir.name or _BASE_OID.fullmatch(entry.name) is None:
            continue
        try:
            if _link_state(path) is not False or not entry.is_dir(follow_symlinks=False):
                continue
            if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def compiler_cache_env(
    repo_root: Path,
    backend: str,
    base_oid: str | None,
    workspace_root: Path,
    declared_env: Mapping[str, str] | None = None,
    *,
    which: Which = shutil.which,
) -> dict[str, str]:
    """Return the ccache env for one validation command, or ``{}``.

    ``workspace_root`` becomes ``CCACHE_BASEDIR`` and ``CCACHE_NOHASHDIR`` is
    set, so the same sources (including ``-g`` builds) under different
    worktree paths hash to the same entries.  Keys already present in
    ``declared_env`` are never overridden.
    """
    declared = declared_env or {}
    if not _sandbox_can_write_cache(backend):
        return {}
    if not isinstance(base_oid, str) or _BASE_OID.fullmatch(base_oid) is None:
        return {}
    launcher = which("ccache", path=declared.get("PATH"))
    if not launcher or not Path(launcher).is_absolute():
        return {}
    cache_dir = _real_cache_dir(Path(repo_root), base_oid)
    if cache_dir is None:
        return {}
    _prune_stale_bases(cache_dir.parent)
    env = {
        "CCACHE_DIR": str(cache_dir),
        "CCACHE_BASEDIR": str(workspace_root),
        "CCACHE_NOHASHDIR": "1",
        "CCACHE_MAXSIZE": COMPILER_CACHE_MAX_SIZE,
        "CMAKE_C_COMPILER_LAUNCHER": launcher,
        "CMAKE_CXX_COMPILER_LAUNCHER": launcher,
    }
    return {key: value for key, value in env.items() if key not in declared}
