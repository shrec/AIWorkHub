from __future__ import annotations

import concurrent.futures
import json
import math
import mmap
import os
import sys
import time
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


def diagnose(
    probes: Mapping[str, Probe], *, timeout_seconds: float = 10.0
) -> dict[str, Any]:
    """Run every probe concurrently and fold the results into one verdict."""
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


def server_process() -> dict[str, Any]:
    """Identify and size the process answering this call; never raises."""

    return {
        "pid": os.getpid(),
        **platform_io.process_memory_bytes(),
        "python_allocated_blocks": sys.getallocatedblocks(),
        "runtime_generation": _or_none(runtime_generation.own_generation),
        "superseded": _or_none(runtime_generation.superseded),
    }


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
    own_uid = os.getuid()
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
