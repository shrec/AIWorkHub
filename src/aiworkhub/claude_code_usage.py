"""Read-only surfacing of Claude Code's own transcript usage numbers.

Claude Code already writes exact per-call token usage into
``<config>/projects/<slug>/*.jsonl`` (and ``<session>/subagents/*.jsonl``
for Agent-tool subagents). This module locates that project directory for a
repository root and sums the usage blocks it finds. It never reads, returns
or logs message content -- only usage numbers, model names, message ids,
timestamps and session ids.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any


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

# NF-2026-02056: a live transcript grows on every append, so a (size, mtime)
# key alone re-parsed ~50 MB from byte 0 on every polled dashboard snapshot.
# A cache entry for a recently written file also keeps the resumable parse
# state; growth whose prefix guard (sha256 of the window just before the old
# offset) still matches parses only the appended bytes. Shrink, guard mismatch
# or missing state -> full parse. State is retained only for files written in
# the last _STATE_RETAIN_MAX_AGE_SECONDS, which bounds its memory to the live
# transcripts instead of the whole corpus.
_GUARD_WINDOW_BYTES = 4096
_STATE_RETAIN_MAX_AGE_SECONDS = 6 * 3600

# Short memo of the whole section per (repo root, config dir): back-to-back
# snapshots inside the TTL do no file IO at all. Bounded LRU.
_COLLECT_MEMO_TTL_SECONDS = 30.0
_COLLECT_MEMO_MAX_ENTRIES = 32
_COLLECT_MEMO: OrderedDict[tuple[str, str], tuple[float, dict[str, Any]]] = OrderedDict()

# Measured hit rates of the two caches above (process lifetime counters).
CACHE_STATS: dict[str, int] = {
    "memo_hits": 0,
    "memo_misses": 0,
    "full_parses": 0,
    "tail_parses": 0,
    "tail_bytes_read": 0,
    "transient_failures": 0,
}

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


# One billed call: (input, cache_read, cache_creation, output, model).
_CallSnapshot = tuple[int, int, int, int, str]


@dataclass(slots=True)
class _ParseState:
    """Resumable parse of every complete line before ``offset``.

    ``offset`` is the byte just past the last newline consumed; a trailing
    partial line is never folded in, so the next parse re-reads it whole.
    ``guard`` is the sha256 of up to _GUARD_WINDOW_BYTES just before ``offset``.
    """

    offset: int
    guard: bytes
    calls_by_id: dict[str, _CallSnapshot]
    anonymous_calls: list[_CallSnapshot]
    malformed_lines: int
    first_at: str
    last_at: str

    def copy(self) -> _ParseState:
        return _ParseState(
            self.offset,
            self.guard,
            dict(self.calls_by_id),
            list(self.anonymous_calls),
            self.malformed_lines,
            self.first_at,
            self.last_at,
        )


def _empty_state() -> _ParseState:
    return _ParseState(0, b"", {}, [], 0, "", "")


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    size: int
    mtime_ns: int
    usage: _FileUsage
    # In-process only: never persisted and never part of equality, so the
    # persisted-cache "changed?" comparison is about usage alone.
    state: _ParseState | None = field(default=None, compare=False)


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


def _parse_line(raw_line: bytes, state: _ParseState) -> None:
    """Fold one transcript line into ``state``. Malformed lines are counted, never raised."""

    line = raw_line.decode("utf-8", errors="replace").strip()
    if not line:
        return
    try:
        entry = json.loads(line)
    except ValueError:
        state.malformed_lines += 1
        return
    if not isinstance(entry, dict):
        state.malformed_lines += 1
        return

    timestamp = entry.get("timestamp")
    if isinstance(timestamp, str) and timestamp:
        if not state.first_at or timestamp < state.first_at:
            state.first_at = timestamp
        if not state.last_at or timestamp > state.last_at:
            state.last_at = timestamp

    message = entry.get("message")
    if not isinstance(message, dict):
        return
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return

    model = message.get("model")
    if model == _SYNTHETIC_MODEL:
        # A local placeholder assistant message Claude Code writes for
        # itself, never a billed API call.
        return
    snapshot: _CallSnapshot = (
        _as_int(usage.get("input_tokens")),
        _as_int(usage.get("cache_read_input_tokens")),
        _as_int(usage.get("cache_creation_input_tokens")),
        _as_int(usage.get("output_tokens")),
        model if isinstance(model, str) else "",
    )

    message_id = message.get("id")
    if isinstance(message_id, str) and message_id:
        # A streamed message is rewritten several times under the same id;
        # the last write in the file carries the final usage. Re-assigning an
        # existing key keeps its first position, exactly as a cold parse does.
        state.calls_by_id[message_id] = snapshot
    else:
        state.anonymous_calls.append(snapshot)


def _summarise(state: _ParseState) -> _FileUsage:
    all_calls = list(state.calls_by_id.values()) + state.anonymous_calls
    per_call_context = [call[0] + call[1] + call[2] for call in all_calls]
    models = tuple(sorted({call[4] for call in all_calls if call[4]}))
    return _FileUsage(
        api_calls=len(all_calls),
        input_tokens=sum(call[0] for call in all_calls),
        cache_read_input_tokens=sum(call[1] for call in all_calls),
        cache_creation_input_tokens=sum(call[2] for call in all_calls),
        output_tokens=sum(call[3] for call in all_calls),
        context_tokens=sum(per_call_context),
        max_context=max(per_call_context, default=0),
        malformed_lines=state.malformed_lines,
        models=models,
        first_at=state.first_at,
        last_at=state.last_at,
    )


def _guard_digest(handle: IO[bytes], offset: int) -> bytes:
    start = max(0, offset - _GUARD_WINDOW_BYTES)
    handle.seek(start)
    return hashlib.sha256(handle.read(offset - start)).digest()


def _resume_state(handle: IO[bytes], previous: _ParseState | None) -> _ParseState | None:
    """Return a copy of ``previous`` when the file is an append-only growth of it, else None."""

    if previous is None or previous.offset <= 0:
        return None
    if os.fstat(handle.fileno()).st_size < previous.offset:
        return None  # truncated
    if _guard_digest(handle, previous.offset) != previous.guard:
        return None  # rewritten
    return previous.copy()


def _parse_transcript_file(
    path: Path, previous: _ParseState | None = None
) -> tuple[_FileUsage, _ParseState] | None:
    """Parse one transcript, resuming from ``previous`` when the prefix guard holds.

    Returns None when the file cannot be read (never cached). Streamed line by
    line rather than read whole: a live transcript in this repository reaches
    roughly 50 MB. A trailing line without a newline is counted in the returned
    usage, exactly like a cold parse, but is not consumed into the state.
    """

    try:
        handle = path.open("rb")
    except OSError:
        return None

    tail = b""
    with handle:
        try:
            state = _resume_state(handle, previous)
            resumed = state is not None
            if state is None:
                state = _empty_state()
            start = state.offset
            handle.seek(start)
            for raw_line in handle:
                if not raw_line.endswith(b"\n"):
                    tail = raw_line
                    break
                _parse_line(raw_line, state)
                state.offset += len(raw_line)
            state.guard = _guard_digest(handle, state.offset)
        except OSError:
            return None

    if resumed:
        CACHE_STATS["tail_parses"] += 1
        CACHE_STATS["tail_bytes_read"] += state.offset - start + len(tail)
    else:
        CACHE_STATS["full_parses"] += 1
    if not tail:
        return _summarise(state), state
    provisional = state.copy()
    _parse_line(tail, provisional)
    return _summarise(provisional), state


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
            previous = entry.state if entry is not None else None
            entry = persisted.get(key)
            if entry is None or not _entry_matches(entry, stat_result):
                parsed = _parse_transcript_file(path, previous)
                if parsed is None:
                    # Open failed (e.g. transiently locked): report zero for
                    # this call but never cache it under this size/mtime.
                    CACHE_STATS["transient_failures"] += 1
                    result[path] = _EMPTY_FILE_USAGE
                    continue
                usage, state = parsed
                age_seconds = time.time() - stat_result.st_mtime_ns / 1e9
                retained = state if age_seconds < _STATE_RETAIN_MAX_AGE_SECONDS else None
                entry = _CacheEntry(
                    stat_result.st_size, stat_result.st_mtime_ns, usage, retained
                )
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

    A result younger than _COLLECT_MEMO_TTL_SECONDS for the same repository
    root and config dir is returned (as a copy) without any file IO.
    """

    root = Path(repo_root)
    if not root.is_absolute():
        root = root.resolve()
    memo_key = (str(root), str(_config_dir()))
    now = time.monotonic()
    memo = _COLLECT_MEMO.get(memo_key)
    if memo is not None and 0.0 <= now - memo[0] < _COLLECT_MEMO_TTL_SECONDS:
        CACHE_STATS["memo_hits"] += 1
        _COLLECT_MEMO.move_to_end(memo_key)
        return copy.deepcopy(memo[1])
    CACHE_STATS["memo_misses"] += 1
    failures_before = CACHE_STATS["transient_failures"]
    result = _collect_uncached(root)
    if CACHE_STATS["transient_failures"] != failures_before:
        # A transiently unreadable file reported zero for this call only; the
        # memo must not serve that zero for the next TTL.
        return result
    _COLLECT_MEMO[memo_key] = (now, copy.deepcopy(result))
    _COLLECT_MEMO.move_to_end(memo_key)
    while len(_COLLECT_MEMO) > _COLLECT_MEMO_MAX_ENTRIES:
        _COLLECT_MEMO.popitem(last=False)
    return result


def _collect_uncached(root: Path) -> dict[str, Any]:
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
