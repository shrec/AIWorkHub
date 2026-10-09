"""Three-way promotion merges for candidates whose parent changed since launch.

A plain whole-file promote refuses a second accept on the same path even when
two candidates touch disjoint hunks: the canonical hash matches neither the
recorded parent baseline nor the second candidate's bytes.  This module records
parent blob OIDs at workspace creation and merges drifted paths with
``git merge-file`` against that recorded base, so disjoint edits from two
worktrees promote sequentially and overlapping edits fail closed with a named
conflict error (NF-2026-01381).

It also owns the rework base-drift rebase, which merges one predecessor
worktree onto a newer successor base and prefers those same recorded blobs as
its three-way ancestor (NF-2026-01113, NF-2026-01431).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable

from .worker_workspace import (
    WorkerWorkspace,
    WorkspaceError,
    _hash_path,
    _isolated_worktree_base_oid,
    _run,
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


ReworkRebase = Callable[[list[tuple[str, bytes | None]]], dict[str, bytes | None]]


def rework_base_drift_rebase(
    repo: Path, worktree: Path, source: WorkerWorkspace
) -> ReworkRebase | None:
    """3-way merge P0=``source.base_oid`` predecessor bytes onto S0
    (NF-2026-01113): ours=S0, base=P0, theirs=predecessor; conflicts and
    symlink/gitlink drift fail closed. ``changed_path_hashes`` pins theirs
    before this merge, never the merged output.
    """
    base = source.base_oid
    if not base or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base) is None:
        raise WorkspaceError("rework_base_invalid" if base else "rework_base_unknown")
    if base == (successor := _isolated_worktree_base_oid(repo, worktree)):
        return None

    def git(failure: str, *args: str) -> Any:
        done = _run(["git", *args], cwd=worktree, phase="rework_base_drift", text=False)
        if done.returncode != 0 and failure:
            raise WorkspaceError(failure)
        return None if done.returncode else done.stdout

    def rebase(planned: list[tuple[str, bytes | None]]) -> dict[str, bytes | None]:
        names = [relative for relative, _ in planned]
        argv = ("--literal-pathspecs", "diff-tree", "-r", "-z", base, successor, "--", *names)
        fields = os.fsdecode(git("rework_base_drift_diff_failed", *argv)).split("\0")
        drifted = {path: meta.split()  # [":old_mode", new_mode, P0 oid, S0 oid, status]
                   for meta, path in zip(fields[::2], fields[1::2], strict=False)}
        merged: dict[str, bytes | None] = {}
        conflicts: list[str] = []
        # The sparse worktree holds no .gitattributes and git 2.47 then
        # converts nothing, so both EOL filters read S0's attributes.
        attr_source = f"--attr-source={successor}"

        def launch_ancestor(relative: str, failure: str, scratch_file: Path) -> str | None:
            """P0's recorded launch-time canonical blob, re-cleaned for S0.

            A predecessor provisioned over promoted-but-uncommitted canonical
            bytes really started from those bytes, not from ``base``'s tree. The
            ``base`` ancestor then makes ours AND theirs both look like they
            added the seeded hunk, so an adjacent worker edit conflicts and the
            relaunch fails with a false ``rework_base_drift`` (NF-2026-01431).
            An unrecorded, misshaped, unreadable or digest-mismatched blob keeps
            the ``base`` ancestor; the re-clean is required because an
            un-normalised ancestor would conflict on EOL alone.
            """
            oid = source.parent_baseline_blob.get(relative)
            if not oid or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid) is None:
                return None
            raw = _cat_blob(repo, oid)
            digest = _parent_digest(source, relative)
            if raw is None or digest is None or hashlib.sha256(raw).hexdigest() != digest:
                return None
            scratch_file.write_bytes(raw)
            recorded = (attr_source, "hash-object", "-w", f"--path={relative}", str(scratch_file))
            return os.fsdecode(git(failure, *recorded)).strip() or None

        with tempfile.TemporaryDirectory(prefix="aiworkhub-rebase-") as scratch:
            sides = [Path(scratch, name) for name in ("ours", "base", "theirs")]
            for relative, theirs in (entry for entry in planned if entry[0] in drifted):
                failed = f"rework_base_drift_blob_failed:{relative}"
                clean = (attr_source, "hash-object", "-w", f"--path={relative}", str(sides[2]))
                meta = drifted[relative]
                ancestor, ours = (None if set(oid) == {"0"} else oid for oid in meta[2:4])
                ancestor = launch_ancestor(relative, failed, sides[1]) or ancestor
                sides[2].write_bytes(theirs or b"")
                result = None if theirs is None else os.fsdecode(git(failed, *clean)).strip()
                if {meta[0].lstrip(":"), meta[1]} - {"100644", "100755", "000000"}:
                    result = ""
                elif result in (ancestor, ours):
                    result = ours
                elif result is None and ancestor == ours:
                    pass  # theirs deleted a path ours kept at its launch bytes: propagate the deletion
                elif None in (ancestor, ours, result):
                    result = ""
                else:
                    for side, oid in zip(sides, (ours, ancestor, result), strict=True):
                        side.write_bytes(git(failed, "cat-file", "blob", str(oid)))
                    three_way = git("", "merge-file", "-p", *map(str, sides))
                    sides[2].write_bytes(three_way or b"")
                    result = "" if three_way is None else os.fsdecode(git(failed, *clean)).strip()
                if result == "":
                    conflicts.append(relative)
                else:
                    checkout = (attr_source, "cat-file", "--filters", f"--path={relative}", str(result))
                    merged[relative] = result and git(failed, *checkout)
        if conflicts:
            raise WorkspaceError(f"rework_base_drift:{','.join(sorted(conflicts))}")
        return merged

    return rebase
