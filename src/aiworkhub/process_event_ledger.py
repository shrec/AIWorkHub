"""Bounded active-file lifecycle for the process event ledger.

The process ledger is append-only evidence.  The active file is rotated before
it can reach the retention reader's historical 64 MiB fail-closed boundary;
rotated files remain immutable and readers stream them in chronological order.
"""

from __future__ import annotations

import json
import heapq
import os
import stat
import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .platform_io import atomic_replace, chmod_fd, lock_fd, unlock_fd


ACTIVE_LEDGER_MAX_BYTES = 48 * 1024 * 1024
_LATEST_EVENT_CACHE_MAX_ENTRIES = 32
_FileSignature = tuple[str, int, int, int, int, int]


@dataclass(frozen=True)
class _LatestEventProjection:
    signatures: tuple[_FileSignature, ...]
    complete_active_offset: int
    latest: dict[str, dict[str, Any]]


# A projection's identity is the ledger plus every fold option that changes
# what it produces. The key was once the path and key field alone, so widening
# the fold with skip/replace would have served one projection's answer to a
# different projection's question.
_ProjectionKey = tuple[str, str, tuple[str, ...], bool]

_LATEST_EVENT_CACHE_LOCK = threading.RLock()
_LATEST_EVENT_CACHE: OrderedDict[
    _ProjectionKey, _LatestEventProjection
] = OrderedDict()


def _archive_pattern(path: Path) -> str:
    return f"{path.stem}.*{path.suffix}"


def _spill_marker(path: Path) -> str:
    return f"{path.stem}.spill."


def _is_spill(path: Path, candidate: Path) -> bool:
    return candidate.name.startswith(_spill_marker(path))


def ledger_paths(path: Path) -> list[Path]:
    """Return immutable rotations oldest-first, followed by the active file."""

    archives: list[Path] = []
    if path.parent.is_dir():
        for candidate in path.parent.glob(_archive_pattern(path)):
            try:
                info = candidate.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                archives.append(candidate)
    archives.sort(key=lambda item: item.name)
    try:
        active_info = path.lstat()
    except OSError:
        active_info = None
    if (
        active_info is not None
        and stat.S_ISREG(active_info.st_mode)
        and not stat.S_ISLNK(active_info.st_mode)
    ):
        archives.append(path)
    return archives


@contextmanager
def _append_lock(path: Path) -> Iterator[None]:
    lock_path = Path(f"{path}.append.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_APPEND | os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    chmod_fd(fd, 0o600)
    with os.fdopen(fd, "a+", encoding="utf-8") as handle:
        lock_fd(handle.fileno(), blocking=True)
        try:
            yield
        finally:
            unlock_fd(handle.fileno())


def _rotate(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    archive = path.with_name(
        f"{path.stem}.{stamp}.{os.getpid()}.{uuid.uuid4().hex[:8]}{path.suffix}"
    )
    # Use cross-platform atomic_replace instead of bare os.replace: on Windows,
    # a concurrent dashboard/reader holding the active ledger open can briefly
    # make os.replace fail with WinError 32 (sharing violation). The bounded
    # retry in atomic_replace tolerates the transient without weakening the
    # lock-held exclusion of other writers.
    atomic_replace(path, archive)
    os.chmod(archive, 0o600)
    return archive


def _write_immutable_spill(path: Path, payload: bytes) -> Path:
    """Persist one event without the shared append lock.

    A Windows process can retain the advisory append lock after its provider
    and supervisor have already exited.  Status, cancellation, finalization
    recovery and unrelated launches must not all become unavailable behind
    that one stale owner.  A uniquely named, atomically published immutable
    segment preserves the event without stealing or deleting the lock.

    Readers merge spill segments by the event's canonical timestamp, so an
    older row in the active file cannot overwrite a newer recovery event.
    """

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    identity = f"{os.getpid()}.{uuid.uuid4().hex}"
    spill = path.with_name(f"{path.stem}.spill.{stamp}.{identity}{path.suffix}")
    temporary = path.with_name(f".{spill.name}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    published = False
    try:
        chmod_fd(fd, 0o600)
        written = os.write(fd, payload)
        if written != len(payload):
            raise OSError("short_process_event_spill_write")
        os.fsync(fd)
        os.close(fd)
        fd = -1
        atomic_replace(temporary, spill)
        os.chmod(spill, 0o600)
        published = True
        return spill
    finally:
        if fd >= 0:
            os.close(fd)
        if not published:
            try:
                temporary.unlink()
            except OSError:
                pass


_FAILURE_TERMINAL_STATES = frozenset(
    {
        "validation_failed",
        "worker_failed",
        "launch_failed",
        "finalize_failed",
        "blocked",
        "cancelled",
        "timed_out",
        "process_lost",
        "liveness_lost",
        "scope_rejected",
        "output_budget_exceeded",
    }
)
_TERMINAL_REASON_MESSAGE_MAX_CHARS = 512
_TERMINAL_REASON_RAW_MAX_CHARS = 512
_TERMINAL_REASON_RAW_MAX_KEYS = 16
_TERMINAL_REASON_RAW_MAX_INT = 2**63
# Exact named prefixes that mean the validation environment itself cannot
# support the run (sandbox incapacity), never that the candidate is wrong.
_ENVIRONMENT_UNSUPPORTED_PREFIXES: tuple[str, ...] = (
    "validation_unsupported_in_sandbox:",
    "unsupported_sandbox_backend:",
    "validation_exec_scratch_unavailable:",
    "validation_executable_unavailable:",
    "validation_pytest_runtime_unavailable:",
    "validation_pytest_runtime_missing_pytest:",
)


def _bounded_cause(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    message = value.strip()
    if not message:
        return None
    return message[:_TERMINAL_REASON_MESSAGE_MAX_CHARS]


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _bounded_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return value[:_TERMINAL_REASON_RAW_MAX_CHARS]
    # json.dumps raises ValueError above the interpreter's int-to-str digit
    # limit (and str() would too), so a huge caller integer must be replaced,
    # never serialized or stringified, or append_event itself fails and the
    # failure event is lost.
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and not -_TERMINAL_REASON_RAW_MAX_INT <= value <= _TERMINAL_REASON_RAW_MAX_INT
    ):
        return "int_out_of_bounds"
    return value


def _bounded_raw_reason(value: Any) -> Any:
    """Preserve a caller-supplied ``terminal_reason`` bounded, never mutated.

    A mapping becomes a shallow copy of at most ``_TERMINAL_REASON_RAW_MAX_KEYS``
    entries whose keys are already ``str`` (length-bounded) and whose values are
    scalar; a non-``str`` key is dropped outright without ever invoking its
    ``__str__``/``repr`` (a hostile key must never be able to crash or stall
    ``append_event``), and nested or non-scalar values are dropped, so no
    unbounded caller payload is persisted. A non-mapping scalar is
    length-bounded; any other value collapses to None. There is no recursion or
    stringification of arbitrary objects.
    """

    if isinstance(value, dict):
        bounded: dict[str, Any] = {}
        for key in list(value)[:_TERMINAL_REASON_RAW_MAX_KEYS]:
            if not isinstance(key, str):
                continue
            item = value[key]
            if _is_scalar(item):
                bounded[key[:_TERMINAL_REASON_RAW_MAX_CHARS]] = _bounded_scalar(item)
        return bounded
    if _is_scalar(value):
        return _bounded_scalar(value)
    return None


def _canonical_terminal_reason(event: dict[str, Any], state: str) -> dict[str, Any]:
    """Build the bounded fixed-key terminal_reason for a failure-terminal event.

    ``code``/``taxonomy``/``source`` are drawn only from server-side allowlisted
    constants (never caller values) and ``message`` is hard length-bounded, so
    no arbitrary or non-scalar caller payload flows into the canonical schema.

    Cause selection is deterministic with this documented priority; the first
    bounded scalar cause wins:

    1. caller ``terminal_reason.message`` then ``terminal_reason.reason``
    2. top-level ``error``
    3. top-level ``blocked_reason`` then ``blocker_reason``
    4. ``evidence.message`` then ``evidence.summary`` then ``evidence.reason``
    5. top-level ``message``

    When no supported scalar cause exists the record is forced to the
    observability-missing-cause schema, regardless of any conflicting
    caller-supplied ``code``/``taxonomy``.
    """

    supplied = event.get("terminal_reason")
    reason = supplied if isinstance(supplied, dict) else {}
    candidates: list[tuple[str, Any]] = [
        ("terminal_reason", reason.get("message")),
        ("terminal_reason", reason.get("reason")),
        ("error", event.get("error")),
        ("blocked_reason", event.get("blocked_reason")),
        ("blocker_reason", event.get("blocker_reason")),
    ]
    evidence = event.get("evidence")
    if isinstance(evidence, dict):
        candidates.extend(
            ("evidence", evidence.get(key)) for key in ("message", "summary", "reason")
        )
    candidates.append(("message", event.get("message")))

    for source, value in candidates:
        message = _bounded_cause(value)
        if message is not None:
            alertable_value = reason.get("alertable")
            alertable = alertable_value if isinstance(alertable_value, bool) else True
            code = state
            taxonomy = "lifecycle_terminal_failure"
            # NF-548/M7: strictly named validation-environment impossibility
            # prefixes classify as environment-unsupported so sandbox
            # incapacity is machine-separable from real candidate failures.
            # Generic chmod/landlock/PermissionError prose is deliberately NOT
            # promoted: such text may come from tests that assert chmod
            # authority on purpose, so only these exact named prefixes count.
            if message.startswith(_ENVIRONMENT_UNSUPPORTED_PREFIXES):
                code = "validation_unsupported_in_sandbox"
                taxonomy = "validation_environment_unsupported"
            return {
                "code": code,
                "taxonomy": taxonomy,
                "source": source,
                "message": message,
                "missing_cause": False,
                "alertable": alertable,
            }
    return {
        "code": "terminal_reason_missing",
        "taxonomy": "observability_missing_cause",
        "source": "append_event",
        "message": "terminal failure has no supported scalar cause",
        "missing_cause": True,
        "alertable": True,
    }


def append_event(
    path: Path,
    event: dict[str, Any],
    *,
    max_active_bytes: int = ACTIVE_LEDGER_MAX_BYTES,
) -> None:
    """Append one JSON row, rotating the active file before the size bound."""

    if max_active_bytes < 1024:
        raise ValueError("process_ledger_max_bytes_too_small")
    persisted_event = event.copy()
    state_value = persisted_event.get("state")
    if isinstance(state_value, str):
        state = state_value.strip().lower()
        if state in _FAILURE_TERMINAL_STATES:
            persisted_event["state"] = state
            had_supplied_reason = "terminal_reason" in persisted_event
            supplied_reason = persisted_event.get("terminal_reason")
            persisted_event["terminal_reason"] = _canonical_terminal_reason(
                persisted_event, state
            )
            if had_supplied_reason:
                # Never discard what the caller sent: the canonical fixed-key
                # schema above is authoritative, but the raw caller value is
                # preserved bounded in a side field so conflicting or non-dict
                # inputs stay auditable without unbounded passthrough.
                persisted_event["terminal_reason_raw"] = _bounded_raw_reason(
                    supplied_reason
                )
    payload = (
        json.dumps(persisted_event, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(payload) > max_active_bytes:
        raise ValueError("process_event_exceeds_active_ledger_bound")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    try:
        with _append_lock(path):
            try:
                info = path.lstat()
            except FileNotFoundError:
                info = None
            if info is not None:
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise OSError("process_event_ledger_invalid")
                if info.st_size and info.st_size + len(payload) > max_active_bytes:
                    _rotate(path)
            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags, 0o600)
            try:
                chmod_fd(fd, 0o600)
                written = os.write(fd, payload)
                if written != len(payload):
                    raise OSError("short_process_event_write")
            finally:
                os.close(fd)
    except TimeoutError:
        _write_immutable_spill(path, payload)


def _iter_ledger_file(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def iter_events(path: Path) -> Iterator[dict[str, Any]]:
    """Stream valid object rows across rotations without whole-file reads."""

    ledgers = ledger_paths(path)
    if not any(_is_spill(path, ledger) for ledger in ledgers):
        for ledger in ledgers:
            yield from _iter_ledger_file(ledger)
        return

    # Each ordinary ledger is append-ordered and every spill contains one
    # atomically published row. Merge their heads instead of loading the
    # bounded-but-potentially-large ledger history into memory.
    streams = [iter(_iter_ledger_file(ledger)) for ledger in ledgers]
    heap: list[tuple[str, int, int, dict[str, Any]]] = []
    ordinals = [0] * len(streams)
    for index, stream in enumerate(streams):
        try:
            row = next(stream)
        except StopIteration:
            continue
        heapq.heappush(
            heap,
            (str(row.get("timestamp") or ""), index, 0, row),
        )
    while heap:
        _timestamp, index, _ordinal, row = heapq.heappop(heap)
        yield row
        try:
            following = next(streams[index])
        except StopIteration:
            continue
        ordinals[index] += 1
        heapq.heappush(
            heap,
            (
                str(following.get("timestamp") or ""),
                index,
                ordinals[index],
                following,
            ),
        )


def _ledger_signatures(path: Path) -> tuple[_FileSignature, ...] | None:
    signatures: list[_FileSignature] = []
    ledgers = ledger_paths(path)
    for ledger in ledgers:
        try:
            info = ledger.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        signatures.append(
            (
                str(ledger.resolve(strict=False)),
                int(info.st_dev),
                int(info.st_ino),
                int(info.st_size),
                int(info.st_mtime_ns),
                int(info.st_ctime_ns),
            )
        )
    return tuple(signatures)


def _active_complete_offset(path: Path, *, size: int) -> int:
    """Return the end of the final complete JSONL row in the active file."""

    if size <= 0:
        return 0
    try:
        with path.open("rb") as handle:
            position = size
            while position > 0:
                start = max(0, position - 64 * 1024)
                handle.seek(start)
                chunk = handle.read(position - start)
                newline = chunk.rfind(b"\n")
                if newline >= 0:
                    return start + newline + 1
                position = start
    except OSError:
        return 0
    return 0


def _merge_latest_row(
    latest: dict[str, dict[str, Any]],
    row: dict[str, Any],
    key_field: str,
    *,
    skip_event_kinds: tuple[str, ...] = (),
    replace: bool = False,
) -> None:
    """Fold one row into the latest-per-key map.

    ``replace`` selects the launcher's semantics, where the newest row for a
    request IS the state rather than an overlay on the previous one, and
    ``skip_event_kinds`` drops observation rows that are appended alongside the
    lifecycle rows and must never overwrite them. Both are parameters rather
    than a second copy of this projection: the launcher previously reimplemented
    the fold over an uncached full ledger pass, which is how it ended up paying
    1.87 s where the cached projection costs 21 ms.
    """

    if skip_event_kinds and str(row.get("event_kind") or "") in skip_event_kinds:
        return
    key = str(row.get(key_field) or "")
    if not key:
        return
    latest[key] = dict(row) if replace else {**latest.get(key, {}), **row}


def _parse_complete_jsonl(payload: bytes) -> Iterator[dict[str, Any]]:
    for raw_line in payload.split(b"\n")[:-1]:
        try:
            row = json.loads(raw_line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


def _cache_projection(
    cache_key: _ProjectionKey, projection: _LatestEventProjection
) -> None:
    with _LATEST_EVENT_CACHE_LOCK:
        _LATEST_EVENT_CACHE[cache_key] = projection
        _LATEST_EVENT_CACHE.move_to_end(cache_key)
        while len(_LATEST_EVENT_CACHE) > _LATEST_EVENT_CACHE_MAX_ENTRIES:
            _LATEST_EVENT_CACHE.popitem(last=False)


def _rebuild_latest_events(
    path: Path,
    *,
    key_field: str,
    cache_key: _ProjectionKey,
    skip_event_kinds: tuple[str, ...] = (),
    replace: bool = False,
) -> dict[str, dict[str, Any]]:
    before = _ledger_signatures(path)
    latest: dict[str, dict[str, Any]] = {}
    for row in iter_events(path):
        _merge_latest_row(
            latest, row, key_field, skip_event_kinds=skip_event_kinds, replace=replace
        )
    after = _ledger_signatures(path)

    # Cache only a stable read. A concurrent rotation/append still returns the
    # same bounded stream semantics as iter_events, but cannot seed stale state.
    if before is not None and before == after:
        active_offset = 0
        if after and after[-1][0] == str(path.resolve(strict=False)):
            active_offset = _active_complete_offset(path, size=after[-1][3])
            if _ledger_signatures(path) != after:
                return {key: dict(value) for key, value in latest.items()}
        _cache_projection(
            cache_key,
            _LatestEventProjection(
                signatures=after,
                complete_active_offset=active_offset,
                latest=latest,
            ),
        )
    return {key: dict(value) for key, value in latest.items()}


def latest_events(
    path: Path,
    *,
    key_field: str = "request_id",
    skip_event_kinds: tuple[str, ...] = (),
    replace: bool = False,
) -> dict[str, dict[str, Any]]:
    """Return the latest merged row per key using an append-aware projection.

    The append-only common path parses only complete bytes added since the
    preceding call. Rotations, spills, replacements, truncations, deletions or
    any immutable-segment change invalidate the projection and replay the
    canonical ``iter_events`` ordering. The cache is process-local, bounded and
    never an authority: a restart merely pays one cold replay.
    """

    resolved_path = str(path.resolve(strict=False))
    skip_event_kinds = tuple(sorted(skip_event_kinds))
    # The fold options are part of the identity: a replace-fold that drops
    # notices is a different projection from a merge-fold that keeps them, and
    # serving one for the other would be a silent wrong answer.
    cache_key = (resolved_path, key_field, skip_event_kinds, bool(replace))
    current = _ledger_signatures(path)
    with _LATEST_EVENT_CACHE_LOCK:
        cached = _LATEST_EVENT_CACHE.get(cache_key)
        if cached is not None:
            _LATEST_EVENT_CACHE.move_to_end(cache_key)

    if cached is not None and current is not None:
        if current == cached.signatures:
            return {key: dict(value) for key, value in cached.latest.items()}

        # Incremental replay is safe only for growth of the same active file,
        # with every immutable segment unchanged and no spill merge involved.
        old = cached.signatures
        active_is_last = bool(
            old
            and current
            and old[-1][0] == resolved_path
            and current[-1][0] == resolved_path
        )
        no_spills = not any(_is_spill(path, Path(item[0])) for item in current)
        immutable_unchanged = (
            len(old) == len(current) and old[:-1] == current[:-1]
        )
        same_active = active_is_last and old[-1][1:3] == current[-1][1:3]
        active_grew = same_active and current[-1][3] > old[-1][3]
        if no_spills and immutable_unchanged and active_grew:
            start = cached.complete_active_offset
            observed_size = current[-1][3]
            try:
                with path.open("rb") as handle:
                    opened = os.fstat(handle.fileno())
                    if (
                        int(opened.st_dev),
                        int(opened.st_ino),
                        int(opened.st_size),
                    ) != (current[-1][1], current[-1][2], observed_size):
                        raise OSError("process_event_ledger_changed_during_read")
                    handle.seek(start)
                    payload = handle.read(observed_size - start)
            except OSError:
                return _rebuild_latest_events(
                    path,
                    key_field=key_field,
                    cache_key=cache_key,
                    skip_event_kinds=skip_event_kinds,
                    replace=replace,
                )

            complete_length = payload.rfind(b"\n") + 1
            latest = {key: dict(value) for key, value in cached.latest.items()}
            if complete_length:
                for row in _parse_complete_jsonl(payload[:complete_length]):
                    _merge_latest_row(
                        latest,
                        row,
                        key_field,
                        skip_event_kinds=skip_event_kinds,
                        replace=replace,
                    )
            if _ledger_signatures(path) == current:
                _cache_projection(
                    cache_key,
                    _LatestEventProjection(
                        signatures=current,
                        complete_active_offset=start + complete_length,
                        latest=latest,
                    ),
                )
                return {key: dict(value) for key, value in latest.items()}

    return _rebuild_latest_events(
        path,
        key_field=key_field,
        cache_key=cache_key,
        skip_event_kinds=skip_event_kinds,
        replace=replace,
    )
