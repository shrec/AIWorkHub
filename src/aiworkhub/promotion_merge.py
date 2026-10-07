"""Three-way promotion merges for candidates whose parent changed since launch.

A plain whole-file promote refuses a second accept on the same path even when
two candidates touch disjoint hunks: the canonical hash matches neither the
recorded parent baseline nor the second candidate's bytes.  This module records
parent blob OIDs at workspace creation and merges drifted paths with
``git merge-file`` against that recorded base, so disjoint edits from two
worktrees promote sequentially and overlapping edits fail closed with a named
conflict error (NF-2026-01381).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable

from .worker_workspace import (
    WorkerWorkspace,
    WorkspaceError,
    _hash_path,
    promote,
)


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def record_base_blobs(root: Path, baseline: dict[str, str | None]) -> dict[str, str]:
    """Write one blob per regular baseline file and map path -> OID.

    Exactly one ``git hash-object --stdin-paths`` pass records every baseline
    regular file in the repository.  Any failure returns ``{}`` so a workspace
    without recorded blobs keeps the legacy parent-changed refusal.
    """

    paths = sorted(
        rel
        for rel, value in baseline.items()
        if value is not None
        and value.startswith("file:")
        and _regular_file(root / rel)
    )
    if not paths:
        return {}
    try:
        result = subprocess.run(
            ["git", "hash-object", "-w", "--no-filters", "--stdin-paths"],
            cwd=root,
            input=("\n".join(paths) + "\n").encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    oids = result.stdout.decode("utf-8", "replace").split()
    if len(oids) != len(paths):
        return {}
    return {rel: oid for rel, oid in zip(paths, oids)}


def merged_path_hashes(merges: dict[str, bytes]) -> dict[str, str]:
    """Map merged paths to the sha256 of their merged bytes."""

    return {rel: hashlib.sha256(data).hexdigest() for rel, data in merges.items()}


def _parent_digest(workspace: WorkerWorkspace, rel: str) -> str | None:
    value = workspace.parent_baseline.get(rel)
    if value is None or not value.startswith("file:"):
        return None
    return value.rsplit(":", 1)[1]


def _cat_blob(repo: Path, oid: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "cat-file", "blob", oid],
            cwd=repo,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _merge_file(
    home: Path,
    parent_path: Path,
    base: bytes,
    candidate_path: Path,
) -> tuple[int | None, bytes]:
    """Run ``git merge-file -p``; ``(None, b"")`` means skip this path."""

    fd, base_tmp = tempfile.mkstemp(dir=home)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(base)
        result = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                "-L",
                "canonical",
                "-L",
                "base",
                "-L",
                "candidate",
                str(parent_path),
                str(base_tmp),
                str(candidate_path),
            ],
            # NF-2026-01401: never inside the caller's repository; in the
            # AppContainer that cwd is a worker worktree whose gitdir the
            # sandbox cannot read, and git merge-file dies there.
            cwd=home,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None, b""
    finally:
        try:
            os.unlink(base_tmp)
        except OSError:
            pass
    # git caps a conflict count at 127; 128+ is a die/usage exit and 255 a
    # negative error on Windows, never a conflict count (NF-2026-01401).
    if result.returncode < 0 or result.returncode >= 128:
        return None, b""
    return result.returncode, result.stdout


def plan_merges(
    workspace: WorkerWorkspace,
    changed: Iterable[str],
    observed: dict[str, str] | None = None,
) -> dict[str, bytes]:
    """Merge drifted changed paths; skip any path that cannot be merged.

    Only paths whose canonical hash matches neither the recorded parent
    baseline nor the candidate worktree are considered.  A path additionally
    needs a recorded blob OID whose verified digest equals the parent baseline
    and regular parent and candidate files.  A conflict exit code raises
    ``promotion_merge_conflict`` before any canonical write; binary or failed
    merges leave the path for ``promote``'s existing parent-changed refusal.
    """

    repo = workspace.repo.resolve()
    worktree = workspace.path.resolve()
    merges: dict[str, bytes] = {}
    for rel in sorted(set(changed)):
        canonical_hash = _hash_path(repo / rel)
        candidate_hash = _hash_path(worktree / rel)
        if canonical_hash in {
            workspace.parent_baseline.get(rel),
            candidate_hash,
        }:
            continue
        oid = (workspace.parent_baseline_blob or {}).get(rel)
        expected_digest = _parent_digest(workspace, rel)
        parent_path = repo / rel
        candidate_path = worktree / rel
        if (
            not oid
            or expected_digest is None
            or not _regular_file(parent_path)
            or not _regular_file(candidate_path)
        ):
            continue
        base = _cat_blob(repo, oid)
        if base is None or hashlib.sha256(base).hexdigest() != expected_digest:
            continue
        returncode, merged_bytes = _merge_file(
            workspace.home, parent_path, base, candidate_path
        )
        if returncode is None:
            continue
        if returncode != 0:
            raise WorkspaceError(f"promotion_merge_conflict:{rel}:conflicts={returncode}")
        merges[rel] = merged_bytes
        if observed is not None:
            observed[rel] = canonical_hash
    return merges


def promote_merged(
    workspace: WorkerWorkspace,
    changed: Iterable[str],
    *, merged_hashes: dict[str, str] | None = None,
    promote_fn: Callable[..., list[str]] = promote,
) -> list[str]:
    """Promote changed paths, three-way merging drifted parent files first.

    With no mergeable drift this is exactly ``promote``.  Otherwise a private
    temp directory under the workspace home holds every changed path (merged
    bytes for merged paths, candidate bytes and mode for the rest, deletions
    absent) and ``promote`` runs against that tree with the merged paths'
    parent baseline updated to the current canonical hashes, reusing
    ``promote``'s scope, race and write guards.  When ``merged_hashes`` is
    provided it must equal the merged paths' sha256 mapping.
    """
    observed: dict[str, str] = {}
    changed_rel = sorted(set(changed))
    merges = plan_merges(workspace, changed_rel, observed)
    if merged_hashes is not None and merged_path_hashes(merges) != {
        str(rel): str(digest) for rel, digest in merged_hashes.items()
    }:
        raise WorkspaceError("promotion_merge_changed_since_validation")
    if not merges:
        return promote_fn(workspace, changed_rel)
    tmp = Path(tempfile.mkdtemp(dir=workspace.home))
    try:
        source = workspace.path
        parent_baseline = dict(workspace.parent_baseline)
        for rel in changed_rel:
            origin = source / rel
            destination = tmp / rel
            if rel in merges:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(merges[rel])
                os.chmod(destination, stat.S_IMODE(origin.stat().st_mode))
            elif origin.is_symlink():
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(os.readlink(origin), destination)
            elif origin.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origin, destination)
            # Paths that no longer exist stay absent so promote deletes them.
        for rel in merges:
            # A canonical write after plan_merges hashed the parent fails closed
            # (NF-2026-01381). ponytail: an A->B->A flip inside that window passes;
            # snapshot parent bytes into merge-file if promotions race that tightly.
            current_parent = _hash_path(workspace.repo / rel)
            if current_parent != observed[rel]:
                raise WorkspaceError(f"promotion_merge_parent_changed:{rel}")
            parent_baseline[rel] = current_parent
        return promote_fn(
            replace(workspace, path=tmp, parent_baseline=parent_baseline),
            changed_rel,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
