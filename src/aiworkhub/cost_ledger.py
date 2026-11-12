from __future__ import annotations

import copy
import json
import re
import sqlite3
import threading
from collections import OrderedDict, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from aiworkhub import (
    claude_code_usage,
    core,
    launch_queue_persist,
    task_store,
    terminal_failure_classification,
)


USAGE_LINE_RE = re.compile(
    r"^(?P<task_id>\S+) \| runner=(?P<runner>[^|]+) \| topic=(?P<topic>[^|]+) "
    r"\| records=(?P<records>\d+) \| tokens=(?P<tokens>\d+) "
    r"\| in=(?P<input_tokens>\d+) out=(?P<output_tokens>\d+) \| cost=\$(?P<cost>[0-9.]+)"
)
_ECONOMIC_MODEL_ALIASES: tuple[tuple[str, str], ...] = (
    ("gpt-5.3-codex-spark", "gpt-5.3-codex-spark"),
    ("gpt-5.3-codex", "gpt-5.3-codex"),
    ("gpt-5.5", "gpt-5.5"),
    ("deepseek-v4-flash", "deepseek-v4-flash"),
    ("deepseek-v4-pro", "deepseek-v4-pro"),
    ("glm-5.2", "glm-5.2"),
    ("claude-sonnet-5", "sonnet"),
    ("claude-sonnet", "sonnet"),
    ("claude-opus-5", "opus"),
    ("claude-opus", "opus"),
    ("claude-haiku-4-5", "haiku"),
    ("claude-haiku-4.5", "haiku"),
    ("claude-haiku", "haiku"),
)


def _parse_usage_stdout(stdout: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        match = USAGE_LINE_RE.match(line.strip())
        if not match:
            continue
        data = match.groupdict()
        rows.append({
            "source": "taskctl_usage_report",
            "task_id": data["task_id"],
            "runner": data["runner"].strip(),
            "topic": data["topic"].strip(),
            "model": "",
            "role": "unknown",
            "role_observed": False,
            "records": int(data["records"]),
            "input_tokens": int(data["input_tokens"]),
            "output_tokens": int(data["output_tokens"]),
            "total_tokens": int(data["tokens"]),
            "cached_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "cache_metrics_observed": False,
            "cost_usd": float(data["cost"]),
            "cost_known": not (
                int(data["tokens"]) > 0 and float(data["cost"]) == 0.0
            ),
            "day": "",
        })
    return rows


def _launch_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in summary.get("last_entries", []) or []:
        task_id = entry.get("task_id")
        if not task_id:
            continue
        ts = entry.get("requested_at") or entry.get("ts") or ""
        day = ""
        if isinstance(ts, str) and len(ts) >= 10:
            day = ts[:10]
        elif isinstance(ts, (float, int)):
            day = datetime.fromtimestamp(float(ts), tz=timezone.utc).date().isoformat()
        rows.append({
            "source": "launch_queue_audit",
            "task_id": task_id,
            "runner": entry.get("runner") or "",
            "topic": entry.get("topic") or "",
            "model": entry.get("model") or "",
            "role": "unknown",
            "role_observed": False,
            "records": 1,
            "input_tokens": int(entry.get("usage_input_tokens") or 0),
            "output_tokens": int(entry.get("usage_output_tokens") or 0),
            "total_tokens": int(entry.get("usage_total_tokens") or 0),
            "cached_input_tokens": int(entry.get("usage_cached_input_tokens") or 0),
            "cache_creation_input_tokens": int(entry.get("usage_cache_creation_input_tokens") or 0),
            "cache_write_input_tokens": int(entry.get("usage_cache_write_input_tokens") or 0),
            "cache_metrics_observed": bool(entry.get("usage_cache_metrics_observed")),
            "cost_usd": float(entry.get("cost_usd") or 0.0),
            "cost_known": not (
                int(entry.get("usage_total_tokens") or 0) > 0
                and float(entry.get("cost_usd") or 0.0) == 0.0
            ),
            "day": day,
        })
    return rows


_USAGE_FIELDS = (
    "input_tokens", "output_tokens", "visible_output_tokens",
    "reasoning_output_tokens", "total_tokens",
)
_CACHE_FIELDS = (
    "cached_input_tokens", "cache_creation_input_tokens",
    "cache_write_input_tokens",
)
_PROCESS_EVENTS_PATH = Path(".aiworkhub/runtime/process_logs/process_events.jsonl")
_PROCESS_EVENT_LIMIT = 10_000
# Newest canonical usage events one ledger build reads (task_store clamps it).
_USAGE_EVENT_LIMIT = 10_000


def _request_identity(entry: dict[str, Any]) -> str:
    note = str(entry.get("note") or "")
    return str(
        entry.get("request_id")
        or entry.get("attempt_id")
        or (note.removeprefix("task_mcp_request:") if note.startswith("task_mcp_request:") else "")
    )


def _repository_identity(
    entry: dict[str, Any],
    repo_root: Path | str,
    *,
    resolved_root: str | None = None,
) -> str:
    return str(
        entry.get("repository_id")
        or entry.get("repo_id")
        or entry.get("repository")
        or entry.get("repo_root")
        or (resolved_root if resolved_root is not None else Path(repo_root).resolve())
    )


def _event_telemetry_field_is_observed(event: dict[str, Any], field: str) -> bool:
    """Whether an event has durable evidence for one telemetry field."""

    value = event.get(field)
    if value is None:
        return False
    observed_flag = (
        "usage_observed"
        if field in _USAGE_FIELDS
        else "cache_metrics_observed"
        if field in _CACHE_FIELDS
        else "cost_observed"
    )
    return bool(event.get(observed_flag)) or bool(value)


def _merge_process_events(
    earlier: dict[str, Any], later: dict[str, Any]
) -> dict[str, Any]:
    """Keep later lifecycle metadata without discarding observed telemetry."""

    merged = {**earlier, **later}
    for field in (*_USAGE_FIELDS, *_CACHE_FIELDS, "cost_usd"):
        if (
            not _event_telemetry_field_is_observed(later, field)
            and field in earlier
        ):
            merged[field] = earlier[field]
    for observed_flag in (
        "usage_observed",
        "cache_metrics_observed",
        "cost_observed",
    ):
        if not bool(later.get(observed_flag)) and bool(earlier.get(observed_flag)):
            merged[observed_flag] = True
    return merged


def _process_event_index(
    repo_root: Path | str, *, resolved_root: str | None = None
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Read the bounded repository-local process ledger, ignoring bad records."""

    path = Path(repo_root) / _PROCESS_EVENTS_PATH
    if not path.is_file():
        return {}
    events: dict[tuple[str, str, str], dict[str, Any]] = {}
    try:
        with path.open(encoding="utf-8") as handle:
            # Append-only ledger: keep the newest lines, since usage rows are read newest-first.
            for line in deque(handle, maxlen=_PROCESS_EVENT_LIMIT):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                task_id = str(event.get("task_id") or "")
                request_id = _request_identity(event)
                repository = _repository_identity(
                    event, repo_root, resolved_root=resolved_root
                )
                if task_id and request_id and repository:
                    identity = (repository, task_id, request_id)
                    previous = events.get(identity)
                    events[identity] = (
                        _merge_process_events(previous, event)
                        if previous is not None
                        else event
                    )
    except OSError:
        return {}
    return events


def _matching_process_event(
    entry: dict[str, Any],
    repo_root: Path | str,
    process_events: dict[tuple[str, str, str], dict[str, Any]] | None = None,
    *,
    resolved_root: str | None = None,
) -> dict[str, Any] | None:
    task_id = str(entry.get("task_id") or "")
    request_id = _request_identity(entry)
    repository = _repository_identity(entry, repo_root, resolved_root=resolved_root)
    if not task_id or not request_id:
        return None
    candidates: list[dict[str, Any]] = []
    if process_events is not None:
        event = process_events.get((repository, task_id, request_id))
        if event is not None:
            candidates.append(event)
    nested = entry.get("process_events") or entry.get("process_event") or []
    if isinstance(nested, dict):
        nested = [nested]
    if isinstance(nested, list):
        candidates.extend(event for event in nested if isinstance(event, dict))
    for event in candidates:
        if (
            str(event.get("task_id") or "") != task_id
            or _request_identity(event) != request_id
            or _repository_identity(event, repo_root, resolved_root=resolved_root)
            != repository
        ):
            continue
        usage = event.get("usage")
        if isinstance(usage, dict):
            return {**event, **usage}
        return event
    return None


def _fill_process_usage(
    entry: dict[str, Any],
    repo_root: Path | str,
    process_events: dict[tuple[str, str, str], dict[str, Any]] | None = None,
    *,
    resolved_root: str | None = None,
) -> dict[str, Any]:
    """Fill unobserved placeholders only from an exactly matched process event."""

    event = _matching_process_event(
        entry, repo_root, process_events, resolved_root=resolved_root
    )
    if event is None:
        return entry
    merged = dict(entry)
    if not bool(entry.get("usage_observed")):
        for field in _USAGE_FIELDS:
            if not entry.get(field) and event.get(field) is not None:
                merged[field] = event[field]
        merged["usage_observed"] = bool(
            any(merged.get(field) for field in _USAGE_FIELDS)
            or event.get("usage_observed")
        )
    if not bool(entry.get("cache_metrics_observed")) and bool(
        event.get("cache_metrics_observed")
    ):
        for field in _CACHE_FIELDS:
            if not entry.get(field) and event.get(field) is not None:
                merged[field] = event[field]
        merged["cache_metrics_observed"] = True
    if not bool(entry.get("cost_observed")) and event.get("cost_observed") is True:
        merged["cost_usd"] = event.get("cost_usd") or 0.0
        merged["cost_observed"] = True
    if (
        str(entry.get("role") or "").strip().lower() not in {"worker", "reviewer"}
        and str(event.get("role") or "").strip().lower() in {"worker", "reviewer"}
    ):
        merged["role"] = event["role"]
    return merged


def _task_topic_index(repo_root: Path | str) -> dict[str, str]:
    """``{task_id: tasks.topic}`` from one bounded query, for topic-less rows.

    Built lazily and only when a usage event lacks its topic, so a healthy
    ledger never pays for it. ``list_tasks`` is capped at 5000 rows ordered
    by ``updated_at`` DESC; a task beyond that window stays unresolved rather
    than being guessed.
    """

    try:
        tasks = task_store.list_tasks(repo_root, status="all", limit=5000)
    except task_store.TaskStoreError:
        return {}
    return {
        str(task.get("task_id") or ""): str(task.get("topic") or "")
        for task in tasks
        if task.get("task_id")
    }


# One window over terminal_failure, reject_review AND claim_start rows. The
# claim_start rows were added for event-order attribution of legacy rejects
# (NF-2026-01989); measured on this repository: 2,660 claim_start events
# (core.py claim census) and 741 legacy request-id-less reject_review rows, so
# the combined population stays an order of magnitude under this bound.
_STORE_CAUSE_EVENT_LIMIT = 50_000

# terminal_cause_source of a reject attributed to the task's latest
# claim_start because its legacy payload carries no request_id.
_EVENT_ORDER_CAUSE_SOURCE = "task_store_event_order"


def _process_event_miss_reason(
    entry: dict[str, Any],
    indexed_requests: set[tuple[str, str]] | None,
) -> str:
    """Why one usage row found no process event (diagnostic, never a join)."""

    if not str(entry.get("task_id") or ""):
        return "no_task_id"
    if not _request_identity(entry):
        return "no_request_id"
    if indexed_requests is None:
        return "process_ledger_empty"
    pair = (str(entry.get("task_id") or ""), _request_identity(entry))
    return "repository_mismatch" if pair in indexed_requests else "no_process_event"


def _substatus_cause(substatus: str) -> str:
    """The failure category of one terminal substatus, else the substatus itself.

    Uses the same classifier ``learning_commit`` does; a substatus the
    classifier cannot place keeps its own name rather than collapsing into
    ``inconclusive``, so it stays distinguishable from rows with no evidence.
    """

    disposition = terminal_failure_classification.failure_disposition_from_substatus(
        terminal_substatus=substatus
    )
    category = terminal_failure_classification.failure_category_projection(disposition)
    if category == terminal_failure_classification.FAILURE_CATEGORY_INCONCLUSIVE:
        return substatus
    return category


def _task_store_terminal_causes(
    repo_root: Path | str, wanted: set[tuple[str, str]]
) -> tuple[dict[tuple[str, str], tuple[str, str]], str]:
    """``{(task_id, request_id): (cause, source)}`` from ONE bulk read.

    ``terminal_failure`` events carry the request's terminal substatus and
    ``reject_review`` events the manager-pinned ``failure_category``. A legacy
    ``reject_review`` (written before NF-00638 pinned ``request_id``) carries
    neither; it is attributed, in ascending ``event_id`` order, to the request
    of the task's latest preceding ``claim_start`` as
    ``reject:<failure_category or terminal_disposition>`` (else
    ``reject:unclassified``) with source ``task_store_event_order``. A reject
    before any claim_start of its task attributes nothing. Precedence:
    terminal substatus > explicit-request reject > event-order reject. Only
    scalar JSON fields cross the SQL boundary. A failed read returns no causes
    and a named reason instead of raising.
    """

    wanted_tasks = {task_id for task_id, _request in wanted}
    try:
        _readiness, db_path = task_store._require_ready(repo_root)
        conn = task_store._connect(db_path, readonly=True)
        try:
            rows = conn.execute(
                "SELECT event_id, task_id, event, "
                "json_extract(payload_json, '$.request_id') AS request_id, "
                "json_extract(payload_json, '$.substatus') AS substatus, "
                "json_extract(payload_json, '$.failure_category') AS failure_category, "
                "CASE WHEN json_type(payload_json, '$.terminal_disposition') = 'text' "
                "THEN json_extract(payload_json, '$.terminal_disposition') END "
                "AS terminal_disposition "
                "FROM task_events "
                "WHERE event IN ('terminal_failure','reject_review','claim_start') "
                "AND json_valid(payload_json) ORDER BY event_id DESC LIMIT ?",
                (_STORE_CAUSE_EVENT_LIMIT,),
            ).fetchall()
        finally:
            conn.close()
        terminal: dict[tuple[str, str], str] = {}
        rejected: dict[tuple[str, str], str] = {}
        ordered: dict[tuple[str, str], str] = {}
        current_request: dict[str, str] = {}
        # The window is the newest rows; walk it oldest-first so the latest
        # claim_start before each reject names the request it judged, and a
        # later event for the same key overwrites an earlier one.
        for row in sorted(rows, key=lambda item: int(item["event_id"])):
            task_id = str(row["task_id"] or "")
            if task_id not in wanted_tasks:
                continue
            request_id = str(row["request_id"] or "").strip()
            event = row["event"]
            if event == "claim_start":
                if request_id:
                    current_request[task_id] = request_id
                continue
            if event == "terminal_failure":
                substatus = str(row["substatus"] or "").strip()
                if substatus and (task_id, request_id) in wanted:
                    terminal[(task_id, request_id)] = _substatus_cause(substatus)
                continue
            category = str(row["failure_category"] or "").strip()
            if request_id:
                if category and (task_id, request_id) in wanted:
                    rejected[(task_id, request_id)] = f"reject:{category}"
                continue
            claimed = current_request.get(task_id, "")
            if not claimed or (task_id, claimed) not in wanted:
                continue
            category = category or str(row["terminal_disposition"] or "").strip()
            ordered[(task_id, claimed)] = f"reject:{category or 'unclassified'}"
    except (task_store.TaskStoreError, sqlite3.Error, OSError, ValueError) as exc:
        return {}, f"task_store_unreadable:{type(exc).__name__}"
    causes = {key: (cause, _EVENT_ORDER_CAUSE_SOURCE) for key, cause in ordered.items()}
    causes.update({key: (cause, "task_store") for key, cause in rejected.items()})
    causes.update({key: (cause, "task_store") for key, cause in terminal.items()})
    return causes, ""


def _attribute_causes_from_task_store(
    rows: list[dict[str, Any]], repo_root: Path | str
) -> None:
    """Name the cause of rows the process ledger could not, from the task store.

    The store is read once for the whole build and answered by dict lookups.
    Rows with no evidence stay empty-caused and carry the reason.
    """

    pending: list[dict[str, Any]] = []
    for row in rows:
        if row["terminal_cause"]:
            row["terminal_cause_source"] = "process_event"
        else:
            pending.append(row)
    wanted = {
        (str(row["task_id"]), str(row["attempt_id"]))
        for row in pending
        if row["task_id"] and row["attempt_id"]
    }
    causes: dict[tuple[str, str], tuple[str, str]] = {}
    read_error = ""
    if wanted:
        causes, read_error = _task_store_terminal_causes(repo_root, wanted)
    for row in pending:
        cause, source = causes.get((str(row["task_id"]), str(row["attempt_id"])), ("", ""))
        if cause:
            row["terminal_cause"] = cause
            row["terminal_cause_source"] = source
            continue
        row["terminal_cause_source"] = "unknown"
        row["terminal_cause_unresolved"] = (
            "no_request_id"
            if not (row["task_id"] and row["attempt_id"])
            else read_error or "no_store_evidence"
        )


def _records_by_topic_source(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Record counts keyed by where each row's topic came from.

    ``usage_event`` is the writer's own field; ``process_event_join`` and
    ``task_join`` are read-side recoveries for rows the live writer left
    topic-less; ``unresolved`` rows still report as an empty topic. Rows from
    the legacy usage report or launch audit carry no label and are counted
    as ``unlabeled``.
    """

    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        source = str(row.get("topic_source") or "") or "unlabeled"
        counts[source] += int(row.get("records") or 0)
    return dict(sorted(counts.items()))


def _attempt_time(row: dict[str, Any]) -> str:
    """When the attempt happened: its recorded instant, else the ledger write.

    A backfilled row is written long after the run it accounts for; its
    ``created_at`` is the backfill instant. ``attempt_recorded_at`` is the
    run's own finish time carried in the payload, so ordering and day
    buckets prefer it whenever it is present.
    """

    return str(row.get("attempt_recorded_at") or row.get("created_at") or "")


def _usage_row_context(repo_root: Path | str) -> dict[str, Any]:
    """The per-build join inputs every canonical usage row reads.

    Resolves the repository root once per build instead of once per row that
    lacks an explicit repository id, indexes the bounded process ledger once,
    and leaves the task-topic index to be built lazily on the first topic-less
    row. A memoized ledger keeps this context so folding new usage events
    joins against exactly the inputs the cached rows were built from.
    """
    resolved_root = str(Path(repo_root).resolve())
    process_events = _process_event_index(repo_root, resolved_root=resolved_root)
    return {
        "resolved_root": resolved_root,
        "process_events": process_events,
        "indexed_requests": (
            {(key[1], key[2]) for key in process_events} if process_events else None
        ),
        "topic_index": None,
    }


def _canonical_usage_rows(repo_root: Path | str) -> list[dict[str, Any]]:
    return _canonical_rows_from_entries(
        repo_root,
        task_store.list_usage_events(repo_root, limit=_USAGE_EVENT_LIMIT),
        _usage_row_context(repo_root),
    )


def _canonical_rows_from_entries(
    repo_root: Path | str,
    raw_entries: Iterable[dict[str, Any]],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Canonical ledger rows for ``raw_entries`` (newest first), in order.

    Each row depends only on its own entry, ``context`` and the task store's
    terminal-cause events for its own (task, request) key, so rows built for
    a batch of newly appended events equal the rows a full rebuild would
    produce for them.
    """
    rows: list[dict[str, Any]] = []
    resolved_root = context["resolved_root"]
    process_events = context["process_events"]
    indexed_requests = context["indexed_requests"]
    for raw_entry in raw_entries:
        entry = _fill_process_usage(
            raw_entry, repo_root, process_events, resolved_root=resolved_root
        )
        created_at = str(entry.get("created_at") or "")
        attempt_recorded_at = str(entry.get("attempt_recorded_at") or "")
        attempt_at = attempt_recorded_at or created_at
        task_id = str(entry.get("task_id") or "")
        topic = str(entry.get("topic") or "")
        topic_source = "usage_event" if topic else ""
        # Resolve the attempt's process event once per row: both the topic
        # join and the terminal cause read this single lookup, which is a
        # dict hit against the already-indexed ledger.
        event = _matching_process_event(
            entry, repo_root, process_events, resolved_root=resolved_root
        )
        if not topic:
            # A live writer regression dropped the topic from usage_record
            # payloads; recover it from request-time identity first (the
            # process ledger row for the same task/request), then from the
            # task itself. Provenance stays visible: tokens are unchanged,
            # only the attribution key is joined.
            joined = str(event.get("topic") or "") if event is not None else ""
            if joined:
                topic, topic_source = joined, "process_event_join"
        if not topic:
            topic_index = context.get("topic_index")
            if topic_index is None:
                topic_index = _task_topic_index(repo_root)
                context["topic_index"] = topic_index
            joined = topic_index.get(task_id, "")
            topic, topic_source = joined, ("task_join" if joined else "unresolved")
        explicit_role = str(entry.get("role") or "").strip().lower()
        role = (
            explicit_role
            if explicit_role in {"worker", "reviewer"}
            else "reviewer" if topic == "quality_review" else "worker"
        )
        total_tokens = int(entry.get("total_tokens") or 0)
        cost_usd = float(entry.get("cost_usd") or 0.0)
        cost_observed = entry.get("cost_observed")
        usage_observed = entry.get("usage_observed")
        if usage_observed is None:
            usage_observed = bool(
                total_tokens
                or entry.get("cache_metrics_observed")
                or cost_observed
            )
        note = str(entry.get("note") or "")
        rows.append({
            "source": "canonical_usage_event",
            "task_id": task_id,
            "runner": str(entry.get("runner") or ""),
            "topic": topic,
            "topic_source": topic_source,
            "model": str(entry.get("model") or ""),
            "role": role,
            "role_observed": explicit_role in {"worker", "reviewer"},
            "requested_model": str(entry.get("requested_model") or ""),
            "observed_model": str(entry.get("observed_model") or ""),
            "model_observed": bool(entry.get("model_observed")),
            "provider": str(entry.get("provider") or ""),
            "records": int(entry.get("records") or 1),
            "input_tokens": int(entry.get("input_tokens") or 0),
            "output_tokens": int(entry.get("output_tokens") or 0),
            "visible_output_tokens": int(entry.get("visible_output_tokens") or 0),
            "reasoning_output_tokens": int(entry.get("reasoning_output_tokens") or 0),
            "total_tokens": total_tokens,
            "cached_input_tokens": int(entry.get("cached_input_tokens") or 0),
            "cache_creation_input_tokens": int(entry.get("cache_creation_input_tokens") or 0),
            "cache_write_input_tokens": int(entry.get("cache_write_input_tokens") or 0),
            "cache_metrics_observed": bool(entry.get("cache_metrics_observed")),
            "usage_observed": bool(usage_observed),
            "telemetry_reason": str(entry.get("telemetry_reason") or ""),
            "cost_usd": cost_usd,
            "cost_observed": bool(cost_observed),
            "cost_known": (
                bool(cost_observed)
                if cost_observed is not None
                else not (total_tokens > 0 and cost_usd == 0.0)
            ),
            "source_detail": str(entry.get("source") or ""),
            "note": note,
            "attempt_id": _request_identity(entry),
            # Why this attempt ended: the terminal substatus when the ledger
            # recorded one, else its final state. A retry's cause reads this
            # from the PREVIOUS attempt row.
            "terminal_cause": (
                str(event.get("terminal_substatus") or event.get("state") or "")
                if event is not None
                else ""
            ),
            "terminal_cause_source": "",
            "terminal_cause_unresolved": "",
            "process_event_miss_reason": (
                ""
                if event is not None
                else _process_event_miss_reason(entry, indexed_requests)
            ),
            # created_at stays the ledger write time for audit; the day
            # bucket and attempt ordering follow the attempt's own instant.
            "created_at": created_at,
            "attempt_recorded_at": attempt_recorded_at,
            "day": attempt_at[:10] if len(attempt_at) >= 10 else "",
        })
    _attribute_causes_from_task_store(rows, repo_root)
    return rows


def _aggregate(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "records": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "visible_output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "usage_observed_records": 0,
        "usage_unknown_records": 0,
        "cache_observed_records": 0,
        "cache_eligible_input_tokens": 0,
        "cache_hit_ratio_numerator": 0,
        "cost_usd": 0.0,
        "cost_known_records": 0,
        "cost_unknown_records": 0,
        "tokens_with_unknown_cost": 0,
    })
    for row in rows:
        bucket = str(row.get(key) or "unknown")
        out[bucket]["records"] += int(row.get("records") or 0)
        out[bucket]["input_tokens"] += int(row.get("input_tokens") or 0)
        out[bucket]["output_tokens"] += int(row.get("output_tokens") or 0)
        out[bucket]["visible_output_tokens"] += int(
            row.get("visible_output_tokens") or 0
        )
        out[bucket]["reasoning_output_tokens"] += int(
            row.get("reasoning_output_tokens") or 0
        )
        out[bucket]["total_tokens"] += int(row.get("total_tokens") or 0)
        out[bucket]["cached_input_tokens"] += int(row.get("cached_input_tokens") or 0)
        out[bucket]["cache_creation_input_tokens"] += int(
            row.get("cache_creation_input_tokens") or 0
        )
        out[bucket]["cache_write_input_tokens"] += int(
            row.get("cache_write_input_tokens") or 0
        )
        if row.get("usage_observed"):
            out[bucket]["usage_observed_records"] += int(row.get("records") or 0)
        else:
            out[bucket]["usage_unknown_records"] += int(row.get("records") or 0)
        if row.get("cache_metrics_observed"):
            out[bucket]["cache_observed_records"] += int(row.get("records") or 0)
            out[bucket]["cache_eligible_input_tokens"] += int(row.get("input_tokens") or 0)
            out[bucket]["cache_hit_ratio_numerator"] += int(row.get("cached_input_tokens") or 0)
        out[bucket]["cost_usd"] = round(out[bucket]["cost_usd"] + float(row.get("cost_usd") or 0.0), 6)
        if row.get("cost_known"):
            out[bucket]["cost_known_records"] += int(row.get("records") or 0)
        else:
            out[bucket]["cost_unknown_records"] += int(row.get("records") or 0)
            out[bucket]["tokens_with_unknown_cost"] += int(row.get("total_tokens") or 0)
    for aggregate in out.values():
        denominator = int(aggregate["cache_eligible_input_tokens"] or 0)
        numerator = int(aggregate.pop("cache_hit_ratio_numerator", 0) or 0)
        aggregate["cache_hit_ratio"] = (
            round(numerator / denominator, 6)
            if denominator > 0
            else None
        )
    return dict(sorted(out.items()))


def _model_outcome_matrix(
    usage_rows: list[dict[str, Any]],
    decisions: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Associate each task's latest recorded model attempt with its decision."""

    usage_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in usage_rows:
        task_id = str(row.get("task_id") or "")
        if task_id:
            usage_by_task[task_id].append(row)
    matrix: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "decided_tasks": 0,
        "accepted": 0,
        "rejected": 0,
        "acceptance_rate_percent": None,
        "usage_observed_tasks": 0,
        "cost_observed_tasks": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
    })
    unmatched_decisions = 0
    for task_id, decision_row in decisions.items():
        candidates = usage_by_task.get(task_id) or []
        if not candidates:
            unmatched_decisions += 1
            continue
        decision_at = str(decision_row.get("created_at") or "")
        eligible = [
            row
            for row in candidates
            if not decision_at
            or not _attempt_time(row)
            or _attempt_time(row) <= decision_at
        ]
        if not eligible:
            unmatched_decisions += 1
            continue
        usage = max(eligible, key=_attempt_time)
        model = str(
            usage.get("observed_model")
            or usage.get("model")
            or usage.get("requested_model")
            or "unknown"
        )
        bucket = matrix[model]
        decision = str(decision_row.get("decision") or "")
        bucket["decided_tasks"] += 1
        if decision == "accepted":
            bucket["accepted"] += 1
        elif decision == "rejected":
            bucket["rejected"] += 1
        if usage.get("usage_observed"):
            bucket["usage_observed_tasks"] += 1
            bucket["total_tokens"] += int(usage.get("total_tokens") or 0)
        if usage.get("cost_known"):
            bucket["cost_observed_tasks"] += 1
            bucket["cost_usd"] = round(
                float(bucket["cost_usd"]) + float(usage.get("cost_usd") or 0.0),
                6,
            )
    for bucket in matrix.values():
        decided = int(bucket["decided_tasks"] or 0)
        bucket["acceptance_rate_percent"] = (
            round(100.0 * int(bucket["accepted"] or 0) / decided, 1)
            if decided
            else None
        )
    return {
        "schema_id": "aiworkhub.model_outcome_matrix.v1",
        "association_only": True,
        "attribution": "latest_usage_attempt_at_or_before_latest_manager_decision",
        "models": dict(sorted(matrix.items())),
        "unmatched_decisions": unmatched_decisions,
    }


def _task_family(card: dict[str, Any]) -> str:
    """Return the explicit routing family recorded on a canonical card.

    Historical cards do not persist the workforce ``kinds`` argument.  The
    project-context task type is the closest durable authority and is mapped
    without inspecting prose.  Missing/unsupported values remain ``unknown``
    instead of being guessed from titles or topics.
    """

    if str(card.get("topic") or "").strip() == "quality_review":
        return "review"
    project_context = card.get("project_context")
    task_type = (
        str(project_context.get("task_type") or "").strip().lower()
        if isinstance(project_context, dict)
        else ""
    )
    return {
        "code": "code",
        "research": "research",
        "data_classification": "linguistic",
    }.get(task_type, "unknown")


def _task_risk(card: dict[str, Any]) -> str:
    risk = str(card.get("risk_tier") or "").strip().lower()
    return risk if risk in {"low", "medium", "high", "critical"} else "unknown"


def _economic_model_identity(row: dict[str, Any]) -> str:
    """Normalize only declared provider model aliases, never adapter names."""

    candidates = (
        row.get("observed_model"),
        row.get("model"),
        row.get("requested_model"),
    )
    for candidate in candidates:
        raw = str(candidate or "").strip()
        folded = raw.casefold()
        if not raw:
            continue
        for marker, canonical in _ECONOMIC_MODEL_ALIASES:
            if marker in folded:
                return canonical
        if folded not in {
            "claude_cli", "codex_cli", "vscode_lm", "glm_vscode_lm",
            "deepseek_vscode_lm", "deepseek_copilot_cli", "glm_copilot_cli",
        }:
            return raw
    return ""


def _economic_route_identity(row: dict[str, Any]) -> str:
    """Return the recorded adapter/transport authority for one usage row."""

    explicit = str(row.get("adapter_id") or "").strip()
    if explicit:
        return explicit
    provider = str(row.get("provider") or "").strip().lower()
    return {
        "claude": "claude_cli",
        "codex": "codex_cli",
        "deepseek_copilot": "deepseek_copilot_cli",
        "glm_copilot": "glm_copilot_cli",
    }.get(provider, provider or "unknown")


def cost_per_accepted_outcome_view(
    usage_rows: list[dict[str, Any]],
    decisions: dict[str, dict[str, str]],
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Join exact task cost and manager decisions on one matched population.

    A task is attributable to a model only when every worker attempt for that
    task reports the same non-empty model identity.  Reviewer usage is not
    charged to the worker model.  Any unknown-cost attempt makes that exact
    model/family bucket ``UNKNOWN``; zero accepted outcomes makes it
    ``UNMEASURED``.  Consequently neither missing telemetry nor a zero-dollar
    placeholder can rank as free.
    """

    card_by_task = {
        str(card.get("task_id") or ""): card
        for card in cards
        if str(card.get("task_id") or "")
    }
    usage_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in usage_rows:
        task_id = str(row.get("task_id") or "")
        if task_id and str(row.get("role") or "worker") == "worker":
            usage_by_task[task_id].append(row)

    buckets: dict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(lambda: {
        "matched_decided_tasks": 0,
        "accepted_outcomes": 0,
        "rejected_outcomes": 0,
        "attempt_records": 0,
        "cost_known_attempts": 0,
        "cost_unknown_attempts": 0,
        "total_cost_usd": 0.0,
        "total_tokens": 0,
    })
    unmatched_decisions = 0
    unknown_model_tasks = 0
    mixed_model_tasks = 0
    for task_id, decision_row in decisions.items():
        decision = str(decision_row.get("decision") or "").strip().lower()
        if decision not in {"accepted", "rejected"}:
            continue
        rows = usage_by_task.get(task_id) or []
        if not rows:
            unmatched_decisions += 1
            continue
        models = {_economic_model_identity(row) for row in rows}
        models.discard("")
        if not models:
            unknown_model_tasks += 1
            continue
        if len(models) != 1:
            mixed_model_tasks += 1
            continue
        route_ids = {_economic_route_identity(row) for row in rows}
        if len(route_ids) != 1:
            mixed_model_tasks += 1
            continue
        model = next(iter(models))
        route = next(iter(route_ids))
        family = _task_family(card_by_task.get(task_id) or {})
        risk = _task_risk(card_by_task.get(task_id) or {})
        bucket = buckets[(model, route, family, risk)]
        bucket["matched_decided_tasks"] += 1
        bucket[f"{decision}_outcomes"] += 1
        bucket["attempt_records"] += len(rows)
        for row in rows:
            bucket["total_tokens"] += int(row.get("total_tokens") or 0)
            if row.get("cost_known") is True:
                bucket["cost_known_attempts"] += 1
                bucket["total_cost_usd"] = round(
                    float(bucket["total_cost_usd"])
                    + float(row.get("cost_usd") or 0.0),
                    6,
                )
            else:
                bucket["cost_unknown_attempts"] += 1

    route_tree: dict[str, dict[str, dict[str, dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(dict))
    )
    for (model, route, family, risk), raw in sorted(buckets.items()):
        row = dict(raw)
        attempts = int(row["attempt_records"])
        accepted = int(row["accepted_outcomes"])
        unknown_cost = int(row["cost_unknown_attempts"])
        if unknown_cost:
            state = "UNKNOWN"
            reason = "one_or_more_matched_attempt_costs_unknown"
            cost_per_accepted: float | None = None
        elif accepted == 0:
            state = "UNMEASURED"
            reason = "no_accepted_outcome_in_matched_population"
            cost_per_accepted = None
        else:
            state = "MEASURED"
            reason = "complete_matched_cost_and_acceptance_population"
            cost_per_accepted = round(
                float(row["total_cost_usd"]) / accepted, 6
            )
        row.update({
            "state": state,
            "reason": reason,
            "cost_coverage": (
                round(int(row["cost_known_attempts"]) / attempts, 6)
                if attempts else None
            ),
            "acceptance_rate": (
                round(accepted / int(row["matched_decided_tasks"]), 6)
                if row["matched_decided_tasks"] else None
            ),
            "cost_per_accepted_outcome_usd": cost_per_accepted,
            "risk_partition": risk,
            "risk_evidence": (
                "explicit_task_card"
                if risk != "unknown"
                else "not_persisted_on_historical_task_card"
            ),
        })
        route_tree[model][route][family][risk] = row

    return {
        "schema_id": "aiworkhub.cost_per_accepted_outcome.v1",
        "mode": "advisory_only",
        "automatic_routing": False,
        "attribution": "single_worker_model_per_task_all_attempt_costs",
        "routes": {
            model: {
                route: {
                    family: dict(sorted(risks.items()))
                    for family, risks in sorted(families.items())
                }
                for route, families in sorted(model_routes.items())
            }
            for model, model_routes in sorted(route_tree.items())
        },
        "unmatched_decisions": unmatched_decisions,
        "unknown_model_tasks": unknown_model_tasks,
        "mixed_model_tasks": mixed_model_tasks,
        "claim_boundary": (
            "Measured rows are repository associations, not causal savings. "
            "Shadow or canary activation requires matched uncapped parity "
            "evidence partitioned by task family and risk."
        ),
    }


def _retry_economics(
    usage_rows: list[dict[str, Any]],
    decisions: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Measure durable repeated attempts without calling activity savings."""
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in usage_rows:
        task_id = str(row.get("task_id") or "")
        if task_id:
            by_task[task_id].append(row)
    retry_rows: list[dict[str, Any]] = []
    retried_tasks: set[str] = set()
    by_cause: dict[str, dict[str, Any]] = {}
    by_source: dict[str, int] = defaultdict(int)
    unknown_reasons: dict[str, int] = defaultdict(int)
    miss_reasons: dict[str, int] = defaultdict(int)
    for task_id, rows in by_task.items():
        # Order by when the attempt ran, not when its row was written: a
        # backfilled first attempt otherwise sorts after the live retries.
        ordered = sorted(rows, key=_attempt_time)
        if len(ordered) > 1:
            retried_tasks.add(task_id)
            for previous, retry in zip(ordered, ordered[1:]):
                retry_rows.append(retry)
                # A retry's cause is why the PREVIOUS attempt ended. Rows
                # built before the field existed stay cause-unknown rather
                # than raising: legacy callers must keep working.
                cause = str(previous.get("terminal_cause") or "") or "unknown"
                source = (
                    str(previous.get("terminal_cause_source") or "")
                    or ("process_event" if cause != "unknown" else "unknown")
                )
                by_source[source] += 1
                if cause == "unknown":
                    unknown_reasons[
                        str(previous.get("terminal_cause_unresolved") or "") or "unspecified"
                    ] += 1
                miss = str(previous.get("process_event_miss_reason") or "")
                if miss:
                    miss_reasons[miss] += 1
                bucket = by_cause.setdefault(
                    cause,
                    {"retry_records": 0, "retry_tokens": 0, "retry_cost_usd": 0.0},
                )
                bucket["retry_records"] += 1
                bucket["retry_tokens"] += int(retry.get("total_tokens") or 0)
                if retry.get("cost_known"):
                    bucket["retry_cost_usd"] += float(retry.get("cost_usd") or 0.0)
    accepted_retried = sum(
        1
        for task_id in retried_tasks
        if str((decisions.get(task_id) or {}).get("decision") or "") == "accepted"
    )
    return {
        "schema_id": "aiworkhub.retry_economics.v1",
        "association_only": True,
        "tasks_with_usage": len(by_task),
        "attempt_records": len(usage_rows),
        "tasks_with_retries": len(retried_tasks),
        "retry_records": len(retry_rows),
        "retry_record_rate_percent": (
            round(100.0 * len(retry_rows) / len(usage_rows), 1)
            if usage_rows else None
        ),
        "retry_tokens": sum(int(row.get("total_tokens") or 0) for row in retry_rows),
        "retry_cost_usd": round(
            sum(
                float(row.get("cost_usd") or 0.0)
                for row in retry_rows
                if row.get("cost_known")
            ),
            6,
        ),
        "retry_cost_unknown_records": sum(
            1 for row in retry_rows if not row.get("cost_known")
        ),
        "by_cause": {
            cause: {
                "retry_records": int(bucket["retry_records"]),
                "retry_tokens": int(bucket["retry_tokens"]),
                "retry_cost_usd": round(float(bucket["retry_cost_usd"]), 6),
            }
            for cause, bucket in sorted(by_cause.items())
        },
        "by_cause_source": dict(sorted(by_source.items())),
        "unknown_retry_records": by_cause.get("unknown", {}).get("retry_records", 0),
        "unknown_share_percent": (
            round(
                100.0 * by_cause.get("unknown", {}).get("retry_records", 0)
                / len(retry_rows),
                1,
            )
            if retry_rows else None
        ),
        "unknown_reasons": dict(sorted(unknown_reasons.items())),
        "process_event_miss_reasons": dict(sorted(miss_reasons.items())),
        "accepted_retried_tasks": accepted_retried,
        "accepted_rate_among_retried_tasks_percent": (
            round(100.0 * accepted_retried / len(retried_tasks), 1)
            if retried_tasks else None
        ),
        "claim_boundary": (
            "Repeated usage records are measured attempts. They do not prove "
            "that the retry caused acceptance or that its tokens were avoidable."
        ),
    }


def _assemble_cost_ledger(
    *,
    repo_root: Path | str | None,
    runner: str | None,
    topic: str | None,
    status: str | None,
    include_tasks: bool,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ledger payload (without Claude Code sessions) from read inputs.

    A repo-bound build reads only ``inputs`` (see :func:`_read_ledger_inputs`)
    and copies every row and card it touches, so a memoized ``inputs`` is
    never mutated by the aggregation below.
    """
    if repo_root is not None:
        if inputs is None:
            inputs = _read_ledger_inputs(repo_root, through_event_id=None)
        usage_rows = [dict(row) for row in inputs["rows"]]
        manager_decisions = {
            task_id: dict(decision) if isinstance(decision, dict) else decision
            for task_id, decision in inputs["manager_decisions"].items()
        }
        if runner:
            usage_rows = [row for row in usage_rows if row.get("runner") == runner]
        if topic:
            usage_rows = [row for row in usage_rows if row.get("topic") == topic]
        if status:
            usage_rows = [row for row in usage_rows if row.get("status") == status]
        scoped_task_ids = {str(row.get("task_id") or "") for row in usage_rows}
        manager_decisions = {
            task_id: decision
            for task_id, decision in manager_decisions.items()
            if task_id in scoped_task_ids
        }
        # The persisted launch queue is process-global and carries no complete
        # repository identity.  Never mix it into an explicitly repo-bound
        # catalog; canonical usage events are the sole authority here.
        usage = {"ok": True}
        launch_summary = {"ok": True}
        launch_rows: list[dict[str, Any]] = []
    else:
        manager_decisions = {}
        usage = core.usage_report(runner=runner, topic=topic, status=status)
        usage_stdout = usage.get("stdout", "")
        usage_rows = _parse_usage_stdout(
            usage_stdout if isinstance(usage_stdout, str) else ""
        )
        launch_summary = launch_queue_persist.read_persisted_log(max_entries=10_000)
        launch_rows = _launch_rows(launch_summary)

    if repo_root is not None:
        # Every canonical usage event is a real attempt. Collapsing on
        # task/runner/model hides retries and under-reports their cost.
        union_rows = list(usage_rows)
    else:
        seen: set[tuple[str, str, str]] = set()
        union_rows = []
        for row in usage_rows + launch_rows:
            ident = (
                str(row.get("task_id") or ""),
                str(row.get("runner") or ""),
                str(row.get("model") or ""),
            )
            if ident in seen:
                continue
            seen.add(ident)
            union_rows.append(row)

    cards: list[Any] = (
        [dict(card) if isinstance(card, dict) else card for card in inputs["cards"]]
        if repo_root is not None and inputs is not None
        else []
    )
    accepted_economics = cost_per_accepted_outcome_view(
        usage_rows, manager_decisions, cards
    )

    result = {
        "tool": "aiworkhub_task_cost_ledger",
        "contract": "B288_v1_readonly_cost_ledger",
        "readonly": True,
        "filters": {"runner": runner, "topic": topic, "status": status},
        "counts": {
            "usage_rows": len(usage_rows),
            "launch_rows": len(launch_rows),
            "union_rows": len(union_rows),
        },
        "cost_quality": {
            "known_records": sum(int(row.get("records") or 0) for row in union_rows if row.get("cost_known")),
            "unknown_records": sum(int(row.get("records") or 0) for row in union_rows if not row.get("cost_known")),
            "tokens_with_unknown_cost": sum(
                int(row.get("total_tokens") or 0)
                for row in union_rows if not row.get("cost_known")
            ),
            "zero_cost_is_free": False,
            "reason": "provider_cost_absence_is_unknown_not_zero",
        },
        "cache_quality": {
            "observed_records": sum(
                int(row.get("records") or 0)
                for row in union_rows
                if row.get("cache_metrics_observed")
            ),
            "cached_input_tokens": sum(
                int(row.get("cached_input_tokens") or 0) for row in union_rows
            ),
            "cache_creation_input_tokens": sum(
                int(row.get("cache_creation_input_tokens") or 0) for row in union_rows
            ),
            "cache_write_input_tokens": sum(
                int(row.get("cache_write_input_tokens") or 0) for row in union_rows
            ),
            "absent_metrics_are_unknown_not_zero": True,
        },
        "role_quality": {
            "explicit_records": sum(
                int(row.get("records") or 0)
                for row in union_rows
                if row.get("role_observed")
            ),
            "legacy_inferred_records": sum(
                int(row.get("records") or 0)
                for row in union_rows
                if not row.get("role_observed")
            ),
            "legacy_inference": "quality_review_topic_is_reviewer_otherwise_worker",
        },
        "topic_quality": {
            "records_by_source": _records_by_topic_source(union_rows),
            "joined_topic_changes_attribution_not_tokens": True,
        },
        "aggregates": {
            "by_topic": _aggregate(union_rows, "topic"),
            "by_runner": _aggregate(union_rows, "runner"),
            "by_model": _aggregate(union_rows, "model"),
            "by_provider": _aggregate(union_rows, "provider"),
            "by_role": _aggregate(union_rows, "role"),
            "by_day": _aggregate(union_rows, "day"),
        },
        "model_outcomes": _model_outcome_matrix(usage_rows, manager_decisions),
        "cost_per_accepted_outcome": accepted_economics,
        "retry_economics": _retry_economics(usage_rows, manager_decisions),
        "tasks": union_rows if include_tasks else [],
        "authority_flags": {
            "runtime_authority": False,
            "support_authority": False,
            "queue_write": False,
            "audit_write": False,
            "process_launch": False,
            "agent_launch": False,
        },
        "source_status": {
            "usage_report_ok": bool(usage.get("ok")),
            "launch_log_ok": bool(launch_summary.get("ok")),
        },
    }
    return result


def _normalize_claude_code_model_id(model: str) -> str:
    """Normalize a raw Claude Code model id to its pricing-family prefix.

    Strips a trailing ``[1m]``-style bracket suffix and a trailing
    ``-YYYYMMDD`` date suffix, leaving the bare family id that the rate
    table below is matched against by prefix, never by exact equality.
    """
    raw = str(model or "").strip()
    raw = re.sub(r"\[[^\]]*\]\s*$", "", raw).strip()
    raw = re.sub(r"-\d{8}$", "", raw).strip()
    return raw


# The declared manager-seat rate table. This is the only token-price table
# in this file: worker/reviewer costs are never computed from a rate table,
# they are reported cost_usd observed at the provider, so there is nothing
# here to duplicate or reuse from that path.
_CLAUDE_CODE_MODEL_RATES_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-sonnet-5": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-opus-5": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    "claude-haiku-5": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
    "claude-haiku-4.5": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
}


def _claude_code_model_rates(model: str) -> dict[str, float] | None:
    """Match one normalized Claude Code model id to its rate row by prefix."""
    normalized = _normalize_claude_code_model_id(model)
    for prefix, rates in _CLAUDE_CODE_MODEL_RATES_USD_PER_MTOK.items():
        if normalized.startswith(prefix):
            return rates
    return None


def _claude_code_session_usd(session: dict[str, Any]) -> float | str:
    """Price one Claude Code session from its stored usage, or ``"UNKNOWN"``.

    A session that touched more than one model, or a model whose normalized
    family is absent from the rate table above, never gets a guessed price.
    """
    models = session.get("models") or []
    if len(models) != 1:
        return "UNKNOWN"
    rates = _claude_code_model_rates(models[0])
    if rates is None:
        return "UNKNOWN"
    usd = (
        int(session.get("input_tokens") or 0) * rates["input"]
        + int(session.get("output_tokens") or 0) * rates["output"]
        + int(session.get("cache_read_input_tokens") or 0) * rates["cache_read"]
        + int(session.get("cache_creation_input_tokens") or 0) * rates["cache_write"]
    ) / 1_000_000.0
    return round(usd, 6)


def _price_claude_code_sessions(section: dict[str, Any]) -> dict[str, Any]:
    """Attach per-session USD, a manager_seat total, top sessions and a per-day series.

    Never merged into worker/reviewer totals; an unpriceable model marks
    ``partial_coverage`` on the total rather than guessing a price. Days
    where every session is unpriced are still emitted, with ``usd`` null.
    """
    sessions = section.get("sessions") or []
    priced_sessions = []
    total_usd = 0.0
    known = 0
    unknown = 0
    by_day: dict[str, float] = defaultdict(float)
    by_day_unknown: dict[str, int] = defaultdict(int)
    for row in sessions:
        usd = _claude_code_session_usd(row)
        priced = dict(row)
        priced["usd"] = usd
        priced_sessions.append(priced)
        day = str(row.get("last_at") or "")[:10]
        if isinstance(usd, (int, float)):
            total_usd += usd
            known += 1
            if day:
                by_day[day] += usd
        else:
            unknown += 1
            if day:
                by_day_unknown[day] += 1
    top_sessions = sorted(
        (row for row in priced_sessions if isinstance(row.get("usd"), (int, float))),
        key=lambda row: row["usd"],
        reverse=True,
    )[:10]
    all_days = sorted(set(by_day) | set(by_day_unknown))
    per_day_series = [
        {
            "day": day,
            "usd": round(by_day[day], 6) if day in by_day else None,
            "unknown_sessions": by_day_unknown.get(day, 0),
        }
        for day in all_days
    ]
    section_out = dict(section)
    section_out["sessions"] = priced_sessions
    section_out["pricing"] = {
        "role": "manager_seat",
        "total_usd": round(total_usd, 6),
        "known_sessions": known,
        "unknown_sessions": unknown,
        "partial_coverage": unknown > 0,
        "top_sessions_by_usd": top_sessions,
        "per_day_usd": per_day_series,
    }
    return section_out


def build_cost_ledger(
    *,
    repo_root: Path | str | None = None,
    runner: str | None = None,
    topic: str | None = None,
    status: str | None = None,
    include_tasks: bool = False,
) -> dict[str, Any]:
    if repo_root is None:
        return _assemble_cost_ledger(
            repo_root=None,
            runner=runner,
            topic=topic,
            status=status,
            include_tasks=include_tasks,
        )
    result = _memoized_cost_ledger(
        repo_root,
        runner=runner,
        topic=topic,
        status=status,
        include_tasks=include_tasks,
    )
    # Read-only surfacing of Claude Code's own transcript usage; never
    # touches usage_rows/aggregates above, so existing attribution holds. It
    # carries its own bounded TTL memo, so it is never frozen in this one.
    raw_sessions = claude_code_usage.collect_claude_code_usage(repo_root)
    priced_sessions = _price_claude_code_sessions(raw_sessions)
    result["claude_code_sessions"] = priced_sessions
    manager_pricing = priced_sessions.get("pricing", {}) or {}
    manager_seat_usd = manager_pricing.get("total_usd", 0.0)
    manager_unknown_sessions = int(manager_pricing.get("unknown_sessions") or 0)
    by_role = result.get("aggregates", {}).get("by_role", {})
    worker_reviewer_usd = round(
        sum(
            float(by_role.get(role, {}).get("cost_usd") or 0.0)
            for role in ("worker", "reviewer")
        ),
        6,
    )
    # manager_seat is never silently folded into worker/reviewer totals; the
    # grand total is an explicit, separately labelled addition. Either total
    # carries a partial-coverage flag whenever any manager-seat session
    # could not be priced.
    result["cost_totals"] = {
        "worker_reviewer_usd": worker_reviewer_usd,
        "manager_seat_usd": manager_seat_usd,
        "grand_total_usd": round(worker_reviewer_usd + manager_seat_usd, 6),
        "manager_seat_separately_labelled": True,
        "priced_complete": manager_unknown_sessions == 0,
        "unknown_sessions": manager_unknown_sessions,
    }
    return result


# --- Per-process memo of the repo-bound ledger (NF-2026-02067) ---------------
#
# A dashboard poll rebuilt the whole ledger (every usage event decoded, the
# process ledger and terminal causes re-joined) even when nothing changed.
# The memo is keyed by the usage-event high-water mark plus a fingerprint of
# everything else the ledger joins against. An unchanged key answers from the
# cache; appended usage events with an unchanged fingerprint are folded in
# (only the new events are read and joined); any other change rebuilds. It is
# bounded (LRU over repositories, and over filter combinations per
# repository), measured (LEDGER_CACHE_STATS) and never holds its lock during
# IO. A ledger whose marks cannot be read is built uncached, never served
# stale.
_LEDGER_MEMO_MAX_REPOSITORIES = 8
_LEDGER_MEMO_MAX_RESULTS = 8
_LEDGER_MEMO_LOCK = threading.Lock()
_LEDGER_MEMO: OrderedDict[str, dict[str, Any]] = OrderedDict()
LEDGER_CACHE_STATS: dict[str, int] = {
    "hits": 0,
    "filter_misses": 0,
    "incremental_folds": 0,
    "full_builds": 0,
    "uncached_builds": 0,
}
# The task-store readers a ledger build goes through. A cache entry is valid
# only for the exact reader set that produced it, and only the canonical set
# may fold events read through ``list_usage_event_window``.
_LEDGER_READER_NAMES = (
    "list_usage_events",
    "list_usage_event_window",
    "cost_ledger_marks",
    "latest_manager_decisions",
    "list_task_routing_facts",
    "list_tasks",
    "list_task_cards",
    "get_task",
    "_decode_task_card",
    "_connect",
    "_require_ready",
)


def _ledger_readers() -> tuple[Any, ...]:
    return tuple(getattr(task_store, name, None) for name in _LEDGER_READER_NAMES)


_CANONICAL_LEDGER_READERS = _ledger_readers()


def reset_cost_ledger_cache() -> None:
    """Drop every memoized ledger and zero the cache counters."""

    with _LEDGER_MEMO_LOCK:
        _LEDGER_MEMO.clear()
        for name in LEDGER_CACHE_STATS:
            LEDGER_CACHE_STATS[name] = 0


def _count_ledger_cache(event: str) -> None:
    with _LEDGER_MEMO_LOCK:
        LEDGER_CACHE_STATS[event] += 1


def _process_ledger_stamp(repo_root: Path | str) -> tuple[Any, ...]:
    path = Path(repo_root) / _PROCESS_EVENTS_PATH
    try:
        stat = path.stat()
    except OSError:
        return (str(_PROCESS_EVENTS_PATH), "absent")
    return (str(_PROCESS_EVENTS_PATH), stat.st_size, stat.st_mtime_ns)


def _read_ledger_inputs(
    repo_root: Path | str, *, through_event_id: int | None
) -> dict[str, Any]:
    """Every store-derived input of one repo-bound ledger build.

    ``through_event_id`` bounds the usage events read to the high-water mark
    the cache key records, so a cached entry never holds an event newer than
    its key (which a later fold would then count twice).
    """

    context = _usage_row_context(repo_root)
    if through_event_id is None:
        entries = task_store.list_usage_events(repo_root, limit=_USAGE_EVENT_LIMIT)
    else:
        entries = [
            entry
            for _event_id, entry in task_store.list_usage_event_window(
                repo_root, through_event_id=through_event_id, limit=_USAGE_EVENT_LIMIT
            )
        ]
    rows = _canonical_rows_from_entries(repo_root, entries, context)
    try:
        manager_decisions = task_store.latest_manager_decisions(repo_root)
    except task_store.TaskStoreError:
        manager_decisions = {}
    try:
        cards = task_store.list_task_routing_facts(repo_root, limit=5000)
    except task_store.TaskStoreError:
        cards = []
    return {
        "rows": rows,
        "context": context,
        "manager_decisions": manager_decisions,
        "cards": cards,
    }


def _fold_usage_events(
    repo_root: Path | str,
    inputs: dict[str, Any],
    *,
    after_event_id: int,
    through_event_id: int,
    expected_new: int,
) -> dict[str, Any] | None:
    """Fold only the usage events in ``(after, through]`` into cached inputs.

    New events are strictly newer than every cached row, so the newest
    ``_USAGE_EVENT_LIMIT`` rows of ``new + cached`` are exactly the rows a full
    rebuild reads. Returns ``None`` (caller rebuilds) when the window does not
    hold exactly ``expected_new`` events -- a deletion or a burst beyond the
    read bound is never folded.
    """

    if expected_new <= 0 or expected_new > _USAGE_EVENT_LIMIT:
        return None
    window = task_store.list_usage_event_window(
        repo_root,
        after_event_id=after_event_id,
        through_event_id=through_event_id,
        limit=_USAGE_EVENT_LIMIT,
    )
    if len(window) != expected_new:
        return None
    new_rows = _canonical_rows_from_entries(
        repo_root, [entry for _event_id, entry in window], inputs["context"]
    )
    return {**inputs, "rows": (new_rows + inputs["rows"])[:_USAGE_EVENT_LIMIT]}


def _copy_ledger(result: dict[str, Any]) -> dict[str, Any]:
    """An independent copy a caller may mutate without touching the memo."""

    copied: dict[str, Any] = {}
    for key, value in result.items():
        if key == "tasks":
            # Rows hold scalars only; a per-row dict copy is a full copy.
            copied[key] = [dict(row) for row in value]
        else:
            copied[key] = copy.deepcopy(value)
    return copied


def _memoized_cost_ledger(
    repo_root: Path | str,
    *,
    runner: str | None,
    topic: str | None,
    status: str | None,
    include_tasks: bool,
) -> dict[str, Any]:
    filters = (runner, topic, status, bool(include_tasks))
    readers = _ledger_readers()
    canonical = readers == _CANONICAL_LEDGER_READERS
    try:
        marks = task_store.cost_ledger_marks(repo_root)
        root_key = str(Path(repo_root).resolve())
    except Exception:
        # Unreadable marks: no key can prove freshness, so build uncached.
        _count_ledger_cache("uncached_builds")
        return _assemble_cost_ledger(
            repo_root=repo_root,
            runner=runner,
            topic=topic,
            status=status,
            include_tasks=include_tasks,
            inputs=_read_ledger_inputs(repo_root, through_event_id=None),
        )
    usage_mark = (int(marks["usage_high_water"]), int(marks["usage_count"]))
    fingerprint = (
        marks["store_fingerprint"],
        _process_ledger_stamp(repo_root),
        readers,
    )
    cached: dict[str, Any] | None = None
    fresh_entry: dict[str, Any] | None = None
    with _LEDGER_MEMO_LOCK:
        entry = _LEDGER_MEMO.get(root_key)
        if entry is not None:
            _LEDGER_MEMO.move_to_end(root_key)
            if entry["fingerprint"] == fingerprint and entry["usage"] == usage_mark:
                fresh_entry = entry
                cached = entry["results"].get(filters)
                if cached is not None:
                    LEDGER_CACHE_STATS["hits"] += 1
                    entry["results"].move_to_end(filters)
    if cached is not None:
        return _copy_ledger(cached)
    results: OrderedDict[tuple[Any, ...], dict[str, Any]]
    if fresh_entry is not None:
        _count_ledger_cache("filter_misses")
        inputs = fresh_entry["inputs"]
        results = fresh_entry["results"]
    else:
        folded: dict[str, Any] | None = None
        if (
            entry is not None
            and canonical
            and entry["fingerprint"] == fingerprint
            and usage_mark[0] > entry["usage"][0]
        ):
            folded = _fold_usage_events(
                repo_root,
                entry["inputs"],
                after_event_id=entry["usage"][0],
                through_event_id=usage_mark[0],
                expected_new=usage_mark[1] - entry["usage"][1],
            )
        if folded is not None:
            _count_ledger_cache("incremental_folds")
            inputs = folded
        else:
            _count_ledger_cache("full_builds")
            inputs = _read_ledger_inputs(
                repo_root, through_event_id=usage_mark[0] if canonical else None
            )
        results = OrderedDict()
    result = _assemble_cost_ledger(
        repo_root=repo_root,
        runner=runner,
        topic=topic,
        status=status,
        include_tasks=include_tasks,
        inputs=inputs,
    )
    with _LEDGER_MEMO_LOCK:
        results[filters] = result
        results.move_to_end(filters)
        while len(results) > _LEDGER_MEMO_MAX_RESULTS:
            results.popitem(last=False)
        _LEDGER_MEMO[root_key] = {
            "fingerprint": fingerprint,
            "usage": usage_mark,
            "inputs": inputs,
            "results": results,
        }
        _LEDGER_MEMO.move_to_end(root_key)
        while len(_LEDGER_MEMO) > _LEDGER_MEMO_MAX_REPOSITORIES:
            _LEDGER_MEMO.popitem(last=False)
    return _copy_ledger(result)
