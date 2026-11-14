"""Measure what an in-process AIWorkHub server keeps resident (NF-2026-02134).

Field report NF-2026-00274: aiworkhub.server processes at 4.3-4.5 GB RSS, a
VmHWM of 8.4 GB, a 964 MB task DB with ~34k task_events.  This script imports
``aiworkhub.server`` in-process against ``--repo`` and drives the same server
entry points a dashboard or session server serves, in a fixed order:

    startup, task_list, task_health, doctor_diagnose, review_queue,
    completion_inbox, janitor_sweep

once (round ``initial``) and then ``--repeats`` more times (``repeat_1`` ..
``repeat_N``) so growth across identical work is visible.

After every step it records RSS, the process peak (Windows PeakWorkingSet /
Linux VmHWM), private bytes, tracemalloc current and per-step peak, the gc
object count, and on glibc Linux the RSS after ``malloc_trim(0)``.  For the
heaviest operation step and for the repeat-growth delta it records the top
tracemalloc allocators grouped by ``filename:lineno``.

``--synthetic-cards N`` (or ``--synthetic``) first builds a synthetic task DB
in a repository that has no tasks yet (default 2000 cards x 160 KB card_json
plus 17 events of 16 KB per card, about 1 GB).  A repository that already has
tasks is refused, never mixed with synthetic rows.

Two opt-in flags bound a run without changing what is measured in process:
``--exclude-provider-clis`` drops the directories of installed provider CLIs
from PATH for the operation steps (completion_inbox's route preflight would
otherwise run them as child processes), and ``--no-trace-startup`` imports
the server untraced (its RSS is still recorded) and starts tracemalloc right
after the startup step.  Both are recorded in the report's ``config``.

The report is written to ``<repo>/.scratch/memory_profile/<timestamp>.json``
and its path is printed as one JSON line.  Measurement only: no product fix.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import json
import os
import sqlite3
import sys
import time
import tracemalloc
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_ID = "aiworkhub.server_memory_profile.v1"
REPORT_DIR_REL = Path(".scratch") / "memory_profile"

STEP_NAMES: tuple[str, ...] = (
    "startup",
    "task_list",
    "task_health",
    "doctor_diagnose",
    "review_queue",
    "completion_inbox",
    "janitor_sweep",
)
# Startup is the import itself; it is reported separately from the operation
# steps so module-import cost never hides the heaviest request path.
OPERATION_STEPS: tuple[str, ...] = STEP_NAMES[1:]
STEP_METRIC_KEYS: tuple[str, ...] = (
    "rss_bytes",
    "rss_before_gc_bytes",
    "peak_rss_bytes",
    "private_bytes",
    "rss_after_trim_bytes",
    "tracemalloc_current_bytes",
    "tracemalloc_peak_bytes",
    "tracemalloc_step_peak_bytes",
    "gc_objects",
    "elapsed_seconds",
)
TASK_LIST_STATUSES: tuple[str, ...] = ("pending", "processing", "review", "blocked")
TRACEMALLOC_BASIS = (
    "startup traces the package import; traces are then cleared once, so every later "
    "step's tracemalloc figures, its top allocators and the repeat growth count only "
    "blocks allocated after startup"
)

DEFAULT_SYNTHETIC_CARDS = 2000
DEFAULT_CARD_KB = 160
DEFAULT_EVENTS_PER_CARD = 17
DEFAULT_EVENT_KB = 16
DEFAULT_REPEATS = 3
DEFAULT_TOP = 25
MAX_SYNTHETIC_CARDS = 1_000_000
MAX_PAYLOAD_KB = 64 * 1024
MAX_REPEATS = 100
INSERT_BATCH = 200

# (status, worker_status) cycle for synthetic cards.
SYNTHETIC_STATES: tuple[tuple[str, str], ...] = (
    ("pending", "unclaimed"),
    ("processing", "claimed"),
    ("review", "done"),
    ("blocked", "blocked"),
    ("finished", "done"),
    ("archived", "done"),
)
SYNTHETIC_EVENTS: tuple[str, ...] = (
    "created",
    "claimed",
    "launch",
    "validation",
    "review_requested",
    "terminal_review",
)


def _bootstrap_import_path() -> None:
    """Prefer this checkout's ``src`` only when it holds the complete package.

    A sparse worker checkout carries a partial ``src/aiworkhub``; putting it
    first would shadow the installed package with a half tree.
    """

    src = Path(__file__).resolve().parents[1] / "src"
    if (src / "aiworkhub" / "server.py").is_file() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


_bootstrap_import_path()

from aiworkhub import platform_io  # noqa: E402  (after the import-path bootstrap)


class ProfileRefusedError(RuntimeError):
    """A named, bounded refusal: the report is not produced."""


# ---------------------------------------------------------------- platform probe


def _linux_status_bytes(keys: Sequence[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    with open("/proc/self/status", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            key, _, rest = line.partition(":")
            if key in keys:
                parts = rest.split()
                if len(parts) == 2 and parts[1] == "kB":
                    found[key] = int(parts[0]) * 1024
    return found


def probe_process_memory() -> dict[str, Any]:
    """The one platform probe: RSS, lifetime peak and private bytes; no psutil.

    Windows: ``K32GetProcessMemoryInfo`` (WorkingSetSize, PeakWorkingSetSize,
    PrivateUsage) through the owner ``platform_io.process_memory_bytes``.
    Linux: ``/proc/self/status`` -- VmRSS, VmHWM, and private bytes as
    RssAnon + VmSwap (anonymous memory, resident or swapped).
    Anything else, or any failure, yields ``None`` values with the basis named.
    """

    sample: dict[str, Any] = {
        "rss_bytes": None,
        "peak_rss_bytes": None,
        "private_bytes": None,
        "memory_basis": "unavailable",
    }
    if platform_io.is_windows():
        owner = platform_io.process_memory_bytes()
        sample.update(owner)
        sample["memory_basis"] = "windows:GetProcessMemoryInfo"
        return sample
    if platform_io.is_linux():
        try:
            found = _linux_status_bytes(("VmRSS", "VmHWM", "RssAnon", "VmSwap"))
        except OSError as exc:
            sample["memory_basis"] = f"linux:/proc/self/status_unreadable:{type(exc).__name__}"
            return sample
        sample["rss_bytes"] = found.get("VmRSS")
        sample["peak_rss_bytes"] = found.get("VmHWM")
        if "RssAnon" in found:
            sample["private_bytes"] = found["RssAnon"] + found.get("VmSwap", 0)
        sample["memory_basis"] = "linux:/proc/self/status"
        return sample
    owner = platform_io.process_memory_bytes()
    sample.update(owner)
    sample["memory_basis"] = f"other:{sys.platform}"
    return sample


def _trim_unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason, "rss_after_trim_bytes": None}


def trim_heap_and_probe() -> dict[str, Any]:
    """glibc ``malloc_trim(0)`` then RSS; unrated, unlike the product's trim.

    Uses the owner's cached ctypes handle (``platform_io._glibc_malloc_trim``)
    under its lock, so the measurement trims after every step instead of
    being rate limited.
    """

    getter = getattr(platform_io, "_glibc_malloc_trim", None)
    lock = getattr(platform_io, "_HEAP_TRIM_LOCK", None)
    if getter is None or lock is None:
        return _trim_unavailable("owner_trim_handle_missing")
    with lock:
        trim = getter()
        if trim is None:
            return _trim_unavailable("not_glibc_linux")
        released = bool(trim(0))
    return {
        "available": True,
        "released": released,
        "rss_after_trim_bytes": probe_process_memory()["rss_bytes"],
    }


# ---------------------------------------------------------------- synthetic DB


def _padding(seed: str, size_bytes: int) -> str:
    unit = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    reps = size_bytes // len(unit) + 1
    return (unit * reps)[:size_bytes]


def _iso(base: datetime, minutes: int) -> str:
    return (base + timedelta(minutes=minutes)).isoformat()


def _card_rows(cards: int, card_kb: int, base: datetime) -> Iterator[tuple[Any, ...]]:
    for index in range(cards):
        status, worker_status = SYNTHETIC_STATES[index % len(SYNTHETIC_STATES)]
        task_id = f"synthetic-nf02134-{index:07d}"
        stamp = _iso(base, index)
        card = {
            "task_id": task_id,
            "runner": "synthetic_runner",
            "topic": f"synthetic_topic_{index % 7}",
            "status": status,
            "worker_status": worker_status,
            "objective": f"synthetic memory-profile card {index}",
            "acceptance": [f"synthetic acceptance {index}"],
            "allowed_writes": [f"synthetic/path_{index % 13}.py"],
            "validation": ["python -c pass"],
            "synthetic_padding": _padding(task_id, card_kb * 1024),
        }
        yield (
            task_id,
            "synthetic_runner",
            card["topic"],
            status,
            worker_status,
            card["objective"],
            json.dumps(card),
            stamp,
            stamp,
            stamp if status in ("finished", "archived") else None,
            stamp if status == "archived" else "",
        )


def _event_rows(
    cards: int, events_per_card: int, event_kb: int, base: datetime
) -> Iterator[tuple[Any, ...]]:
    for index in range(cards):
        task_id = f"synthetic-nf02134-{index:07d}"
        for number in range(events_per_card):
            event = SYNTHETIC_EVENTS[number % len(SYNTHETIC_EVENTS)]
            payload = {"seq": number, "blob": _padding(f"{task_id}:{number}", event_kb * 1024)}
            yield (task_id, event, "synthetic_runner", json.dumps(payload), _iso(base, index + number))


def _batched(rows: Iterator[tuple[Any, ...]], size: int) -> Iterator[list[tuple[Any, ...]]]:
    batch: list[tuple[Any, ...]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def task_db_path(repo: Path) -> Path:
    from aiworkhub import task_store

    _readiness, db_path = task_store._require_ready(repo)
    return Path(db_path)


def build_synthetic_db(
    repo: Path, *, cards: int, card_kb: int, events_per_card: int, event_kb: int
) -> dict[str, Any]:
    """Initialise ``repo`` and fill an EMPTY canonical task DB; refuse otherwise."""

    from aiworkhub import task_store

    started = time.monotonic()
    task_store.initialize_repository(repo)
    db_path = task_db_path(repo)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = sqlite3.connect(db_path)
    try:
        existing = int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        if existing:
            raise ProfileRefusedError(f"synthetic_refused:repo_has_tasks:{existing}")
        with conn:
            for batch in _batched(_card_rows(cards, card_kb, base), INSERT_BATCH):
                conn.executemany(
                    "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
                    "priority, objective, card_json, created_at, updated_at, "
                    "completed_at, archived_at) VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?)",
                    batch,
                )
            for batch in _batched(
                _event_rows(cards, events_per_card, event_kb, base), INSERT_BATCH
            ):
                conn.executemany(
                    "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    batch,
                )
    finally:
        conn.close()
    return {
        "built": True,
        "cards": cards,
        "card_kb": card_kb,
        "events_per_card": events_per_card,
        "event_kb": event_kb,
        "seconds": round(time.monotonic() - started, 3),
    }


def describe_db(repo: Path) -> dict[str, Any]:
    try:
        db_path = task_db_path(repo)
    except Exception as exc:  # noqa: BLE001 -- recorded, never hidden
        return {"error": f"task_db_unavailable:{type(exc).__name__}:{exc}"}
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        tasks = int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        events = int(conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0])
    finally:
        conn.close()
    wal_path = Path(f"{db_path}-wal")
    return {
        "path": str(db_path),
        "db_bytes": db_path.stat().st_size,
        "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
        "tasks": tasks,
        "task_events": events,
    }


# ---------------------------------------------------------------- steps


def startup_step() -> Any:
    """The startup step: import the full server package in-process."""

    return importlib.import_module("aiworkhub.server")


def operation_steps(repo: Path, *, janitor: bool) -> dict[str, Callable[[], Any] | None]:
    """Server entry points in STEP order; ``None`` marks an absent or disabled step.

    Built only after the startup step, so the import cost stays attributed to
    startup.  The janitor is the records janitor (callbacks, NeedFix, task
    hygiene); it writes to the profiled task DB, exactly as a live server does.
    """

    server = importlib.import_module("aiworkhub.server")
    sweep: Callable[[], Any] | None = None
    if janitor:
        retention = importlib.import_module("aiworkhub.task_retention")
        records_janitor = getattr(retention, "run_records_janitor", None)
        if callable(records_janitor):

            def janitor_sweep() -> Any:
                return records_janitor(repo)

            sweep = janitor_sweep

    return {
        "task_list": lambda: {
            status: server.aiworkhub_task_list(status=status) for status in TASK_LIST_STATUSES
        },
        "task_health": server.aiworkhub_task_health,
        "doctor_diagnose": server.aiworkhub_doctor_diagnose,
        "review_queue": server.aiworkhub_task_review_queue,
        "completion_inbox": server.aiworkhub_completion_inbox,
        "janitor_sweep": sweep,
    }


def exclude_provider_cli_dirs() -> dict[str, Any]:
    """Drop from PATH every directory that resolves a provider CLI (opt-in).

    ``completion_inbox`` builds the route preflight, whose provider rows run
    the installed provider CLIs (auth, capability and model-list probes) as
    child processes.  That cost is host-specific and out of process, so it is
    not memory this server keeps; ``--exclude-provider-clis`` gives a bounded,
    hermetic run.  Binary names come from their owner,
    ``runtime_adapters.ADAPTER_EXECUTABLES``.  Called only after the startup
    import, so it never imports anything the startup step should attribute;
    ``main`` restores PATH.  A binary that still resolves afterwards (for
    example from the current directory) is named, never hidden.
    """

    import shutil

    runtime_adapters = importlib.import_module("aiworkhub.runtime_adapters")
    executables = getattr(runtime_adapters, "ADAPTER_EXECUTABLES", None)
    if not isinstance(executables, Mapping) or not executables:
        raise ProfileRefusedError("provider_cli_exclusion_unavailable:ADAPTER_EXECUTABLES")
    binaries = sorted({str(value) for value in executables.values() if value})
    entries = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    excluded_dirs: list[str] = []
    still_resolvable: dict[str, str] = {}
    for binary in binaries:
        # Each pass removes at least one entry, so PATH length bounds the loop.
        for _ in range(len(entries) + 1):
            found = shutil.which(binary, path=os.pathsep.join(entries))
            if found is None:
                break
            parent = os.path.normcase(os.path.normpath(os.path.dirname(found)))
            kept = [e for e in entries if os.path.normcase(os.path.normpath(e)) != parent]
            if len(kept) == len(entries):
                still_resolvable[binary] = found
                break
            excluded_dirs.append(parent)
            entries = kept
    os.environ["PATH"] = os.pathsep.join(entries)
    return {
        "enabled": True,
        "binaries": binaries,
        "excluded_dirs": excluded_dirs,
        "still_resolvable": still_resolvable,
    }


# ---------------------------------------------------------------- tracemalloc


# (filename, lineno) -> (size_bytes, block_count) of one tracemalloc snapshot.
LineStats = dict[tuple[str, int], tuple[int, int]]


def excluded_filenames() -> frozenset[str]:
    """Frames that belong to the profiler itself, never to the server."""

    here = Path(__file__)
    return frozenset(
        {str(here), str(here.resolve()), str(tracemalloc.__file__), "<unknown>"}
    )


def line_statistics(excluded: frozenset[str]) -> LineStats:
    """One snapshot grouped by ``filename:lineno``; only the small grouping is kept.

    ``Snapshot.filter_traces`` runs a Python-level ``fnmatch`` per trace and per
    filter, which on a full server heap (millions of traces) took minutes per
    snapshot.  Grouping once and dropping excluded files by exact name on the
    grouped rows is linear in traces with a constant per-trace cost, and the
    raw snapshot is released before the next step runs so it never inflates
    that step's RSS.
    """

    snapshot = tracemalloc.take_snapshot()
    grouped: LineStats = {}
    for stat in snapshot.statistics("lineno"):
        frame = stat.traceback[0]
        if frame.filename in excluded:
            continue
        grouped[(frame.filename, frame.lineno)] = (int(stat.size), int(stat.count))
    del snapshot
    return grouped


def top_allocators(new: LineStats, old: LineStats, top: int) -> list[dict[str, Any]]:
    """Top growing allocators of ``new`` versus ``old`` by ``filename:lineno``."""

    rows: list[dict[str, Any]] = []
    for (filename, lineno), (size, count) in new.items():
        old_size, old_count = old.get((filename, lineno), (0, 0))
        if size - old_size <= 0:
            continue
        rows.append(
            {
                "location": f"{filename}:{lineno}",
                "size_bytes": size,
                "size_diff_bytes": size - old_size,
                "count": count,
                "count_diff": count - old_count,
            }
        )
    rows.sort(key=lambda row: (-row["size_diff_bytes"], row["location"]))
    return rows[:top]


def _reply_bytes(result: Any) -> int | None:
    if result is None or type(result).__name__ == "module":
        return None
    try:
        return len(json.dumps(result, default=str))
    except (TypeError, ValueError):
        return None


def _run_step(
    name: str,
    fn: Callable[[], Any] | None,
    *,
    round_label: str,
    previous: LineStats | None,
    excluded: frozenset[str],
    top: int,
) -> tuple[dict[str, Any], LineStats | None]:
    record: dict[str, Any] = {"round": round_label, "step": name, "error": None, "skipped": None}
    before_current, _ = tracemalloc.get_traced_memory()
    if tracemalloc.is_tracing():
        tracemalloc.reset_peak()
    started = time.monotonic()
    result: Any = None
    if fn is None:
        record["skipped"] = "janitor_absent_or_disabled"
    else:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 -- recorded per step, counted in step_errors
            record["error"] = f"{type(exc).__name__}: {exc}"[:500]
    record["elapsed_seconds"] = round(time.monotonic() - started, 4)
    alive_current, step_peak = tracemalloc.get_traced_memory()
    record["tracemalloc_result_alive_bytes"] = alive_current
    record["tracemalloc_step_peak_bytes"] = max(0, step_peak - before_current)
    record["tracemalloc_peak_bytes"] = step_peak
    snapshot: LineStats | None = None
    attribution_started = time.monotonic()
    if previous is not None:
        # Taken while the step's reply is still alive, so what the step returns
        # and what it cached are both attributed; transient internals freed
        # before return are only visible in the peak figures.
        snapshot = line_statistics(excluded)
        record["top_allocators"] = top_allocators(snapshot, previous, top)
    record["attribution_seconds"] = round(time.monotonic() - attribution_started, 4)
    before_gc = probe_process_memory()
    record["rss_before_gc_bytes"] = before_gc["rss_bytes"]
    reply_bytes = _reply_bytes(result)
    del result
    gc.collect()
    memory = probe_process_memory()
    record["rss_bytes"] = memory["rss_bytes"]
    record["peak_rss_bytes"] = memory["peak_rss_bytes"]
    record["private_bytes"] = memory["private_bytes"]
    record["memory_basis"] = memory["memory_basis"]
    record["tracemalloc_current_bytes"] = tracemalloc.get_traced_memory()[0]
    record["gc_objects"] = len(gc.get_objects())
    trim = trim_heap_and_probe()
    record["rss_after_trim_bytes"] = trim.pop("rss_after_trim_bytes")
    record["trim"] = trim
    record["reply_bytes"] = reply_bytes
    return record, snapshot


def _round_labels(repeats: int) -> list[str]:
    return ["initial"] + [f"repeat_{number}" for number in range(1, repeats + 1)]


def profile(
    repo: Path,
    *,
    repeats: int,
    top: int,
    janitor: bool,
    exclude_provider_clis: bool = False,
) -> dict[str, Any]:
    """Run every round of steps; return steps, heaviest step and repeat growth.

    When tracemalloc is not tracing on entry (``--no-trace-startup``), the
    startup import runs untraced and tracing starts right after it: startup
    still records RSS, peak and private bytes, only its allocator attribution
    is skipped.  Every operation step is traced either way.
    """

    excluded = excluded_filenames()
    steps: list[dict[str, Any]] = []
    round_end: dict[str, dict[str, Any]] = {}
    operations: dict[str, Callable[[], Any] | None] | None = None
    provider_clis: dict[str, Any] = {"enabled": False}
    startup_traced = tracemalloc.is_tracing()
    labels = _round_labels(repeats)
    growth_from = "repeat_1" if repeats >= 2 else "initial"
    growth_to = labels[-1]
    growth_snapshots: dict[str, LineStats] = {}
    for label in labels:
        # Per-step attribution only in the cold first round, where caches fill;
        # one grouped snapshot per step keeps the run bounded on a large heap.
        previous: LineStats | None = (
            line_statistics(excluded)
            if label == "initial" and tracemalloc.is_tracing()
            else None
        )
        for name in STEP_NAMES:
            if name == "startup":
                fn: Callable[[], Any] | None = startup_step
            else:
                if operations is None:
                    if exclude_provider_clis:
                        provider_clis = exclude_provider_cli_dirs()
                    operations = operation_steps(repo, janitor=janitor)
                fn = operations[name]
            record, previous = _run_step(
                name,
                fn,
                round_label=label,
                previous=previous,
                excluded=excluded,
                top=top,
            )
            steps.append(record)
            if name == "startup" and label == "initial":
                record["traced"] = startup_traced
                if not tracemalloc.is_tracing():
                    tracemalloc.start(1)
                # The import is attributed by the startup record above.  Its
                # traces dominate every later snapshot (seconds each to group)
                # while never changing between steps, so they are dropped once:
                # later steps and the repeat growth see post-startup blocks only.
                tracemalloc.clear_traces()
                previous = line_statistics(excluded)
        previous = None
        gc.collect()
        memory = probe_process_memory()
        round_end[label] = {
            "rss_bytes": memory["rss_bytes"],
            "tracemalloc_current_bytes": tracemalloc.get_traced_memory()[0],
            "gc_objects": len(gc.get_objects()),
        }
        if label in (growth_from, growth_to):
            growth_snapshots[label] = line_statistics(excluded)

    operation_records = [
        s for s in steps if s["round"] == "initial" and s["step"] in OPERATION_STEPS
    ]
    heaviest = max(
        operation_records, key=lambda s: s.get("tracemalloc_step_peak_bytes") or 0
    )
    startup = next(s for s in steps if s["round"] == "initial" and s["step"] == "startup")
    first, last = round_end[growth_from], round_end[growth_to]

    def _delta(key: str) -> int | None:
        if first.get(key) is None or last.get(key) is None:
            return None
        return int(last[key]) - int(first[key])

    return {
        "steps": steps,
        "round_end": round_end,
        "startup": {
            "traced": startup_traced,
            "tracemalloc_step_peak_bytes": startup.get("tracemalloc_step_peak_bytes"),
            "top_allocators": startup.get("top_allocators", []),
        },
        "provider_clis": provider_clis,
        "heaviest_step": {
            "round": heaviest["round"],
            "step": heaviest["step"],
            "basis": "max tracemalloc_step_peak_bytes among initial-round operation steps; "
            "allocators are the step's retained growth (reply alive) by filename:lineno",
            "tracemalloc_step_peak_bytes": heaviest.get("tracemalloc_step_peak_bytes"),
            "top_allocators": heaviest.get("top_allocators", []),
        },
        "repeat_growth": {
            "from": growth_from,
            "to": growth_to,
            "rss_delta_bytes": _delta("rss_bytes"),
            "tracemalloc_delta_bytes": _delta("tracemalloc_current_bytes"),
            "gc_objects_delta": _delta("gc_objects"),
            "top_allocators": top_allocators(
                growth_snapshots[growth_to], growth_snapshots[growth_from], top
            ),
        },
    }


# ---------------------------------------------------------------- CLI


def _bounded_int(low: int, high: int) -> Callable[[str], int]:
    def parse(raw: str) -> int:
        value = int(raw)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be within {low}..{high}, got {value}")
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, type=Path, help="repository to profile")
    parser.add_argument("--synthetic", action="store_true", help="build the default synthetic DB first")
    parser.add_argument(
        "--synthetic-cards",
        type=_bounded_int(1, MAX_SYNTHETIC_CARDS),
        default=None,
        help=f"build a synthetic DB with N cards first (default {DEFAULT_SYNTHETIC_CARDS} with --synthetic)",
    )
    parser.add_argument("--card-kb", type=_bounded_int(1, MAX_PAYLOAD_KB), default=DEFAULT_CARD_KB)
    parser.add_argument(
        "--events-per-card", type=_bounded_int(0, 10_000), default=DEFAULT_EVENTS_PER_CARD
    )
    parser.add_argument("--event-kb", type=_bounded_int(1, MAX_PAYLOAD_KB), default=DEFAULT_EVENT_KB)
    parser.add_argument("--repeats", type=_bounded_int(1, MAX_REPEATS), default=DEFAULT_REPEATS)
    parser.add_argument("--top", type=_bounded_int(1, 1000), default=DEFAULT_TOP)
    parser.add_argument("--no-janitor", action="store_true", help="skip the records-janitor sweep step")
    parser.add_argument(
        "--no-trace-startup",
        action="store_true",
        help="import the server untraced; tracemalloc starts right after the startup step",
    )
    parser.add_argument(
        "--exclude-provider-clis",
        action="store_true",
        help="drop provider CLI directories from PATH for the operation steps (hermetic, bounded run)",
    )
    return parser


def _write_report(repo: Path, report: dict[str, Any]) -> Path:
    out_dir = repo / REPORT_DIR_REL
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = out_dir / f"{stamp}.json"
    staging = out_dir / f".{stamp}.json.tmp"
    staging.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.replace(staging, target)
    return target


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = args.repo.resolve()
    if not repo.is_dir():
        print(json.dumps({"ok": False, "error": f"repo_not_a_directory:{repo}"}))
        return 2

    # PATH is saved too: --exclude-provider-clis narrows it for the run.
    saved_env = {
        key: os.environ.get(key) for key in ("AIWORKHUB_REPO_ROOT", "AIWORKHUB_REPO", "PATH")
    }
    os.environ["AIWORKHUB_REPO_ROOT"] = str(repo)
    os.environ.pop("AIWORKHUB_REPO", None)
    # Tracing this run did not start (e.g. PYTHONTRACEMALLOC) is never stopped.
    external_tracing = tracemalloc.is_tracing()
    try:
        synthetic: dict[str, Any] = {"built": False}
        if args.synthetic or args.synthetic_cards is not None:
            try:
                synthetic = build_synthetic_db(
                    repo,
                    cards=args.synthetic_cards or DEFAULT_SYNTHETIC_CARDS,
                    card_kb=args.card_kb,
                    events_per_card=args.events_per_card,
                    event_kb=args.event_kb,
                )
            except ProfileRefusedError as exc:
                print(json.dumps({"ok": False, "error": str(exc)}))
                return 2
        db_before = describe_db(repo)
        gc.collect()
        if not external_tracing and not args.no_trace_startup:
            tracemalloc.start(1)
        baseline = probe_process_memory()
        try:
            measured = profile(
                repo,
                repeats=args.repeats,
                top=args.top,
                janitor=not args.no_janitor,
                exclude_provider_clis=args.exclude_provider_clis,
            )
        except ProfileRefusedError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 2
        import aiworkhub

        step_errors = [f"{s['round']}:{s['step']}:{s['error']}" for s in measured["steps"] if s["error"]]
        report: dict[str, Any] = {
            "schema_id": SCHEMA_ID,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "repo": str(repo),
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "pid": os.getpid(),
            "aiworkhub_version": getattr(aiworkhub, "__version__", None),
            "aiworkhub_package": str(Path(aiworkhub.__file__).resolve().parent),
            "config": {
                "repeats": args.repeats,
                "top": args.top,
                "janitor": not args.no_janitor,
                "tracemalloc_frames": tracemalloc.get_traceback_limit(),
                "tracemalloc_basis": TRACEMALLOC_BASIS,
                "trace_startup": measured["startup"]["traced"],
                "exclude_provider_clis": args.exclude_provider_clis,
            },
            "synthetic": synthetic,
            "db_before": db_before,
            "db_after": describe_db(repo),
            "baseline": baseline,
            "step_errors": step_errors,
            **measured,
        }
        target = _write_report(repo, report)
        print(json.dumps({"ok": not step_errors, "report": str(target), "step_errors": len(step_errors)}))
        return 0 if not step_errors else 1
    finally:
        # profile() may have started tracing after an untraced startup.
        if not external_tracing and tracemalloc.is_tracing():
            tracemalloc.stop()
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    raise SystemExit(main())
