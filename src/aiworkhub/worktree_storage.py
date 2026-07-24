"""Repo-local visibility and SAFE, explicit cleanup for retained task worktrees.

Background
----------
AIWorkHub isolates each task in a git worktree under
``configured_worktree_root()`` (``$AIWORKHUB_WORKTREE_ROOT`` or
``$TMPDIR/aiworkhub-worktrees``), laid out as
``<root>/<request_id>/{worktree,home}``, and RETAINS it while the task is in
review (B914) so the reviewer can inspect the exact validated state. The
finalized-workspace GC (``process_launcher._gc_finalized_workspaces``) reclaims
a worktree only once its task reaches ``finished``/``archived``. There is no
bound on worktrees whose tasks reach ``review`` (or block) and are never
finalized, so they accumulate without limit -- a real 245 GB incident on a full
disk.

This module is Phase 1 of the B931 storage/retention design:

* **Visibility** (:func:`scan_worktrees`, :func:`summarize`): disk usage over the
  worktree base, per parent repository and per safety class, so the growth is
  seen long before the disk fills.
* **Data-loss-proof cleanup** (:func:`plan_cleanup`, :func:`execute_cleanup`): a
  worktree is removable ONLY when nothing can be lost -- it has no uncommitted
  changes AND its ``HEAD`` is already on a remote (fully pushed). A worktree
  with uncommitted edits or unpushed commits is NEVER removed, which is exactly
  what protects live/review work (that work is always dirty or committed-but-
  unpushed) without coupling to task state. Orphaned worktree directories whose
  git metadata is broken (parent repo gone/pruned) are reported separately and
  removed only when the caller explicitly opts in.

Automatic cleanup is OFF by default: :func:`execute_cleanup` performs a dry run
and deletes nothing unless the caller passes ``confirm=True``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .worker_workspace import configured_worktree_root

_GIT_TIMEOUT_SECONDS = 30

# Safety classes assigned to each retained worktree.
CLASS_REMOVABLE_SAFE = "removable_safe"      # git-clean AND fully pushed: nothing is lost
CLASS_RETAINED_UNSAVED = "retained_unsaved"  # uncommitted OR unpushed work: never auto-removed
CLASS_ORPHANED = "orphaned"                   # git metadata broken (parent gone): opt-in removal only


def _git(cwd: Path, *args: str) -> tuple[int, str]:
    """Run a git command in ``cwd``; return ``(returncode, stdout)``.

    Never raises: a missing git binary, a timeout, or a broken worktree pointer
    resolves to a non-zero return code so the caller classifies the worktree as
    orphaned rather than crashing the whole scan.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return result.returncode, result.stdout.strip()


def _dir_size_bytes(path: Path) -> int:
    """Apparent size of ``path`` in bytes (symlinks never followed)."""
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            fp = Path(root) / name
            try:
                if fp.is_symlink():
                    continue
                total += fp.stat().st_size
            except OSError:
                continue
    return total


def _repo_name(origin_url: str) -> str:
    """Short repository name from a remote URL (``.../foo.git`` -> ``foo``)."""
    tail = origin_url.rstrip("/").rsplit("/", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def _worktree_git_state(worktree_dir: Path) -> dict[str, Any]:
    """Classify one worktree checkout's git safety state.

    ``git_ok`` is false when the worktree's ``.git`` pointer is missing or its
    parent repository no longer resolves it (an orphan). ``dirty`` is any
    uncommitted tracked change or untracked file; ``unpushed`` is true when
    ``HEAD`` carries commits not reachable from any remote-tracking ref.
    """
    state: dict[str, Any] = {
        "git_ok": False,
        "origin": "",
        "head": "",
        "dirty": False,
        "unpushed": False,
        "parent_git_dir": "",
    }
    if not (worktree_dir / ".git").exists():
        return state
    rc, head = _git(worktree_dir, "rev-parse", "HEAD")
    if rc != 0 or not head:
        return state
    state["git_ok"] = True
    state["head"] = head
    _, origin = _git(worktree_dir, "config", "--get", "remote.origin.url")
    state["origin"] = origin
    _, porcelain = _git(worktree_dir, "status", "--porcelain")
    state["dirty"] = bool(porcelain.strip())
    rc_ahead, ahead = _git(worktree_dir, "rev-list", "--count", "HEAD", "--not", "--remotes")
    # Fail closed: if we cannot prove every commit is on a remote, treat it as
    # unpushed (never removable) rather than risk losing local commits.
    state["unpushed"] = not (rc_ahead == 0 and ahead == "0")
    _, common = _git(worktree_dir, "rev-parse", "--absolute-git-dir")
    if not common:
        _, common = _git(worktree_dir, "rev-parse", "--git-common-dir")
    state["parent_git_dir"] = common
    return state


def _classify(git_state: dict[str, Any]) -> str:
    if not git_state["git_ok"]:
        return CLASS_ORPHANED
    if git_state["dirty"] or git_state["unpushed"]:
        return CLASS_RETAINED_UNSAVED
    return CLASS_REMOVABLE_SAFE


def scan_worktrees(base: Path | None = None, *, with_sizes: bool = True) -> dict[str, Any]:
    """Enumerate every retained worktree under ``base`` with its safety class.

    Returns ``{base, exists, worktrees: [...], summary: {...}}``. Read-only:
    mutates nothing. ``with_sizes=False`` skips the (potentially slow) disk-usage
    walk for a fast listing.
    """
    base = (base or configured_worktree_root()).resolve()
    worktrees: list[dict[str, Any]] = []
    if base.is_dir():
        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            worktree_dir = entry / "worktree"
            git_state = (
                _worktree_git_state(worktree_dir)
                if worktree_dir.is_dir()
                else {"git_ok": False, "origin": "", "head": "", "dirty": False,
                      "unpushed": False, "parent_git_dir": ""}
            )
            worktrees.append(
                {
                    "id": entry.name,
                    "path": str(entry),
                    "size_bytes": _dir_size_bytes(entry) if with_sizes else None,
                    "repo": _repo_name(git_state["origin"]),
                    "origin": git_state["origin"],
                    "head": git_state["head"],
                    "git_ok": git_state["git_ok"],
                    "dirty": git_state["dirty"],
                    "unpushed": git_state["unpushed"],
                    "parent_git_dir": git_state["parent_git_dir"],
                    "class": _classify(git_state),
                }
            )
    return {
        "base": str(base),
        "exists": base.is_dir(),
        "worktrees": worktrees,
        "summary": summarize(worktrees),
    }


def summarize(worktrees: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a worktree list by safety class and by parent repository."""
    by_class: dict[str, dict[str, int]] = {}
    by_repo: dict[str, dict[str, int]] = {}
    total = 0
    for wt in worktrees:
        size = wt.get("size_bytes") or 0
        total += size
        cls = wt["class"]
        by_class.setdefault(cls, {"count": 0, "bytes": 0})
        by_class[cls]["count"] += 1
        by_class[cls]["bytes"] += size
        repo = wt.get("repo") or "unknown"
        by_repo.setdefault(repo, {"count": 0, "bytes": 0})
        by_repo[repo]["count"] += 1
        by_repo[repo]["bytes"] += size
    removable = by_class.get(CLASS_REMOVABLE_SAFE, {"count": 0, "bytes": 0})
    return {
        "count": len(worktrees),
        "total_bytes": total,
        "removable_safe_bytes": removable["bytes"],
        "by_class": by_class,
        "by_repo": by_repo,
    }


def plan_cleanup(
    scan: dict[str, Any] | None = None, *, include_orphaned: bool = False
) -> dict[str, Any]:
    """Split a scan into what a cleanup WOULD remove vs keep (no deletion).

    ``removable_safe`` worktrees are always eligible (fully committed + pushed).
    ``orphaned`` worktrees are eligible only when ``include_orphaned=True`` (their
    git metadata is broken, so their safety cannot be verified). ``retained_
    unsaved`` worktrees -- anything with uncommitted or unpushed work, including
    every live/review worktree -- are NEVER eligible.
    """
    if scan is None:
        scan = scan_worktrees()
    eligible = {CLASS_REMOVABLE_SAFE} | ({CLASS_ORPHANED} if include_orphaned else set())
    would_remove = [wt for wt in scan["worktrees"] if wt["class"] in eligible]
    would_keep = [wt for wt in scan["worktrees"] if wt["class"] not in eligible]
    return {
        "base": scan["base"],
        "include_orphaned": include_orphaned,
        "would_remove": would_remove,
        "would_keep": would_keep,
        "reclaim_bytes": sum((wt.get("size_bytes") or 0) for wt in would_remove),
        "kept_bytes": sum((wt.get("size_bytes") or 0) for wt in would_keep),
    }


def execute_cleanup(
    *,
    base: Path | None = None,
    include_orphaned: bool = False,
    confirm: bool = False,
    prune: bool = True,
) -> dict[str, Any]:
    """Remove only data-loss-proof worktrees; a dry run unless ``confirm=True``.

    With ``confirm=False`` (the default) nothing is deleted and the returned
    plan shows exactly what WOULD be removed. With ``confirm=True`` each eligible
    worktree directory is deleted and, when ``prune`` is set, each affected
    parent repository is ``git worktree prune``-d to drop the now-dangling
    registration. Every deletion is confined to ``base`` (a path escaping the
    base is refused, never deleted).
    """
    base = (base or configured_worktree_root()).resolve()
    scan = scan_worktrees(base)
    plan = plan_cleanup(scan, include_orphaned=include_orphaned)
    if not confirm:
        return {"ok": True, "dry_run": True, "confirmed": False, "reclaimed_bytes": 0,
                "removed": [], "errors": [], **plan}

    removed: list[str] = []
    errors: list[dict[str, str]] = []
    reclaimed = 0
    parents: set[str] = set()
    for wt in plan["would_remove"]:
        target = Path(wt["path"]).resolve()
        # Hard containment guard: never delete anything outside the base dir.
        if base != target and base not in target.parents:
            errors.append({"id": wt["id"], "error": "refused_path_outside_base"})
            continue
        if wt.get("parent_git_dir"):
            parents.add(wt["parent_git_dir"])
        try:
            shutil.rmtree(target)
            removed.append(wt["id"])
            reclaimed += wt.get("size_bytes") or 0
        except OSError as exc:
            errors.append({"id": wt["id"], "error": str(exc)[:200]})
    if prune:
        for parent_git_dir in sorted(parents):
            # `git --git-dir=<parent> worktree prune` drops registrations whose
            # worktree directory we just deleted. Best-effort and safe: prune
            # only removes entries for already-missing worktrees.
            try:
                subprocess.run(
                    ["git", "--git-dir", parent_git_dir, "worktree", "prune"],
                    capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.SubprocessError):
                continue
    return {
        "ok": not errors,
        "dry_run": False,
        "confirmed": True,
        "base": str(base),
        "include_orphaned": include_orphaned,
        "removed": removed,
        "errors": errors,
        "reclaimed_bytes": reclaimed,
        "kept": [wt["id"] for wt in plan["would_keep"]],
    }
