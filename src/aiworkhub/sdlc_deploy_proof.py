"""Deploy and Maintain stage proofs from the release receipt ledger (RM-2026-00076 E).

``scripts/release_receipt.py`` appends two kinds of line to
``.aiworkhub/releases.jsonl``: a ``built`` line naming the version, the
``release_commit`` it was built from, the VSIX digest, ``built_at``, the
``previous_vsix`` it replaces and its ``target``; and an ``installed``
confirmation carrying ``installed_at`` and the ``server_version`` the installed
server reported. This module is the one parser of that ledger -- the script
delegates here -- and the proof both late SDLC stages rest on.

deploy    the target is in the repository policy's ``deploy.targets`` and a
          confirmed release (a built line plus a confirmation whose
          ``server_version`` equals its version) was built from a commit that
          holds every promoted path of the task's accepted receipt with exactly
          the accepted bytes. A path whose bytes differ there still counts when
          they are exactly the bytes a LATER accepted receipt promoted for the
          same path. The earliest such release, in ledger order, is the
          deployed one.
maintain  the release the recorded Deploy evidence names -- never re-proven
          here -- with no control band in tier ``needfix`` over the current
          band window, evaluated once per pass, and no open NeedFix naming
          this task as its ``caused_by``.

Everything here is read-only: git runs without a shell, with a scrubbed git
environment and a timeout, and every store is read through its public reader.
A refusal is a typed code, never a guess.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEDGER_REL = (".aiworkhub", "releases.jsonl")
GIT_TIMEOUT_SECONDS = 15
# The stage gate refuses promoted files above this size; a release blob larger
# than any file acceptance could have hashed cannot match one.
MAX_RELEASE_BLOB_BYTES = 16 * 1024 * 1024
MAX_LEDGER_TEXT_CHARS = 128
# Every variable that redirects git to other objects, another repository or
# injected configuration; the ``*_PREFIXES`` entries match numbered families.
GIT_ENV_BLOCKLIST = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_REPLACE_REF_BASE",
)
GIT_ENV_BLOCKLIST_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
# Refusals a later pass may clear without any ledger or card change.
TRANSIENT_REFUSALS = frozenset(
    {"task_store_not_ready", "task_store_unreadable", "release_blob_unverifiable"}
)
ACCEPTED_CANONICAL_STATUS = "finished"
DEPLOYED_RELEASE_KEYS = ("version", "release_commit", "vsix_sha256", "installed_at", "target")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_UNVERIFIABLE = object()
_OVERSIZED = object()
_NOT_REMEMBERED = object()
# Content-addressed git facts, remembered across passes: a present commit, and
# per (commit, path, size bound) its digest or oversize. A commit's bytes never
# change, so a fact never goes stale; the cap is a memory safety backstop, not
# an operating limit -- an evicted fact only costs one more git read.
MAX_GIT_FACTS = 4096
# Safety backstops on the later-receipt read, never operating limits: one
# oversized sibling receipt is skipped, and only the newest receipts are parsed.
MAX_LATER_RECEIPT_CHARS = 1_000_000
MAX_LATER_RECEIPTS = 500
_GIT_FACTS: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
_GIT_FACTS_LOCK = threading.Lock()
_PASS_BAND_REPORTS: ContextVar[dict[tuple[str, str], Any] | None] = ContextVar(
    "sdlc_pass_band_reports", default=None
)


class ReleaseLedgerError(ValueError):
    """The release ledger exists but is not a well-formed receipt ledger."""


# --------------------------------------------------------------------------- #
# the ledger
# --------------------------------------------------------------------------- #


def parse_ledger(path: Path | str) -> list[dict[str, Any]]:
    """Every entry of one ledger file in order; a missing file is empty."""

    ledger_path = Path(path)
    if not ledger_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with ledger_path.open("r", encoding="utf-8", errors="strict") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except (ValueError, RecursionError):
                    raise ReleaseLedgerError(
                        f"ledger {ledger_path} line {line_number} is not valid JSON"
                    ) from None
                if not isinstance(entry, dict):
                    raise ReleaseLedgerError(
                        f"ledger {ledger_path} line {line_number} is not a JSON object"
                    )
                version = entry.get("version")
                if version is not None and not isinstance(version, str):
                    raise ReleaseLedgerError(f"ledger line {line_number} has non-string version")
                if entry.get("kind") in ("built", "installed") and version is None:
                    raise ReleaseLedgerError(f"ledger line {line_number} has missing version")
                entries.append(entry)
    except (UnicodeDecodeError, OSError) as exc:
        raise ReleaseLedgerError(f"ledger unreadable: {type(exc).__name__}") from exc
    return entries


def load_release_ledger(root: Path | str) -> list[dict[str, Any]]:
    """The repository's own release ledger, parsed."""

    return parse_ledger(Path(root).joinpath(*LEDGER_REL))


def version_sort_key(version: str) -> tuple[int, ...]:
    core = version.split("+", 1)[0].split("-", 1)[0]
    key: list[int] = []
    for part in core.split("."):
        try:
            key.append(int(part))
        except ValueError:
            key.append(0)
    return tuple(key)


def confirmed_releases(
    entries: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """``(built, confirmation)`` per confirmed version, in first-built ledger order.

    A confirmation counts only when the installed server reported exactly the
    built version; the last built line of a version is the one that stands.
    """

    built: dict[str, dict[str, Any]] = {}
    confirmations: dict[str, dict[str, Any]] = {}
    for entry in entries:
        version = entry.get("version")
        if entry.get("kind") == "built":
            built[version] = entry
        elif entry.get("kind") == "installed" and entry.get("server_version") == version:
            confirmations.setdefault(version, entry)
    return [
        (line, confirmations[version])
        for version, line in built.items()
        if version in confirmations
    ]


def newest_confirmed(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The built line of the highest confirmed version, or None."""

    releases = confirmed_releases(entries)
    if not releases:
        return None
    return max((line for line, _ in releases), key=lambda line: version_sort_key(line["version"]))


def latest_confirmed(root: Path | str) -> dict[str, Any] | None:
    """The built line of the repository's newest confirmed release, or None."""

    return newest_confirmed(load_release_ledger(root))


# --------------------------------------------------------------------------- #
# policy, git and the canonical stores, read-only
# --------------------------------------------------------------------------- #


def deploy_policy(root: Path | str) -> dict[str, Any] | str:
    """The repository's validated deploy section, or ``deploy_policy_invalid``."""

    from . import repo_policy

    try:
        return dict(repo_policy.load_policy(root)["deploy"])
    except repo_policy.RepoPolicyError:
        return "deploy_policy_invalid"


def instant(value: Any) -> datetime | None:
    """An ISO-8601 timestamp as an aware instant; ``Z`` and ``+00:00`` are the same."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def scrubbed_git_env() -> dict[str, str]:
    """The process environment minus every git redirection and injected config."""

    return {
        key: value for key, value in os.environ.items()
        if key not in GIT_ENV_BLOCKLIST and not key.startswith(GIT_ENV_BLOCKLIST_PREFIXES)
    }


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    """One bounded git read: no shell, and no inherited repository redirection."""

    try:
        return subprocess.run(
            ["git", "--no-replace-objects", "-C", str(root), *args],
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=scrubbed_git_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _clear_git_facts() -> None:
    """Forget every remembered git fact (tests; never needed for correctness)."""

    with _GIT_FACTS_LOCK:
        _GIT_FACTS.clear()


def _remembered_git_fact(key: tuple[Any, ...]) -> Any:
    with _GIT_FACTS_LOCK:
        if key not in _GIT_FACTS:
            return _NOT_REMEMBERED
        _GIT_FACTS.move_to_end(key)
        return _GIT_FACTS[key]


def _commit_present(root: Path, commit: str) -> bool | None:
    """Whether ``commit`` is in the repository; None when git itself failed.

    Only presence is remembered: an absent commit may be fetched later.
    """

    key = ("commit", str(Path(root).resolve()), commit)
    if _remembered_git_fact(key) is True:
        return True
    result = _git(root, "cat-file", "-e", f"{commit}^{{commit}}")
    if result is None:
        return None
    if result.returncode == 0:
        from .process_event_ledger import _cache_bounded_projection

        _cache_bounded_projection(_GIT_FACTS, _GIT_FACTS_LOCK, key, True, MAX_GIT_FACTS)
    return result.returncode == 0


def _blob_sha256(root: Path, commit: str, path: str) -> Any:
    """SHA-256 of ``path`` at ``commit``; None when absent there, else unverifiable.

    A blob above the size bound is ``_OVERSIZED``: it can match no accepted
    hash, so it refuses rather than asking for a retry. A digest or an
    oversize verdict is remembered keyed by the size bound it was judged
    under, so a changed bound always re-reads git; absence and an
    unverifiable read are never remembered.
    """

    key = ("blob", str(Path(root).resolve()), commit, path, MAX_RELEASE_BLOB_BYTES)
    remembered = _remembered_git_fact(key)
    if remembered is not _NOT_REMEMBERED:
        return remembered
    spec = f"{commit}:{path}"
    size = _git(root, "cat-file", "-s", spec)
    if size is None:
        return _UNVERIFIABLE
    if size.returncode != 0:
        return None
    try:
        byte_count = int(size.stdout.strip())
    except ValueError:
        return _UNVERIFIABLE
    fact: Any
    if byte_count > MAX_RELEASE_BLOB_BYTES:
        fact = _OVERSIZED
    else:
        blob = _git(root, "cat-file", "blob", spec)
        if blob is None or blob.returncode != 0 or len(blob.stdout) != byte_count:
            return _UNVERIFIABLE
        fact = hashlib.sha256(blob.stdout).hexdigest()
    from .process_event_ledger import _cache_bounded_projection

    _cache_bounded_projection(_GIT_FACTS, _GIT_FACTS_LOCK, key, fact, MAX_GIT_FACTS)
    return fact


def _later_promoted_hashes(
    root: Path, repo_id: str, task_id: str, accepted_at: str
) -> dict[str, set[str]] | str:
    """Per path, the hashes other ACCEPTED tasks promoted strictly after ``accepted_at``."""

    from . import task_store
    from .sqlite_readonly import connect_readonly

    since = instant(accepted_at)
    if since is None:
        return {}
    try:
        readiness = task_store.storage_readiness(root)
    except (task_store.TaskStoreError, OSError, sqlite3.Error, ValueError):
        return "task_store_not_ready"
    if not readiness.ready:
        return "task_store_not_ready"
    if readiness.repo_id != repo_id:
        return "cross_repository_evidence"
    try:
        conn = connect_readonly(readiness.canonical_db)
    except (sqlite3.Error, OSError, ValueError):
        return "task_store_unreadable"
    try:
        rows = conn.execute(
            "SELECT status, worker_status, archived_at, "
            "json_extract(card_json, '$.accepted_at'), "
            "json_extract(card_json, '$.accept_evidence.accepted_outcome_receipt') FROM tasks "
            "WHERE task_id != ? AND json_valid(card_json) "
            "AND json_extract(card_json, '$.accepted_at') IS NOT NULL "
            "AND length(json_extract(card_json, "
            "'$.accept_evidence.accepted_outcome_receipt')) <= ? "
            "ORDER BY json_extract(card_json, '$.accepted_at') DESC LIMIT ?",
            (task_id, MAX_LATER_RECEIPT_CHARS, MAX_LATER_RECEIPTS),
        ).fetchall()
    except sqlite3.Error:
        return "task_store_unreadable"
    finally:
        conn.close()
    later: dict[str, set[str]] = {}
    for status, worker_status, archived_at, raw_at, raw_receipt in rows:
        canonical = task_store.canonical_status(
            {"status": status, "worker_status": worker_status, "archived_at": archived_at}
        )
        when = instant(raw_at)
        if canonical != ACCEPTED_CANONICAL_STATUS or when is None or when <= since:
            continue
        try:
            receipt = json.loads(raw_receipt) if isinstance(raw_receipt, str) else None
        except (ValueError, RecursionError):
            continue
        paths = receipt.get("promoted_paths") if isinstance(receipt, dict) else None
        hashes = receipt.get("changed_path_hashes") if isinstance(receipt, dict) else None
        if not (isinstance(paths, list) and isinstance(hashes, dict)):
            continue
        for path in paths:
            digest = hashes.get(path) if isinstance(path, str) else None
            # Only a real digest supersedes; no sentinel or other text can.
            if isinstance(digest, str) and _SHA256_HEX.fullmatch(digest):
                later.setdefault(path, set()).add(digest)
    return later


def _open_caused_by_count(root: Path, task_id: str) -> int | str:
    """Open NeedFix rows whose ``caused_by`` names ``task_id``, paged to the end."""

    from . import needfix_store

    if not root.joinpath(*needfix_store.NEEDFIX_DB_REL).is_file():
        return 0
    count, offset, page = 0, 0, needfix_store.MAX_LIST_LIMIT
    while True:
        try:
            rows = needfix_store.list_needfix(root, limit=page, offset=offset)
        except (needfix_store.NeedFixError, sqlite3.Error, OSError, ValueError):
            return "needfix_store_unreadable"
        for row in rows:
            cause = row.get("caused_by")
            if (
                row.get("status") not in needfix_store.NEEDFIX_TERMINAL_STATUSES
                and isinstance(cause, dict)
                and cause.get("task_id") == task_id
            ):
                count += 1
        if len(rows) < page:
            return count
        offset += page


# --------------------------------------------------------------------------- #
# the proofs
# --------------------------------------------------------------------------- #


def _text(value: Any) -> str:
    if isinstance(value, str) and len(value) <= MAX_LEDGER_TEXT_CHARS and value.isprintable():
        return value
    return ""


def _previous_vsix(value: Any) -> dict[str, str] | None:
    """The rollback artifact a release names, when it names one well-formed."""

    if not isinstance(value, dict):
        return None
    path, digest = _text(value.get("path")), value.get("sha256")
    if not path or not (isinstance(digest, str) and _SHA256_HEX.fullmatch(digest)):
        return None
    return {"path": path, "sha256": digest}


def _release_evidence(
    built: Mapping[str, Any], confirmation: Mapping[str, Any], target: str, approval_policy: str
) -> dict[str, Any]:
    # ``approval_policy`` is the configured policy, not an observed approval.
    return {
        "version": _text(built.get("version")),
        "release_commit": built["release_commit"],
        "vsix_sha256": built["vsix_sha256"],
        "built_at": _text(built.get("built_at")),
        "installed_at": _text(confirmation.get("installed_at")),
        "previous_vsix": _previous_vsix(built.get("previous_vsix")),
        "target": target,
        "approval_policy": approval_policy,
    }


def _receipt_hashes(receipt: Mapping[str, Any]) -> dict[str, str | None] | None:
    """Promoted path -> accepted SHA-256 (None: removed); None unless every value is one."""

    paths = receipt.get("promoted_paths")
    hashes = receipt.get("changed_path_hashes")
    if (
        not isinstance(paths, list)
        or not isinstance(hashes, dict)
        or not all(isinstance(path, str) and path in hashes for path in paths)
    ):
        return None
    for path in paths:
        digest = hashes[path]
        if digest is not None and not (isinstance(digest, str) and _SHA256_HEX.fullmatch(digest)):
            return None
    return {path: hashes[path] for path in paths}


def deploy_proof(
    root: Path | str,
    repo_id: str,
    task_id: str,
    accepted_receipt: Mapping[str, Any],
    target: str,
    *,
    accepted_at: str = "",
) -> dict[str, Any] | str:
    """The confirmed release that shipped exactly the accepted bytes, or why none did.

    A path whose bytes at the release commit differ from this receipt's hash
    still counts only when they equal a hash a LATER accepted receipt promoted
    for that same path. A git failure that left some release unverified
    refuses with the transient ``release_blob_unverifiable``.
    """

    root = Path(root)
    policy = deploy_policy(root)
    if isinstance(policy, str):
        return policy
    if not target or target not in policy["targets"]:
        return "deploy_target_unknown"
    try:
        releases = [
            pair for pair in confirmed_releases(load_release_ledger(root))
            if pair[0].get("target") == target
        ]
    except ReleaseLedgerError:
        return "release_ledger_invalid"
    if not releases:
        return "release_not_confirmed"
    expected = _receipt_hashes(accepted_receipt)
    if expected is None:
        return "acceptance_receipt_malformed"
    later: dict[str, set[str]] | None = None
    oversized = False
    for built, confirmation in releases:
        commit, vsix = built.get("release_commit"), built.get("vsix_sha256")
        if not (isinstance(commit, str) and _COMMIT.fullmatch(commit)):
            continue
        if not (isinstance(vsix, str) and _SHA256_HEX.fullmatch(vsix)):
            continue
        present = _commit_present(root, commit)
        if present is None:
            # Skipping it could record a later release as the deployed one.
            return "release_blob_unverifiable"
        if not present:
            continue
        superseded: list[str] = []
        for path, digest in expected.items():
            actual = _blob_sha256(root, commit, path)
            if actual is _UNVERIFIABLE:
                return "release_blob_unverifiable"
            if actual is _OVERSIZED:
                oversized = True
                break
            if actual == digest:
                continue
            if later is None:
                found = _later_promoted_hashes(root, repo_id, task_id, accepted_at)
                if isinstance(found, str):
                    return found
                later = found
            if actual not in later.get(path, ()):
                break
            superseded.append(path)
        else:
            return {
                **_release_evidence(built, confirmation, target, policy["approver"]),
                "promoted_path_count": len(expected),
                "superseded_paths": sorted(superseded),
            }
    return "release_blob_oversized" if oversized else "release_commit_missing_promoted_hash"


def _deployed_release(deployed: Any) -> dict[str, str] | None:
    """The release identity the recorded Deploy evidence names, when well-formed."""

    if not isinstance(deployed, Mapping):
        return None
    release = {key: deployed.get(key) for key in DEPLOYED_RELEASE_KEYS}
    if not all(isinstance(value, str) for value in release.values()):
        return None
    if not (_COMMIT.fullmatch(release["release_commit"])
            and _SHA256_HEX.fullmatch(release["vsix_sha256"]) and release["target"]):
        return None
    return release


@contextmanager
def band_report_per_pass() -> Iterator[None]:
    """One pass: one band evaluation per repository."""

    token = _PASS_BAND_REPORTS.set({})
    try:
        yield
    finally:
        _PASS_BAND_REPORTS.reset(token)


def band_report(root: Path | str, repo_id: str) -> Any:
    """The control-band report, evaluated once per ``band_report_per_pass`` scope."""

    from . import sdlc_control_bands, task_store

    memo = _PASS_BAND_REPORTS.get()
    key = (str(root), repo_id)
    if memo is not None and key in memo:
        return memo[key]
    try:
        report: Any = sdlc_control_bands.evaluate(Path(root), repo_id)
    except (task_store.StorageNotReadyError, sdlc_control_bands.ConfigError,
            sqlite3.Error, OSError, ValueError):
        report = "band_report_unavailable"
    if memo is not None:
        memo[key] = report
    return report


def maintain_proof(
    root: Path | str,
    repo_id: str,
    task_id: str,
    deployed: Any,
    *,
    report: Any = None,
) -> dict[str, Any] | str:
    """Inside every control band, and no open defect caused by this task.

    ``deployed`` is the Deploy stage's recorded ready evidence: Maintain watches
    that release and never re-proves it, so it runs no git and scans no task
    store. ``report`` is a band report already evaluated for this pass.
    """

    release = _deployed_release(deployed)
    if release is None:
        return "deploy_not_ready"
    root = Path(root)
    if report is None:
        report = band_report(root, repo_id)
    if isinstance(report, str):
        return report
    tiers = {band.metric_id: band.tier for band in report.metrics}
    if any(tier == "needfix" for tier in tiers.values()):
        return "band_breached"
    open_count = _open_caused_by_count(root, task_id)
    if isinstance(open_count, str):
        return open_count
    if open_count:
        return "open_caused_by_needfix"
    return {
        "deployed_release": release,
        "band_report": {"config_sha256": report.config_sha256, "tiers": tiers},
        "open_caused_by_count": 0,
    }
