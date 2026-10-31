"""Hide live-seeded ``.gitignore`` edits from Git surfaces in worker worktrees.

NF-2026-01889: provisioning seeds the owner's LIVE ``.gitignore`` files over the
worktree's HEAD checkout.  When the owner has uncommitted edits, ``git status``
and ``git diff`` in every worktree then report ``.gitignore`` as modified.  The
live bytes must stay on disk (they drive ignore matching), so the entry is
hidden from Git instead of being reverted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Any

_GITIGNORE_NAME = ".gitignore"
_HIDE_FLAGS = ("--skip-worktree", "--assume-unchanged")


def hide_live_gitignore_seeds(
    worktree: Path,
    seeded: Iterable[str],
    run: Callable[..., Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Mark seeded ``.gitignore`` files whose live bytes differ from HEAD.

    ``run`` is the shell-free git runner of the caller.  Both index flags are
    required: in the sparse provisioning worktree ``--skip-worktree`` alone is
    cleared again.  Returns ``(marked_paths, notes)``; any git failure becomes a
    note and never raises, so provisioning is never failed by this step.
    """
    candidates = [
        relative
        for relative in dict.fromkeys(seeded)
        if PurePosixPath(relative).name == _GITIGNORE_NAME
        and not any(character in relative for character in "\n\r\0")
    ]
    if not candidates:
        return (), ()
    try:
        listed = run(
            ["git", "--literal-pathspecs", "ls-tree", "-z", "HEAD", "--", *candidates],
            cwd=worktree,
            phase="workspace_provision",
            text=False,
        )
        if listed.returncode != 0:
            return (), (f"gitignore_seed_ls_tree_failed:{listed.returncode}",)
        head_oids: dict[str, str] = {}
        for record in listed.stdout.decode("utf-8", "replace").split("\0"):
            meta, separator, listed_path = record.partition("\t")
            entry = meta.split()
            if separator and len(entry) == 3 and entry[1] == "blob":
                head_oids[listed_path] = entry[2]
        tracked = [relative for relative in candidates if relative in head_oids]
        if not tracked:
            return (), ()
        hashed = run(
            ["git", "hash-object", "--stdin-paths"],
            cwd=worktree,
            phase="workspace_provision",
            input_text="".join(f"{relative}\n" for relative in tracked),
        )
        oids = hashed.stdout.split()
        if hashed.returncode != 0 or len(oids) != len(tracked):
            return (), (f"gitignore_seed_hash_failed:{hashed.returncode}",)
        changed = [
            relative
            for relative, oid in zip(tracked, oids, strict=True)
            if oid != head_oids[relative]
        ]
        if not changed:
            return (), ()
        marked = run(
            ["git", "--literal-pathspecs", "update-index", *_HIDE_FLAGS, "--", *changed],
            cwd=worktree,
            phase="workspace_provision",
        )
        if marked.returncode != 0:
            return (), (f"gitignore_seed_update_index_failed:{marked.returncode}",)
        return tuple(changed), ()
    except Exception as exc:  # noqa: BLE001 - a git failure must never fail the launch
        return (), (f"gitignore_seed_hide_failed:{type(exc).__name__}",)
