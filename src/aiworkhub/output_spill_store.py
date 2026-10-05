"""RM-2026-00044: repository-scoped output spill store and model-free pruner.

Foundation for output-boundary context economy: a tool result that is too
large to hand a model in full is, today, thrown away the moment it is
truncated -- only the bounded preview survives, and the exact original is
gone. This module splits those two concerns:

  * ``spill_text`` / ``retrieve_text`` -- an atomic, content-addressed,
    repository-scoped store. Oversized UTF-8 text is persisted durably
    BEFORE any pruning happens, keyed by its own sha256 digest, and handed
    back only as an opaque locator (never a filesystem path). A storage or
    durability failure raises :class:`OutputSpillError` rather than letting
    a caller fall through to a preview-only result that quietly lost the
    full text. ``try_spill_text`` is the one deliberate exception
    (NF-2026-01162): a seat with no writable spill root anywhere gets a
    typed ``spill_unavailable`` and no locator, so a bounded answer still
    reaches the caller instead of the whole request failing. Collision and
    tamper failures stay fail-closed there too.
  * ``prune_text`` -- a pure, deterministic, model-free transform. Text at
    or under the byte budget is returned byte-for-byte unchanged. Text over
    budget becomes a bounded head, one explicit measured-pruning marker
    (naming exactly how many bytes were omitted and, when available, the
    spill locator that still holds them), and a bounded tail.

``spill_and_prune`` composes the two for the common case (spill first, then
prune with the locator embedded), and ``build_telemetry`` reports the byte
accounting a caller needs -- explicitly marking provider-token savings as
UNKNOWN, since a byte or character count is not a token count.

Lifetime: a spilled file lives ``logs_days`` (repository retention policy)
from its last write or verified repeat spill; ``prune_expired`` -- run by the
terminal-log retention janitor -- removes older payloads and stale temp
files. A locator whose payload has expired fails closed on retrieval with
``output_spill_store_missing``.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .platform_io import is_windows

_SPILL_SUBDIR = Path(".aiworkhub") / "spill"
_LOCATOR_PREFIX = "aiworkhub-spill-sha256:"
_MARKER_SCHEMA_ID = "aiworkhub.output_spill_store.measured_pruning_marker.v1"
_TELEMETRY_SCHEMA_ID = "aiworkhub.output_spill_store.telemetry.v1"
_SPILL_FILE_RE = re.compile(r"^[0-9a-f]{64}\.txt$")
_SPILL_TMP_RE = re.compile(r"^\.[0-9a-f]{64}\..*\.tmp$")
_PRUNE_ERROR_CAP = 100
# The one pure-storage persist failure, named once so ``try_spill_text`` can
# tell it apart from the collision/tamper failures that must stay fail-closed.
_PERSIST_FAILED_PREFIX = "output_spill_store_persist_failed"
# Typed reason a caller reports when nothing could be spilled anywhere.
SPILL_UNAVAILABLE = "spill_unavailable"
_UNAVAILABLE_DETAIL_CAP = 200
# A worker-writable, repository-shaped root for seats whose own checkout is
# read-only; consulted by both the spill fallback and retrieval.
_WORKER_SPILL_ROOT_ENV = "AIWORKHUB_WORKER_SPILL_ROOT"


class OutputSpillError(RuntimeError):
    """The one named, fail-closed error for this module.

    Raised for any storage/durability failure (persist or retrieve), any
    tampered or malformed locator, and any pruning budget too small to hold
    even the marker. Callers must let this propagate rather than falling
    back to a preview built from a result that failed to persist.
    """


@dataclass(frozen=True, slots=True)
class SpillReceipt:
    """Opaque proof that ``original_bytes`` of exact text were persisted."""

    locator: str
    original_bytes: int
    content_sha256: str
    retrieval_hint: str


@dataclass(frozen=True, slots=True)
class SpillUnavailable:
    """Typed, non-fatal outcome: this seat has no writable spill root.

    Deliberately carries NO locator -- there is nothing to retrieve -- so a
    caller handed one must present truncated inline content and say why,
    never imply that a full payload is still recoverable.
    """

    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class PruneResult:
    """Output of the pure model-free pruner."""

    text: str
    pruned: bool
    original_bytes: int
    presented_bytes: int
    pruned_bytes: int


@dataclass(frozen=True, slots=True)
class SpillAndPruneResult:
    """Combined result of spilling (when needed) and pruning."""

    text: str
    pruned: bool
    receipt: SpillReceipt | None
    telemetry: dict[str, Any]


def _spill_root(repo: Path | str) -> Path:
    return Path(repo) / _SPILL_SUBDIR


def _fsync_dir(path: Path) -> None:
    """Fsync a directory so a prior ``os.replace`` into it survives a crash.

    POSIX only: opening a directory with ``os.O_RDONLY`` for ``os.fsync`` is
    undefined/unsupported on Windows (NTFS has no directory-entry fsync of
    this shape). On Windows this is a deliberate, explicit no-op rather than
    a failing call -- the file itself is already fsynced before the publish
    rename in :func:`spill_text`, so the payload bytes are durable; only the
    directory-entry crash window is platform-dependent here.
    """

    if is_windows():
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def spill_text(text: str, *, repo: Path | str) -> SpillReceipt:
    """Atomically persist ``text`` under repository-owned storage.

    Storage is content-addressed by the sha256 of the UTF-8 encoding, so a
    repeat spill of identical bytes is a cheap no-op rather than a second
    write. The write itself lands in a temp file in the same directory and
    is published with one ``os.replace``, then the containing directory is
    fsynced so the rename itself survives a crash -- a reader can only ever
    observe the file fully absent or fully written, never partial. An
    existing content-addressed target is re-verified by digest (not size)
    before being treated as an idempotent match, so a same-size tampered or
    corrupted file on disk is refused rather than silently reused.
    """

    encoded = text.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    root = _spill_root(repo)
    target = root / f"{digest}.txt"
    tmp_path: Path | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        existing: bytes | None = None
        try:
            existing = target.read_bytes()
        except FileNotFoundError:
            existing = None  # absent, or pruned by the janitor since the last spill
        if existing is not None:
            if hashlib.sha256(existing).hexdigest() != digest:
                raise OutputSpillError(f"output_spill_store_collision:{digest}")
            # A verified repeat spill restarts the payload's logs_days lifetime
            # so prune_expired keeps text that is still being produced. The
            # bytes are already durable, so a refused refresh is not a failure.
            try:
                os.utime(target)
            except OSError:
                pass
        else:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(root), prefix=f".{digest}.", suffix=".tmp"
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target)
            tmp_path = None
            _fsync_dir(root)
    except OutputSpillError:
        raise
    except Exception as exc:  # noqa: BLE001 - one named fail-closed error
        raise OutputSpillError(f"{_PERSIST_FAILED_PREFIX}:{digest}") from exc
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    return SpillReceipt(
        locator=f"{_LOCATOR_PREFIX}{digest}",
        original_bytes=len(encoded),
        content_sha256=digest,
        retrieval_hint=(
            "retrieve_text(locator, repo=<same repository root>) returns the "
            "exact original bytes; the locator carries no filesystem path."
        ),
    )


def _worker_spill_repo() -> Path | None:
    """The worker-writable root this seat declared, if any.

    NF-2026-01162: a worker whose repository checkout is read-only can still
    be handed a scratch root it owns, via ``AIWORKHUB_WORKER_SPILL_ROOT``. It
    is repository-shaped -- the same ``.aiworkhub/spill`` subdirectory is
    used underneath -- so a payload spilled there is found by
    :func:`retrieve_text` without the locator ever carrying a filesystem
    path. Unset (the normal case) means there is no second root at all and
    every lookup behaves exactly as it did before.
    """

    raw = os.environ.get(_WORKER_SPILL_ROOT_ENV, "").strip()
    return Path(raw) if raw else None


def _same_root(left: Path | str, right: Path | str) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return str(left) == str(right)


def _retrieval_roots(repo: Path | str) -> list[Path]:
    """Spill directories to read, in order: the repository's, then the seat's."""

    roots = [_spill_root(repo)]
    worker_repo = _worker_spill_repo()
    if worker_repo is not None and not _same_root(worker_repo, repo):
        roots.append(_spill_root(worker_repo))
    return roots


def try_spill_text(text: str, *, repo: Path | str) -> SpillReceipt | SpillUnavailable:
    """Spill ``text``, or report a typed, NON-fatal ``spill_unavailable``.

    NF-2026-01162: a worker seat whose spill root cannot be written must not
    lose the whole query. :func:`spill_text` stays fail-closed for the
    failures that mean the store itself is untrustworthy -- a digest
    collision, a tampered payload -- and those still propagate here. A pure
    storage failure is a different fact: nothing is corrupt, this seat simply
    cannot persist at that root. The worker-writable root is tried next when
    the seat declared one, and if that is unavailable too the caller receives
    a typed :class:`SpillUnavailable` so it can return truncated inline
    content with NO locator instead of failing a request whose answer was
    otherwise fine.
    """

    try:
        return spill_text(text, repo=repo)
    except OutputSpillError as exc:
        detail = str(exc)
        if not detail.startswith(_PERSIST_FAILED_PREFIX):
            raise
    worker_repo = _worker_spill_repo()
    if worker_repo is not None and not _same_root(worker_repo, repo):
        try:
            return spill_text(text, repo=worker_repo)
        except OutputSpillError as exc:
            detail = str(exc)
            if not detail.startswith(_PERSIST_FAILED_PREFIX):
                raise
    return SpillUnavailable(
        reason=SPILL_UNAVAILABLE, detail=detail[:_UNAVAILABLE_DETAIL_CAP]
    )


def prune_expired(
    repo: Path | str, *, max_age_days: int, now: float | None = None
) -> dict[str, Any]:
    """Remove spill payloads and stale temp files older than ``max_age_days``.

    Only ``<digest>.txt`` payloads and ``.<digest>.*.tmp`` leftovers directly
    under the repository's spill root are considered; anything else is left
    alone. Age is the file mtime, which :func:`spill_text` refreshes on every
    verified repeat spill, so a payload still being produced stays young.
    Each removal is isolated: a failure is recorded as ``"<path>: <exc>"``
    (at most ``_PRUNE_ERROR_CAP`` entries) and the sweep continues.
    """

    root = _spill_root(repo)
    reference = time.time() if now is None else float(now)
    cutoff = reference - max(0, int(max_age_days)) * 86400
    scanned = removed = bytes_freed = 0
    errors: list[str] = []

    def _record(path: Path, exc: OSError) -> None:
        if len(errors) < _PRUNE_ERROR_CAP:
            errors.append(f"{path}: {exc}")

    try:
        entries = list(root.iterdir())
    except FileNotFoundError:
        entries = []
    except OSError as exc:
        entries = []
        _record(root, exc)
    for path in entries:
        if not (_SPILL_FILE_RE.fullmatch(path.name) or _SPILL_TMP_RE.fullmatch(path.name)):
            continue
        scanned += 1
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
                continue
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            _record(path, exc)
            continue
        removed += 1
        bytes_freed += int(info.st_size)
    return {
        "scanned": scanned,
        "removed": removed,
        "bytes_freed": bytes_freed,
        "errors": errors,
    }


def retrieve_text(locator: str, *, repo: Path | str) -> str:
    """Digest-verify and return the exact original text for ``locator``.

    Fails closed (:class:`OutputSpillError`) on a malformed locator, a
    missing spill file, or a digest mismatch -- tampering is refused, never
    silently served. The repository's own spill root is read first; the
    worker-writable root (NF-2026-01162) is consulted only when this seat
    declared one, so an ordinary repository resolves exactly as before.
    """

    if not isinstance(locator, str) or not locator.startswith(_LOCATOR_PREFIX):
        raise OutputSpillError("output_spill_store_locator_malformed")
    digest = locator[len(_LOCATOR_PREFIX):]
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise OutputSpillError("output_spill_store_locator_malformed")
    encoded: bytes | None = None
    missing: OSError | None = None
    for root in _retrieval_roots(repo):
        try:
            encoded = (root / f"{digest}.txt").read_bytes()
            break
        except (FileNotFoundError, NotADirectoryError) as exc:  # POSIX: a file holds the root
            missing = exc
        except OSError as exc:
            raise OutputSpillError(f"output_spill_store_retrieve_failed:{digest}") from exc
    if encoded is None:
        raise OutputSpillError(f"output_spill_store_missing:{digest}") from missing
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise OutputSpillError(f"output_spill_store_digest_mismatch:{digest}")
    return encoded.decode("utf-8")


def _safe_utf8_head(encoded: bytes, limit: int) -> str:
    if limit <= 0:
        return ""
    chunk = encoded[:limit]
    while chunk:
        try:
            return chunk.decode("utf-8")
        except UnicodeDecodeError:
            chunk = chunk[:-1]
    return ""


def _safe_utf8_tail(encoded: bytes, limit: int) -> str:
    if limit <= 0:
        return ""
    chunk = encoded[-limit:]
    while chunk:
        try:
            return chunk.decode("utf-8")
        except UnicodeDecodeError:
            chunk = chunk[1:]
    return ""


def _build_marker(*, omitted_bytes: int, locator: str | None) -> str:
    return (
        f"\n--- {_MARKER_SCHEMA_ID} "
        f"omitted_bytes={omitted_bytes} locator={locator or 'unspilled'} ---\n"
    )


def prune_text(
    text: str, *, max_bytes: int, locator: str | None = None
) -> PruneResult:
    """Pure, deterministic, model-free head/marker/tail pruner.

    Text at or under ``max_bytes`` is returned byte-for-byte unchanged
    (``pruned=False``). Qualified over-budget text is transformed into a
    bounded head, one explicit measured-pruning marker, and a bounded tail,
    with the final result never exceeding ``max_bytes``.
    """

    if max_bytes <= 0:
        raise OutputSpillError("output_spill_store_prune_budget_non_positive")
    encoded = text.encode("utf-8")
    original_bytes = len(encoded)
    if original_bytes <= max_bytes:
        return PruneResult(
            text=text,
            pruned=False,
            original_bytes=original_bytes,
            presented_bytes=original_bytes,
            pruned_bytes=0,
        )

    marker_estimate = len(
        _build_marker(omitted_bytes=original_bytes, locator=locator).encode("utf-8")
    )
    budget_for_text = max_bytes - marker_estimate
    if budget_for_text < 2:
        raise OutputSpillError("output_spill_store_prune_budget_too_small")
    head_budget = budget_for_text // 2
    tail_budget = budget_for_text - head_budget
    head = _safe_utf8_head(encoded, head_budget)
    tail = _safe_utf8_tail(encoded, tail_budget)

    def _assemble(head_text: str, tail_text: str) -> tuple[str, int, int]:
        kept = len(head_text.encode("utf-8")) + len(tail_text.encode("utf-8"))
        omitted = original_bytes - kept
        marker = _build_marker(omitted_bytes=omitted, locator=locator)
        presented = f"{head_text}{marker}{tail_text}"
        return presented, len(presented.encode("utf-8")), omitted

    presented, presented_bytes, omitted_bytes = _assemble(head, tail)
    while presented_bytes > max_bytes:
        overage = presented_bytes - max_bytes
        tail_encoded = tail.encode("utf-8")
        if not tail_encoded:
            head_encoded = head.encode("utf-8")
            new_head_limit = max(0, len(head_encoded) - overage)
            if new_head_limit >= len(head_encoded):
                raise OutputSpillError("output_spill_store_prune_budget_too_small")
            head = _safe_utf8_head(head_encoded, new_head_limit)
        else:
            new_tail_limit = max(0, len(tail_encoded) - overage)
            tail = _safe_utf8_tail(tail_encoded, new_tail_limit)
        presented, presented_bytes, omitted_bytes = _assemble(head, tail)

    return PruneResult(
        text=presented,
        pruned=True,
        original_bytes=original_bytes,
        presented_bytes=presented_bytes,
        pruned_bytes=omitted_bytes,
    )


def build_telemetry(
    *,
    original_bytes: int,
    presented_bytes: int,
    spilled_bytes: int,
    pruned_bytes: int,
) -> dict[str, Any]:
    """Byte-accounting telemetry with token savings explicitly unmeasured."""

    return {
        "schema_id": _TELEMETRY_SCHEMA_ID,
        "original_bytes": original_bytes,
        "presented_bytes": presented_bytes,
        "spilled_bytes": spilled_bytes,
        "pruned_bytes": pruned_bytes,
        "provider_token_savings": "UNKNOWN",
        "provider_token_savings_basis": (
            "unmeasured: a byte or character reduction is not a provider "
            "token count and must never be reported as a savings figure"
        ),
    }


def spill_and_prune(
    text: str, *, repo: Path | str, max_bytes: int
) -> SpillAndPruneResult:
    """Spill the full text (only if oversized), then prune for presentation.

    Below-threshold text is returned unchanged with nothing spilled. Over
    threshold, the full text is persisted FIRST -- a storage failure raises
    :class:`OutputSpillError` before any preview is built, so a durability
    failure can never be mistaken for a successful bounded reply. A
    non-positive ``max_bytes`` is rejected before that persist happens: the
    budget is validated up front so an invalid call never leaves a spill
    artifact behind for a request that was always going to fail.
    """

    if max_bytes <= 0:
        raise OutputSpillError("output_spill_store_prune_budget_non_positive")

    encoded = text.encode("utf-8")
    original_bytes = len(encoded)
    if original_bytes <= max_bytes:
        telemetry = build_telemetry(
            original_bytes=original_bytes,
            presented_bytes=original_bytes,
            spilled_bytes=0,
            pruned_bytes=0,
        )
        return SpillAndPruneResult(
            text=text, pruned=False, receipt=None, telemetry=telemetry
        )

    receipt = spill_text(text, repo=repo)
    pruned = prune_text(text, max_bytes=max_bytes, locator=receipt.locator)
    telemetry = build_telemetry(
        original_bytes=original_bytes,
        presented_bytes=pruned.presented_bytes,
        spilled_bytes=receipt.original_bytes,
        pruned_bytes=pruned.pruned_bytes,
    )
    return SpillAndPruneResult(
        text=pruned.text, pruned=True, receipt=receipt, telemetry=telemetry
    )
