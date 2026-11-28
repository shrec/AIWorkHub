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


def _normalised(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _line_ending_only(
    worktree: Path, modified: list[str], run: Callable[..., Any]
) -> set[str]:
    """Listed-modified paths whose seed equals the base blob modulo CRLF.

    NF-2026-01029: ``git status`` reports a size-changed tracked file as
    modified without comparing content, and the NF-2026-01168 restat cannot
    clear a CRLF-committed blob, an ambient ``core.autocrlf`` mismatch or a
    non-ASCII path.  The base blob is read directly (one ``git ls-files`` and
    one ``git cat-file --batch`` for any number of paths) and compared with
    the seeded bytes after CRLF -> LF on both sides, independent of any Git
    config.  Any probe failure returns no path, so every listed path stays
    dirty: an unknown state never reads as clean.
    """
    # ponytail: an owner edit that only changes line endings is not dirty;
    # a byte-exact comparison would lift this ceiling.
    if not modified:
        return set()
    from .worker_workspace import GitCommandTimeout

    try:
        listed = run(
            ["git", "--no-optional-locks", "ls-files", "-s", "-z"],
            cwd=worktree,
            phase="workspace_provision",
            text=False,
        )
        if listed.returncode != 0 or not isinstance(listed.stdout, bytes):
            return set()
        wanted = set(modified)
        oids: dict[str, str] = {}
        for record in _as_text(listed.stdout).split("\0"):
            meta, separator, listed_path = record.partition("\t")
            entry = meta.split()
            if (
                separator
                and listed_path in wanted
                and len(entry) == 3
                and entry[0] in {"100644", "100755"}
                and entry[2] == "0"
            ):
                oids[listed_path] = entry[1]
        ordered = [relative for relative in modified if relative in oids]
        if not ordered:
            return set()
        batch = run(
            ["git", "--no-optional-locks", "cat-file", "--batch"],
            cwd=worktree,
            phase="workspace_provision",
            input_text="".join(f"{oids[path]}\n" for path in ordered).encode("ascii"),
            text=False,
        )
        if batch.returncode != 0 or not isinstance(batch.stdout, bytes):
            return set()
        out: bytes = batch.stdout
        equal: set[str] = set()
        offset = 0
        for relative in ordered:
            newline = out.find(b"\n", offset)
            if newline < 0:
                return set()
            header = out[offset:newline].split()
            if (
                len(header) != 3
                or header[0] != oids[relative].encode("ascii")
                or header[1] != b"blob"
            ):
                return set()
            size = int(header[2])
            start = newline + 1
            blob = out[start : start + size]
            if len(blob) != size:
                return set()
            offset = start + size + 1
            seeded = worktree / relative
            if seeded.is_symlink() or not seeded.is_file():
                continue
            if _normalised(seeded.read_bytes()) == _normalised(blob):
                equal.add(relative)
        return equal
    except (GitCommandTimeout, OSError, ValueError, UnicodeError):
        return set()


def dirty_seeded_paths(
    worktree: Path, seeded: Iterable[str], run: Callable[..., Any]
) -> tuple[str, ...]:
    """Seeded paths whose seeded bytes carry a real change against the base.

    Runs inside the just-seeded worktree, whose index still equals the base
    commit, so a listed path is exactly one whose live canonical copy carried
    an uncommitted change (modified, staged or untracked) at seed time.  A
    tracked path Git lists as modified is dropped when its seed equals the
    base blob modulo line endings (NF-2026-01168, NF-2026-01029), whatever the
    ambient Git config.  A failed status probe refuses provisioning and a
    failed blob probe keeps the path dirty: an unknown dirty state must never
    read as clean.
    """
    from .worker_workspace import WorkspaceError, _relative_repo_path

    wanted = sorted(dict.fromkeys(str(relative) for relative in seeded))
    dirty: set[str] = set()
    modified: list[str] = []
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
                relative = _relative_repo_path(entry[3:])
                dirty.add(relative)
                status = entry[:2]
                if status in {" M", "M ", "MM"}:
                    modified.append(relative)
    listed = dirty & set(wanted)
    listed -= _line_ending_only(
        worktree, sorted(set(modified) & listed), run
    )
    return tuple(sorted(listed))


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
