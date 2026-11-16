"""Accept-time scope identity for canonical files leaked into a review worktree.

NF-2026-02290. While a card waits in review, a peer card's file can be accepted
into canonical ``HEAD``. The completion quality gate then fills tracked
canonical ``scripts/``/``src/``/``tests/`` files the sparse candidate root lacks
from its canonical ``baseline_root``
(``quality_evidence._fill_sparse_toolchain_inputs``, NF-2026-02284); when that
root is the retained review worktree, the peer's new file lands there as an
UNTRACKED file byte-identical to canonical ``HEAD``; a peer's newer bytes for
a file the worktree's pinned base already tracks land there as a TRACKED,
MODIFIED file (`` M``, no skip-worktree bit) equally byte-identical to
canonical ``HEAD``. Accept revalidation then reported
``scope_violation:<path>`` for bytes the candidate never wrote.

This module names that one shape and nothing wider. A path is dropped from the
accept-time delta only when ALL of these hold:

* it is outside the candidate's ``allowed_writes`` -- finalization refused any
  such path, so the candidate cannot have recorded it in ``changed_paths``;
* it is a regular file in the review worktree (untracked or tracked-modified);
* canonical ``HEAD`` tracks a regular blob at that exact path; and
* the worktree bytes are that blob's raw bytes, or are byte-identical to the
  canonical checkout of that path whose content Git hashes to that same blob.

Every other path keeps its delta, so bytes that differ from canonical, a path
the candidate may write, or anything a failed Git probe cannot prove still
raises ``scope_violation`` (fail closed). The probe is bounded: past
``MAX_LEAK_PROBE_PATHS`` outside paths nothing is relaxed.
"""

from __future__ import annotations

import filecmp
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol

#: Upper bound on outside-scope paths probed per check; beyond it no relaxation.
MAX_LEAK_PROBE_PATHS = 256

_REGULAR_BLOB_MODES = frozenset({"100644", "100755"})


class _ScopedWorkspace(Protocol):
    @property
    def repo(self) -> Path: ...
    @property
    def path(self) -> Path: ...
    @property
    def allowed_writes(self) -> Iterable[str]: ...


RunGit = Callable[..., Any]
MatchScope = Callable[[str, Iterable[str]], bool]


def _nul_rows(text: object) -> list[str]:
    return [row for row in str(text or "").split("\x00") if row]


def _is_regular(path: Path) -> bool:
    return not path.is_symlink() and path.is_file()


def _head_blobs(
    repo: Path, paths: Sequence[str], *, run: RunGit, timeout: float, phase: str
) -> dict[str, str]:
    result = run(
        ["git", "--literal-pathspecs", "ls-tree", "-r", "-z", "--full-tree",
         "HEAD", "--", *paths],
        cwd=repo, timeout=timeout, phase=phase,
    )
    if result.returncode != 0:
        return {}
    blobs: dict[str, str] = {}
    for row in _nul_rows(result.stdout):
        meta, _, relative = row.partition("\t")
        fields = meta.split()
        if len(fields) == 3 and fields[0] in _REGULAR_BLOB_MODES and fields[1] == "blob":
            blobs[relative] = fields[2]
    return blobs


def _hash_objects(
    cwd: Path,
    paths: Sequence[str],
    *,
    raw: bool,
    run: RunGit,
    timeout: float,
    phase: str,
) -> list[str] | None:
    argv = ["git", "hash-object", *(["--no-filters"] if raw else []), "--", *paths]
    result = run(argv, cwd=cwd, timeout=timeout, phase=phase)
    oids = str(result.stdout or "").split()
    if result.returncode != 0 or len(oids) != len(paths):
        return None
    return oids


def leaked_canonical_paths(
    repo: Path,
    worktree: Path,
    paths: Iterable[str],
    *,
    run: RunGit,
    timeout: float,
    phase: str,
) -> set[str]:
    """Return the subset of ``paths`` whose worktree bytes are canonical HEAD's.

    Tracked-modified and untracked paths alike: callers pass only paths outside
    ``allowed_writes``, so byte identity with canonical HEAD's blob already
    proves the candidate did not author those bytes.
    """

    candidates = sorted({p for p in paths if _is_regular(worktree / p)})
    if not candidates or len(candidates) > MAX_LEAK_PROBE_PATHS:
        return set()
    probe = {"run": run, "timeout": timeout, "phase": phase}
    head = _head_blobs(repo, candidates, **probe)
    candidates = [p for p in candidates if p in head]
    if not candidates:
        return set()
    raw_oids = _hash_objects(worktree, candidates, raw=True, **probe)
    if raw_oids is None:
        return set()
    leaked = {p for p, oid in zip(candidates, raw_oids) if oid == head[p]}
    # Canonical's own checkout of the blob (e.g. CRLF under core.autocrlf):
    # same bytes as the canonical file, which Git hashes to the HEAD blob.
    checkout = [
        p for p in candidates
        if p not in leaked
        and _is_regular(repo / p)
        and filecmp.cmp(worktree / p, repo / p, shallow=False)
    ]
    if checkout:
        canonical_oids = _hash_objects(repo, checkout, raw=False, **probe)
        if canonical_oids is not None:
            leaked.update(p for p, oid in zip(checkout, canonical_oids) if oid == head[p])
    return leaked


def drop_leaked_canonical(
    workspace: _ScopedWorkspace,
    changed: Sequence[str],
    matches: MatchScope,
    *,
    run: RunGit,
    timeout: float,
    phase: str = "review_acceptance",
) -> list[str]:
    """``changed`` minus outside-scope byte copies of canonical HEAD blobs."""

    outside = [p for p in changed if not matches(p, workspace.allowed_writes)]
    if not outside:
        return list(changed)
    leaked = leaked_canonical_paths(
        Path(workspace.repo), Path(workspace.path), outside,
        run=run, timeout=timeout, phase=phase,
    )
    return [p for p in changed if p not in leaked]
