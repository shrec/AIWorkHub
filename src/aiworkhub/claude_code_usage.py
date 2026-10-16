"""Read-only surfacing of Claude Code's own transcript usage numbers.

Claude Code already writes exact per-call token usage into
``<config>/projects/<slug>/*.jsonl`` (and ``<session>/subagents/*.jsonl``
for Agent-tool subagents). This module locates that project directory for a
repository root and sums the usage blocks it finds. It never reads, returns
or logs message content -- only usage numbers, model names, message ids,
timestamps and session ids.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SLUG_RE = re.compile(r"[^A-Za-z0-9]")
_MAX_SESSIONS_RETURNED = 10
_SCHEMA_ID = "aiworkhub.cost_ledger.claude_code_sessions.v1"

# Keyed by path alone, holding the (size, mtime_ns) it was last parsed at, so
# a transcript that is still being written gets its ONE entry replaced rather
# than accumulating a new key per write -- keying by (path, size, mtime_ns)
# instead let a growing transcript leak one entry per call for the life of a
# long-running MCP server. Bounded by max(_FILE_CACHE_MAX_ENTRIES, files in the
# current call) and LRU-evicted only among paths outside the current file set:
# a plain LRU smaller than a sequential scan evicts every entry before reuse.
_FILE_CACHE_MAX_ENTRIES = 512
_FILE_CACHE: OrderedDict[str, _CacheEntry] = OrderedDict()

# Bump whenever _parse_transcript_file's output changes: a persisted cache with
# any other version is ignored and rebuilt by a full parse.
_CACHE_VERSION = 1
_USAGE_INT_FIELDS = (
    "api_calls",
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
    "context_tokens",
    "max_context",
    "malformed_lines",
)
_USAGE_STR_FIELDS = ("first_at", "last_at")


@dataclass(frozen=True, slots=True)
class _FileUsage:
    api_calls: int
    input_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    output_tokens: int
    context_tokens: int
    max_context: int
    malformed_lines: int
    models: tuple[str, ...]
    first_at: str
    last_at: str


_EMPTY_FILE_USAGE = _FileUsage(
    api_calls=0,
    input_tokens=0,
    cache_read_input_tokens=0,
    cache_creation_input_tokens=0,
    output_tokens=0,
    context_tokens=0,
    max_context=0,
    malformed_lines=0,
    models=(),
    first_at="",
    last_at="",
)


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    size: int
    mtime_ns: int
    usage: _FileUsage


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def _slugify(repo_root: Path) -> str:
    return _SLUG_RE.sub("-", str(repo_root))


def _matching_project_dirs(projects_root: Path, slug: str) -> list[Path]:
    """Case-insensitive, exact (never prefix) match against projects_root entries."""

    if not projects_root.is_dir():
        return []
    target = slug.lower()
    return sorted(
        entry
        for entry in projects_root.iterdir()
        if entry.is_dir() and entry.name.lower() == target
    )


_SYNTHETIC_MODEL = "<synthetic>"


def _parse_transcript_file(path: Path) -> _FileUsage:
    """Parse one transcript. Malformed lines are skipped and counted, never raised.

    Streamed line by line rather than read whole: a live transcript in this
    repository reaches roughly 50 MB.
    """

    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return _EMPTY_FILE_USAGE

    calls_by_id: dict[str, dict[str, Any]] = {}
    anonymous_calls: list[dict[str, Any]] = []
    malformed_lines = 0
    first_at = ""
    last_at = ""

    with handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                malformed_lines += 1
                continue
            if not isinstance(entry, dict):
                malformed_lines += 1
                continue

            timestamp = entry.get("timestamp")
            if isinstance(timestamp, str) and timestamp:
                if not first_at or timestamp < first_at:
                    first_at = timestamp
                if not last_at or timestamp > last_at:
                    last_at = timestamp

            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue

            model = message.get("model")
            if model == _SYNTHETIC_MODEL:
                # A local placeholder assistant message Claude Code writes
                # for itself, never a billed API call.
                continue
            snapshot = {
                "input_tokens": _as_int(usage.get("input_tokens")),
                "cache_read_input_tokens": _as_int(usage.get("cache_read_input_tokens")),
                "cache_creation_input_tokens": _as_int(usage.get("cache_creation_input_tokens")),
                "output_tokens": _as_int(usage.get("output_tokens")),
                "model": model if isinstance(model, str) else "",
            }

            message_id = message.get("id")
            if isinstance(message_id, str) and message_id:
                # A streamed message is rewritten several times under the same
                # id; the last write in the file carries the final usage.
                calls_by_id[message_id] = snapshot
            else:
                anonymous_calls.append(snapshot)

    all_calls = list(calls_by_id.values()) + anonymous_calls
    per_call_context = [
        call["input_tokens"] + call["cache_read_input_tokens"] + call["cache_creation_input_tokens"]
        for call in all_calls
    ]
    models = tuple(sorted({call["model"] for call in all_calls if call["model"]}))

    return _FileUsage(
        api_calls=len(all_calls),
        input_tokens=sum(call["input_tokens"] for call in all_calls),
        cache_read_input_tokens=sum(call["cache_read_input_tokens"] for call in all_calls),
        cache_creation_input_tokens=sum(call["cache_creation_input_tokens"] for call in all_calls),
        output_tokens=sum(call["output_tokens"] for call in all_calls),
        context_tokens=sum(per_call_context),
        max_context=max(per_call_context, default=0),
        malformed_lines=malformed_lines,
        models=models,
        first_at=first_at,
        last_at=last_at,
    )


def _persistent_cache_path(repo_root: Path) -> Path:
    return repo_root / ".aiworkhub" / "runtime" / "cache" / "claude_code_usage_v1.json"


def _entry_to_json(entry: _CacheEntry) -> dict[str, Any]:
    payload: dict[str, Any] = {"size": entry.size, "mtime_ns": entry.mtime_ns}
    for name in _USAGE_INT_FIELDS:
        payload[name] = getattr(entry.usage, name)
    payload["models"] = list(entry.usage.models)
    for name in _USAGE_STR_FIELDS:
        payload[name] = getattr(entry.usage, name)
    return payload


def _entry_from_json(raw: Any) -> _CacheEntry | None:
    if not isinstance(raw, dict):
        return None
    ints = {name: raw.get(name) for name in ("size", "mtime_ns", *_USAGE_INT_FIELDS)}
    strs = {name: raw.get(name) for name in _USAGE_STR_FIELDS}
    models = raw.get("models")
    if not all(type(value) is int for value in ints.values()):
        return None
    if not all(isinstance(value, str) for value in strs.values()):
        return None
    if not isinstance(models, list) or not all(isinstance(model, str) for model in models):
        return None
    usage = _FileUsage(
        **{name: ints[name] for name in _USAGE_INT_FIELDS},
        models=tuple(models),
        **strs,
    )
    return _CacheEntry(ints["size"], ints["mtime_ns"], usage)


def _load_persistent_cache(cache_path: Path) -> dict[str, _CacheEntry]:
    """Return the persisted entries, or {} for any missing/corrupt/stale-version file."""

    try:
        document = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(document, dict) or document.get("version") != _CACHE_VERSION:
        return {}
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, dict):
        return {}
    entries: dict[str, _CacheEntry] = {}
    for key, raw in raw_entries.items():
        entry = _entry_from_json(raw)
        if not isinstance(key, str) or entry is None:
            return {}
        entries[key] = entry
    return entries


def _store_persistent_cache(cache_path: Path, entries: dict[str, _CacheEntry]) -> None:
    """Atomically write the cache; a failed write is dropped, never raised."""

    document = {
        "version": _CACHE_VERSION,
        "entries": {key: _entry_to_json(entry) for key, entry in entries.items()},
    }
    temp_name = ""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            dir=cache_path.parent, prefix=cache_path.name + ".", suffix=".tmp"
        )
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, separators=(",", ":"), sort_keys=True)
        os.replace(temp_name, cache_path)
    except OSError:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _parse_files(paths: list[Path], cache_path: Path | None = None) -> dict[Path, _FileUsage]:
    # Cold-parse parallelism, measured 2026-10-16 on the manager host (real
    # corpus, 714 files / 1588.4 MB, 16 cores): sequential 23.01 s vs
    # ProcessPoolExecutor(14) 11.10 s (2.07x, cold parse only). Decision: stay
    # sequential -- the persistent cache makes the cold parse a one-time cost per
    # changed file, and a 14-process pool inside the MCP server would starve
    # interactive calls.
    # ponytail: sequential cold parse ~23 s once; ProcessPool if first-run latency matters
    persisted = _load_persistent_cache(cache_path) if cache_path is not None else {}
    current: dict[str, _CacheEntry] = {}
    result: dict[Path, _FileUsage] = {}
    for path in paths:
        try:
            stat_result = path.stat()
        except OSError:
            result[path] = _EMPTY_FILE_USAGE
            continue
        key = str(path)
        entry = _FILE_CACHE.get(key)
        if entry is None or not _entry_matches(entry, stat_result):
            entry = persisted.get(key)
            if entry is None or not _entry_matches(entry, stat_result):
                usage = _parse_transcript_file(path)
                if usage is _EMPTY_FILE_USAGE:
                    # Open failed (e.g. transiently locked): report zero for
                    # this call but never cache it under this size/mtime.
                    result[path] = usage
                    continue
                entry = _CacheEntry(stat_result.st_size, stat_result.st_mtime_ns, usage)
        _FILE_CACHE[key] = entry
        _FILE_CACHE.move_to_end(key)
        current[key] = entry
        result[path] = entry.usage

    # Only paths outside this call's file set may be evicted: a sequential scan
    # larger than the bound must not push out entries before the next call.
    bound = max(_FILE_CACHE_MAX_ENTRIES, len(current))
    while len(_FILE_CACHE) > bound:
        _FILE_CACHE.popitem(last=False)

    if cache_path is not None and current != persisted and cache_path.parents[3].is_dir():
        _store_persistent_cache(cache_path, current)
    return result


def _entry_matches(entry: _CacheEntry, stat_result: os.stat_result) -> bool:
    return entry.size == stat_result.st_size and entry.mtime_ns == stat_result.st_mtime_ns


def _zero_bucket() -> dict[str, int]:
    return {
        "api_calls": 0,
        "input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 0,
        "context_tokens": 0,
        "malformed_lines": 0,
    }


def _accumulate(bucket: dict[str, int], usage: _FileUsage) -> None:
    bucket["api_calls"] += usage.api_calls
    bucket["input_tokens"] += usage.input_tokens
    bucket["cache_read_input_tokens"] += usage.cache_read_input_tokens
    bucket["cache_creation_input_tokens"] += usage.cache_creation_input_tokens
    bucket["output_tokens"] += usage.output_tokens
    bucket["context_tokens"] += usage.context_tokens
    bucket["malformed_lines"] += usage.malformed_lines


def _discover_sessions(project_dirs: list[Path]) -> list[tuple[str, Path, list[Path]]]:
    sessions: list[tuple[str, Path, list[Path]]] = []
    for project_dir in project_dirs:
        for main_file in sorted(project_dir.glob("*.jsonl")):
            session_id = main_file.stem
            subagent_dir = project_dir / session_id / "subagents"
            subagent_files = (
                sorted(subagent_dir.glob("*.jsonl")) if subagent_dir.is_dir() else []
            )
            sessions.append((session_id, main_file, subagent_files))
    return sessions


def _empty_section(status: str, project_dirs: list[Path]) -> dict[str, Any]:
    return {
        "schema_id": _SCHEMA_ID,
        "source": "claude_code_transcripts",
        "project_dirs": [str(path) for path in project_dirs],
        "status": status,
        "totals": {
            "sessions": 0,
            "subagent_transcripts": 0,
            "main": _zero_bucket(),
            "subagent": _zero_bucket(),
        },
        "sessions": [],
        "total_count": 0,
        "returned_count": 0,
        "truncated": False,
    }


def collect_claude_code_usage(repo_root: Path | str) -> dict[str, Any]:
    """Sum Claude Code's own transcript usage for the given repository root.

    Best-effort: it reads transcripts and writes only its own derived cache at
    ``.aiworkhub/runtime/cache/claude_code_usage_v1.json``; a failed cache
    write is dropped and never raised. A missing project directory yields
    ``status: "not_found"``, never an exception. There is no price table, so
    cost stays unknown -- this never reports a dollar figure.
    """

    root = Path(repo_root)
    if not root.is_absolute():
        root = root.resolve()
    slug = _slugify(root)
    project_dirs = _matching_project_dirs(_config_dir() / "projects", slug)

    if not project_dirs:
        return _empty_section("not_found", [])

    sessions = _discover_sessions(project_dirs)
    all_files: list[Path] = []
    for _session_id, main_file, subagent_files in sessions:
        all_files.append(main_file)
        all_files.extend(subagent_files)

    parsed = _parse_files(all_files, _persistent_cache_path(root))

    main_bucket = _zero_bucket()
    subagent_bucket = _zero_bucket()
    rows: list[dict[str, Any]] = []
    for session_id, main_file, subagent_files in sessions:
        main_usage = parsed.get(main_file, _EMPTY_FILE_USAGE)
        _accumulate(main_bucket, main_usage)

        sub_usages = [parsed.get(path, _EMPTY_FILE_USAGE) for path in subagent_files]
        for sub_usage in sub_usages:
            _accumulate(subagent_bucket, sub_usage)

        context_tokens = main_usage.context_tokens + sum(u.context_tokens for u in sub_usages)
        max_context = max(
            [main_usage.max_context, *(u.max_context for u in sub_usages)], default=0
        )
        models = sorted(set(main_usage.models) | {m for u in sub_usages for m in u.models})
        last_at = max(
            (t for t in (main_usage.last_at, *(u.last_at for u in sub_usages)) if t),
            default="",
        )

        rows.append({
            "session_id": session_id,
            "main_calls": main_usage.api_calls,
            "subagent_calls": sum(u.api_calls for u in sub_usages),
            "input_tokens": main_usage.input_tokens + sum(u.input_tokens for u in sub_usages),
            "cache_read_input_tokens": (
                main_usage.cache_read_input_tokens + sum(u.cache_read_input_tokens for u in sub_usages)
            ),
            "cache_creation_input_tokens": (
                main_usage.cache_creation_input_tokens
                + sum(u.cache_creation_input_tokens for u in sub_usages)
            ),
            "output_tokens": main_usage.output_tokens + sum(u.output_tokens for u in sub_usages),
            "context_tokens": context_tokens,
            "max_context": max_context,
            "models": models,
            "last_at": last_at,
        })

    rows.sort(key=lambda row: row["context_tokens"], reverse=True)
    total_count = len(rows)
    returned = rows[:_MAX_SESSIONS_RETURNED]

    return {
        "schema_id": _SCHEMA_ID,
        "source": "claude_code_transcripts",
        "project_dirs": [str(path) for path in project_dirs],
        "status": "measured",
        "totals": {
            "sessions": len(sessions),
            "subagent_transcripts": sum(len(sub) for _, _, sub in sessions),
            "main": main_bucket,
            "subagent": subagent_bucket,
        },
        "sessions": returned,
        "total_count": total_count,
        "returned_count": len(returned),
        "truncated": total_count > len(returned),
    }
