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

from .worker_workspace import configured_runtime_root, configured_worktree_root

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


def _git_stdin(cwd: Path, stdin_text: str, *args: str) -> tuple[int, str]:
    """Run a git command in ``cwd`` feeding ``stdin_text`` on standard input.

    The sibling of :func:`_git` for the one batched query that must pass an
    unbounded ref list (``git rev-list --stdin``) without ever hitting a
    command-line length limit -- so a repository with hundreds of worktrees still
    costs ONE spawn, not one per chunk. Never raises: a missing binary or timeout
    resolves to a non-zero return code the caller treats as fail-closed.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return result.returncode, result.stdout.strip()


def directory_size_bytes(path: Path, *, exclude=None) -> int:
    """Apparent size of ``path`` in bytes (symlinks never followed).

    Walks with ``os.scandir`` rather than ``os.walk``: each entry's type comes
    from the single ``readdir`` batch instead of a separate ``lstat`` per file,
    and ``st_size`` is read with one ``stat`` -- roughly halving the syscalls per
    file over a tree of hundreds of thousands of files, which is a direct cause
    of the retention footprint walk running to its caller's deadline.

    ``exclude`` names directories to prune from the walk (compared by absolute
    path). The footprint measurement passes the worktree base here when it lives
    under ``.aiworkhub/runtime`` so the runtime subtotal never re-walks -- and
    never double-counts -- the same worktree bytes the worktree scan already
    measured. Pruning by exact path only; ownership is never inferred by name.
    """
    excluded: set[str] = set()
    for item in exclude or ():
        excluded.add(os.path.normcase(os.path.abspath(str(item))))
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if excluded and os.path.normcase(
                                os.path.abspath(entry.path)
                            ) in excluded:
                                continue
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
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


def _classify_deferred(git_state: dict[str, Any]) -> str:
    """Classify a worktree whose ``dirty`` state was DEFERRED in preview.

    Orphaned is still provable from ``git_ok`` alone (the ``.git`` pointer is
    missing or its parent no longer resolves it). But a git-ok worktree cannot be
    proven ``removable_safe`` -- that requires proving it CLEAN, and the clean
    check is the one per-worktree spawn the preview deliberately does not pay
    (see :func:`_batch_repo_worktree_states`). Rather than risk presenting an
    unproven-clean worktree as removable, it is conservatively ``retained_unsaved``
    here; the reclaim planner selects candidates from the canonical task lineage
    and the quarantine action re-verifies the exact per-worktree git state before
    it moves anything.
    """
    if not git_state["git_ok"]:
        return CLASS_ORPHANED
    return CLASS_RETAINED_UNSAVED


def _norm_path(path: str | Path) -> str:
    """A canonical key for comparing a filesystem path to git's own output.

    ``git worktree list`` prints each checkout's absolute path; the scan holds
    the same checkout as ``<base>/<id>/worktree``. Both are reduced to a
    real-path + ``normcase`` key so the two always match regardless of a symlink
    on the temp/base path or case folding on Windows -- attribution by exact
    identity, never by name.
    """
    try:
        resolved = os.path.realpath(str(path))
    except OSError:
        resolved = os.path.abspath(str(path))
    return os.path.normcase(resolved)


def _repo_registered_worktrees(repo_root: Path) -> dict[str, str]:
    """Map every checkout THIS repository has registered to its HEAD, from ONE
    ``git worktree list --porcelain -z`` spawn.

    Keyed by :func:`_norm_path` of the checkout so membership answers, in a
    single query for all worktrees at once, both "does this repository own the
    worktree" (the attribution the per-worktree ``git-common-dir`` spawn used to
    answer) and "what is its HEAD" (the per-worktree ``rev-parse HEAD`` spawn).
    """
    rc, raw = _git(repo_root, "worktree", "list", "--porcelain", "-z")
    if rc != 0 or not raw:
        return {}
    mapping: dict[str, str] = {}
    for record in _parse_worktree_porcelain(raw):
        raw_path = record.get("worktree", "")
        if not raw_path:
            continue
        mapping[_norm_path(raw_path)] = str(record.get("HEAD") or "").strip()
    return mapping


def _unpushed_heads(repo_root: Path, heads: set[str]) -> set[str]:
    """The subset of ``heads`` not reachable from any remote-tracking ref, from
    ONE ``git rev-list --stdin --not --remotes`` spawn.

    ``--stdin`` is consumed at its position on the command line -- BEFORE the
    ``--not`` that follows it -- so the piped HEADs are the positive set and
    ``--not --remotes`` excludes everything reachable from a remote (the standard
    ``rev-list --stdin --not --all`` idiom). A given HEAD therefore appears in the
    output exactly when it is not on any remote, i.e. unpushed. Any failure fails
    closed -- every head is treated as unpushed -- so an unproven commit is never
    presented as already saved.
    """
    real_heads = {head for head in heads if head}
    if not real_heads:
        return set()
    stdin_text = "\n".join(sorted(real_heads)) + "\n"
    rc, out = _git_stdin(repo_root, stdin_text, "rev-list", "--stdin", "--not", "--remotes")
    if rc != 0:
        return set(real_heads)
    reachable = {line.strip() for line in out.splitlines() if line.strip()}
    return {head for head in real_heads if head in reachable}


def _request_ledger_owner_task_id(repo_root: Path, base: Path, entry: Path) -> str:
    """Return the exact task id whose durable request ledger owns ``entry``.

    Git may prune a linked-worktree registration before retention gets a chance
    to account for the directory.  The request envelope is a second, exact
    ownership record: its request id, repository, checkout and HOME must all bind
    to this one repo-local entry.  A missing, oversized, symlinked or malformed
    envelope fails closed.  This is attribution only; deletion still goes through
    task-lineage protection and the quarantine path's exact safety checks.
    """
    if not _WORKTREE_ID_RE.fullmatch(entry.name):
        return ""
    request_path = (
        configured_runtime_root(repo_root)
        / "process_logs"
        / "processes"
        / f"{entry.name}.request.json"
    )
    try:
        if request_path.is_symlink() or not request_path.is_file():
            return ""
        if request_path.stat().st_size > 4 * 1024 * 1024:
            return ""
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    if payload.get("schema_id") != "aiworkhub.task_mcp.isolated_request.v1":
        return ""
    if str(payload.get("request_id") or "") != entry.name:
        return ""
    workspace = payload.get("workspace")
    if not isinstance(workspace, dict):
        return ""
    owner_task_id = str(payload.get("task_id") or "").strip()
    if not owner_task_id:
        return ""
    expected_entry = (base / entry.name).resolve()
    try:
        owned = (
            Path(str(workspace.get("repo") or "")).resolve() == repo_root.resolve()
            and Path(str(workspace.get("path") or "")).resolve()
            == (expected_entry / "worktree").resolve()
            and Path(str(workspace.get("home") or "")).resolve()
            == (expected_entry / "home").resolve()
            and str(workspace.get("request_id") or "") == entry.name
        )
        return owner_task_id if owned else ""
    except (OSError, RuntimeError):
        return ""


def _request_ledger_owns_entry(repo_root: Path, base: Path, entry: Path) -> bool:
    """Compatibility predicate for callers that need attribution only."""
    return bool(_request_ledger_owner_task_id(repo_root, base, entry))


def _linked_worktree_head(checkout: Path, repo_common_dir: str) -> str:
    """Read a detached worktree HEAD without spawning Git.

    This recovers attribution when ``git worktree list`` omitted a stale locked
    registration but the checkout still points at a live admin directory owned
    by this repository.  Symbolic HEADs deliberately fail closed; worker
    worktrees are created detached and a later exact cleanup check handles any
    exceptional layout.
    """
    try:
        pointer = (checkout / ".git").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    target = ""
    for line in pointer.splitlines():
        if line.strip().startswith("gitdir:"):
            target = line.split(":", 1)[1].strip()
            break
    if not target:
        return ""
    admin = Path(target)
    if not admin.is_absolute():
        admin = (checkout / admin).resolve()
    try:
        common = Path(repo_common_dir).resolve()
        resolved_admin = admin.resolve()
        if not resolved_admin.is_relative_to(common / "worktrees"):
            return ""
        head = (resolved_admin / "HEAD").read_text(encoding="ascii").strip().lower()
    except (OSError, UnicodeDecodeError, RuntimeError):
        return ""
    return head if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) else ""


def _batch_repo_worktree_states(
    repo_root: Path, repo_common_dir: str, entries: list[Path]
) -> dict[str, dict[str, Any]]:
    """Resolve every retained worktree's git safety state with a spawn count that
    does NOT grow with the number of worktrees.

    :func:`_worktree_git_state` pays FIVE git subprocesses per worktree
    (``rev-parse HEAD``, ``config --get remote.origin.url``, ``status
    --porcelain``, ``rev-list --count HEAD --not --remotes`` and the
    ``git-common-dir`` ``rev-parse``). On Windows -- no ``fork``, so every
    ``CreateProcess`` is far dearer than a Linux ``fork``+``exec`` and every git
    binary open is scanned by real-time AV -- that per-worktree spawn cost is
    what drives the retention preview past its deadline on a repository with
    hundreds of worktrees. The 0.9.79 change collapsed three full worktree walks
    into one but never touched this residual per-worktree cost.

    This replaces the per-worktree spawns with a FIXED set of repository-level
    queries and answers all worktrees from them:

    * one ``git worktree list --porcelain -z`` -> each registered checkout's HEAD
      and its attribution to this repository;
    * one ``git config --get remote.origin.url`` -> the origin shared by every
      worktree of this repository;
    * one ``git rev-list --stdin --not --remotes`` -> the unpushed HEAD set.

    The single field that genuinely cannot be answered without entering each
    worktree -- ``dirty`` (an uncommitted change is local to that checkout's
    working tree, with no repository-level batch equivalent) -- is DEFERRED and
    marked ``dirty_deferred`` rather than paid as a per-worktree ``git status``
    spawn. The complete counts, footprint and protected/candidate sets the
    preview reports do not depend on ``dirty``: candidates are established from
    the canonical task lineage, and the quarantine action re-verifies the exact
    per-worktree git state at the point of actual removal.
    """
    registered = _repo_registered_worktrees(repo_root)
    _rc, repo_origin = _git(repo_root, "config", "--get", "remote.origin.url")
    owned_head: dict[str, str] = {}
    ledger_owned: dict[str, str] = {}
    heads: set[str] = set()
    for entry in entries:
        checkout = entry / "worktree"
        head = registered.get(_norm_path(checkout), "")
        if not head:
            owner_task_id = _request_ledger_owner_task_id(repo_root, entry.parent, entry)
        else:
            owner_task_id = ""
        if not head and owner_task_id:
            ledger_owned[entry.name] = owner_task_id
            head = _linked_worktree_head(checkout, repo_common_dir)
        if head and checkout.is_dir():
            owned_head[entry.name] = head
            heads.add(head)
        else:
            owned_head[entry.name] = ""
    unpushed = _unpushed_heads(repo_root, heads)
    states: dict[str, dict[str, Any]] = {}
    for name, head in owned_head.items():
        git_ok = bool(head)
        owned = git_ok or name in ledger_owned
        states[name] = {
            "git_ok": git_ok,
            "origin": repo_origin if git_ok else "",
            "head": head,
            # Deferred: measuring an uncommitted working-tree change would cost a
            # per-worktree ``git status`` spawn. Left False but flagged so no
            # consumer reads it as a proven-clean signal.
            "dirty": False,
            "dirty_deferred": True,
            "unpushed": bool(git_ok and head in unpushed),
            "parent_git_dir": repo_common_dir if owned else "",
            "ownership_source": (
                "git_registration" if name not in ledger_owned else "request_ledger"
            ) if owned else "",
            "owner_task_id": ledger_owned.get(name, ""),
        }
    return states


def scan_worktrees(
    base: Path | None = None,
    *,
    with_sizes: bool = True,
    repo_root: Path | None = None,
    progress: Any | None = None,
) -> dict[str, Any]:
    """Enumerate every retained worktree under ``base`` with its safety class.

    Returns ``{base, exists, worktrees: [...], summary: {...}}``. Read-only:
    mutates nothing. ``with_sizes=False`` skips the (potentially slow) disk-usage
    walk for a fast listing.

    When ``repo_root`` is given the returned ``worktrees``/``summary`` stay
    repository-scoped exactly as before, but the single enumeration ALSO measures
    the foreign/orphaned entries once and returns their aggregate as
    ``global_summary`` -- so the footprint measurement obtains both the
    repo-scoped and the global figures from ONE pass instead of two full walks
    that each re-ran per-worktree git state over every entry.

    When ``repo_root`` is given the per-worktree git state is ALSO resolved in a
    fixed, repository-level batch (:func:`_batch_repo_worktree_states`) rather
    than by ~5 git subprocesses per worktree: the git subprocess spawn count of
    this scan no longer grows with the number of worktrees. That residual
    per-worktree spawn cost -- cheap on Linux, an order of magnitude dearer on
    Windows where every ``CreateProcess`` is AV-scanned and there is no ``fork``
    -- is the reason the preview overran its deadline on a Windows repository with
    hundreds of worktrees. The ``dirty`` field is deferred by that batch (see
    :func:`_classify_deferred`); the ``repo_root=None`` path keeps the exact,
    per-worktree ``git status`` classification the CLI cleanup relies on.

    ``progress`` is an optional sink notified as each worktree is fully measured
    (``begin`` with the id list, then ``observe`` per worktree). It lets a
    deadline-limited preview surface the reclaim candidates already established
    rather than an empty list; it never changes what is measured.
    """
    base = (base or configured_worktree_root(repo_root)).resolve()
    repo_common_dir = _git_common_dir(Path(repo_root).resolve()) if repo_root else ""
    all_worktrees: list[dict[str, Any]] = []
    worktrees: list[dict[str, Any]] = []
    if base.is_dir():
        entries = [
            entry
            for entry in sorted(base.iterdir())
            if entry.is_dir() and not entry.name.startswith(".")
        ]
        if progress is not None:
            try:
                progress.begin([entry.name for entry in entries])
            except Exception:  # noqa: BLE001 -- progress is best-effort telemetry
                pass
        # Repository-scoped scans resolve git state in a fixed repository-level
        # batch whose spawn count does not grow with the number of worktrees; the
        # global (``repo_root=None``) path keeps the exact per-worktree
        # classification the CLI cleanup deletes on.
        batched_states = (
            _batch_repo_worktree_states(Path(repo_root).resolve(), repo_common_dir, entries)
            if repo_root is not None
            else None
        )
        for entry in entries:
            worktree_dir = entry / "worktree"
            if batched_states is not None:
                git_state = batched_states[entry.name]
                worktree_class = _classify_deferred(git_state)
            else:
                git_state = (
                    _worktree_git_state(worktree_dir)
                    if worktree_dir.is_dir()
                    else {"git_ok": False, "origin": "", "head": "", "dirty": False,
                          "unpushed": False, "parent_git_dir": ""}
                )
                worktree_class = _classify(git_state)
            try:
                modified_at = entry.stat().st_mtime
            except OSError:
                modified_at = time.time()
            worktree = {
                "id": entry.name,
                "path": str(entry),
                "size_bytes": directory_size_bytes(entry) if with_sizes else None,
                "repo": _repo_name(git_state["origin"]),
                "origin": git_state["origin"],
                "head": git_state["head"],
                "git_ok": git_state["git_ok"],
                "dirty": git_state["dirty"],
                # True only on the repository-scoped preview path, where the
                # per-worktree ``git status`` spawn is deferred: ``dirty`` is then
                # unmeasured, so it must never be read as a proven-clean signal.
                "dirty_deferred": bool(git_state.get("dirty_deferred", False)),
                "unpushed": git_state["unpushed"],
                "parent_git_dir": git_state["parent_git_dir"],
                "ownership_source": str(git_state.get("ownership_source") or ""),
                "owner_task_id": str(git_state.get("owner_task_id") or ""),
                "modified_at_epoch": modified_at,
                "age_seconds": max(0.0, time.time() - modified_at),
                "class": worktree_class,
            }
            all_worktrees.append(worktree)
            in_repo = repo_root is None or (
                bool(repo_common_dir)
                and git_state.get("parent_git_dir") == repo_common_dir
            )
            if in_repo:
                # Orphaned or foreign-repository worktrees are deliberately
                # excluded from a repository-scoped view. Ownership cannot be
                # inferred from the directory name or remote URL.
                worktrees.append(worktree)
            if progress is not None:
                try:
                    progress.observe(worktree)
                except Exception:  # noqa: BLE001 -- progress is best-effort telemetry
                    pass
    result: dict[str, Any] = {
        "base": str(base),
        "exists": base.is_dir(),
        "scope": "repository" if repo_root else "global",
        "worktrees": worktrees,
        "summary": summarize(worktrees),
    }
    if repo_root is not None:
        # Same single pass, no second walk: the global aggregate the observed
        # footprint and unattributed-share comparison need.
        result["global_summary"] = summarize(all_worktrees)
    return result


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
