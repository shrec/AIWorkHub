from __future__ import annotations

import concurrent.futures
import ctypes
from ctypes import wintypes
import gc
import heapq
import itertools
import json
import math
import mmap
import os
import sys
import time
import types
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import platform_io, runtime_generation

SCHEMA_ID = "aiworkhub.doctor_diagnose.v1"
STATE_OK = "ok"
STATE_PROBLEM = "problem"
STATE_UNAVAILABLE = "unavailable"
# A probe still running at the deadline only proves it is slow, not that it is down.
STATE_SLOW = "slow"
VERDICT_DEGRADED = "degraded"
SUMMARY_MAX_CHARS = 200
REPLY_MAX_BYTES = 8 * 1024
# Opt-in heap census (NF-2026-02273): every bound is a module constant.
HEAP_CENSUS_MAX_OBJECTS = 3_000_000
HEAP_CENSUS_TOP_TYPES = 15
HEAP_CENSUS_LARGEST_OBJECTS = 10
HEAP_CENSUS_NAME_CHARS = 120
HEAP_CENSUS_CONTAINER_TYPES = (dict, list, str, bytes, bytearray)
# Untracked payload totals (NF-2026-02331): gc never tracks these; counted via referents.
HEAP_CENSUS_PAYLOAD_TYPES = (str, bytes, bytearray)
NESTED_PROBE_KEYS = ("reconciler", "learning_coverage")
DETAIL_KEYS = ("error", "last_error", "reason")
# Task DB holder scan (NF-2026-00274): every bound is a constant, never a per-call knob.
TASK_DB_ID = "task_queue"
TASK_DB_HOLDER_ROLES = ("db", "wal", "shm", "writer_lock")
TASK_DB_HOLDERS_MAX_PIDS = 4096
TASK_DB_HOLDERS_MAX_ENTRIES = 8
TASK_DB_HOLDERS_MAX_ARGS = 64
TASK_DB_HOLDERS_CMDLINE_BYTES = 64 * 1024
TASK_DB_HOLDERS_CMDLINE_CHARS = 200
TASK_DB_HOLDERS_BUDGET_SECONDS = 1.0
TASK_DB_HOLDERS_SECRET_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD")

Probe = Callable[[], dict[str, Any]]


def _bounded(text: Any) -> str:
    return str(text)[:SUMMARY_MAX_CHARS]


def _detail(payload: Mapping[str, Any]) -> str:
    for key in DETAIL_KEYS:
        value = payload.get(key)
        if value not in (None, ""):
            return _bounded(value)
    return "ok=false"


def _source_graph_generation_problem(result: Mapping[str, Any]) -> str | None:
    """Name a never-built or stale Source Graph index (NF-2026-02081).

    Only a payload carrying the canonical-generation projection
    (``readable_generation``) is judged. An index that has never produced a
    readable generation, or whose generation is older than the stale threshold,
    is a problem even when the writer process itself answers ``ok``.
    """
    if "readable_generation" not in result:
        return None
    if result.get("readable_generation") is not True:
        detail = result.get("generation_read_error") or result.get("status") or "unknown"
        return _bounded(f"source_graph_never_built: no readable generation ({detail})")
    age = result.get("index_age_seconds")
    if isinstance(age, bool) or not isinstance(age, (int, float)):
        return None
    threshold = result.get("stale_after_seconds")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or threshold <= 0:
        # An unregistered daemon reports no threshold; judge it against the
        # daemon's own default instead of skipping the check.
        from .source_graph_daemon import (
            DEFAULT_REFRESH_INTERVAL_SECONDS,
            DEFAULT_STALE_MULTIPLIER,
            MIN_STALE_AFTER_SECONDS,
        )

        threshold = max(
            MIN_STALE_AFTER_SECONDS,
            DEFAULT_REFRESH_INTERVAL_SECONDS * DEFAULT_STALE_MULTIPLIER,
        )
    if float(age) > float(threshold):
        return _bounded(
            f"source_graph_stale: generation age {float(age):.0f}s exceeds {float(threshold):.0f}s"
        )
    return None


def _problem_summary(result: Mapping[str, Any]) -> str | None:
    """Return the excerpt for a failing probe result, or None when it is healthy."""
    if result.get("ok") is False:
        detail = _detail(result)
        if detail != "ok=false":
            return detail
        generation = _source_graph_generation_problem(result)
        return generation if generation is not None else detail
    for key in NESTED_PROBE_KEYS:
        nested = result.get(key)
        if isinstance(nested, Mapping) and nested.get("ok") is False:
            return _bounded(f"{key}: {_detail(nested)}")
    return _source_graph_generation_problem(result)


def _entry(name: str, state: str, summary: str) -> dict[str, Any]:
    return {"name": name, "state": state, "summary": summary, "next_tool": name}


def _classify(name: str, result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return _entry(name, STATE_UNAVAILABLE, _bounded(f"non_dict:{type(result).__name__}"))
    problem = _problem_summary(result)
    if problem is not None:
        return _entry(name, STATE_PROBLEM, problem)
    return _entry(name, STATE_OK, "")


def _verdict(entries: list[dict[str, Any]]) -> str:
    states = {entry["state"] for entry in entries}
    if STATE_PROBLEM in states:
        return STATE_PROBLEM
    if STATE_UNAVAILABLE in states:
        return STATE_UNAVAILABLE
    if STATE_SLOW in states:
        return VERDICT_DEGRADED
    return STATE_OK


def _encoded_size(payload: Mapping[str, Any]) -> int:
    return len(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))


@platform_io.trim_heap_after
def diagnose(
    probes: Mapping[str, Probe], *, timeout_seconds: float = 10.0, heap_census: bool = False
) -> dict[str, Any]:
    """Run every probe concurrently and fold the results into one verdict.

    ``heap_census=True`` also attaches ``server_process`` with its heap census,
    fitted into REPLY_MAX_BYTES; the default reply is unchanged.
    """
    if not probes:
        raise ValueError("doctor_diagnose_requires_at_least_one_probe")
    # Probes are IO-bound; keep one core free for the MCP server itself.
    workers = min(len(probes), max(1, (os.cpu_count() or 2) - 1))
    # One timeout per wave of workers; a probe still queued at the deadline is cancelled.
    deadline = timeout_seconds * math.ceil(len(probes) / workers)
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="aiworkhub-doctor"
    )
    try:
        futures = {name: pool.submit(probe) for name, probe in probes.items()}
        concurrent.futures.wait(futures.values(), timeout=deadline)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    entries: list[dict[str, Any]] = []
    for name, future in futures.items():
        if future.cancelled():
            # Still queued behind a hung probe at the deadline: it never ran.
            entries.append(_entry(name, STATE_UNAVAILABLE, "not_started"))
            continue
        if not future.done():
            # Still running at the deadline: slow, not down (NF-2026-01967).
            slow = _entry(name, STATE_SLOW, "timeout")
            slow["timeout_seconds"] = timeout_seconds
            entries.append(slow)
            continue
        error = future.exception()
        if error is not None:
            entries.append(_entry(name, STATE_UNAVAILABLE, type(error).__name__))
            continue
        entries.append(_classify(name, future.result()))
    reply: dict[str, Any] = {
        "schema_id": SCHEMA_ID,
        "verdict": _verdict(entries),
        "problems": [entry for entry in entries if entry["state"] != STATE_OK],
        "probes": entries,
    }
    reply["task_db_holders"] = task_db_holders()
    if heap_census:
        reply["server_process"] = server_process(heap_census=True)
        census = reply["server_process"]["heap_census"]
        native = reply["server_process"]["native_memory"]
        # Optional sections give way first: the payload holders, then mallinfo2.
        payload = census.get("untracked_payload")
        if isinstance(payload, dict) and payload.get("top_holders"):
            if _encoded_size(reply) > REPLY_MAX_BYTES:
                payload["top_holders"] = []
                payload["top_holders_dropped"] = True
        if isinstance(native, dict) and isinstance(native.get("mallinfo2"), dict):
            if _encoded_size(reply) > REPLY_MAX_BYTES:
                native["mallinfo2"] = None
                native["reasons"]["mallinfo2"] = "dropped_to_fit_reply"
        # Then the census core: the largest-objects list, then the type table.
        for key in ("largest_objects", "top_types"):
            if key in census and _encoded_size(reply) > REPLY_MAX_BYTES:
                census[key] = []
                census[f"{key}_dropped"] = True
    if _encoded_size(reply) > REPLY_MAX_BYTES:
        for entry in entries:
            entry["summary"] = ""
        reply["summaries_dropped"] = True
    if _encoded_size(reply) > REPLY_MAX_BYTES:
        # Still oversized: drop the holder cmdlines (pid, role and generation stay).
        for holder in reply["task_db_holders"].get("entries", []):
            holder["cmdline"] = ""
        reply["holder_cmdlines_dropped"] = True
    return reply


def _or_none(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except Exception:
        return None


def _type_name(kind: type) -> str:
    """Bounded ``module.qualname`` of a type; a name only, never an instance repr."""
    try:
        module = type.__dict__["__module__"].__get__(kind)
        qualname = type.__dict__["__qualname__"].__get__(kind)
    except Exception:
        return "?"
    if isinstance(module, str) and module != "builtins":
        return f"{module}.{qualname}"[:HEAP_CENSUS_NAME_CHARS]
    return str(qualname)[:HEAP_CENSUS_NAME_CHARS]


def _shallow_size(obj: Any) -> int:
    try:
        return int(sys.getsizeof(obj))
    except Exception:
        return 0


def _container_len(obj: Any) -> int | None:
    # The base-class __len__ never runs a subclass override.
    for base in HEAP_CENSUS_CONTAINER_TYPES:
        if issubclass(type(obj), base):
            return int(base.__len__(obj))
    return None


def _register_owner(
    obj: Any, owners: dict[int, tuple[str, str]], namespaces: list[dict[Any, Any]]
) -> None:
    """Remember the namespace dict of a module, class or function by id, and keep the dict."""
    kind = type(obj)
    if issubclass(kind, types.ModuleType):
        namespace = vars(types.ModuleType)["__dict__"].__get__(obj)
        if type(namespace) is dict:
            name = namespace.get("__name__")
            owners[id(namespace)] = ("module", str(name)[:HEAP_CENSUS_NAME_CHARS])
            namespaces.append(namespace)
    elif issubclass(kind, type):
        proxy = type.__dict__["__dict__"].__get__(obj)
        for ref in gc.get_referents(proxy):
            if type(ref) is dict:
                owners[id(ref)] = ("class", _type_name(obj))
                namespaces.append(ref)
    elif kind is types.FunctionType:
        # Reading __dict__ would create it; the referents expose it without allocating.
        for ref in gc.get_referents(obj):
            # Every function also references the one shared builtins dict; skip it and
            # any dict already registered so the owner pass scans each namespace once.
            if (
                type(ref) is dict
                and id(ref) not in owners
                and ref is not obj.__globals__
                and ref is not obj.__kwdefaults__
                and ref is not obj.__builtins__
            ):
                owners[id(ref)] = ("function", str(obj.__qualname__)[:HEAP_CENSUS_NAME_CHARS])
                namespaces.append(ref)


def _namespace_owners(
    targets: list[Any],
    namespaces: list[dict[Any, Any]],
    owners: Mapping[int, tuple[str, str]],
) -> dict[int, tuple[str, str]]:
    """First registered namespace dict directly referencing each target, in one pass.

    Never gc.get_referrers: that walks the whole heap once per target (NF-2026-02334).
    """
    wanted = {id(target) for target in targets}
    found: dict[int, tuple[str, str]] = {}
    for namespace in namespaces:
        if len(found) >= len(wanted):
            break
        owner = owners.get(id(namespace))
        if owner is None:
            continue
        for ref in gc.get_referents(namespace):
            key = id(ref)
            if key in wanted and key not in found:
                found[key] = owner
    return found


class _TopBySize:
    """Bounded largest-N with id dedupe; keeps strong refs to at most ``limit`` objects."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._heap: list[tuple[int, int, Any]] = []
        self._ids: set[int] = set()
        self._sequence = 0

    def offer(self, candidate: Any, size: int) -> None:
        if self._limit <= 0 or id(candidate) in self._ids:
            return
        self._sequence += 1
        entry = (size, self._sequence, candidate)
        if len(self._heap) < self._limit:
            heapq.heappush(self._heap, entry)
        elif size > self._heap[0][0]:
            self._ids.discard(id(heapq.heapreplace(self._heap, entry)[2]))
        else:
            return
        self._ids.add(id(candidate))

    def ranked(self) -> list[tuple[int, Any]]:
        return [(size, obj) for size, _, obj in sorted(self._heap, key=lambda i: (-i[0], i[1]))]


def _census_entry(
    obj: Any, size_key: str, size: int, owner: tuple[str, str] | None
) -> dict[str, Any]:
    """One bounded census row: type name, length, a size and the owner name, never contents."""
    try:
        length = _container_len(obj)
    except Exception:
        length = None
    return {
        "type": _type_name(type(obj)),
        "len": length,
        size_key: size,
        "referrer": owner[0] if owner else None,
        "referrer_name": owner[1] if owner else None,
    }


def _payload_base(kind: type) -> type | None:
    for base in HEAP_CENSUS_PAYLOAD_TYPES:
        if issubclass(kind, base):
            return base
    return None


def _heap_census(max_objects: int) -> dict[str, Any]:
    """Bounded stdlib census of the live heap: type names, counts and sizes only.

    Type counts cover gc-tracked objects; untracked str/bytes/bytearray/dict
    values are reached through their referring containers for the largest list.
    ``untracked_payload`` totals those str/bytes/bytearray referents per type
    (deduped by id, at most ``max_objects`` distinct payloads) in the same walk,
    one level through untracked dict/tuple referents, and ranks the containers
    that directly reference the most payload bytes.
    Ceiling: gc.get_objects() materialises one list of every tracked object
    (8 bytes each). Owners of the ranked objects come from one pass over the
    namespace dicts registered during the walk, never gc.get_referrers (NF-2026-02334).
    """
    started = time.monotonic()
    before = list(gc.get_count())
    collected = gc.collect()
    after = list(gc.get_count())
    objects = gc.get_objects()
    truncated = len(objects) > max_objects
    by_type: dict[type, list[int]] = {}
    owners: dict[int, tuple[str, str]] = {}
    namespaces: list[dict[Any, Any]] = []
    largest = _TopBySize(HEAP_CENSUS_LARGEST_OBJECTS)
    holders = _TopBySize(HEAP_CENSUS_LARGEST_OBJECTS)
    payload_totals: dict[type, list[int]] = {base: [0, 0] for base in HEAP_CENSUS_PAYLOAD_TYPES}
    payload_seen: set[int] = set()
    descended: set[int] = set()
    payload_capped = False

    def count_payload(ref: Any) -> int:
        """Shallow size of a payload referent (0 otherwise); type totals dedupe by id."""
        nonlocal payload_capped
        base = _payload_base(type(ref))
        if base is None:
            return 0
        size = _shallow_size(ref)
        key = id(ref)
        if key not in payload_seen:
            if len(payload_seen) < max_objects:
                payload_seen.add(key)
                slot = payload_totals[base]
                slot[0] += 1
                slot[1] += size
            else:
                payload_capped = True
        return size

    def descend(ref: Any) -> None:
        """Untracked dict/tuple values are invisible to gc.get_objects(): scan each once."""
        nonlocal payload_capped
        key = id(ref)
        if key in descended:
            return
        if len(descended) >= max_objects:
            payload_capped = True
            return
        descended.add(key)
        nested = sum(count_payload(inner) for inner in gc.get_referents(ref))
        if nested:
            holders.offer(ref, nested)

    scanned = 0
    skipped = 0
    for obj in itertools.islice(objects, max_objects):
        scanned += 1
        # type(obj), never isinstance: a weakref proxy forwards __class__ to its referent.
        kind = type(obj)
        size = _shallow_size(obj)
        slot = by_type.get(kind)
        if slot is None:
            by_type[kind] = [1, size]
        else:
            slot[0] += 1
            slot[1] += size
        try:
            _register_owner(obj, owners, namespaces)
            if issubclass(kind, HEAP_CENSUS_CONTAINER_TYPES):
                largest.offer(obj, size)
            held = 0
            for ref in gc.get_referents(obj):
                ref_kind = type(ref)
                untracked = not gc.is_tracked(ref)
                if untracked and issubclass(ref_kind, HEAP_CENSUS_CONTAINER_TYPES):
                    largest.offer(ref, _shallow_size(ref))
                held += count_payload(ref)
                if untracked and (ref_kind is dict or ref_kind is tuple):
                    descend(ref)
            if held:
                holders.offer(obj, held)
        except Exception:
            # One exotic object costs its owner/largest entry, never the census.
            skipped += 1
    del objects
    top = heapq.nlargest(HEAP_CENSUS_TOP_TYPES, by_type.items(), key=lambda item: item[1])
    ranked_largest = largest.ranked()
    ranked_holders = holders.ranked()
    found = _namespace_owners(
        [obj for _, obj in ranked_largest] + [obj for _, obj in ranked_holders],
        namespaces,
        owners,
    )
    del namespaces
    largest_objects = [
        _census_entry(obj, "shallow_bytes", size, found.get(id(obj)))
        for size, obj in ranked_largest
    ]
    # A holder that is itself a module/class/function namespace is named directly.
    top_holders = [
        _census_entry(obj, "payload_bytes", held, owners.get(id(obj)) or found.get(id(obj)))
        for held, obj in ranked_holders
    ]
    return {
        "top_types": [
            {"type": _type_name(kind), "count": count, "shallow_bytes": total}
            for kind, (count, total) in top
        ],
        "largest_objects": largest_objects,
        "untracked_payload": {
            "types": {
                _type_name(base): {"count": count, "shallow_bytes": total}
                for base, (count, total) in payload_totals.items()
            },
            "total_bytes": sum(total for _, total in payload_totals.values()),
            "payloads_truncated": payload_capped,
            "top_holders": top_holders,
        },
        "gc_counts": {
            "before_collect": before,
            "collected": collected,
            "after_collect": after,
        },
        "objects_scanned": scanned,
        "objects_skipped": skipped,
        "census_truncated": truncated,
        "census_seconds": round(time.monotonic() - started, 3),
    }


NATIVE_MEMORY_KEYS = (
    "sqlite_memory_used",
    "sqlite_memory_highwater",
    "live_sqlite_connections",
    "python_allocated_blocks",
    "private_bytes",
    "peak_working_set_bytes",
    "vm_rss_bytes",
    "vm_swap_bytes",
    "rss_anon_bytes",
    "mallinfo2",
)
# Linux probes (NF-2026-02331): one bounded read of /proc/self/status per field.
PROC_STATUS_PATH = Path("/proc/self/status")
PROC_STATUS_MAX_BYTES = 64 * 1024
# glibc ``struct mallinfo2`` layout (all size_t) and the subset the doctor reports.
MALLINFO2_STRUCT_FIELDS = (
    "arena",
    "ordblks",
    "smblks",
    "hblks",
    "hblkhd",
    "usmblks",
    "fsmblks",
    "uordblks",
    "fordblks",
    "keepcost",
)
MALLINFO2_FIELDS = ("arena", "hblkhd", "uordblks", "fordblks", "keepcost")


class Mallinfo2(ctypes.Structure):
    _fields_ = tuple((name, ctypes.c_size_t) for name in MALLINFO2_STRUCT_FIELDS)


class ProcessMemoryCountersEx(ctypes.Structure):
    _fields_ = (
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    )


class _NativeUnavailable(Exception):
    """A native probe cannot run here; the message is the named reason."""


def _sqlite_library() -> Any:
    """ctypes handle on the sqlite3 library that the stdlib ``_sqlite3`` module loaded."""
    try:
        import ctypes
        import _sqlite3

        module_file = Path(str(_sqlite3.__file__))
        if platform_io.is_windows():
            return ctypes.CDLL(str(module_file.parent / "sqlite3.dll"))
        return ctypes.CDLL(str(module_file))
    except (ImportError, OSError, AttributeError, TypeError) as error:
        raise _NativeUnavailable(f"sqlite_library_unresolved:{type(error).__name__}") from error


def _sqlite_counter(symbol: str, *args: int) -> int:
    """Read-only call of a sqlite3 ``sqlite3_int64 f(...)`` memory counter."""
    import ctypes

    library = _sqlite_library()
    try:
        function = getattr(library, symbol)
    except AttributeError as error:
        raise _NativeUnavailable(f"sqlite_symbol_missing:{symbol}") from error
    function.restype = ctypes.c_int64
    function.argtypes = [ctypes.c_int] * len(args)
    return int(function(*args))


def _sqlite_memory_used() -> int:
    return _sqlite_counter("sqlite3_memory_used")


def _sqlite_memory_highwater() -> int:
    # resetFlag=0: the high-water mark is read, never reset.
    return _sqlite_counter("sqlite3_memory_highwater", 0)


def _live_sqlite_connections() -> int:
    import sqlite3

    connection_type = sqlite3.Connection
    objects = gc.get_objects()
    try:
        # type(), never isinstance: a weakref proxy forwards __class__ to its referent.
        return sum(1 for obj in objects if issubclass(type(obj), connection_type))
    finally:
        del objects


def _windows_memory_counters() -> Mapping[str, int]:
    if not platform_io.is_windows():
        raise _NativeUnavailable("windows_only")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.K32GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCountersEx),
        wintypes.DWORD,
    ]
    kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    if not kernel32.K32GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
    return {
        "private_bytes": int(counters.PrivateUsage),
        "peak_working_set_bytes": int(counters.PeakWorkingSetSize),
    }


def _private_bytes() -> int:
    return _windows_memory_counters()["private_bytes"]


def _peak_working_set_bytes() -> int:
    return _windows_memory_counters()["peak_working_set_bytes"]


def _read_proc_status() -> str:
    """Bounded read of ``/proc/self/status``; the only filesystem touch of the Linux probes."""
    with PROC_STATUS_PATH.open("rb") as handle:
        return handle.read(PROC_STATUS_MAX_BYTES).decode("ascii", "replace")


def _proc_status_bytes(field: str) -> int:
    """One ``<field>: <n> kB`` line of ``/proc/self/status`` in bytes (Linux only)."""
    if not platform_io.is_linux():
        raise _NativeUnavailable("linux_only")
    try:
        text = _read_proc_status()
    except OSError as error:
        raise _NativeUnavailable(f"proc_status_unreadable:{type(error).__name__}") from error
    prefix = f"{field}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            parts = line[len(prefix) :].split()
            if len(parts) == 2 and parts[1] == "kB" and parts[0].isdigit():
                return int(parts[0]) * 1024
            raise _NativeUnavailable(f"proc_status_unparsed:{field}")
    raise _NativeUnavailable(f"proc_status_field_missing:{field}")


def _vm_rss_bytes() -> int:
    return _proc_status_bytes("VmRSS")


def _vm_swap_bytes() -> int:
    return _proc_status_bytes("VmSwap")


def _rss_anon_bytes() -> int:
    return _proc_status_bytes("RssAnon")


def _glibc_library() -> Any:
    """ctypes handle on glibc, probed like ``platform_io._glibc_malloc_trim``.

    The glibc version string identifies glibc (musl and every non-Linux host
    lack it); each miss raises ``_NativeUnavailable`` with a named reason.
    """
    if not platform_io.is_linux():
        raise _NativeUnavailable("linux_only")
    confstr = getattr(os, "confstr", None)
    if not callable(confstr):
        raise _NativeUnavailable("glibc_only")
    try:
        version = str(confstr("CS_GNU_LIBC_VERSION"))
    except (OSError, ValueError) as error:
        raise _NativeUnavailable("glibc_only") from error
    if not version.lower().startswith("glibc"):
        raise _NativeUnavailable("glibc_only")
    import ctypes

    try:
        return ctypes.CDLL("libc.so.6")
    except OSError as error:
        raise _NativeUnavailable(f"libc_unresolved:{type(error).__name__}") from error


def _mallinfo2() -> dict[str, int]:
    """Read-only glibc ``mallinfo2()``: arena/mmap/in-use/free-retained/trimmable bytes."""
    import ctypes

    library = _glibc_library()
    try:
        function = library.mallinfo2
    except AttributeError as error:
        raise _NativeUnavailable("mallinfo2_missing") from error

    function.argtypes = ()
    function.restype = Mallinfo2
    info = function()
    return {name: int(getattr(info, name)) for name in MALLINFO2_FIELDS}


def _native_memory() -> dict[str, Any]:
    """Bounded read-only native-memory probes; each value is an int, null or ``{error}``.

    ``mallinfo2`` is the one dict-valued probe ({arena, hblkhd, uordblks,
    fordblks, keepcost}). A null value names its cause under ``reasons``; a
    probe that raises reports ``{"error": <exception type name>}`` and never
    affects the other probes. Nothing is mutated: sqlite's high-water mark is
    read with resetFlag=0.
    """
    probes: dict[str, Callable[[], Any]] = {
        "sqlite_memory_used": _sqlite_memory_used,
        "sqlite_memory_highwater": _sqlite_memory_highwater,
        "live_sqlite_connections": _live_sqlite_connections,
        "python_allocated_blocks": sys.getallocatedblocks,
        "private_bytes": _private_bytes,
        "peak_working_set_bytes": _peak_working_set_bytes,
        "vm_rss_bytes": _vm_rss_bytes,
        "vm_swap_bytes": _vm_swap_bytes,
        "rss_anon_bytes": _rss_anon_bytes,
        "mallinfo2": _mallinfo2,
    }
    section: dict[str, Any] = {}
    reasons: dict[str, str] = {}
    for key in NATIVE_MEMORY_KEYS:
        try:
            section[key] = probes[key]()
        except _NativeUnavailable as unavailable:
            section[key] = None
            reasons[key] = _bounded(unavailable)
        except Exception as error:
            section[key] = {"error": type(error).__name__}
    section["reasons"] = reasons
    return section


def server_process(*, heap_census: bool = False) -> dict[str, Any]:
    """Identify and size the process answering this call; never raises.

    ``heap_census=True`` adds a bounded live-heap census (NF-2026-02273):
    type names, counts and shallow sizes only, never object contents, and a
    read-only ``native_memory`` section (NF-2026-02289).
    """

    reply: dict[str, Any] = {
        "pid": os.getpid(),
        **platform_io.process_memory_bytes(),
        "python_allocated_blocks": sys.getallocatedblocks(),
        "runtime_generation": _or_none(runtime_generation.own_generation),
        "superseded": _or_none(runtime_generation.superseded),
    }
    if heap_census:
        try:
            reply["heap_census"] = _heap_census(HEAP_CENSUS_MAX_OBJECTS)
        except Exception as error:
            reply["heap_census"] = {"error": type(error).__name__}
        try:
            reply["native_memory"] = _native_memory()
        except Exception as error:
            reply["native_memory"] = {"error": type(error).__name__}
        # NF-2026-02407: which MCP backend served, its arena-cap outcome and the
        # trim state, so a census taken on a host is attributable.
        try:
            reply.update(platform_io.serving_heap_state())
        except Exception as error:
            reply["mcp_backend"] = None
            reply["arena_cap"] = None
            reply["heap_trim"] = None
            reply["serving_reason"] = f"state_unreadable:{type(error).__name__}"
    return reply


def _task_db_path() -> Path:
    """Canonical task DB path via the storage registry (never a hardcoded path)."""
    from . import repository_state, storage_registry

    repo = repository_state.inspect_repository()
    registry = storage_registry.load_storage_registry(
        repo.root, expected_repo_id=repo.manifest.repo_id
    )
    return storage_registry.resolve_database_path(registry, TASK_DB_ID)


def _masked_cmdline(raw_args: list[str]) -> str:
    """Join argv with every secret value replaced by ``***``.

    An argument naming a secret marker is masked. When it carries no inline
    ``=value``, the following argument is its value and is masked too.
    """
    parts: list[str] = []
    mask_next = False
    for arg in raw_args:
        marked = any(marker in arg.upper() for marker in TASK_DB_HOLDERS_SECRET_MARKERS)
        if mask_next or marked:
            parts.append("***")
            # A marker flag in value position still takes the next argument as its value.
            mask_next = marked and "=" not in arg
        else:
            parts.append(arg)
    return " ".join(parts)


def _cmdline_args(pid_dir: str) -> list[str]:
    """Raw argv of one process; /proc/<pid>/environ is never opened."""
    try:
        with open(os.path.join(pid_dir, "cmdline"), "rb") as handle:
            raw = handle.read(TASK_DB_HOLDERS_CMDLINE_BYTES + 1)
    except OSError:
        return []
    if len(raw) > TASK_DB_HOLDERS_CMDLINE_BYTES:
        # Fail closed: a cut argv could split a secret argument and leak its prefix.
        return ["***"]
    parts = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    return parts[:TASK_DB_HOLDERS_MAX_ARGS]


def _ppid_and_rss(pid_dir: str) -> tuple[int | None, int | None]:
    ppid: int | None = None
    rss: int | None = None
    try:
        with open(os.path.join(pid_dir, "stat"), "rb") as handle:
            stat = handle.read().decode("utf-8", "replace")
        # Fields after the parenthesised comm are: state, ppid, ...
        ppid = int(stat[stat.rindex(")") + 2 :].split()[1])
    except (OSError, ValueError, IndexError):
        ppid = None
    try:
        with open(os.path.join(pid_dir, "statm"), "rb") as handle:
            resident_pages = int(handle.read().split()[1])
        rss = resident_pages * mmap.PAGESIZE
    except (OSError, ValueError, IndexError):
        rss = None
    return ppid, rss


def _generation_of(path: str) -> str | None:
    return _or_none(lambda: runtime_generation.own_generation(path))


def _superseded_of(path: str) -> bool | None:
    return _or_none(lambda: runtime_generation.superseded(path))


def _holder_generation(raw_args: list[str]) -> tuple[str | None, bool | None]:
    """Generation of the first install path in argv; (None, None) when not derivable."""
    for arg in raw_args:
        generation = _generation_of(arg)
        if generation is not None:
            return generation, _superseded_of(arg)
    return None, None


def _holder_entry(
    proc_root: str, name: str, targets: Mapping[str, str], own_uid: int, deadline: float
) -> dict[str, Any] | None:
    pid_dir = os.path.join(proc_root, name)
    try:
        if os.stat(pid_dir).st_uid != own_uid:
            return None
        fd_dir = os.path.join(pid_dir, "fd")
        fds = os.listdir(fd_dir)
    except OSError:
        # PermissionError (not ours, or hidden) and FileNotFoundError (exited) skip this pid only.
        return None
    holds: set[str] = set()
    for fd in fds:
        if time.monotonic() > deadline:
            # Budget spent mid-pid: keep the roles already seen; the caller marks truncated.
            break
        try:
            role = targets.get(os.readlink(os.path.join(fd_dir, fd)))
        except OSError:
            continue
        if role is not None:
            holds.add(role)
    if not holds:
        return None
    raw_args = _cmdline_args(pid_dir)
    ppid, rss = _ppid_and_rss(pid_dir)
    generation, superseded = _holder_generation(raw_args)
    return {
        "pid": int(name),
        "ppid": ppid,
        "cmdline": _masked_cmdline(raw_args)[:TASK_DB_HOLDERS_CMDLINE_CHARS],
        "holds": [role for role in TASK_DB_HOLDER_ROLES if role in holds],
        "runtime_generation": generation,
        "superseded": superseded,
        "rss_bytes": rss,
    }


def _scan_task_db_holders(proc_root: str, db_path: Path | None) -> dict[str, Any]:
    from . import db_writer

    db = os.path.realpath(db_path if db_path is not None else _task_db_path())
    targets = {
        db: "db",
        db + "-wal": "wal",
        db + "-shm": "shm",
        str(db_writer.lock_path_for(db)): "writer_lock",
    }
    try:
        pids = sorted(
            (name for name in os.listdir(proc_root) if name.isdigit()), key=int
        )
    except OSError as exc:
        return {
            "supported": True,
            "error": type(exc).__name__,
            "entries": [],
            "scanned_pids": 0,
            "truncated": False,
        }
    own_uid = os.getuid()  # type: ignore[attr-defined,unused-ignore]
    deadline = time.monotonic() + TASK_DB_HOLDERS_BUDGET_SECONDS
    entries: list[dict[str, Any]] = []
    scanned = 0
    truncated = False
    for name in pids:
        if (
            scanned >= TASK_DB_HOLDERS_MAX_PIDS
            or len(entries) >= TASK_DB_HOLDERS_MAX_ENTRIES
            or time.monotonic() > deadline
        ):
            truncated = True
            break
        scanned += 1
        entry = _holder_entry(proc_root, name, targets, own_uid, deadline)
        if entry is not None:
            entries.append(entry)
    if time.monotonic() > deadline:
        # The last pid's fd loop may have stopped on the budget; that scan is incomplete.
        truncated = True
    # truncated means the scan stopped early (entry, pid or time budget); scanned_pids counts pids read.
    return {
        "supported": True,
        "entries": entries,
        "scanned_pids": scanned,
        "truncated": truncated,
    }


def task_db_holders(
    proc_root: str | os.PathLike[str] = "/proc",
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Processes holding the canonical task DB, with their runtime generation.

    Linux only; stdlib /proc reads of this user's processes. Never raises: an
    unsupported platform or an unresolvable DB is reported as data.
    """
    if not platform_io.is_linux():
        return {"supported": False, "reason": "linux_only"}
    try:
        return _scan_task_db_holders(os.fspath(proc_root), db_path)
    except Exception as exc:
        return {
            "supported": True,
            "error": type(exc).__name__,
            "entries": [],
            "scanned_pids": 0,
            "truncated": False,
        }
