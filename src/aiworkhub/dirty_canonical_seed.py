"""Owner's uncommitted canonical edits never become worker output (NF-2026-01997).

Provisioning live-seeds declared inputs, ``allowed_writes`` and the support
closure from the canonical working tree (NF-2026-00423: one coherent
current-canonical generation).  When the owner has an uncommitted edit to such
a file, the seeded copy differs from the base commit the worktree is detached
at, so a raw ``git diff <base_oid>`` inside the worktree lists it.

The candidate delta is measured against the bytes that were actually seeded
(``workspace_baseline``), so an unchanged dirty seed is never a changed path.
A worker that *edits* such a path, however, would carry the owner's
uncommitted hunk inside its candidate bytes, and promotion would write it --
and the accept auto-commit would commit it -- under the worker's name.  That
candidate is refused before any canonical write as a ``promotion_conflict``.

:func:`dirty_seeded_paths` measures the dirty-at-seed set once, inside the
freshly seeded worktree (index == base commit), with one bounded
``git status`` per argv chunk; the result is recorded in the workspace
metadata so finalize and promote never re-derive it.
:func:`refuse_dirty_at_seed` is the promote-time preflight.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable

# Code of the refusal; listed in ``promotion_conflict_detail`` so it maps to
# ``promotion_conflict`` and to a pre-write accept refusal.
DIRTY_AT_SEED_CODE = "promotion_canonical_dirty_at_seed"
# Keep each ``git status`` argv well below the Windows 32 KiB command limit.
_ARGV_PATH_CHARS = 8000


def _chunks(paths: list[str]) -> Iterable[list[str]]:
    chunk: list[str] = []
    size = 0
    for relative in paths:
        if chunk and size + len(relative) + 1 > _ARGV_PATH_CHARS:
            yield chunk
            chunk, size = [], 0
        chunk.append(relative)
        size += len(relative) + 1
    if chunk:
        yield chunk


def _as_text(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "surrogateescape")
    return value or ""


def dirty_seeded_paths(
    worktree: Path, seeded: Iterable[str], run: Callable[..., Any]
) -> tuple[str, ...]:
    """Seeded paths whose seeded bytes differ from the worktree's base commit.

    Runs inside the just-seeded worktree, whose index still equals the base
    commit, so a listed path is exactly one whose live canonical copy carried
    an uncommitted change (modified, staged or untracked) at seed time.  Git's
    own content normalisation applies, so a line-ending-only stat difference
    (NF-2026-01168) is not dirty.  A failed probe refuses provisioning: an
    unknown dirty state must never read as clean.
    """
    from .worker_workspace import WorkspaceError, _relative_repo_path

    wanted = sorted(dict.fromkeys(str(relative) for relative in seeded))
    dirty: set[str] = set()
    for chunk in _chunks(wanted):
        result = run(
            [
                "git", "--no-optional-locks", "--literal-pathspecs", "status",
                "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all",
                "--", *chunk,
            ],
            cwd=worktree,
            phase="workspace_provision",
            text=False,
        )
        if result.returncode != 0:
            stderr = _as_text(result.stderr)
            raise WorkspaceError(f"dirty_canonical_seed_probe_failed:{stderr[:300]}")
        for entry in _as_text(result.stdout).split("\0"):
            if len(entry) > 3:
                dirty.add(_relative_repo_path(entry[3:]))
    return tuple(sorted(dirty & set(wanted)))


def refuse_dirty_at_seed(workspace: Any, paths: Iterable[str]) -> None:
    """Refuse promotion of a candidate path that was dirty in canonical at seed.

    Raised from ``promote``'s preflight, before any canonical write.  A path
    whose canonical bytes already equal the candidate bytes needs no write and
    is not refused.  The tail names the expected (seeded canonical), observed
    (current canonical) and candidate hashes (NF-2026-01960).
    """
    from .promotion_conflict_detail import conflict_detail
    from .worker_workspace import WorkspaceError, _hash_path

    dirty = set(getattr(workspace, "dirty_at_seed", ()) or ())
    for relative in sorted(set(paths) & dirty):
        current = _hash_path(workspace.repo / relative)
        candidate = _hash_path(workspace.path / relative)
        if current == candidate:
            continue
        raise WorkspaceError(
            f"{DIRTY_AT_SEED_CODE}:{relative}"
            + conflict_detail(workspace.parent_baseline.get(relative), current, candidate)
        )
