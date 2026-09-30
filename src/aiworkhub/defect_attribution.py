"""Automatic, fail-closed escaped-defect attribution via git SZZ blame.

For a converted, accepted NeedFix this locates the fix's landing commit on
main by matching promoted-path blob hashes against the fix card's own
accepted receipt, blames the lines that commit deleted or modified, and
matches the introducing commits' blobs against the supplied corpus of
accepted-outcome receipts (the same receipts
``sdlc_outcome_metrics.accepted_outcome_identity`` reads). A unique majority
match writes ``caused_by`` through the existing validated write-once path in
``needfix_store.update_needfix``; blame that resolves only to commits
matching no accepted receipt writes a verified-negative ``attribution_json``
disposition instead. Anything ambiguous -- a tie, an additive-only fix, a
missing fix commit, or a git error -- leaves the row unknown rather than
guessing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from aiworkhub import needfix_store

_GIT_TIMEOUT_SECONDS = 30
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
_BLAME_HEADER_RE = re.compile(r"^[0-9a-f]{40} \d+ \d+")
_AMBIGUOUS = object()  # sentinel: 2+ accepted receipts match one blamed commit's blobs


class AttributionError(Exception):
    """Internal, always-caught failure of a single git step."""


def _run_git(args: Sequence[str], *, cwd: str | Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AttributionError(f"git {' '.join(args)} failed to run: {exc}") from exc
    if result.returncode != 0:
        raise AttributionError(
            f"git {' '.join(args)} exited {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout


def _blob_sha256(repo_root: str | Path, commit: str, path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "show", f"{commit}:{path}"],
            cwd=str(repo_root),
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AttributionError(f"git show {commit}:{path} failed to run: {exc}") from exc
    if result.returncode != 0:
        return None
    return hashlib.sha256(result.stdout).hexdigest()


def _first_parent(repo_root: str | Path, commit: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", f"{commit}^"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AttributionError(f"git rev-parse {commit}^ failed to run: {exc}") from exc
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _tree_matches(
    repo_root: str | Path, commit: str, changed_path_hashes: Mapping[str, str | None]
) -> bool:
    for path, expected in changed_path_hashes.items():
        actual = _blob_sha256(repo_root, commit, path)
        if expected is None:
            if actual is not None:
                return False
        elif actual != expected:
            return False
    return True


def _find_landing_commit(
    repo_root: str | Path,
    changed_path_hashes: Mapping[str, str | None],
    *,
    ref: str = "main",
) -> str | None:
    if not changed_path_hashes:
        return None
    try:
        history = _run_git(
            ["log", "--first-parent", "--format=%H", ref, "--", *changed_path_hashes.keys()],
            cwd=repo_root,
        )
    except AttributionError:
        return None
    for commit in (line.strip() for line in history.splitlines() if line.strip()):
        if not _tree_matches(repo_root, commit, changed_path_hashes):
            continue
        parent = _first_parent(repo_root, commit)
        if parent is None or not _tree_matches(repo_root, parent, changed_path_hashes):
            return commit
    return None


def _deleted_modified_src_lines(
    repo_root: str | Path, fix_commit: str, parent_commit: str
) -> dict[str, list[int]]:
    """Map parent-side path -> deleted/modified line numbers, restricted to src/."""
    try:
        diff = _run_git(
            [
                "-c", "diff.mnemonicPrefix=false",
                "diff", "-U0", "--no-color", "--no-prefix",
                parent_commit, fix_commit, "--", "src/",
            ],
            cwd=repo_root,
        )
    except AttributionError:
        return {}
    result: dict[str, list[int]] = {}
    current_path: str | None = None
    in_header = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current_path = None
            in_header = True
        elif in_header and line.startswith("--- "):
            raw = line[4:]
            current_path = None if raw == "/dev/null" else raw
        elif in_header and line.startswith("+++ "):
            continue
        elif line.startswith("@@ "):
            in_header = False
            match = _HUNK_RE.match(line)
            if match and current_path:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) is not None else 1
                if count > 0:
                    result.setdefault(current_path, []).extend(range(start, start + count))
    return result


def _blame_commits_for_lines(
    repo_root: str | Path, parent_commit: str, path: str, line_numbers: Sequence[int]
) -> list[str]:
    if not line_numbers:
        return []
    args = ["blame", "--porcelain"]
    for line in line_numbers:
        args += ["-L", f"{line},{line}"]
    args += [parent_commit, "--", path]
    try:
        output = _run_git(args, cwd=repo_root)
    except AttributionError:
        return []
    return [line.split(" ", 1)[0] for line in output.splitlines() if _BLAME_HEADER_RE.match(line)]


def _identity_key(identity: Mapping[str, Any]) -> str:
    return json.dumps(dict(identity), sort_keys=True, separators=(",", ":"))


def _find_identity_for_task(
    accepted_receipts: Sequence[Mapping[str, Any]], task_id: str, repository_id: str
) -> Mapping[str, Any] | None:
    for identity in accepted_receipts:
        if (
            isinstance(identity, Mapping)
            and str(identity.get("task_id") or "") == task_id
            and str(identity.get("repository_id") or "") == str(repository_id)
            and isinstance(identity.get("accepted_outcome_receipt"), Mapping)
        ):
            return identity
    return None


def _norm(path: str) -> str:
    """Normalize a git path for stable cross-form comparison."""
    return str(path).replace("\\", "/").lstrip("./")


def _receipt_hash_index(
    accepted_receipts: Sequence[Mapping[str, Any]], repository_id: str
) -> dict[tuple[str, str], set[str]]:
    """Map (normalized path, blob sha256) -> identity keys that promoted that exact path+hash."""
    index: dict[tuple[str, str], set[str]] = {}
    for identity in accepted_receipts:
        if not isinstance(identity, Mapping):
            continue
        if str(identity.get("repository_id") or "") != str(repository_id):
            continue
        receipt = identity.get("accepted_outcome_receipt")
        if not isinstance(receipt, Mapping):
            continue
        hashes = receipt.get("changed_path_hashes")
        if not isinstance(hashes, Mapping):
            continue
        key = _identity_key(identity)
        for path, digest in hashes.items():
            if not isinstance(path, str) or not isinstance(digest, str):
                continue
            index.setdefault((_norm(path), digest), set()).add(key)
    return index


def _match_commit_to_identity_key(
    repo_root: str | Path, commit: str, index: Mapping[tuple[str, str], set[str]]
) -> str | None:
    try:
        changed_paths = _run_git(
            ["diff-tree", "--no-commit-id", "--name-only", "-r", commit], cwd=repo_root
        ).splitlines()
    except AttributionError:
        return None
    candidates: set[str] = set()
    for path in (p.strip() for p in changed_paths if p.strip()):
        digest = _blob_sha256(repo_root, commit, path)
        if digest is None:
            continue
        candidates |= index.get((_norm(path), digest), set())
    if not candidates:
        return None
    if len(candidates) > 1:
        return _AMBIGUOUS
    return next(iter(candidates))


def _majority(votes: Mapping[str | None, int]) -> tuple[str | None, bool]:
    """The unique strict-majority key, or (None, True) when the top is tied.

    A winner needs a strict majority of all votes AND no other accepted-card
    key holding any votes at all; a plurality or a split across multiple
    cards returns no winner (None, False) rather than guessing.
    """
    if not votes:
        return None, False
    total = sum(votes.values())
    top = max(votes.values())
    leaders = [key for key, count in votes.items() if count == top]
    if len(leaders) != 1:
        return None, True
    winner = leaders[0]
    if top * 2 <= total:
        return None, False
    other_card_key_has_votes = any(
        key is not None and key != winner and count > 0 for key, count in votes.items()
    )
    if other_card_key_has_votes:
        return None, False
    return winner, False


def attribute_needfix(
    repo_root: str | Path,
    needfix_id: str,
    *,
    repository_id: str,
    accepted_receipts: Sequence[Mapping[str, Any]],
    verify_accepted_outcome: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Attribute one converted NeedFix to its introducing accepted card, failing closed."""
    result: dict[str, Any] = {"needfix_id": needfix_id, "outcome": "skipped", "reason": None}

    try:
        needfix_row = needfix_store.get_needfix(repo_root, needfix_id)
    except needfix_store.NeedFixNotFoundError:
        result["reason"] = "needfix_not_found"
        return result

    fix_task_id = str(needfix_row.get("converted_task_id") or "").strip()
    if not fix_task_id:
        result["reason"] = "not_converted"
        return result
    if needfix_row.get("caused_by") or needfix_row.get("attribution_json"):
        result["reason"] = "already_attributed"
        return result

    fix_identity = _find_identity_for_task(accepted_receipts, fix_task_id, repository_id)
    if fix_identity is None:
        result["outcome"] = "unknown"
        result["reason"] = "fix_receipt_unavailable"
        return result

    receipt = fix_identity["accepted_outcome_receipt"]
    changed_path_hashes = receipt.get("changed_path_hashes") or {}

    try:
        fix_commit = _find_landing_commit(repo_root, changed_path_hashes)
        if fix_commit is None:
            result["outcome"] = "unknown"
            result["reason"] = "fix_commit_not_found"
            return result
        parent_commit = _first_parent(repo_root, fix_commit)
        if parent_commit is None:
            result["outcome"] = "unknown"
            result["reason"] = "fix_commit_is_root"
            return result
        deleted_lines = _deleted_modified_src_lines(repo_root, fix_commit, parent_commit)
        if not deleted_lines:
            result["outcome"] = "unknown"
            result["reason"] = "additive_only_fix"
            return result
        blamed_commits: list[str] = []
        for path, lines in deleted_lines.items():
            blamed_commits.extend(_blame_commits_for_lines(repo_root, parent_commit, path, lines))
    except AttributionError as exc:
        result["outcome"] = "unknown"
        result["reason"] = f"git_error:{exc}"
        return result

    if not blamed_commits:
        result["outcome"] = "unknown"
        result["reason"] = "no_blame_evidence"
        return result

    index = _receipt_hash_index(accepted_receipts, repository_id)
    identity_by_key = {
        _identity_key(identity): identity
        for identity in accepted_receipts
        if isinstance(identity, Mapping)
    }
    votes: dict[str | None, int] = {}
    for commit in blamed_commits:
        candidate_key = _match_commit_to_identity_key(repo_root, commit, index)
        votes[candidate_key] = votes.get(candidate_key, 0) + 1

    introducing_commits = sorted(set(blamed_commits))

    if _AMBIGUOUS in votes:
        result["outcome"] = "unknown"
        result["reason"] = "ambiguous_blame"
        return result

    if set(votes) == {None}:
        attribution = {
            "schema_id": needfix_store.ATTRIBUTION_SCHEMA_ID,
            "state": "non_card_change",
            "method": "szz_blame_v1",
            "fix_commit": fix_commit,
            "introducing_commits": introducing_commits,
            "evidence": {"blamed_line_votes": {"unmatched": votes[None]}},
        }
        if not dry_run:
            written = needfix_store.update_needfix(
                repo_root, needfix_id, attribution_json=attribution, repository_id=repository_id,
            )
            result["update_receipt"] = written.get("update_receipt")
        result["outcome"] = "non_card"
        result["attribution_json"] = attribution
        return result

    winner_key, tie = _majority(votes)

    if tie:
        result["outcome"] = "unknown"
        result["reason"] = "tie"
        return result

    if winner_key is None:
        result["outcome"] = "unknown"
        result["reason"] = "mixed_blame"
        return result

    identity = identity_by_key[winner_key]
    if not dry_run:
        written = needfix_store.update_needfix(
            repo_root,
            needfix_id,
            caused_by=identity,
            repository_id=repository_id,
            verify_accepted_outcome=verify_accepted_outcome,
        )
        result["update_receipt"] = written.get("update_receipt")
    result["outcome"] = "attributed"
    result["caused_by"] = identity
    return result


def attribute_many(
    repo_root: str | Path,
    needfix_ids: Sequence[str],
    *,
    repository_id: str,
    accepted_receipts: Sequence[Mapping[str, Any]],
    verify_accepted_outcome: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
    dry_run: bool = False,
    max_workers: int | None = None,
) -> list[dict[str, Any]]:
    """Attribute many converted NeedFix rows.

    Each row's work is independent git subprocess I/O, so a bounded thread
    pool overlaps that wait across rows instead of a process pool; sizing off
    os.cpu_count() with the stdlib ThreadPoolExecutor's own IO-bound headroom
    (+4) keeps this identical in outcome to running the rows sequentially.
    """
    if not needfix_ids:
        return []
    workers = max_workers if max_workers is not None else min(32, (os.cpu_count() or 1) + 4)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [
            pool.submit(
                attribute_needfix,
                repo_root,
                needfix_id,
                repository_id=repository_id,
                accepted_receipts=accepted_receipts,
                verify_accepted_outcome=verify_accepted_outcome,
                dry_run=dry_run,
            )
            for needfix_id in needfix_ids
        ]
        return [future.result() for future in futures]
