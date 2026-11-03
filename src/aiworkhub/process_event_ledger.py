"""Bounded active-file lifecycle for the process event ledger.

The process ledger is append-only evidence.  The active file is rotated before
it can reach the retention reader's historical 64 MiB fail-closed boundary;
rotated files remain immutable and readers stream them in chronological order.
"""

from __future__ import annotations

import heapq
import json
import os
import stat
import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from .platform_io import atomic_replace, chmod_fd, lock_fd, unlock_fd


ACTIVE_LEDGER_MAX_BYTES = 48 * 1024 * 1024
_LATEST_EVENT_CACHE_MAX_ENTRIES = 32
# Estimated retained bytes across every cached latest-event projection; the
# entry cap alone let 32 projections of a 48 MiB ledger stay resident.
_LATEST_EVENT_CACHE_MAX_BYTES = 64 * 1024 * 1024
_FileSignature = tuple[str, int, int, int, int, int]


@dataclass(frozen=True)
class _LatestEventProjection:
    signatures: tuple[_FileSignature, ...]
    complete_active_offset: int
    latest: dict[str, dict[str, Any]]
    estimated_bytes: int = 0


# A projection's identity is the ledger plus every fold option that changes
# what it produces. The key was once the path and key field alone, so widening
# the fold with skip/replace would have served one projection's answer to a
# different projection's question. ``drop_fields`` joined the identity for the
# same reason: a caller that never reads a large field (e.g. ``packet``) must
# never be served -- or seed the cache for -- a projection that retained it.
# A keep-fields projection appends one ``("keep", *fields)`` element; every
# other projection keeps the five-element identity unchanged.
_ProjectionKey = tuple[Any, ...]

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
    caller-supplied ``code``/``taxonomy``. A typed ``terminal_failure``
    category of ``credential`` classifies as retryable ``credential_expired``
    whether or not a scalar cause also exists; beside a scalar cause the
    validation-environment prefix and the ``sandbox_spawn_failed`` code hint
    are checked first and outrank it.
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
            # NF-2026-01136: spawn_failed means the worker never ran, so the
            # supervisor's own bounded error is control-plane evidence about
            # the sandbox, not untrusted work output -- the caller names it
            # via this closed-vocabulary code hint rather than message text.
            elif reason.get("code") == "sandbox_spawn_failed":
                code = "sandbox_spawn_failed"
                taxonomy = "sandbox_spawn_failure"
            # NF-2026-01174: a typed credential category is the cause's class
            # even when a scalar cause (e.g. the exit text) is also present.
            elif (
                isinstance(failure := event.get("terminal_failure"), dict)
                and _bounded_cause(failure.get("category")) == "credential"
            ):
                code = "credential_expired"
                taxonomy = "provider_credential"
            scalar: dict[str, Any] = {
                "code": code,
                "taxonomy": taxonomy,
                "source": source,
                "message": message,
                "missing_cause": False,
                "alertable": alertable,
            }
            if code == "credential_expired":
                scalar["retryable"] = True
            return scalar
    # NF-2026-01126: with no scalar cause, a typed terminal_failure category
    # (e.g. an expired OAuth credential) is still a known cause, not a missing one.
    failure = event.get("terminal_failure")
    category = _bounded_cause(failure.get("category")) if isinstance(failure, dict) else None
    if isinstance(failure, dict) and category is not None:
        failure_message = next(
            (
                bounded
                for key in ("reason", "detail", "message")
                if (bounded := _bounded_cause(failure.get(key))) is not None
            ),
            category,
        )
        alertable_value = reason.get("alertable")
        typed: dict[str, Any] = {
            "code": state,
            "taxonomy": "lifecycle_terminal_failure",
            "source": "terminal_failure",
            "message": failure_message,
            "missing_cause": False,
            "alertable": alertable_value if isinstance(alertable_value, bool) else True,
        }
        if category == "credential":
            typed.update(
                code="credential_expired", taxonomy="provider_credential", retryable=True
            )
        return typed
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
) -> dict[str, Any]:
    """Append one JSON row, rotating the active file before the size bound.

    Returns the row as it was actually persisted. A failure-terminal event is
    canonicalised here -- ``terminal_reason`` is built from server-side
    constants -- so the caller's own dict and the durable record are not the
    same object. Handing the persisted row back keeps one event with one
    representation: a caller that finalizes and a caller that replays the
    ledger afterwards must not disagree about what happened.
    The one exception is a causeless workspace-GC row (NF-2026-01174): it is
    persisted without ``terminal_reason`` so the merge fold of
    ``latest_events`` keeps the reason already recorded for the request.
    """

    if max_active_bytes < 1024:
        raise ValueError("process_ledger_max_bytes_too_small")
    persisted_event = event.copy()
    # The seal is set once as a Mapping on the reservation row (NF-2026-01131);
    # a non-Mapping value is never a seal and must not overlay it in the merge fold.
    if not isinstance(persisted_event.get("quality_review_attempt"), Mapping):
        persisted_event.pop("quality_review_attempt", None)
    state_value = persisted_event.get("state")
    if isinstance(state_value, str):
        state = state_value.strip().lower()
        if state in _FAILURE_TERMINAL_STATES:
            persisted_event["state"] = state
            had_supplied_reason = "terminal_reason" in persisted_event
            supplied_reason = persisted_event.get("terminal_reason")
            canonical_reason = _canonical_terminal_reason(persisted_event, state)
            # NF-2026-01174: a causeless workspace-GC row only restates the
            # state; stamping it missing_cause would overlay the recorded
            # terminal reason in the default merge fold of latest_events.
            causeless_gc_row = (
                persisted_event.get("workspace_gc") is True
                and not had_supplied_reason
                and canonical_reason["missing_cause"] is True
            )
            if not causeless_gc_row:
                persisted_event["terminal_reason"] = canonical_reason
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
    return persisted_event


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


def _json_token(value: str) -> bytes | None:
    """``value`` as the JSON-quoted bytes a ledger line carries for it, or
    ``None`` when JSON would escape it (the raw line then need not contain it)."""

    token = json.dumps(value)
    return token.encode("ascii") if token == f'"{value}"' else None


def _key_token(key: Any) -> bytes | None:
    """The bytes every raw line folded under ``key`` contains, or ``None``.

    The latest-row fold keys a row by ``str(row[key_field])``, so a key a
    number, boolean or container could stringify to has no exact raw token.
    """

    if not isinstance(key, str) or not key or key in ("True", "False") or key[0] in "[{":
        return None
    try:
        float(key)
    except ValueError:
        return _json_token(key)
    return None


def _rows_containing(lines: Iterable[bytes], tokens: list[bytes]) -> Iterator[dict[str, Any]]:
    """Parse only the raw lines that contain one of ``tokens``."""

    for raw_line in lines:
        if not any(token in raw_line for token in tokens):
            continue
        try:
            row = json.loads(raw_line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


def _stream_rows_containing(
    ledgers: list[Path], tokens: list[bytes]
) -> Iterator[dict[str, Any]]:
    for ledger in ledgers:
        try:
            with ledger.open("rb") as handle:
                yield from _rows_containing(handle, tokens)
        except OSError:
            continue


def _iter_events_containing(
    path: Path, tokens: list[bytes | None]
) -> Iterator[dict[str, Any]] | None:
    """``iter_events`` restricted to raw lines containing one of ``tokens``.

    Skipping ``json.loads`` for a line without any token is exact whenever a
    row the caller folds must carry one; a line that merely mentions a token
    inside a string is parsed and judged exactly as before. ``None`` -- no
    tokens, a value JSON would escape, or a spill whose timestamp merge
    reorders lines -- means the caller must use the full ``iter_events``.
    """

    exact = [token for token in tokens if token is not None]
    ledgers = ledger_paths(path)
    if not exact or len(exact) != len(tokens) or any(
        _is_spill(path, ledger) for ledger in ledgers
    ):
        return None
    return _stream_rows_containing(ledgers, exact)


def _iter_events_naming(
    path: Path, fields: tuple[str, ...], key_field: str
) -> Iterator[dict[str, Any]]:
    """``iter_events`` restricted to rows whose raw line names a non-key field.

    For a keep-fields merge fold a row that names none of those fields is never
    folded, so the quoted field names are exact tokens (``_iter_events_containing``).
    """

    tokens = [_json_token(name) for name in fields if name != key_field]
    rows = _iter_events_containing(path, tokens)
    return iter_events(path) if rows is None else rows


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
    drop_fields: tuple[str, ...] = (),
    keep_fields: tuple[str, ...] = (),
) -> tuple[str, dict[str, Any] | None] | None:
    """Fold one row into the latest-per-key map.

    ``replace`` selects the launcher's semantics, where the newest row for a
    request IS the state rather than an overlay on the previous one, and
    ``skip_event_kinds`` drops observation rows that are appended alongside the
    lifecycle rows and must never overwrite them. Both are parameters rather
    than a second copy of this projection: the launcher previously reimplemented
    the fold over an uncached full ledger pass, which is how it ended up paying
    1.87 s where the cached projection costs 21 ms.

    ``drop_fields`` removes named keys from the row before it is folded, so a
    field a consumer never reads (e.g. ``packet``) never enters the retained
    projection -- it is stripped before retention, not merely from a returned
    copy.

    ``keep_fields`` is the inverse: only those keys are retained, and a row
    that carries none of them besides ``key_field`` is not folded at all, so
    the projection holds only keys that ever carried a kept field. Every kept
    field still ends with exactly the value the unfiltered fold would hold.
    """

    if skip_event_kinds and str(row.get("event_kind") or "") in skip_event_kinds:
        return None
    key = str(row.get(key_field) or "")
    if not key:
        return None
    if keep_fields:
        row = {k: v for k, v in row.items() if k in keep_fields}
        if not any(k != key_field for k in row):
            return None
    if drop_fields:
        row = {k: v for k, v in row.items() if k not in drop_fields}
    previous = latest.get(key)
    latest[key] = dict(row) if replace else {**(previous or {}), **row}
    return key, previous


def _parse_complete_jsonl(payload: bytes) -> Iterator[dict[str, Any]]:
    for raw_line in payload.split(b"\n")[:-1]:
        try:
            row = json.loads(raw_line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


def _projection_weight(projection: Any) -> int:
    return int(getattr(projection, "estimated_bytes", 0) or 0)


def _cache_bounded_projection(
    cache: OrderedDict[Any, Any],
    lock: Any,
    cache_key: Any,
    projection: Any,
    max_entries: int,
    *,
    max_bytes: int | None = None,
) -> None:
    """Store ``projection`` and evict least-recently-used entries until the
    cache is within ``max_entries`` and, when given, ``max_bytes`` of retained
    ``estimated_bytes``. A single projection heavier than ``max_bytes`` is not
    retained at all (and displaces any stale entry for its key)."""

    with lock:
        if max_bytes is not None and _projection_weight(projection) > max_bytes:
            cache.pop(cache_key, None)
            return
        cache[cache_key] = projection
        cache.move_to_end(cache_key)
        while len(cache) > max_entries or (
            max_bytes is not None
            and len(cache) > 1
            and sum(_projection_weight(item) for item in cache.values()) > max_bytes
        ):
            cache.popitem(last=False)


def _cache_projection(
    cache_key: _ProjectionKey, projection: _LatestEventProjection
) -> None:
    _cache_bounded_projection(
        _LATEST_EVENT_CACHE,
        _LATEST_EVENT_CACHE_LOCK,
        cache_key,
        projection,
        _LATEST_EVENT_CACHE_MAX_ENTRIES,
        max_bytes=_LATEST_EVENT_CACHE_MAX_BYTES,
    )


def _estimate_value_bytes(value: Any) -> int:
    """Approximate in-memory footprint of a JSON-shaped value (an estimate, not
    an exact ``sys.getsizeof`` walk). Iterative, so a row nested deeper than
    the interpreter stack cannot raise ``RecursionError``."""

    total = 0
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            total += 56 + len(item)
        elif isinstance(item, dict):
            total += 64 + 24 * len(item)
            for k, v in item.items():
                stack.append(k)
                stack.append(v)
        elif isinstance(item, (list, tuple)):
            total += 56 + 8 * len(item)
            stack.extend(item)
        else:
            total += 32
    return total


def _estimate_row_bytes(key: str, row: Mapping[str, Any]) -> int:
    return _estimate_value_bytes(key) + _estimate_value_bytes(row)


def _copy_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a retained row so a caller can never mutate the cached projection,
    including through nested dicts and lists. Iterative for the same reason as
    ``_estimate_value_bytes``."""

    copied: dict[str, Any] = {}
    pending: list[tuple[Any, Any]] = [(row, copied)]
    while pending:
        source, target = pending.pop()
        items: Iterable[tuple[Any, Any]] = (
            enumerate(source) if isinstance(source, list) else source.items()
        )
        for k, v in items:
            if isinstance(v, dict):
                child: Any = {}
            elif isinstance(v, list):
                child = [None] * len(v)
            else:
                target[k] = v
                continue
            target[k] = child
            pending.append((v, child))
    return copied


def _rebuild_latest_events(
    path: Path,
    *,
    key_field: str,
    cache_key: _ProjectionKey,
    skip_event_kinds: tuple[str, ...] = (),
    replace: bool = False,
    drop_fields: tuple[str, ...] = (),
    keep_fields: tuple[str, ...] = (),
) -> dict[str, dict[str, Any]]:
    """Cold replay. Returns the projection itself, uncopied: callers must copy
    only the rows they hand out."""

    before = _ledger_signatures(path)
    latest: dict[str, dict[str, Any]] = {}
    rows: Iterable[dict[str, Any]] = iter_events(path)
    if keep_fields and not skip_event_kinds and not replace:
        rows = _iter_events_naming(path, keep_fields, key_field)
    for row in rows:
        _merge_latest_row(
            latest,
            row,
            key_field,
            skip_event_kinds=skip_event_kinds,
            replace=replace,
            drop_fields=drop_fields,
            keep_fields=keep_fields,
        )
    after = _ledger_signatures(path)

    # Cache only a stable read. A concurrent rotation/append still returns the
    # same bounded stream semantics as iter_events, but cannot seed stale state.
    if before is not None and before == after:
        active_offset = 0
        if after and after[-1][0] == str(path.resolve(strict=False)):
            active_offset = _active_complete_offset(path, size=after[-1][3])
            if _ledger_signatures(path) != after:
                return latest
        _cache_projection(
            cache_key,
            _LatestEventProjection(
                signatures=after,
                complete_active_offset=active_offset,
                latest=latest,
                estimated_bytes=sum(
                    _estimate_row_bytes(key, value) for key, value in latest.items()
                ),
            ),
        )
    return latest


def _projection_key(
    path: Path,
    *,
    key_field: str,
    skip_event_kinds: tuple[str, ...],
    replace: bool,
    drop_fields: tuple[str, ...],
    keep_fields: tuple[str, ...] = (),
) -> _ProjectionKey:
    """The normalized cache identity of one latest-per-key fold.

    The fold options are part of the identity: a replace-fold that drops
    notices is a different projection from a merge-fold that keeps them, and
    serving one for the other would be a silent wrong answer. drop_fields
    joins the same identity so two different discard sets never share a
    cached projection. keep_fields extends the key only when given, so every
    existing projection keeps its exact identity.
    """

    cache_key: _ProjectionKey = (
        str(path.resolve(strict=False)),
        key_field,
        tuple(sorted(skip_event_kinds)),
        bool(replace),
        tuple(sorted(drop_fields)),
    )
    keep = tuple(sorted(set(keep_fields)))
    return (*cache_key, ("keep", *keep)) if keep else cache_key


def _latest_view(
    path: Path,
    *,
    key_field: str,
    skip_event_kinds: tuple[str, ...],
    replace: bool,
    drop_fields: tuple[str, ...],
    keep_fields: tuple[str, ...] = (),
) -> dict[str, dict[str, Any]]:
    """The shared latest-per-key projection, never copied. Read-only: callers
    must not mutate it and must copy any row they return."""

    resolved_path = str(path.resolve(strict=False))
    skip_event_kinds = tuple(sorted(skip_event_kinds))
    drop_fields = tuple(sorted(drop_fields))
    keep_fields = tuple(sorted(set(keep_fields)))
    cache_key = _projection_key(
        path,
        key_field=key_field,
        skip_event_kinds=skip_event_kinds,
        replace=replace,
        drop_fields=drop_fields,
        keep_fields=keep_fields,
    )

    def rebuild() -> dict[str, dict[str, Any]]:
        return _rebuild_latest_events(
            path,
            key_field=key_field,
            cache_key=cache_key,
            skip_event_kinds=skip_event_kinds,
            replace=replace,
            drop_fields=drop_fields,
            keep_fields=keep_fields,
        )
    current = _ledger_signatures(path)
    with _LATEST_EVENT_CACHE_LOCK:
        cached = _LATEST_EVENT_CACHE.get(cache_key)
        if cached is not None:
            _LATEST_EVENT_CACHE.move_to_end(cache_key)

    if cached is not None and current is not None:
        if current == cached.signatures:
            return cached.latest

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
                return rebuild()

            complete_length = payload.rfind(b"\n") + 1
            # Rows are never mutated in place (the fold builds a new dict per
            # touched key), so a shallow copy of the mapping is enough to keep
            # the previously cached projection immutable.
            latest = dict(cached.latest)
            estimated_bytes = cached.estimated_bytes
            if complete_length:
                for row in _parse_complete_jsonl(payload[:complete_length]):
                    merged = _merge_latest_row(
                        latest,
                        row,
                        key_field,
                        skip_event_kinds=skip_event_kinds,
                        replace=replace,
                        drop_fields=drop_fields,
                        keep_fields=keep_fields,
                    )
                    if merged is None:
                        continue
                    key, previous = merged
                    estimated_bytes += _estimate_row_bytes(key, latest[key])
                    if previous is not None:
                        estimated_bytes -= _estimate_row_bytes(key, previous)
            if _ledger_signatures(path) == current:
                _cache_projection(
                    cache_key,
                    _LatestEventProjection(
                        signatures=current,
                        complete_active_offset=start + complete_length,
                        latest=latest,
                        estimated_bytes=estimated_bytes,
                    ),
                )
                return latest

    return rebuild()


def latest_events(
    path: Path,
    *,
    key_field: str = "request_id",
    skip_event_kinds: tuple[str, ...] = (),
    replace: bool = False,
    drop_fields: tuple[str, ...] = (),
) -> dict[str, dict[str, Any]]:
    """Return the latest merged row per key using an append-aware projection.

    The append-only common path parses only complete bytes added since the
    preceding call. Rotations, spills, replacements, truncations, deletions or
    any immutable-segment change invalidate the projection and replay the
    canonical ``iter_events`` ordering. The cache is process-local, bounded by
    entry count and by estimated retained bytes, and never an authority: a
    restart merely pays one cold replay.

    ``drop_fields`` names keys a caller never reads (e.g. ``packet``) that are
    removed from every row before it is retained in the cached projection --
    during both a cold rebuild and an incremental append replay -- not merely
    from the dict handed back to the caller. Omitting it preserves the exact
    default projection byte-for-byte.

    This returns a copy of the WHOLE projection; a caller that needs one key
    or a filtered subset should use ``latest_event`` / ``latest_events_where``.
    """

    view = _latest_view(
        path,
        key_field=key_field,
        skip_event_kinds=skip_event_kinds,
        replace=replace,
        drop_fields=drop_fields,
    )
    return {key: _copy_row(value) for key, value in view.items()}


def latest_event(
    path: Path,
    key: str,
    *,
    key_field: str = "request_id",
    skip_event_kinds: tuple[str, ...] = (),
    replace: bool = False,
    drop_fields: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """The latest merged row for ``key`` (a copy), or ``None``.

    A cached projection for these exact fold options is served, with its
    incremental replay, as ``latest_events`` would. Otherwise one exact
    single-key scan (NF-2026-02013) folds only the rows whose raw line holds
    the key's JSON-quoted value: under merge, replace, skip_event_kinds and
    drop_fields a row for another key cannot change this key's row, so the
    answer equals ``latest_events(...).get(key)`` while the full-projection
    cache is neither filled nor evicted. Spills and keys without an exact raw
    token (``_key_token``) use the full projection.
    """

    options: dict[str, Any] = {
        "key_field": key_field,
        "skip_event_kinds": skip_event_kinds,
        "replace": replace,
        "drop_fields": drop_fields,
    }
    with _LATEST_EVENT_CACHE_LOCK:
        cached = _projection_key(path, **options) in _LATEST_EVENT_CACHE
    rows = None if cached else _iter_events_containing(path, [_key_token(key)])
    if rows is None:
        row = _latest_view(path, **options).get(key)
        return None if row is None else _copy_row(row)
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get(key_field) or "") == key:
            _merge_latest_row(
                latest,
                row,
                key_field,
                skip_event_kinds=skip_event_kinds,
                replace=replace,
                drop_fields=drop_fields,
            )
    # Every retained value came from this scan's own parse: nothing is shared.
    return latest.get(key)


def latest_events_where(
    path: Path,
    predicate: Callable[[str, Mapping[str, Any]], bool],
    *,
    key_field: str = "request_id",
    skip_event_kinds: tuple[str, ...] = (),
    replace: bool = False,
    drop_fields: tuple[str, ...] = (),
) -> dict[str, dict[str, Any]]:
    """Latest merged rows for which ``predicate(key, row)`` is true, copying
    only those rows. ``predicate`` sees the retained row and must neither
    mutate nor keep a reference to it."""

    view = _latest_view(
        path,
        key_field=key_field,
        skip_event_kinds=skip_event_kinds,
        replace=replace,
        drop_fields=drop_fields,
    )
    return {
        key: _copy_row(value) for key, value in view.items() if predicate(key, value)
    }


def latest_event_cache_stats() -> dict[str, int]:
    """Entry count, estimated retained bytes and the configured bounds of the
    latest-event projection cache."""

    with _LATEST_EVENT_CACHE_LOCK:
        return {
            "entries": len(_LATEST_EVENT_CACHE),
            "estimated_bytes": sum(
                _projection_weight(item) for item in _LATEST_EVENT_CACHE.values()
            ),
            "budget_bytes": _LATEST_EVENT_CACHE_MAX_BYTES,
            "max_entries": _LATEST_EVENT_CACHE_MAX_ENTRIES,
        }


def reset_latest_event_cache() -> None:
    """Drop every retained latest-event projection (costs one cold replay)."""

    with _LATEST_EVENT_CACHE_LOCK:
        _LATEST_EVENT_CACHE.clear()


def sealed_request_field(path: Path, field: str) -> dict[str, Any]:
    """``{request_id: value}`` of a field sealed once per request and never rewritten.

    A seal such as ``quality_review_attempt`` is written on a request's
    reservation row, and later lifecycle rows (the supervisor's ``running``
    row, terminal rows) omit it, so the launcher's REPLACE projection loses it
    for the rest of the attempt's life (NF-2026-01131). The cached MERGE fold of
    the same ledger keeps the last value any row carried, which for an
    immutable seal is the reservation's own. Lifecycle state is never read
    from here: that stays the replace projection's latest row.

    The fold is its own small cached projection (NF-2026-02009): it retains
    only ``request_id`` and ``field``, so it fits the cache bound and a warm
    call replays only appended bytes, where the full merge fold (~347 MB on
    the live ledger) was never cached and cold-replayed under the launcher's
    registry lock. Only the returned seal mappings are copied.
    """

    view = _latest_view(
        path,
        key_field="request_id",
        skip_event_kinds=(),
        replace=False,
        drop_fields=(),
        keep_fields=("request_id", field),
    )
    return {
        key: _copy_row(row[field])
        for key, row in view.items()
        if isinstance(row.get(field), Mapping)
    }


# --- Bounded multi-request event projection ---------------------------------
#
# ``latest_events`` answers "what is the newest row per key".  A review drain
# asks a different question of the same bytes: "what is the FULL history of
# these particular request ids".  Answering it by filtering a fresh
# ``iter_events`` pass per request made recovery O(ledger x action): every
# reviewer launch appends, the launcher's request projection was invalidated
# wholesale by any append, and so the measured 22-action backlog replayed the
# whole ledger once per action.
#
# This projection folds ONE canonical pass into a bounded set of tracked keys,
# and a subsequent append parses only the bytes added after the last complete
# row -- it never discards the rows already folded for unrelated requests.

_REQUEST_EVENT_PROJECTION_MAX_KEYS = 64
_REQUEST_EVENT_CACHE_MAX_ENTRIES = 8


@dataclass(frozen=True)
class _RequestEventProjection:
    signatures: tuple[_FileSignature, ...]
    complete_active_offset: int
    events: dict[str, list[dict[str, Any]]]


# (resolved ledger path, key field).  The key field joins the identity for the
# same reason it does for ``latest_events``: a projection bucketed by
# ``request_id`` must never answer a question asked about ``task_id``.
_RequestProjectionKey = tuple[str, str]

_REQUEST_EVENT_CACHE_LOCK = threading.RLock()
_REQUEST_EVENT_CACHE: OrderedDict[
    _RequestProjectionKey, _RequestEventProjection
] = OrderedDict()


def reset_request_event_cache() -> None:
    """Drop every retained request projection.

    The cache is never an authority, so this costs at most one cold replay. It
    exists so a caller -- or a test asserting cold-path behaviour -- can pin
    the projection to a known empty state.
    """

    with _REQUEST_EVENT_CACHE_LOCK:
        _REQUEST_EVENT_CACHE.clear()


def _distinct_request_keys(request_ids: Iterable[Any]) -> list[str]:
    """Order-preserving unique keys, by exact string identity only.

    A non-string id is dropped rather than coerced: ``str(123)`` would let a
    row whose ``request_id`` is the integer 123 answer for the request id
    ``"123"``, which is exactly the identity confusion this projection must
    not introduce.
    """

    keys: list[str] = []
    seen: set[str] = set()
    for value in request_ids:
        if not isinstance(value, str) or value in seen:
            continue
        seen.add(value)
        keys.append(value)
    return keys


def _fold_request_rows(
    events: dict[str, list[dict[str, Any]]],
    rows: Iterable[dict[str, Any]],
    key_field: str,
) -> None:
    """Append each row to its tracked bucket, by exact key identity.

    A row whose key is untracked is skipped, never created: a key that was
    never asked for must not silently start being retained, and a key that was
    asked for always has a bucket already.
    """

    for row in rows:
        value = row.get(key_field)
        if not isinstance(value, str):
            continue
        bucket = events.get(value)
        if bucket is not None:
            bucket.append(row)


def _complete_lines(
    lines: Iterable[bytes], limit: int, consumed: list[int]
) -> Iterator[bytes]:
    """The newline-terminated lines wholly inside the first ``limit`` bytes;
    ``consumed[0]`` accumulates their length (the complete-row offset)."""

    for raw_line in lines:
        if not raw_line.endswith(b"\n") or consumed[0] + len(raw_line) > limit:
            return
        consumed[0] += len(raw_line)
        yield raw_line


def _fold_request_snapshot(
    path: Path,
    events: dict[str, list[dict[str, Any]]],
    snapshot: tuple[_FileSignature, ...],
    tokens: list[bytes],
    key_field: str,
) -> int | None:
    """Fold exactly the bytes ``snapshot`` describes into ``events``.

    Each segment is checked by device, inode and length when opened, and the
    active file is read only up to its snapshot length, so appends made while
    the scan runs never enter it and the result is the projection of
    ``snapshot`` itself. Returns the active complete-row offset, or ``None``
    when that is not exact (a spill, or a segment replaced or unreadable).
    """

    if any(_is_spill(path, Path(item[0])) for item in snapshot):
        return None
    resolved_path = str(path.resolve(strict=False))
    offset = [0]
    try:
        for index, item in enumerate(snapshot):
            active = index == len(snapshot) - 1 and item[0] == resolved_path
            with Path(item[0]).open("rb") as handle:
                opened = os.fstat(handle.fileno())
                size = int(opened.st_size)
                if (int(opened.st_dev), int(opened.st_ino)) != item[1:3] or (
                    size != item[3] and not (active and size > item[3])
                ):
                    return None
                lines: Iterable[bytes] = handle
                if active:
                    lines = _complete_lines(handle, item[3], offset)
                _fold_request_rows(events, _rows_containing(lines, tokens), key_field)
    except OSError:
        return None
    return offset[0]


def _scan_request_events(
    path: Path,
    keys: list[str],
    *,
    key_field: str,
    snapshot: tuple[_FileSignature, ...] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], int | None]:
    """One ledger pass collecting exactly ``keys``, plus the active offset.

    Only raw lines holding a key's JSON-quoted value are parsed: the fold
    buckets a row by exact string identity, so no other line can land in a
    bucket. With ``snapshot`` the pass reads exactly those bytes and returns
    their complete-row offset; otherwise -- or when that is not exact -- it is
    the canonical ``iter_events`` pass and the offset is ``None``.
    """

    tokens = [_json_token(key) for key in keys]
    exact = [token for token in tokens if token is not None]
    if snapshot is not None and len(exact) == len(tokens):
        events: dict[str, list[dict[str, Any]]] = {key: [] for key in keys}
        offset = _fold_request_snapshot(path, events, snapshot, exact, key_field)
        if offset is not None:
            return events, offset
    events = {key: [] for key in keys}
    rows = _iter_events_containing(path, tokens)
    _fold_request_rows(events, iter_events(path) if rows is None else rows, key_field)
    return events, None


def _copy_event_value(value: Any) -> Any:
    """Deep-copy one JSON ledger value so cached rows cannot be mutated."""

    return deepcopy(value)


def _request_slice(
    events: dict[str, list[dict[str, Any]]], requested: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Copy out the requested buckets so no caller can mutate the cache.

    ``dict(row)`` alone is a shallow copy: the new row still points at the
    cached nested values, so a caller mutating ``row["payload"]["x"]`` would
    edit retained state every later reader of this projection sees, and the
    cache would serve that edit back as ledger truth.  Rows are copied all the
    way down, which is what this function's isolation has always claimed.
    """

    return {
        key: [_copy_event_value(row) for row in events.get(key, ())]
        for key in requested
    }


def _cache_request_projection(
    cache_key: _RequestProjectionKey, projection: _RequestEventProjection
) -> None:
    _cache_bounded_projection(
        _REQUEST_EVENT_CACHE,
        _REQUEST_EVENT_CACHE_LOCK,
        cache_key,
        projection,
        _REQUEST_EVENT_CACHE_MAX_ENTRIES,
    )


def _retained_request_keys(
    requested: list[str], tracked: Iterable[str]
) -> list[str]:
    """Bound the tracked key set, keeping this call's own keys first.

    Eviction can only cost a later cold replay for the evicted key, never a
    wrong answer: an untracked key is rebuilt from the canonical ordering
    rather than reported empty.
    """

    keys = list(requested)
    seen = set(keys)
    for key in tracked:
        if len(keys) >= _REQUEST_EVENT_PROJECTION_MAX_KEYS:
            break
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def _rebuild_request_events(
    path: Path,
    *,
    keys: list[str],
    key_field: str,
    cache_key: _RequestProjectionKey,
    base: _RequestEventProjection | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Collect ``keys`` and cache them as one projection.

    Keys ``base`` already tracks keep their folded rows and only the others are
    scanned, against exactly the bytes ``base`` describes, so a key miss no
    longer re-reads every tracked key (NF-2026-02013). An exact snapshot scan
    is cacheable even while the active file grows, so the next call replays
    only the appended bytes.
    """

    snapshot = base.signatures if base is not None else _ledger_signatures(path)
    tracked = base.events if base is not None else {}
    missing = [key for key in keys if key not in tracked]
    found, offset = _scan_request_events(
        path, missing, key_field=key_field, snapshot=snapshot
    )
    events = {key: tracked[key] if key in tracked else found[key] for key in keys}

    # The canonical-pass fallback caches only a stable read.  A rotation or
    # append concurrent with it still yields iter_events' own bounded semantics
    # to this caller, but must never seed a projection that later incremental
    # replays would extend; nor is it merged into ``base``'s snapshot.
    if offset is None and base is None and snapshot is not None:
        if snapshot == _ledger_signatures(path):
            offset = 0
            if snapshot and snapshot[-1][0] == str(path.resolve(strict=False)):
                offset = _active_complete_offset(path, size=snapshot[-1][3])
                if _ledger_signatures(path) != snapshot:
                    offset = None
    if offset is None or snapshot is None:
        return events
    _cache_request_projection(
        cache_key,
        _RequestEventProjection(
            signatures=snapshot,
            complete_active_offset=offset,
            events=events,
        ),
    )
    return events


def _replay_appended_request_events(
    path: Path,
    *,
    cached: _RequestEventProjection,
    current: tuple[_FileSignature, ...],
    resolved_path: str,
    key_field: str,
    cache_key: _RequestProjectionKey,
) -> _RequestEventProjection | None:
    """Fold only the bytes appended since ``cached`` (and cache the resulting
    projection, which is returned), or return ``None``.

    ``None`` means the change was not pure growth of the same active file --
    a rotation, spill, truncation, replacement or any immutable-segment change
    -- and the caller must replay the canonical ordering instead.  Every one of
    those is fail-closed: an unrecognised change never reuses folded rows.
    """

    old = cached.signatures
    if any(_is_spill(path, Path(item[0])) for item in current):
        # A spill is published out of band and iter_events merges it by
        # timestamp, so appended bytes are no longer the whole delta.
        return None
    if len(old) != len(current) or old[:-1] != current[:-1]:
        return None
    if not (old and current):
        return None
    if old[-1][0] != resolved_path or current[-1][0] != resolved_path:
        return None
    if old[-1][1:3] != current[-1][1:3]:
        # Same path, different device/inode: the active file was replaced.
        return None
    observed_size = current[-1][3]
    if observed_size <= old[-1][3]:
        # Truncated or rewritten in place at the same length.
        return None

    start = cached.complete_active_offset
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
        return None

    complete_length = payload.rfind(b"\n") + 1
    events = {key: list(rows) for key, rows in cached.events.items()}
    if complete_length:
        _fold_request_rows(
            events, _parse_complete_jsonl(payload[:complete_length]), key_field
        )
    if _ledger_signatures(path) != current:
        return None
    projection = _RequestEventProjection(
        signatures=current,
        complete_active_offset=start + complete_length,
        events=events,
    )
    _cache_request_projection(cache_key, projection)
    return projection


def events_for_requests(
    path: Path,
    request_ids: Iterable[Any],
    *,
    key_field: str = "request_id",
) -> dict[str, list[dict[str, Any]]]:
    """Return several keys' full ledger-ordered histories from one pass.

    Every requested key appears in the result, mapped to an empty list when
    the ledger holds nothing for it, so a caller can never mistake "not
    tracked" for "no events".  Rows are returned in canonical ``iter_events``
    order and are copies, so the retained projection is unreachable.

    The common path is append-aware: rows appended since the previous call are
    parsed once and folded into the buckets they belong to.  Appending events
    for one request therefore does not discard the rows already projected for
    the others -- which is precisely what made a review drain replay the whole
    ledger once per action.  The cache is process-local, bounded and never an
    authority: a restart, an eviction or any non-append change merely pays one
    cold replay.
    """

    requested = _distinct_request_keys(request_ids)
    if not requested:
        return {}

    resolved_path = str(path.resolve(strict=False))
    cache_key = (resolved_path, key_field)
    if len(requested) > _REQUEST_EVENT_PROJECTION_MAX_KEYS:
        # More keys than the projection is allowed to retain.  Answer them in
        # one uncached pass rather than truncating the request or degrading
        # back to one pass per key, and leave the retained projection alone.
        return _scan_request_events(path, requested, key_field=key_field)[0]

    current = _ledger_signatures(path)
    with _REQUEST_EVENT_CACHE_LOCK:
        cached = _REQUEST_EVENT_CACHE.get(cache_key)
        if cached is not None:
            _REQUEST_EVENT_CACHE.move_to_end(cache_key)

    projection: _RequestEventProjection | None = None
    if cached is not None and current is not None:
        projection = (
            cached
            if current == cached.signatures
            else _replay_appended_request_events(
                path,
                cached=cached,
                current=current,
                resolved_path=resolved_path,
                key_field=key_field,
                cache_key=cache_key,
            )
        )
        if projection is not None and all(key in projection.events for key in requested):
            return _request_slice(projection.events, requested)

    # A key miss: the up-to-date projection keeps its folded keys and only
    # the missing ones are scanned (NF-2026-02013).
    retained = projection or cached
    keys = _retained_request_keys(
        requested, retained.events.keys() if retained is not None else ()
    )
    rebuilt = _rebuild_request_events(
        path, keys=keys, key_field=key_field, cache_key=cache_key, base=projection
    )
    return _request_slice(rebuilt, requested)


# --- Append-aware state-filtered event projection ---------------------------
#
# The reconciler's idle pass only needs the rows of a couple of rare lifecycle
# states, yet it used to replay every ledger segment on every pass.  This
# projection retains just those rows and, on pure growth of the active
# segment, parses only the appended bytes.

CLOSURE_EVENT_STATES = ("needfix_closure_pending", "needfix_closure_reconciled")
_STATE_EVENT_CACHE_MAX_ENTRIES = 8
_STATE_EVENT_CACHE_MAX_ROWS = 20_000


@dataclass(frozen=True)
class _StateEventProjection:
    signatures: tuple[_FileSignature, ...]
    complete_active_offset: int
    rows: tuple[dict[str, Any], ...]


_StateProjectionKey = tuple[str, tuple[str, ...]]

_STATE_EVENT_CACHE_LOCK = threading.RLock()
_STATE_EVENT_CACHE: OrderedDict[
    _StateProjectionKey, _StateEventProjection
] = OrderedDict()


def reset_state_event_cache() -> None:
    """Drop every retained state projection; costs at most one cold replay."""

    with _STATE_EVENT_CACHE_LOCK:
        _STATE_EVENT_CACHE.clear()


def _cache_state_projection(
    cache_key: _StateProjectionKey, projection: _StateEventProjection
) -> None:
    if len(projection.rows) > _STATE_EVENT_CACHE_MAX_ROWS:
        with _STATE_EVENT_CACHE_LOCK:
            _STATE_EVENT_CACHE.pop(cache_key, None)
        return
    _cache_bounded_projection(
        _STATE_EVENT_CACHE,
        _STATE_EVENT_CACHE_LOCK,
        cache_key,
        projection,
        _STATE_EVENT_CACHE_MAX_ENTRIES,
    )


def _row_has_state(row: dict[str, Any], states: frozenset[str]) -> bool:
    state = row.get("state")
    return isinstance(state, str) and state in states


def _copy_state_rows(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [_copy_event_value(row) for row in rows]


def _read_appended_rows(
    path: Path,
    *,
    cached: _StateEventProjection,
    current: tuple[_FileSignature, ...],
    resolved_path: str,
    states: frozenset[str],
) -> tuple[list[dict[str, Any]], int] | None:
    """Matching rows appended since ``cached`` and the new complete offset.

    ``None`` means the change was not pure growth of the same active file, so
    the caller must replay the canonical ordering instead.
    """

    old = cached.signatures
    if not (old and current):
        return None
    if any(_is_spill(path, Path(item[0])) for item in current):
        return None
    if len(old) != len(current) or old[:-1] != current[:-1]:
        return None
    if old[-1][0] != resolved_path or current[-1][0] != resolved_path:
        return None
    if old[-1][1:3] != current[-1][1:3]:
        return None
    observed_size = current[-1][3]
    if observed_size <= old[-1][3]:
        return None
    start = cached.complete_active_offset
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                int(opened.st_dev),
                int(opened.st_ino),
                int(opened.st_size),
            ) != (current[-1][1], current[-1][2], observed_size):
                return None
            handle.seek(start)
            payload = handle.read(observed_size - start)
    except OSError:
        return None
    complete_length = payload.rfind(b"\n") + 1
    rows: list[dict[str, Any]] = []
    if complete_length:
        rows = [
            row
            for row in _parse_complete_jsonl(payload[:complete_length])
            if _row_has_state(row, states)
        ]
    return rows, start + complete_length


def closure_events(
    path: Path, states: Iterable[str] = CLOSURE_EVENT_STATES
) -> list[dict[str, Any]]:
    """Return the rows whose ``state`` is in ``states`` in ``iter_events`` order.

    Equivalent to filtering a fresh ``iter_events`` replay, but append-aware:
    an unchanged ledger parses nothing and pure growth of the active segment
    parses only the appended complete rows. Rotations, spills, truncations and
    replacements replay the canonical ordering. Rows are copies; the cache is
    process-local, bounded and never an authority.
    """

    wanted = tuple(sorted(set(states)))
    wanted_set = frozenset(wanted)
    resolved_path = str(path.resolve(strict=False))
    cache_key = (resolved_path, wanted)
    current = _ledger_signatures(path)
    with _STATE_EVENT_CACHE_LOCK:
        cached = _STATE_EVENT_CACHE.get(cache_key)
        if cached is not None:
            _STATE_EVENT_CACHE.move_to_end(cache_key)

    if cached is not None and current is not None:
        if current == cached.signatures:
            return _copy_state_rows(cached.rows)
        appended = _read_appended_rows(
            path,
            cached=cached,
            current=current,
            resolved_path=resolved_path,
            states=wanted_set,
        )
        if appended is not None:
            new_rows, offset = appended
            rows = cached.rows + tuple(new_rows)
            if _ledger_signatures(path) == current:
                _cache_state_projection(
                    cache_key,
                    _StateEventProjection(
                        signatures=current,
                        complete_active_offset=offset,
                        rows=rows,
                    ),
                )
                return _copy_state_rows(rows)

    before = _ledger_signatures(path)
    rows = tuple(
        row for row in iter_events(path) if _row_has_state(row, wanted_set)
    )
    after = _ledger_signatures(path)
    if before is not None and before == after:
        active_offset = 0
        complete = True
        if after and after[-1][0] == resolved_path:
            active_offset = _active_complete_offset(path, size=after[-1][3])
            complete = active_offset == after[-1][3]
        if complete and _ledger_signatures(path) == after:
            _cache_state_projection(
                cache_key,
                _StateEventProjection(
                    signatures=after,
                    complete_active_offset=active_offset,
                    rows=rows,
                ),
            )
    return _copy_state_rows(rows)
