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
# long-running MCP server. Bounded and LRU-evicted via _FILE_CACHE.popitem.
_FILE_CACHE_MAX_ENTRIES = 512
_FILE_CACHE: OrderedDict[str, _CacheEntry] = OrderedDict()


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


def _parse_transcript_file_cached(path: Path) -> _FileUsage:
    try:
        stat_result = path.stat()
    except OSError:
        return _EMPTY_FILE_USAGE
    key = str(path)
    cached = _FILE_CACHE.get(key)
    if (
        cached is not None
        and cached.size == stat_result.st_size
        and cached.mtime_ns == stat_result.st_mtime_ns
    ):
        _FILE_CACHE.move_to_end(key)
        return cached.usage
    usage = _parse_transcript_file(path)
    _FILE_CACHE[key] = _CacheEntry(stat_result.st_size, stat_result.st_mtime_ns, usage)
    _FILE_CACHE.move_to_end(key)
    while len(_FILE_CACHE) > _FILE_CACHE_MAX_ENTRIES:
        _FILE_CACHE.popitem(last=False)
    return usage


def _parse_files(paths: list[Path]) -> dict[Path, _FileUsage]:
    # Measured on this repository's real transcript set, 46 files / 101.8 MB:
    #   sequential        0.71 s
    #   ThreadPool(2)     0.64 s
    #   ThreadPool(4)     0.66 s
    #   ThreadPool(8)     0.74 s
    # No meaningful gain: json decoding holds the GIL, so a thread pool only
    # adds scheduling contention to work that never releases it. Sequential.
    return {path: _parse_transcript_file_cached(path) for path in paths}


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

    Read-only and best-effort: a missing project directory yields
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

    parsed = _parse_files(all_files)

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
