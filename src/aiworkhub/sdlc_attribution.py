"""Deterministic escaped-defect attribution: which accepted card introduced a NeedFix.

Component B of the SDLC loop-closure design. Given a NeedFix whose converted
task was manager-accepted, this answers one narrow question from immutable
evidence only: whose promoted bytes did the fix have to delete or rewrite?

The whole chain is receipts and git plumbing. Nothing here reads a title, a
description or any other prose, and nothing here calls a model:

1. The fix card's own ``accepted_outcome_receipt`` carries ``base_oid`` and the
   promoted paths. Its canonical commit is the first descendant of ``base_oid``
   holding every promoted-path hash -- :func:`first_holding_commit`, the same
   helper ``scripts/build_accepted_task_eval.py --verify-provenance`` uses, so
   provenance and attribution can never drift into two answers. The commit is
   identified from *every* promoted path (that is what the receipt seals); only
   the non-test ones are then examined for blame.
2. ``git diff base_oid..fix_commit -- <path>`` names the pre-image lines the fix
   deleted or modified, and ``git blame`` at ``base_oid`` names the commit each
   of those lines came from.
3. A blamed commit is explained by an accepted receipt when that receipt
   promoted the same path and its own first canonical holding commit IS the
   blamed commit. Identity, never resemblance.
4. The receipt explaining the most blamed lines is the cause. A tie, a purely
   additive fix, and a blamed commit no receipt explains are each ``unknown``
   with a typed reason -- and ``unknown`` never writes.

Only step 4's single winner is written, as ``caused_by`` through
``needfix_store`` with its own ``validate_caused_by`` verification. Every git
call is an argument list; ``shell=True`` appears nowhere in this module.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import needfix_store, task_store

SCHEMA_ID = "aiworkhub.sdlc_attribution.v1"
REPORT_SCHEMA_ID = "aiworkhub.sdlc_attribution_report.v1"

#: The one reason that writes. Every other reason below leaves the row alone.
ATTRIBUTED = "attributed"
UNKNOWN_ADDITIVE_ONLY = "additive_only"
UNKNOWN_NO_RECEIPT = "no_receipt"
UNKNOWN_TIE = "tie"
UNKNOWN_NOT_CONVERTED = "not_converted"
UNKNOWN_FIX_NOT_ACCEPTED = "fix_not_accepted"
UNKNOWN_NO_SOURCE_PATHS = "no_non_test_promoted_paths"
UNKNOWN_FIX_COMMIT_NOT_FOUND = "fix_commit_not_found"
UNKNOWN_GIT_UNAVAILABLE = "git_unavailable"
UNKNOWN_ROW_UNREADABLE = "row_unreadable"

UNKNOWN_REASONS: tuple[str, ...] = (
    UNKNOWN_ADDITIVE_ONLY,
    UNKNOWN_NO_RECEIPT,
    UNKNOWN_TIE,
    UNKNOWN_NOT_CONVERTED,
    UNKNOWN_FIX_NOT_ACCEPTED,
    UNKNOWN_NO_SOURCE_PATHS,
    UNKNOWN_FIX_COMMIT_NOT_FOUND,
    UNKNOWN_GIT_UNAVAILABLE,
    UNKNOWN_ROW_UNREADABLE,
)

GIT_TIMEOUT_SECONDS = 30
GIT_ENV_BLOCKLIST = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_PREFIX",
)

#: :func:`first_holding_commit` failure tokens. A history that could not be read
#: is not a history that answered "no such commit", so they are typed apart.
GIT_UNAVAILABLE = "git_unavailable"
HASH_MISMATCH = "hash_mismatch"

MAX_TASK_CARDS = 5000
MAX_NEEDFIX_ROWS = 5000

_HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+")
# 40 hex for a sha1 object format, 64 for sha256; the repository decides, not us.
_BLAME_HEADER_RE = re.compile(r"^([0-9a-f]{40,64}) \d+ \d+(?: \d+)?$")
_TEST_DIRECTORY_NAMES = frozenset({"test", "tests"})


# --------------------------------------------------------------------------- #
# git plumbing -- moved here from scripts/build_accepted_task_eval.py so the
# script and this module share one definition of "the commit that holds these
# promoted bytes". src/ never imports from scripts/; the dependency runs the
# other way.
# --------------------------------------------------------------------------- #

def git_env() -> dict[str, str]:
    """The inherited environment with git's repository-override variables gone.

    A caller invoked from inside another git operation inherits ``GIT_DIR`` and
    friends, which would silently retarget every command below at that
    repository instead of ``repo``.
    """
    env = os.environ.copy()
    for key in GIT_ENV_BLOCKLIST:
        env.pop(key, None)
    return env


def git_run(repo: Path, *args: str) -> tuple[int, bytes]:
    """Run one argument-list git command in ``repo``; never a shell.

    Returns ``(returncode, stdout)``; a launch failure or timeout is reported as
    a non-zero return code with empty output rather than raising, so a caller
    reads one failure shape.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return 1, b""
    return result.returncode, result.stdout


def safe_git_oid(value: str) -> bool:
    """True only for a bare hex object name -- never an option or a revision range."""
    if not value or value.startswith("-") or ".." in value:
        return False
    if any(char.isspace() for char in value):
        return False
    return 7 <= len(value) <= 64 and all(
        char in "0123456789abcdefABCDEF" for char in value
    )


def safe_repo_relative_path(relative: str) -> bool:
    """True only for a forward-slash repository-relative path with no traversal."""
    if not relative or relative.startswith("/") or "\\" in relative or ":" in relative:
        return False
    if any(char in relative for char in ("\x00", "\n", "\r")):
        return False
    parts = relative.split("/")
    return bool(parts) and all(part not in ("", ".", "..") for part in parts)


def descendant_commits(repo: Path, base_oid: str) -> list[str] | None:
    """Canonical commits after ``base_oid`` on the path to HEAD, oldest first.

    ``None`` when ``base_oid`` is not a readable commit in this repository --
    which is a different fact from "no descendant holds those bytes".
    """
    if not safe_git_oid(base_oid):
        return None
    rc, kind = git_run(repo, "cat-file", "-t", "--", base_oid)
    if rc != 0 or kind.strip() != b"commit":
        return None
    rc, out = git_run(
        repo, "rev-list", "--reverse", "--ancestry-path", f"{base_oid}..HEAD",
    )
    if rc != 0:
        return None
    return [
        line.decode("ascii", "replace").strip()
        for line in out.splitlines() if line.strip()
    ]


def commit_path_sha256(repo: Path, commit: str, path: str) -> str | None:
    """sha256 of ``path``'s blob in ``commit``'s tree, or ``None`` if it is not one.

    A path that resolves to a tree, a submodule, a symlink or more than one
    entry is refused rather than hashed: only a regular file's committed bytes
    can be compared against a receipt.
    """
    if not safe_git_oid(commit) or not safe_repo_relative_path(path):
        return None
    rc, listing = git_run(
        repo, "ls-tree", "--full-tree", "-z", commit, "--", path,
    )
    if rc != 0 or not listing:
        return None
    entries = [entry for entry in listing.split(b"\0") if entry]
    if len(entries) != 1:
        return None
    meta, sep, name = entries[0].partition(b"\t")
    if not sep or name != path.encode("utf-8"):
        return None
    parts = meta.split(b" ", 2)
    if len(parts) != 3:
        return None
    mode, kind, _object = parts
    if kind != b"blob" or mode not in (b"100644", b"100755"):
        return None
    rc, blob = git_run(repo, "cat-file", "blob", f"{commit}:{path}")
    if rc != 0:
        return None
    return hashlib.sha256(blob).hexdigest()


def first_holding_commit(
    repo: Path,
    base_oid: str,
    paths: Sequence[str],
    path_hashes: Mapping[str, str],
) -> tuple[str | None, str]:
    """First canonical descendant of ``base_oid`` holding every promoted-path hash.

    Returns ``(commit, "")`` on a match, ``(None, GIT_UNAVAILABLE)`` when the
    history could not be read at all, and ``(None, HASH_MISMATCH)`` when it was
    read and no descendant commit's tree carries exactly those bytes. Isolated
    blobs that are in the object database but in no descendant tree are refused:
    a promoted path must have actually landed.
    """
    commits = descendant_commits(repo, base_oid)
    if commits is None:
        return None, GIT_UNAVAILABLE
    for commit in commits:
        observed: dict[str, str] = {}
        missing = False
        for relative in paths:
            digest = commit_path_sha256(repo, commit, relative)
            if digest is None:
                missing = True
                break
            observed[relative] = digest
        if missing:
            continue
        if observed == path_hashes:
            return commit, ""
    return None, HASH_MISMATCH


def is_test_path(relative: str) -> bool:
    """True for a test file, decided by path shape alone -- never by its contents."""
    parts = relative.split("/")
    name = parts[-1]
    if any(part.lower() in _TEST_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    stem = name.rsplit(".", 1)[0].lower()
    return stem.startswith("test_") or stem.endswith("_test")


def deleted_preimage_ranges(
    repo: Path, base_oid: str, fix_commit: str, path: str
) -> list[tuple[int, int]] | None:
    """Inclusive ``base_oid`` line ranges the fix deleted or modified in ``path``.

    A hunk's pre-image count is zero for a pure insertion, so a fix that only
    added lines yields no ranges at all -- which is exactly the evidence that
    nothing was overwritten and nobody is to blame. ``None`` on any git failure.
    """
    if not safe_git_oid(base_oid) or not safe_git_oid(fix_commit):
        return None
    if not safe_repo_relative_path(path):
        return None
    rc, out = git_run(
        repo, "diff", "--unified=0", "--no-color", "--no-renames", "--no-ext-diff",
        f"{base_oid}..{fix_commit}", "--", path,
    )
    if rc != 0:
        return None
    ranges: list[tuple[int, int]] = []
    for line in out.splitlines():
        match = _HUNK_RE.match(line)
        if match is None:
            continue
        start = int(match.group(1))
        count = 1 if match.group(2) is None else int(match.group(2))
        if count > 0:
            ranges.append((start, start + count - 1))
    return ranges


def blamed_commits(
    repo: Path, rev: str, path: str, start: int, end: int
) -> list[str] | None:
    """The commit each of ``path``'s lines ``start..end`` came from, at ``rev``.

    One entry per line, so a caller counts lines rather than distinct commits.
    ``None`` on any git failure.
    """
    if not safe_git_oid(rev) or not safe_repo_relative_path(path):
        return None
    if start < 1 or end < start:
        return None
    rc, out = git_run(
        repo, "blame", "--line-porcelain", "-L", f"{start},{end}", rev, "--", path,
    )
    if rc != 0:
        return None
    commits: list[str] = []
    for raw in out.decode("utf-8", "replace").splitlines():
        # Content lines are tab-prefixed in porcelain output, so a line of source
        # that happens to look like a header can never be read as one.
        if raw.startswith("\t"):
            continue
        match = _BLAME_HEADER_RE.match(raw)
        if match is not None:
            commits.append(match.group(1))
    return commits


# --------------------------------------------------------------------------- #
# accepted-card evidence
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AcceptedCard:
    """One manager-accepted card's sealed identity and promoted-byte evidence."""

    task_id: str
    request_id: str
    base_oid: str
    promoted_paths: tuple[str, ...]
    changed_path_hashes: Mapping[str, str]
    identity: Mapping[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        return (self.task_id, self.request_id)


@dataclass(frozen=True)
class Attribution:
    """One NeedFix's verdict. ``caused_by`` is set only when ``attributed``."""

    schema_id: str
    needfix_id: str
    repository_id: str
    attributed: bool
    reason: str
    caused_by: dict[str, Any] | None = None
    fix_task_id: str = ""
    fix_commit: str = ""
    blamed_lines: dict[str, int] = field(default_factory=dict)
    unexplained_lines: int = 0
    written: bool = False
    detail: str = ""


@dataclass(frozen=True)
class AttributionReport:
    """Whole-repository counts. One unreadable row is counted, never raised."""

    schema_id: str
    repository_id: str
    considered: int
    attributed: int
    unknown: int
    written: int
    unknown_by_reason: dict[str, int] = field(default_factory=dict)
    attributions: tuple[Attribution, ...] = ()


class AcceptedCatalog:
    """Every manager-accepted card in one repository, read once and reused.

    ``discover_candidates`` in ``scripts/build_accepted_task_eval.py`` records
    why this is read once: letting each card re-run the whole-store queries
    turns an N-card store into N full-table scans. The holding-commit memo is
    per-instance and bounded by the card count -- never a module-level cache.
    """

    def __init__(self, repo: Path, cards: Sequence[AcceptedCard]) -> None:
        self._repo = repo
        self._cards = tuple(cards)
        self._by_task = {card.task_id: card for card in self._cards}
        self._by_key = {card.key: card for card in self._cards}
        self._holding: dict[tuple[str, str], tuple[str | None, str]] = {}

    @property
    def cards(self) -> tuple[AcceptedCard, ...]:
        return self._cards

    def card_for_task(self, task_id: str) -> AcceptedCard | None:
        return self._by_task.get(task_id)

    def card_for_key(self, key: tuple[str, str]) -> AcceptedCard | None:
        return self._by_key.get(key)

    def holding_commit(self, card: AcceptedCard) -> tuple[str | None, str]:
        """``first_holding_commit`` for ``card``, computed at most once."""
        memo = self._holding.get(card.key)
        if memo is None:
            memo = first_holding_commit(
                self._repo, card.base_oid, card.promoted_paths, card.changed_path_hashes,
            )
            self._holding[card.key] = memo
        return memo

    def explaining_card(
        self, path: str, commit: str, *, exclude: tuple[str, str]
    ) -> AcceptedCard | None:
        """The accepted card that promoted ``path`` in exactly ``commit``.

        Both halves are required: a card that promoted the path but landed in a
        different commit did not write this line, and a card whose commit
        matches but never promoted this path did not write it either.
        """
        for card in self._cards:
            if card.key == exclude or path not in card.promoted_paths:
                continue
            if self.holding_commit(card)[0] == commit:
                return card
        return None

    def verify_accepted_outcome(
        self, identity: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        """The canonical verifier ``needfix_store.validate_caused_by`` demands.

        It answers from this repository's own store-derived index, so a caller
        cannot hand in an identity that vouches for itself: every field must
        equal the one the canonical store produced.
        """
        key = (
            str(identity.get("task_id") or ""),
            str(identity.get("request_id") or ""),
        )
        card = self._by_key.get(key)
        if card is None or dict(identity) != dict(card.identity):
            return None
        return {**dict(card.identity), "outcome": "accepted"}


def load_accepted_catalog(repo_root: str | Path, repository_id: str) -> AcceptedCatalog:
    """Index every manager-accepted card whose sealed receipt is well formed.

    Manager acceptance is the canonical decision index's answer, never a status
    string read off a card. The identity is built by
    ``sdlc_outcome_metrics.accepted_outcome_identity`` -- one definition of what
    an accepted identity is -- fed the ``accept_review`` event shape the card's
    own sealed accept evidence reconstructs exactly.
    """
    # Deferred: only the store-reading half of this module needs the metrics
    # module, while the git half above is what ``scripts`` imports. Keeping the
    # dependency inside the call leaves the plumbing free of it and leaves no
    # import cycle for the later card that wires attribution into the reconciler.
    from . import sdlc_outcome_metrics

    repo = Path(repo_root)
    cards = task_store.list_task_cards(repo, limit=MAX_TASK_CARDS)
    decisions = task_store.latest_manager_decisions(repo)
    accepted: list[AcceptedCard] = []
    for card in cards:
        task_id = str(card.get("task_id") or "").strip()
        if decisions.get(task_id, {}).get("decision") != "accepted":
            continue
        request_id = str(card.get("accepted_request_id") or "").strip()
        evidence = card.get("accept_evidence")
        receipt = (
            evidence.get("accepted_outcome_receipt")
            if isinstance(evidence, Mapping) else None
        )
        identity = sdlc_outcome_metrics.accepted_outcome_identity(
            {
                "event": "accept_review",
                "task_id": task_id,
                "payload": {
                    "request_id": request_id,
                    "accepted_outcome_receipt": receipt,
                },
            },
            repository_id,
        )
        if identity is None or not isinstance(receipt, Mapping):
            continue
        accepted.append(AcceptedCard(
            task_id=task_id,
            request_id=request_id,
            base_oid=str(receipt.get("base_oid") or ""),
            promoted_paths=tuple(str(path) for path in receipt.get("promoted_paths") or ()),
            changed_path_hashes=dict(receipt.get("changed_path_hashes") or {}),
            identity=identity,
        ))
    return AcceptedCatalog(repo, accepted)


# --------------------------------------------------------------------------- #
# attribution
# --------------------------------------------------------------------------- #

def _attribution(
    needfix_id: str,
    repository_id: str,
    reason: str,
    *,
    caused_by: Mapping[str, Any] | None = None,
    fix_task_id: str = "",
    fix_commit: str = "",
    blamed_lines: Mapping[str, int] | None = None,
    unexplained_lines: int = 0,
    written: bool = False,
    detail: str = "",
) -> Attribution:
    return Attribution(
        schema_id=SCHEMA_ID,
        needfix_id=needfix_id,
        repository_id=repository_id,
        attributed=reason == ATTRIBUTED,
        reason=reason,
        caused_by=dict(caused_by) if caused_by is not None else None,
        fix_task_id=fix_task_id,
        fix_commit=fix_commit,
        blamed_lines=dict(blamed_lines or {}),
        unexplained_lines=unexplained_lines,
        written=written,
        detail=detail,
    )


def attribute_needfix(
    repo_root: str | Path,
    repository_id: str,
    needfix_id: str,
    *,
    catalog: AcceptedCatalog | None = None,
    write: bool = True,
) -> Attribution:
    """Attribute one NeedFix to the accepted card whose bytes its fix overwrote.

    Requires a NeedFix whose converted task is itself manager-accepted: the fix
    card's receipt is what names the commit to diff. Every other shape returns
    an ``unknown`` reason and writes nothing.

    Re-running is a no-op. The second run recomputes the identical identity,
    ``update_needfix`` sees the stored cause already equals it, and no write
    happens (``written`` is False while ``caused_by`` still reports the cause).
    A *different* cause on a row that already has one is refused by
    ``needfix_store`` with a typed error rather than rewritten here.
    """
    repo = Path(repo_root)
    row = needfix_store.get_needfix(repo, needfix_id)
    fix_task_id = str(row.get("converted_task_id") or "").strip()
    if not fix_task_id:
        return _attribution(needfix_id, repository_id, UNKNOWN_NOT_CONVERTED)

    index = catalog if catalog is not None else load_accepted_catalog(repo, repository_id)
    fix = index.card_for_task(fix_task_id)
    if fix is None:
        return _attribution(
            needfix_id, repository_id, UNKNOWN_FIX_NOT_ACCEPTED, fix_task_id=fix_task_id,
        )
    subject = tuple(path for path in fix.promoted_paths if not is_test_path(path))
    if not subject:
        return _attribution(
            needfix_id, repository_id, UNKNOWN_NO_SOURCE_PATHS, fix_task_id=fix_task_id,
        )
    fix_commit, failure = index.holding_commit(fix)
    if fix_commit is None:
        return _attribution(
            needfix_id,
            repository_id,
            UNKNOWN_GIT_UNAVAILABLE if failure == GIT_UNAVAILABLE
            else UNKNOWN_FIX_COMMIT_NOT_FOUND,
            fix_task_id=fix_task_id,
        )

    counts: dict[tuple[str, str], int] = {}
    preimage_lines = 0
    unexplained = 0
    for path in subject:
        ranges = deleted_preimage_ranges(repo, fix.base_oid, fix_commit, path)
        if ranges is None:
            return _attribution(
                needfix_id, repository_id, UNKNOWN_GIT_UNAVAILABLE,
                fix_task_id=fix_task_id, fix_commit=fix_commit,
            )
        for start, end in ranges:
            preimage_lines += end - start + 1
            blamed = blamed_commits(repo, fix.base_oid, path, start, end)
            if blamed is None:
                return _attribution(
                    needfix_id, repository_id, UNKNOWN_GIT_UNAVAILABLE,
                    fix_task_id=fix_task_id, fix_commit=fix_commit,
                )
            for commit in blamed:
                cause = index.explaining_card(path, commit, exclude=fix.key)
                if cause is None:
                    unexplained += 1
                else:
                    counts[cause.key] = counts.get(cause.key, 0) + 1

    measured: dict[str, Any] = {
        "fix_task_id": fix_task_id,
        "fix_commit": fix_commit,
        "blamed_lines": {f"{task}:{request}": n for (task, request), n in counts.items()},
        "unexplained_lines": unexplained,
    }
    if not preimage_lines:
        return _attribution(needfix_id, repository_id, UNKNOWN_ADDITIVE_ONLY, **measured)
    if not counts:
        return _attribution(needfix_id, repository_id, UNKNOWN_NO_RECEIPT, **measured)
    most = max(counts.values())
    winners = sorted(key for key, lines in counts.items() if lines == most)
    if len(winners) > 1:
        return _attribution(needfix_id, repository_id, UNKNOWN_TIE, **measured)
    cause_card = index.card_for_key(winners[0])
    if cause_card is None:
        return _attribution(needfix_id, repository_id, UNKNOWN_NO_RECEIPT, **measured)

    written = False
    if write:
        updated = needfix_store.update_needfix(
            repo,
            needfix_id,
            caused_by=cause_card.identity,
            repository_id=repository_id,
            verify_accepted_outcome=index.verify_accepted_outcome,
        )
        written = "caused_by" in updated["update_receipt"]["fields_changed"]
    return _attribution(
        needfix_id, repository_id, ATTRIBUTED,
        caused_by=cause_card.identity, written=written, **measured,
    )


def converted_needfix_ids(repo_root: str | Path) -> list[str]:
    """Every NeedFix id that has a converted task, oldest first, bounded."""
    repo = Path(repo_root)
    ids: list[str] = []
    offset = 0
    while len(ids) < MAX_NEEDFIX_ROWS:
        page = needfix_store.list_needfix(
            repo,
            include_archived=True,
            limit=needfix_store.MAX_LIST_LIMIT,
            offset=offset,
            order_by="created_at",
            order_dir="ASC",
        )
        if not page:
            break
        for record in page:
            if str(record.get("converted_task_id") or "").strip():
                ids.append(str(record.get("id") or ""))
        offset += len(page)
        if len(page) < needfix_store.MAX_LIST_LIMIT:
            break
    return ids[:MAX_NEEDFIX_ROWS]


def attribute_all(
    repo_root: str | Path, repository_id: str, *, write: bool = True,
) -> AttributionReport:
    """Attribute every converted NeedFix in one repository.

    One unreadable row is a counted ``row_unreadable`` verdict carrying its
    exception type, never a raise: a report that dies on row 7 tells an operator
    nothing about rows 8 onward, and the rows it did decide are still true.
    """
    repo = Path(repo_root)
    catalog = load_accepted_catalog(repo, repository_id)
    results: list[Attribution] = []
    # Sequential is the deliberate exception to "independent items run in parallel",
    # measured: a one-off backfill of 336 rows takes 74.6s, and the reconciler sweep
    # handles only fix cards accepted since its cursor, so the per-row cost never
    # dominates. update_needfix writes the same SQLite store, so rows are decided in
    # order.
    for needfix_id in converted_needfix_ids(repo):
        try:
            results.append(attribute_needfix(
                repo, repository_id, needfix_id, catalog=catalog, write=write,
            ))
        except Exception as exc:  # noqa: BLE001 -- one bad row never costs the report
            results.append(_attribution(
                needfix_id, repository_id, UNKNOWN_ROW_UNREADABLE,
                detail=type(exc).__name__,
            ))

    unknown_by_reason: dict[str, int] = {}
    for result in results:
        if not result.attributed:
            unknown_by_reason[result.reason] = unknown_by_reason.get(result.reason, 0) + 1
    attributed = sum(1 for result in results if result.attributed)
    return AttributionReport(
        schema_id=REPORT_SCHEMA_ID,
        repository_id=repository_id,
        considered=len(results),
        attributed=attributed,
        unknown=len(results) - attributed,
        written=sum(1 for result in results if result.written),
        unknown_by_reason=unknown_by_reason,
        attributions=tuple(results),
    )
