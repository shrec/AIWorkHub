"""Repo-local visibility and SAFE, explicit cleanup for retained task worktrees.

Background
----------
AIWorkHub isolates each task in a git worktree under
``configured_worktree_root()`` (``$AIWORKHUB_WORKTREE_ROOT``,
``$AIWORKHUB_RUNTIME_ROOT/worktrees``, or the repository-local
``.aiworkhub/runtime/worktrees``), laid out as
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

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .worker_workspace import configured_worktree_root

_GIT_TIMEOUT_SECONDS = 30
_WORKTREE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
REGISTRATION_SCHEMA_ID = "aiworkhub.worktree_registration_preview.v1"
REGISTRATION_CANDIDATE_LIMIT = 256

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


def directory_size_bytes(path: Path) -> int:
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


# Backward-compatible private alias for older callers/tests.
_dir_size_bytes = directory_size_bytes


def _repo_name(origin_url: str) -> str:
    """Short repository name from a remote URL (``.../foo.git`` -> ``foo``)."""
    tail = origin_url.rstrip("/").rsplit("/", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def _git_common_dir(cwd: Path) -> str:
    rc, common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if rc != 0 or not common:
        rc, common = _git(cwd, "rev-parse", "--git-common-dir")
    if rc != 0 or not common:
        return ""
    candidate = Path(common)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        return os.path.normcase(str(candidate.resolve()))
    except OSError:
        return ""


def _parse_worktree_porcelain(raw: str) -> list[dict[str, str]]:
    """Parse ``git worktree list --porcelain -z`` without path guessing."""

    records: list[dict[str, str]] = []
    for block in raw.split("\0\0"):
        record: dict[str, str] = {}
        for field in block.split("\0"):
            if not field:
                continue
            key, _, value = field.partition(" ")
            record[key] = value
        if record.get("worktree"):
            records.append(record)
    return records


def _owned_registration_id(raw_path: str, base: Path) -> str:
    """Return the request id only for the exact ``base/<id>/worktree`` shape."""

    if not raw_path:
        return ""
    candidate = Path(os.path.abspath(raw_path))
    normalized_base = Path(os.path.abspath(base))
    if candidate.name != "worktree" or candidate.parent.parent != normalized_base:
        return ""
    request_id = candidate.parent.name
    return request_id if _WORKTREE_ID_RE.fullmatch(request_id) else ""


def scan_worktree_registrations(
    repo_root: Path | str,
    base: Path | None = None,
) -> dict[str, Any]:
    """Read exact Git worktree registrations and attribute stale entries.

    Only the canonical ``<worktree-root>/<request-id>/worktree`` layout is
    attributed to AIWorkHub. Missing/prunable registrations outside that shape
    are counted as foreign and make pruning fail closed.
    """

    root = Path(repo_root).resolve()
    worktree_base = (base or configured_worktree_root(root)).resolve()
    rc, raw = _git(root, "worktree", "list", "--porcelain", "-z")
    if rc != 0:
        return {
            "ok": False,
            "schema_id": REGISTRATION_SCHEMA_ID,
            "error": "worktree_registration_list_failed",
            "registered_count": 0,
            "aiworkhub_registered_count": 0,
            "stale_candidate_count": 0,
            "candidate_overflow_count": 0,
            "foreign_stale_count": 0,
            "stale_candidates": [],
            "safe_to_prune": False,
            "preview_digest": "",
        }

    registered_count = 0
    owned_count = 0
    candidates: list[dict[str, str]] = []
    foreign_stale_count = 0
    for record in _parse_worktree_porcelain(raw):
        registered_count += 1
        raw_path = record.get("worktree", "")
        request_id = _owned_registration_id(raw_path, worktree_base)
        missing = not Path(raw_path).exists()
        prune_reason = str(record.get("prunable") or "")
        stale = missing or bool(prune_reason)
        if request_id:
            owned_count += 1
            if stale:
                candidates.append({
                    "id": request_id,
                    "reason": "prunable" if prune_reason else "missing_checkout",
                })
        elif stale:
            foreign_stale_count += 1

    candidates.sort(key=lambda item: item["id"])
    candidate_count = len(candidates)
    candidate_overflow_count = max(0, candidate_count - REGISTRATION_CANDIDATE_LIMIT)
    bounded_candidates = candidates[:REGISTRATION_CANDIDATE_LIMIT]
    digest_payload = {
        "schema_id": REGISTRATION_SCHEMA_ID,
        "repo_common_dir": _git_common_dir(root),
        "base": os.path.normcase(str(worktree_base)),
        "candidates": candidates,
        "foreign_stale_count": foreign_stale_count,
    }
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "ok": True,
        "schema_id": REGISTRATION_SCHEMA_ID,
        "registered_count": registered_count,
        "aiworkhub_registered_count": owned_count,
        "stale_candidate_count": candidate_count,
        "candidate_overflow_count": candidate_overflow_count,
        "foreign_stale_count": foreign_stale_count,
        "stale_candidates": bounded_candidates,
        "safe_to_prune": bool(candidates) and foreign_stale_count == 0 and candidate_overflow_count == 0,
        "preview_digest": digest,
    }


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
    state["parent_git_dir"] = _git_common_dir(worktree_dir)
    return state


def _classify(git_state: dict[str, Any]) -> str:
    if not git_state["git_ok"]:
        return CLASS_ORPHANED
    if git_state["dirty"] or git_state["unpushed"]:
        return CLASS_RETAINED_UNSAVED
    return CLASS_REMOVABLE_SAFE


def scan_worktrees(
    base: Path | None = None,
    *,
    with_sizes: bool = True,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Enumerate every retained worktree under ``base`` with its safety class.

    Returns ``{base, exists, worktrees: [...], summary: {...}}``. Read-only:
    mutates nothing. ``with_sizes=False`` skips the (potentially slow) disk-usage
    walk for a fast listing.
    """
    base = (base or configured_worktree_root(repo_root)).resolve()
    repo_common_dir = _git_common_dir(Path(repo_root).resolve()) if repo_root else ""
    worktrees: list[dict[str, Any]] = []
    if base.is_dir():
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            worktree_dir = entry / "worktree"
            git_state = (
                _worktree_git_state(worktree_dir)
                if worktree_dir.is_dir()
                else {"git_ok": False, "origin": "", "head": "", "dirty": False,
                      "unpushed": False, "parent_git_dir": ""}
            )
            if repo_root and (
                not repo_common_dir
                or git_state.get("parent_git_dir") != repo_common_dir
            ):
                # Orphaned or foreign-repository worktrees are deliberately
                # excluded from a repository-scoped view. Ownership cannot be
                # inferred from the directory name or remote URL.
                continue
            try:
                modified_at = entry.stat().st_mtime
            except OSError:
                modified_at = time.time()
            worktrees.append(
                {
                    "id": entry.name,
                    "path": str(entry),
                    "size_bytes": directory_size_bytes(entry) if with_sizes else None,
                    "repo": _repo_name(git_state["origin"]),
                    "origin": git_state["origin"],
                    "head": git_state["head"],
                    "git_ok": git_state["git_ok"],
                    "dirty": git_state["dirty"],
                    "unpushed": git_state["unpushed"],
                    "parent_git_dir": git_state["parent_git_dir"],
                    "modified_at_epoch": modified_at,
                    "age_seconds": max(0.0, time.time() - modified_at),
                    "class": _classify(git_state),
                }
            )
    return {
        "base": str(base),
        "exists": base.is_dir(),
        "scope": "repository" if repo_root else "global",
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
    scan: dict[str, Any] | None = None,
    *,
    include_orphaned: bool = False,
    min_age_days: int = 0,
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
    safe_age_days = max(0, min(int(min_age_days), 3650))
    minimum_age_seconds = safe_age_days * 86400
    eligible = {CLASS_REMOVABLE_SAFE} | ({CLASS_ORPHANED} if include_orphaned else set())
    would_remove = [
        wt
        for wt in scan["worktrees"]
        if wt["class"] in eligible
        and float(wt.get("age_seconds") or 0.0) >= minimum_age_seconds
    ]
    would_keep = [wt for wt in scan["worktrees"] if wt not in would_remove]
    return {
        "base": scan["base"],
        "include_orphaned": include_orphaned,
        "min_age_days": safe_age_days,
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


def _human_bytes(num: int | None) -> str:
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def format_report(scan: dict[str, Any]) -> str:
    """Human-readable summary of a :func:`scan_worktrees` result."""
    s = scan["summary"]
    lines = [
        f"AIWorkHub retained worktrees: {scan['base']}",
        f"  total: {s['count']} worktrees, {_human_bytes(s['total_bytes'])}"
        f"  (reclaimable now: {_human_bytes(s['removable_safe_bytes'])})",
    ]
    if s["by_class"]:
        lines.append("  by safety class:")
        for cls, agg in sorted(s["by_class"].items()):
            lines.append(f"    {cls:18s} {agg['count']:4d}  {_human_bytes(agg['bytes'])}")
    if s["by_repo"]:
        lines.append("  by repository:")
        for repo, agg in sorted(s["by_repo"].items(), key=lambda kv: -kv[1]["bytes"]):
            lines.append(f"    {repo:24s} {agg['count']:4d}  {_human_bytes(agg['bytes'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI: report retained-worktree storage, or safely clean it.

    Default prints a read-only report. ``--cleanup`` shows what a cleanup WOULD
    remove (dry run); adding ``--confirm`` actually deletes the data-loss-proof
    (git-clean + fully pushed) worktrees. ``--include-orphaned`` extends cleanup
    to worktrees with broken git metadata. Nothing is ever deleted without
    ``--confirm``.
    """
    parser = argparse.ArgumentParser(
        prog="aiworkhub.worktree_storage",
        description="Visibility and data-loss-proof cleanup for retained task worktrees.",
    )
    parser.add_argument("--base", default=None, help="worktree base dir (default: configured root)")
    parser.add_argument("--cleanup", action="store_true", help="plan/execute cleanup instead of just reporting")
    parser.add_argument("--confirm", action="store_true", help="actually delete (with --cleanup); default is a dry run")
    parser.add_argument("--include-orphaned", action="store_true", help="also remove orphaned (broken-git) worktrees")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    base = Path(args.base) if args.base else None
    if not args.cleanup:
        scan = scan_worktrees(base)
        print(json.dumps(scan, indent=2) if args.json else format_report(scan))
        return 0

    result = execute_cleanup(base=base, include_orphaned=args.include_orphaned, confirm=args.confirm)
    if args.json:
        print(json.dumps(result, indent=2))
    elif result["dry_run"]:
        print(f"DRY RUN — would remove {len(result['would_remove'])} worktree(s), "
              f"reclaim {_human_bytes(result['reclaim_bytes'])}; keep {len(result['would_keep'])}. "
              f"Re-run with --confirm to delete.")
    else:
        print(f"removed {len(result['removed'])} worktree(s), reclaimed "
              f"{_human_bytes(result['reclaimed_bytes'])}; kept {len(result['kept'])}"
              + (f"; {len(result['errors'])} error(s)" if result["errors"] else ""))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
