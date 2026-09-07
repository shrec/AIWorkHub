"""Bounded, read-only, canonical-store-backed history series.

``dashboard.kpi_analytics`` is computed over the LIVE PROCESS REPORT, so its
own window reports ``observed_runs: 37`` and ``truncated: true`` while the
canonical store holds 6,232 terminal outcomes across 44 distinct days. A
charts page fed from that projection draws 37 runs instead of 44 days.

This module reads the canonical store directly -- read-only, bounded and
cached -- and ships the series a charts page needs. It is the third fix for
one root cause (after ``read_efficiency_telemetry`` aggregating 14 live rows
and the accept/reject KPI counting events instead of distinct cards): the
history is in ``.aiworkhub/tasking/task_queue.sqlite`` and the projection
could not reach it.

Three rules run through every series here.

**Honest denominators.** An unmeasured value is ``None``, never ``0``. Only
1,319 of 6,511 usage records carry an observed cost and only 1,018 of 4,628
cards carry a ``risk_tier``, so every series that can be partially unknown
ships its own coverage block and the
``absent_metrics_are_unknown_not_zero`` label this codebase already uses.

**Reviewer children are never work.** ``topic='quality_review'`` cards are
machine lifecycle. 529 of 870 lifetime accepts were reviewer children, which
manufactured an apparent acceptance collapse that never happened. They are
reported as their own labelled series and are never summed into a work-card
numerator or denominator -- the same rule ``dashboard.work_card_outcomes``
already applies.

**Resolution goes to the problematic metrics.** A success needs one number;
a failure needs a taxonomy. ``review_ready`` is the baseline -- what happens
when nothing goes wrong -- and one count is enough for it. The remaining
41.3% of outcomes is the entire actionable signal and keeps full
per-substatus resolution. Where a bound forces a cut, the cut is taken from
the TOP of the distribution and named; a silent top-N would hide exactly the
rare classes worth learning from.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Mapping

from aiworkhub import sqlite_readonly, task_store


HISTORY_SERIES_SCHEMA_ID = "aiworkhub.dashboard.history_series.v1"

# Reviewer children are launched by the quality-review machinery to review a
# work card. ``cost_ledger`` treats this topic as the reviewer role and
# ``dashboard.WORK_CARD_EXCLUDED_TOPICS`` already excludes it from the
# accept/reject series; the history series applies the identical rule.
EXCLUDED_TOPICS = ("quality_review",)

# The outcome that means "nothing went wrong". It is 58.7% of all terminal
# outcomes and is reported as a single baseline count; every other substatus
# keeps full per-day, per-population resolution.
BASELINE_SUBSTATUS = "review_ready"

# Default history window. The store holds ~44 distinct days of events; this
# covers all of it while still bounding any future growth.
DEFAULT_WINDOW_DAYS = 180
# Hard ceiling on returned day buckets, independent of the window.
MAX_DAY_BUCKETS = 400
# Ceiling on distinct substatus classes returned. Above this the cut is taken
# from the most common classes, never the rare tail (see ``_bounded_taxonomy``).
MAX_SUBSTATUS_CLASSES = 40
# Ceiling on distinct model/runner rows in the per-model outcome series.
MAX_MODEL_ROWS = 60
# Cached payloads are reused while the canonical DB is byte-identical, and in
# any case for no longer than this.
CACHE_TTL_SECONDS = 60.0

_POSITIVE_INFINITY_DAYS = 10_000

# A card's topic lives in the ``tasks`` row, falling back to the stored card
# JSON -- the same expression ``task_store.list_tasks`` and
# ``dashboard._TASK_TOPIC_SQL`` use. ``topic`` is non-empty for every live
# row, so the ``json_extract`` fallback is a correctness guard that costs
# nothing in practice (COALESCE evaluates it only when ``topic`` is empty).
_TOPIC_SQL = "COALESCE(NULLIF(topic, ''), json_extract(card_json, '$.topic'), '')"

# One materialized pass over ``tasks`` classifying every card. Materializing
# is load-bearing: probing the expression per event row costs 542 ms, the
# materialized CTE 72 ms on the live 358 MB store.
_CARD_POPULATION_CTE = f"""
card_population AS MATERIALIZED (
    SELECT
        task_id,
        runner,
        CASE WHEN {_TOPIC_SQL} = ? THEN 'reviewer_child' ELSE 'work_card' END
            AS population
    FROM tasks
)
"""

# A decision event whose ``tasks`` row is gone cannot have its population
# established; it is reported as ``unknown_topic`` and never folded into work.
_POPULATION_EXPR = "COALESCE(card_population.population, 'unknown_topic')"

# ---------------------------------------------------------------------------
# The exact queries behind every series. They are shipped in the payload under
# ``queries`` so a reviewer can re-derive any number without reading this file.
# ---------------------------------------------------------------------------

# Series 1, 3 and the cause breakdown all come from this single scan of the
# terminal outcomes. ``nothing_measured`` is the cause field: it says whether
# the deterministic evidence verdict measured anything at all, which turns
# "a validation failed" into "a validation failed and nothing was measured".
_Q_TERMINAL_OUTCOMES = f"""
WITH {_CARD_POPULATION_CTE}
SELECT
    substr(e.created_at, 1, 10) AS day,
    {_POPULATION_EXPR} AS population,
    COALESCE(json_extract(e.payload_json, '$.substatus'), 'unknown_substatus')
        AS substatus,
    CASE
        WHEN json_extract(
            e.payload_json,
            '$.deterministic_verification.evidence_verdict.nothing_measured'
        ) = 1 THEN 'nothing_measured'
        WHEN json_extract(
            e.payload_json,
            '$.deterministic_verification.evidence_verdict.nothing_measured'
        ) = 0 THEN 'evidence_measured'
        ELSE 'verdict_absent'
    END AS evidence_cause,
    COUNT(*) AS events
FROM task_events AS e
LEFT JOIN card_population ON card_population.task_id = e.task_id
WHERE e.event IN ('terminal_review', 'terminal_failure')
  AND e.created_at >= ?
GROUP BY day, population, substatus, evidence_cause
"""

# Series 2: one vote per DISTINCT card per day -- its latest decision that
# day. A card rejected 46 times in one day is one card.
_Q_DAILY_DECISIONS = f"""
WITH {_CARD_POPULATION_CTE},
decision_events AS (
    SELECT task_id, event_id, event, substr(created_at, 1, 10) AS day
    FROM task_events
    WHERE event IN ('accept_review', 'reject_review')
      AND created_at >= ?
),
latest_per_card_day AS (
    SELECT
        task_id,
        day,
        event,
        ROW_NUMBER() OVER (
            PARTITION BY task_id, day ORDER BY event_id DESC
        ) AS rn
    FROM decision_events
)
SELECT
    d.day,
    {_POPULATION_EXPR} AS population,
    d.event,
    COUNT(DISTINCT d.task_id) AS cards,
    COUNT(*) AS card_days
FROM latest_per_card_day AS d
LEFT JOIN card_population ON card_population.task_id = d.task_id
WHERE d.rn = 1
GROUP BY d.day, population, d.event
"""

# Series 4: tokens and cost by day, role, model and transport. ``cost_usd`` is
# summed ONLY over records that declare ``cost_observed``; the unobserved
# records are counted separately so the series can state its own coverage.
# ``zero_token_records`` is the telemetry-absence class: a transport that
# reports no tokens at all is a problem metric, not a free run.
_Q_USAGE = """
SELECT
    substr(created_at, 1, 10) AS day,
    COALESCE(json_extract(payload_json, '$.role'), '') AS role,
    COALESCE(json_extract(payload_json, '$.model'), '') AS model,
    COALESCE(json_extract(payload_json, '$.provider'), '') AS provider,
    COALESCE(json_extract(payload_json, '$.adapter_id'), '') AS adapter_id,
    COUNT(*) AS records,
    SUM(COALESCE(json_extract(payload_json, '$.total_tokens'), 0)) AS total_tokens,
    SUM(COALESCE(json_extract(payload_json, '$.input_tokens'), 0)) AS input_tokens,
    SUM(COALESCE(json_extract(payload_json, '$.output_tokens'), 0)) AS output_tokens,
    SUM(CASE WHEN json_extract(payload_json, '$.cost_observed') = 1
             THEN 1 ELSE 0 END) AS cost_observed_records,
    SUM(CASE WHEN json_extract(payload_json, '$.cost_observed') = 1
             THEN COALESCE(json_extract(payload_json, '$.cost_usd'), 0.0)
             ELSE 0.0 END) AS cost_usd_observed,
    SUM(CASE WHEN COALESCE(json_extract(payload_json, '$.total_tokens'), 0) = 0
             THEN 1 ELSE 0 END) AS zero_token_records
FROM task_events
WHERE event = 'usage_record' AND created_at >= ?
GROUP BY day, role, model, provider, adapter_id
"""

# Series 5a: retry share of tokens. Every usage record is a real attempt;
# attempt 1 is the first try and everything after it is a retry.
_Q_RETRY_TOKENS = """
WITH attempts AS (
    SELECT
        task_id,
        COALESCE(json_extract(payload_json, '$.total_tokens'), 0) AS tokens,
        CASE WHEN json_extract(payload_json, '$.cost_observed') = 1
             THEN COALESCE(json_extract(payload_json, '$.cost_usd'), 0.0)
             ELSE 0.0 END AS cost_observed_usd,
        CASE WHEN json_extract(payload_json, '$.cost_observed') = 1
             THEN 1 ELSE 0 END AS cost_observed,
        ROW_NUMBER() OVER (
            PARTITION BY task_id ORDER BY event_id
        ) AS attempt_index
    FROM task_events
    WHERE event = 'usage_record' AND created_at >= ?
)
SELECT
    CASE WHEN attempt_index = 1 THEN 'first_attempt' ELSE 'retry' END AS kind,
    COUNT(*) AS records,
    COUNT(DISTINCT task_id) AS tasks,
    SUM(tokens) AS total_tokens,
    SUM(cost_observed) AS cost_observed_records,
    SUM(cost_observed_usd) AS cost_usd_observed
FROM attempts
GROUP BY kind
"""

# Series 5b: rejection depth per DISTINCT card, and whether a card at that
# depth was ever accepted. This is the eventual-acceptance rate conditioned
# on how many times a card was sent back.
_Q_REJECTION_DEPTH = f"""
WITH {_CARD_POPULATION_CTE},
per_card AS (
    SELECT
        task_id,
        SUM(CASE WHEN event = 'reject_review' THEN 1 ELSE 0 END) AS rejections,
        MAX(CASE WHEN event = 'accept_review' THEN 1 ELSE 0 END) AS ever_accepted
    FROM task_events
    WHERE event IN ('accept_review', 'reject_review')
    GROUP BY task_id
)
SELECT
    {_POPULATION_EXPR} AS population,
    per_card.rejections,
    COUNT(*) AS cards,
    SUM(per_card.ever_accepted) AS eventually_accepted
FROM per_card
LEFT JOIN card_population ON card_population.task_id = per_card.task_id
GROUP BY population, per_card.rejections
"""

# Series 6: accepted vs rejected DECIDED CARDS per runner -- one vote per
# distinct card, its latest decision, split by population.
_Q_MODEL_OUTCOMES = f"""
WITH {_CARD_POPULATION_CTE},
latest_decision AS (
    SELECT
        task_id,
        event,
        ROW_NUMBER() OVER (
            PARTITION BY task_id ORDER BY event_id DESC
        ) AS rn
    FROM task_events
    WHERE event IN ('accept_review', 'reject_review')
)
SELECT
    {_POPULATION_EXPR} AS population,
    COALESCE(card_population.runner, '') AS runner,
    latest_decision.event,
    COUNT(DISTINCT latest_decision.task_id) AS cards
FROM latest_decision
LEFT JOIN card_population ON card_population.task_id = latest_decision.task_id
WHERE latest_decision.rn = 1
GROUP BY population, runner, latest_decision.event
"""

# Series 7 and 8 share one scan of ``tasks``: queue latency
# (``created_at``->``started_at``), run latency
# (``started_at``->``completed_at``) and the risk tier. ``risk_tier`` is only
# derived at create as of today, so most historical cards carry none -- the
# series reports that coverage rather than defaulting them to a tier.
_Q_CARD_DIMENSIONS = f"""
SELECT
    CASE WHEN {_TOPIC_SQL} = ? THEN 'reviewer_child' ELSE 'work_card' END
        AS population,
    COALESCE(json_extract(card_json, '$.risk_tier'), '') AS risk_tier,
    CASE
        WHEN started_at IS NULL OR started_at = '' THEN NULL
        ELSE CAST((julianday(started_at) - julianday(created_at)) * 86400.0
                  AS INTEGER)
    END AS queue_seconds,
    CASE
        WHEN started_at IS NULL OR started_at = ''
             OR completed_at IS NULL OR completed_at = '' THEN NULL
        ELSE CAST((julianday(completed_at) - julianday(started_at)) * 86400.0
                  AS INTEGER)
    END AS run_seconds
FROM tasks
WHERE created_at >= ?
"""

HISTORY_SERIES_QUERIES: dict[str, str] = {
    "terminal_outcomes": _Q_TERMINAL_OUTCOMES,
    "daily_decisions": _Q_DAILY_DECISIONS,
    "usage": _Q_USAGE,
    "retry_tokens": _Q_RETRY_TOKENS,
    "rejection_depth": _Q_REJECTION_DEPTH,
    "model_outcomes": _Q_MODEL_OUTCOMES,
    "card_dimensions": _Q_CARD_DIMENSIONS,
}

_POPULATION_KEYS = ("work_card", "reviewer_child", "unknown_topic")

_POPULATION_NOTE = (
    "work_card excludes reviewer-child tasks with topic in "
    f"{list(EXCLUDED_TOPICS)}; they are reported as their own "
    "reviewer_child series and are never folded into the work-card "
    "numerator or denominator. unknown_topic is a decision whose tasks row "
    "is gone, so its population cannot be established; it is never counted "
    "as work."
)

_cache_lock = Lock()
_cache: dict[str, Any] = {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _rate(numerator: int | None, denominator: int | None) -> float | None:
    """A rate over an unmeasured or empty denominator is unknown, not zero."""
    if not numerator and not denominator:
        return None
    if not denominator:
        return None
    return round(float(numerator or 0) / float(denominator), 4)


def _bounded_taxonomy(
    counts: Mapping[str, int], limit: int
) -> tuple[dict[str, int], dict[str, Any]]:
    """Bound a class taxonomy by cutting the COMMON classes, never the tail.

    The rare terminal substatuses -- ``launch_failed`` 0.5%, ``timed_out``
    0.4%, ``liveness_lost`` 0.2%, ``scope_rejected`` 0.1%,
    ``output_budget_exceeded`` and ``token_budget_exceeded`` at 0.1% each --
    are precisely the classes worth learning from. A silent top-N bound would
    delete them and keep only the classes that are already obvious, so the
    sort is ASCENDING by count and any cut is taken from the top and named.
    """
    items = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]))
    if len(items) <= limit:
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))), {
            "applied": False,
            "classes": len(items),
            "omitted": [],
        }
    kept = items[:limit]
    cut = items[limit:]
    return dict(sorted(kept, key=lambda kv: (-kv[1], kv[0]))), {
        "applied": True,
        "classes": len(items),
        "returned": len(kept),
        "cut_from": "most_common",
        "reason": (
            "the rare failure classes carry the actionable signal, so a bound "
            "cuts the most common classes and names them here"
        ),
        "omitted": [
            {"substatus": name, "events": count}
            for name, count in sorted(cut, key=lambda kv: (-kv[1], kv[0]))
        ],
    }


def _percentiles(values: list[int]) -> dict[str, Any]:
    """Percentile block over a sorted sample; unknown when the sample is empty."""
    if not values:
        return {
            "measured": False,
            "reason": "no_observed_durations",
            "samples": 0,
            "p50_seconds": None,
            "p90_seconds": None,
            "p95_seconds": None,
            "max_seconds": None,
        }
    ordered = sorted(values)
    n = len(ordered)

    def pick(fraction: float) -> int:
        index = min(n - 1, max(0, int(round(fraction * (n - 1)))))
        return int(ordered[index])

    return {
        "measured": True,
        "samples": n,
        "p50_seconds": pick(0.50),
        "p90_seconds": pick(0.90),
        "p95_seconds": pick(0.95),
        "max_seconds": int(ordered[-1]),
    }


def _empty_population_counts() -> dict[str, int]:
    return {key: 0 for key in _POPULATION_KEYS}


def history_series_unmeasured(reason: str) -> dict[str, Any]:
    """Unmeasured history: every series unknown, explicitly not zero.

    An unready store, a failed read or an unavailable provider must never
    render as a measured history of zero events -- that reads as a collapse
    that never happened.
    """
    return {
        "schema_id": HISTORY_SERIES_SCHEMA_ID,
        "measured": False,
        "reason": str(reason or "unknown"),
        "absent_metrics_are_unknown_not_zero": True,
        "generated_at": _utc_now(),
        "excluded_topics": list(EXCLUDED_TOPICS),
        "population_note": _POPULATION_NOTE,
        "window": {
            "days": None,
            "since": None,
            "observed_days": None,
            "truncated": False,
        },
        "query_cost": {"measured": False, "total_ms": None, "by_query_ms": {}},
        "daily_outcomes": {"measured": False, "days": []},
        "daily_decisions": {"measured": False, "days": []},
        "terminal_composition": {"measured": False},
        "usage": {"measured": False},
        "retry_economics": {"measured": False},
        "model_outcomes": {"measured": False},
        "latency": {"measured": False},
        "risk_tier_distribution": {"measured": False},
        "queries": dict(HISTORY_SERIES_QUERIES),
    }


def _since_bound(window_days: int, now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return (moment - timedelta(days=window_days)).strftime("%Y-%m-%d")


def _cache_key(db_path: Path, window_days: int) -> tuple[Any, ...] | None:
    """Identity of the store contents, so a warm read never rescans it.

    ``task_events`` is append-only in normal operation, so ``(size, mtime_ns)``
    changes on every write. A stat that fails yields ``None``, which disables
    the cache rather than serving a payload that may be stale.
    """
    try:
        stat = db_path.stat()
    except OSError:
        return None
    return (str(db_path), stat.st_size, stat.st_mtime_ns, int(window_days))


def build_history_series(
    repo_root: Path | str | None = None,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Build every history series from the canonical store, read-only.

    The store is 358 MB with 42,233 events, so a snapshot refresh must not
    full-scan it every call. Seven bounded queries cost ~1.0 s cold on the
    live repository and the result is cached against the database's
    ``(size, mtime_ns)``, so an unchanged store answers from memory.
    """
    window_days = max(1, min(int(window_days or DEFAULT_WINDOW_DAYS), 3650))

    readiness = task_store.storage_readiness(repo_root)
    if not getattr(readiness, "ready", False):
        return history_series_unmeasured(
            str(getattr(readiness, "reason", "storage_not_ready"))
        )

    db_path = Path(task_store.canonical_db_path(repo_root))
    key = _cache_key(db_path, window_days) if use_cache else None
    if key is not None:
        with _cache_lock:
            entry = _cache.get("entry")
            if (
                entry is not None
                and entry["key"] == key
                and (time.monotonic() - entry["stored_at"]) < CACHE_TTL_SECONDS
            ):
                payload = dict(entry["payload"])
                cost = dict(payload.get("query_cost") or {})
                cost["cache"] = "hit"
                payload["query_cost"] = cost
                return payload

    since = _since_bound(window_days, now)
    reviewer_topic = EXCLUDED_TOPICS[0]
    timings: dict[str, float] = {}

    try:
        conn = sqlite_readonly.connect_readonly(db_path)
    except (sqlite3.Error, OSError) as exc:
        return history_series_unmeasured(f"store_unreadable:{type(exc).__name__}")

    def run(name: str, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        started = time.perf_counter()
        rows = conn.execute(sql, params).fetchall()
        timings[name] = round((time.perf_counter() - started) * 1000.0, 1)
        return rows

    try:
        conn.row_factory = None
        terminal_rows = run(
            "terminal_outcomes", _Q_TERMINAL_OUTCOMES, (reviewer_topic, since)
        )
        decision_rows = run(
            "daily_decisions", _Q_DAILY_DECISIONS, (reviewer_topic, since)
        )
        usage_rows = run("usage", _Q_USAGE, (since,))
        retry_rows = run("retry_tokens", _Q_RETRY_TOKENS, (since,))
        depth_rows = run("rejection_depth", _Q_REJECTION_DEPTH, (reviewer_topic,))
        model_rows = run("model_outcomes", _Q_MODEL_OUTCOMES, (reviewer_topic,))
        dimension_rows = run(
            "card_dimensions", _Q_CARD_DIMENSIONS, (reviewer_topic, since)
        )
    except sqlite3.Error as exc:
        conn.close()
        return history_series_unmeasured(f"query_failed:{type(exc).__name__}")
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    daily_outcomes, composition = _terminal_series(terminal_rows)
    daily_decisions = _decision_series(decision_rows)
    usage = _usage_series(usage_rows)
    retry = _retry_series(retry_rows, depth_rows)
    model_outcomes = _model_outcome_series(model_rows)
    latency, risk_tiers = _card_dimension_series(dimension_rows)

    observed_days = sorted(
        {row[0] for row in terminal_rows if row[0]}
        | {row[0] for row in decision_rows if row[0]}
        | {row[0] for row in usage_rows if row[0]}
    )

    payload = {
        "schema_id": HISTORY_SERIES_SCHEMA_ID,
        "measured": True,
        "readonly": True,
        "absent_metrics_are_unknown_not_zero": True,
        "generated_at": _utc_now(),
        "excluded_topics": list(EXCLUDED_TOPICS),
        "population_note": _POPULATION_NOTE,
        "baseline_substatus": BASELINE_SUBSTATUS,
        "resolution_note": (
            f"{BASELINE_SUBSTATUS} is the baseline outcome and is reported as "
            "one count; every other substatus keeps full per-day, "
            "per-population resolution because the failure classes are what "
            "the system learns from"
        ),
        "window": {
            "days": window_days,
            "since": since,
            "observed_days": len(observed_days),
            "first_day": observed_days[0] if observed_days else None,
            "last_day": observed_days[-1] if observed_days else None,
            "truncated": len(observed_days) > MAX_DAY_BUCKETS,
        },
        "query_cost": {
            "measured": True,
            "cache": "miss",
            "total_ms": round(sum(timings.values()), 1),
            "by_query_ms": timings,
            "query_count": len(timings),
            "note": (
                "cold cost of the canonical read; an unchanged store answers "
                "from cache keyed on the database (size, mtime_ns)"
            ),
        },
        "daily_outcomes": daily_outcomes,
        "daily_decisions": daily_decisions,
        "terminal_composition": composition,
        "usage": usage,
        "retry_economics": retry,
        "model_outcomes": model_outcomes,
        "latency": latency,
        "risk_tier_distribution": risk_tiers,
        "queries": dict(HISTORY_SERIES_QUERIES),
    }

    if key is not None:
        with _cache_lock:
            _cache["entry"] = {
                "key": key,
                "stored_at": time.monotonic(),
                "payload": payload,
            }
    return payload


def _terminal_series(
    rows: list[tuple[Any, ...]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Series 1 and 3 plus the cause breakdown, from one scan.

    Success gets one number and failure gets a taxonomy: ``baseline`` carries
    ``review_ready`` and ``failures`` keeps every other substatus at full
    per-day, per-population resolution.
    """
    days: dict[str, dict[str, Any]] = {}
    totals_by_substatus: dict[str, int] = {}
    by_population: dict[str, dict[str, int]] = {
        key: {} for key in _POPULATION_KEYS
    }
    causes: dict[str, dict[str, int]] = {}
    total_events = 0

    for day, population, substatus, cause, events in rows:
        day = str(day or "")
        population = str(population or "unknown_topic")
        substatus = str(substatus or "unknown_substatus")
        cause = str(cause or "verdict_absent")
        events = _bounded_int(events)
        total_events += events

        totals_by_substatus[substatus] = totals_by_substatus.get(substatus, 0) + events
        bucket = by_population.setdefault(population, {})
        bucket[substatus] = bucket.get(substatus, 0) + events
        cause_bucket = causes.setdefault(substatus, {})
        cause_bucket[cause] = cause_bucket.get(cause, 0) + events

        entry = days.setdefault(
            day,
            {
                "day": day,
                "total": 0,
                "baseline": _empty_population_counts(),
                "failures": {key: {} for key in _POPULATION_KEYS},
            },
        )
        entry["total"] += events
        if substatus == BASELINE_SUBSTATUS:
            entry["baseline"][population] = entry["baseline"].get(population, 0) + events
        else:
            failures = entry["failures"].setdefault(population, {})
            failures[substatus] = failures.get(substatus, 0) + events

    ordered_days = sorted(days.values(), key=lambda item: item["day"])
    truncated_days = len(ordered_days) > MAX_DAY_BUCKETS
    if truncated_days:
        ordered_days = ordered_days[-MAX_DAY_BUCKETS:]

    baseline_total = totals_by_substatus.get(BASELINE_SUBSTATUS, 0)
    failure_total = total_events - baseline_total
    failure_counts = {
        name: count
        for name, count in totals_by_substatus.items()
        if name != BASELINE_SUBSTATUS
    }
    bounded_failures, taxonomy_bound = _bounded_taxonomy(
        failure_counts, MAX_SUBSTATUS_CLASSES
    )

    daily = {
        "measured": True,
        "query_ref": "terminal_outcomes",
        "counting_unit": "terminal_event",
        "events": total_events,
        "day_count": len(days),
        "truncated": truncated_days,
        "truncation_note": (
            "oldest days dropped first; the failure taxonomy itself is never "
            "truncated from the rare end"
        ),
        "days": ordered_days,
    }

    composition = {
        "measured": True,
        "query_ref": "terminal_outcomes",
        "counting_unit": "terminal_event",
        "events": total_events,
        "baseline": {
            "substatus": BASELINE_SUBSTATUS,
            "events": baseline_total,
            "share": _rate(baseline_total, total_events),
            "note": "the outcome when nothing goes wrong; one count is enough",
        },
        "failures": {
            "events": failure_total,
            "share": _rate(failure_total, total_events),
            "by_substatus": bounded_failures,
            "bound": taxonomy_bound,
            "note": (
                "the entire actionable signal; every class kept at full "
                "resolution including the rare tail"
            ),
        },
        "by_population": {
            key: {
                "baseline": by_population.get(key, {}).get(BASELINE_SUBSTATUS, 0),
                "failures": {
                    name: count
                    for name, count in sorted(
                        by_population.get(key, {}).items(),
                        key=lambda kv: (-kv[1], kv[0]),
                    )
                    if name != BASELINE_SUBSTATUS
                },
            }
            for key in _POPULATION_KEYS
        },
        # The cause alongside the count: whether the deterministic evidence
        # verdict measured anything. "validation_failed" says something
        # happened; "validation_failed + nothing_measured" says why.
        "evidence_cause_by_substatus": {
            name: dict(
                sorted(cause_bucket.items(), key=lambda kv: (-kv[1], kv[0]))
            )
            for name, cause_bucket in sorted(
                causes.items(), key=lambda kv: (-sum(kv[1].values()), kv[0])
            )
        },
        "evidence_cause_legend": {
            "nothing_measured": (
                "the deterministic evidence verdict ran and measured nothing"
            ),
            "evidence_measured": "the verdict measured at least one signal",
            "verdict_absent": (
                "no deterministic verdict was recorded on the event; unknown, "
                "not a pass and not a failure"
            ),
        },
    }
    return daily, composition


def _decision_series(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    """Series 2: decisions per DISTINCT card per day, split by population."""
    days: dict[str, dict[str, Any]] = {}
    totals = {key: {"accepted": 0, "rejected": 0} for key in _POPULATION_KEYS}

    for day, population, event, cards, _card_days in rows:
        day = str(day or "")
        population = str(population or "unknown_topic")
        cards = _bounded_int(cards)
        field = "accepted" if event == "accept_review" else "rejected"
        entry = days.setdefault(
            day,
            {
                "day": day,
                **{
                    key: {"accepted": 0, "rejected": 0}
                    for key in _POPULATION_KEYS
                },
            },
        )
        bucket = entry.setdefault(population, {"accepted": 0, "rejected": 0})
        bucket[field] = bucket.get(field, 0) + cards
        totals.setdefault(population, {"accepted": 0, "rejected": 0})
        totals[population][field] += cards

    ordered = sorted(days.values(), key=lambda item: item["day"])
    truncated = len(ordered) > MAX_DAY_BUCKETS
    if truncated:
        ordered = ordered[-MAX_DAY_BUCKETS:]

    summary = {}
    for key, bucket in totals.items():
        decided = bucket["accepted"] + bucket["rejected"]
        summary[key] = {
            "accepted": bucket["accepted"],
            "rejected": bucket["rejected"],
            "decided": decided,
            "acceptance_rate": _rate(bucket["accepted"], decided),
        }

    return {
        "measured": True,
        "query_ref": "daily_decisions",
        "counting_unit": "distinct_card_latest_decision_per_day",
        "counting_note": (
            "one vote per distinct card per day: a card rejected 46 times in "
            "one day is one card"
        ),
        "day_count": len(days),
        "truncated": truncated,
        "days": ordered,
        "totals": summary,
    }


def _usage_series(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    """Series 4: tokens and cost by day, role and model, with honest coverage."""
    by_day: dict[str, dict[str, Any]] = {}
    by_role: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    telemetry_absent: dict[str, dict[str, Any]] = {}
    records = tokens = cost_records = zero_token = 0
    cost_usd = 0.0

    def bucket(store: dict[str, dict[str, Any]], name: str) -> dict[str, Any]:
        return store.setdefault(
            name,
            {
                "records": 0,
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_observed_records": 0,
                "cost_usd_observed": 0.0,
                "zero_token_records": 0,
            },
        )

    for row in rows:
        (
            day,
            role,
            model,
            provider,
            adapter_id,
            row_records,
            row_tokens,
            row_input,
            row_output,
            row_cost_records,
            row_cost_usd,
            row_zero_tokens,
        ) = row
        day = str(day or "")
        role = str(role or "") or "unknown_role"
        model = str(model or "") or "unknown_model"
        adapter_id = str(adapter_id or "") or "unknown_adapter"
        row_records = _bounded_int(row_records)
        row_tokens = _bounded_int(row_tokens)
        row_cost_records = _bounded_int(row_cost_records)
        row_zero_tokens = _bounded_int(row_zero_tokens)
        row_cost_usd = float(row_cost_usd or 0.0)

        records += row_records
        tokens += row_tokens
        cost_records += row_cost_records
        zero_token += row_zero_tokens
        cost_usd += row_cost_usd

        for store, name in (
            (by_day, day),
            (by_role, role),
            (by_model, model),
        ):
            target = bucket(store, name)
            target["records"] += row_records
            target["total_tokens"] += row_tokens
            target["input_tokens"] += _bounded_int(row_input)
            target["output_tokens"] += _bounded_int(row_output)
            target["cost_observed_records"] += row_cost_records
            target["cost_usd_observed"] = round(
                target["cost_usd_observed"] + row_cost_usd, 6
            )
            target["zero_token_records"] += row_zero_tokens

        # Keyed by (adapter, provider): ``adapter_id`` is absent on most
        # records, so attributing one provider to a shared "unknown_adapter"
        # bucket would invent an attribution the store does not carry. Every
        # row contributes to the denominator, not only the rows that had a
        # zero, so the share below is over the transport's real record count.
        transport = (adapter_id, str(provider or "") or "unknown_provider")
        entry = telemetry_absent.setdefault(
            transport,
            {
                "adapter_id": transport[0],
                "provider": transport[1],
                "records": 0,
                "zero_token_records": 0,
            },
        )
        entry["records"] += row_records
        entry["zero_token_records"] += row_zero_tokens

    for store in (by_day, by_role, by_model):
        for name, entry in store.items():
            entry["cost_coverage"] = _rate(
                entry["cost_observed_records"], entry["records"]
            )
            # Cost is summed only over records that declare an observed cost.
            # The rest are unknown, so the bucket total is a LOWER BOUND and
            # says so rather than presenting itself as the full spend.
            entry["cost_is_lower_bound"] = (
                entry["cost_observed_records"] < entry["records"]
            )
            entry["cost_usd_observed"] = round(entry["cost_usd_observed"], 6)

    ordered_days = [
        {"day": name, **entry}
        for name, entry in sorted(by_day.items(), key=lambda kv: kv[0])
    ]
    truncated = len(ordered_days) > MAX_DAY_BUCKETS
    if truncated:
        ordered_days = ordered_days[-MAX_DAY_BUCKETS:]

    absent_rows = [
        {
            **entry,
            "zero_token_share": _rate(
                entry["zero_token_records"], entry["records"]
            ),
        }
        for entry in sorted(
            telemetry_absent.values(),
            key=lambda item: (
                -item["zero_token_records"],
                item["adapter_id"],
                item["provider"],
            ),
        )
        if entry["zero_token_records"]
    ]

    return {
        "measured": True,
        "query_ref": "usage",
        "counting_unit": "usage_record",
        "records": records,
        "total_tokens": tokens,
        "day_count": len(by_day),
        "truncated": truncated,
        "by_day": ordered_days,
        "by_role": {
            name: entry
            for name, entry in sorted(
                by_role.items(), key=lambda kv: (-kv[1]["total_tokens"], kv[0])
            )
        },
        "by_model": {
            name: entry
            for name, entry in sorted(
                by_model.items(), key=lambda kv: (-kv[1]["total_tokens"], kv[0])
            )
        },
        "cost_quality": {
            "cost_observed_records": cost_records,
            "cost_unknown_records": max(0, records - cost_records),
            "coverage": _rate(cost_records, records),
            "cost_usd_observed": round(cost_usd, 6),
            "cost_usd_total": None,
            "zero_cost_is_free": False,
            "absent_metrics_are_unknown_not_zero": True,
            "reason": "provider_cost_absence_is_unknown_not_zero",
            "note": (
                "cost_usd_observed sums only records that declare "
                "cost_observed, so it is a lower bound; cost_usd_total is "
                "unknown because the remaining records carry no cost"
            ),
        },
        # A transport that reports no tokens at all is itself a problem
        # metric. Dropping these records from the cost series would quietly
        # turn an unmeasurable class into a free one.
        "telemetry_absence": {
            "zero_token_records": zero_token,
            "share_of_records": _rate(zero_token, records),
            "by_adapter": absent_rows,
            "note": (
                "records whose transport reported no token telemetry at all; "
                "their tokens and cost are unknown, never zero, and they are "
                "counted here rather than dropped from the cost series"
            ),
        },
    }


def _retry_series(
    retry_rows: list[tuple[Any, ...]], depth_rows: list[tuple[Any, ...]]
) -> dict[str, Any]:
    """Series 5: retry share of tokens, and rejection depth per distinct card."""
    kinds: dict[str, dict[str, Any]] = {}
    for kind, row_records, tasks, total_tokens, cost_records, cost_usd in retry_rows:
        kinds[str(kind)] = {
            "records": _bounded_int(row_records),
            "tasks": _bounded_int(tasks),
            "total_tokens": _bounded_int(total_tokens),
            "cost_observed_records": _bounded_int(cost_records),
            "cost_usd_observed": round(float(cost_usd or 0.0), 6),
        }
    first = kinds.get("first_attempt", {})
    retry = kinds.get("retry", {})
    all_tokens = _bounded_int(first.get("total_tokens")) + _bounded_int(
        retry.get("total_tokens")
    )
    all_records = _bounded_int(first.get("records")) + _bounded_int(
        retry.get("records")
    )

    depth: dict[str, dict[str, Any]] = {key: {} for key in _POPULATION_KEYS}
    for population, rejections, cards, accepted in depth_rows:
        population = str(population or "unknown_topic")
        depth.setdefault(population, {})[str(_bounded_int(rejections))] = {
            "cards": _bounded_int(cards),
            "eventually_accepted": _bounded_int(accepted),
            "eventual_acceptance_rate": _rate(
                _bounded_int(accepted), _bounded_int(cards)
            ),
        }

    return {
        "measured": True,
        "query_ref": "retry_tokens+rejection_depth",
        "attempts": {
            "first_attempt": first or None,
            "retry": retry or None,
            "records": all_records,
            "total_tokens": all_tokens,
        },
        "retry_share": {
            "of_tokens": _rate(retry.get("total_tokens"), all_tokens),
            "of_records": _rate(retry.get("records"), all_records),
            "counting_unit": "usage_record_attempt_index",
            "note": (
                "every usage record is a real attempt; attempt 1 is the first "
                "try and every later record on the same card is a retry"
            ),
        },
        # Rejection depth is first-class here: it is the shape that says how
        # much a card cost before it landed, or whether it ever did.
        "rejection_depth": {
            "counting_unit": "distinct_card",
            "by_population": {
                key: dict(
                    sorted(depth.get(key, {}).items(), key=lambda kv: int(kv[0]))
                )
                for key in _POPULATION_KEYS
            },
            "note": (
                "cards keyed by how many times they were rejected, with the "
                "eventual acceptance rate at that depth; reviewer children "
                "are reported separately and never folded into work cards"
            ),
        },
    }


def _model_outcome_series(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    """Series 6: accepted vs rejected DECIDED CARDS per runner, with samples."""
    by_population: dict[str, dict[str, dict[str, int]]] = {
        key: {} for key in _POPULATION_KEYS
    }
    for population, runner, event, cards in rows:
        population = str(population or "unknown_topic")
        runner = str(runner or "") or "unknown_runner"
        field = "accepted" if event == "accept_review" else "rejected"
        entry = by_population.setdefault(population, {}).setdefault(
            runner, {"accepted": 0, "rejected": 0}
        )
        entry[field] += _bounded_int(cards)

    result: dict[str, Any] = {}
    for key in _POPULATION_KEYS:
        runners = by_population.get(key, {})
        ordered = sorted(
            runners.items(),
            key=lambda kv: (-(kv[1]["accepted"] + kv[1]["rejected"]), kv[0]),
        )
        truncated = len(ordered) > MAX_MODEL_ROWS
        rows_out = []
        for runner, counts in ordered[:MAX_MODEL_ROWS]:
            decided = counts["accepted"] + counts["rejected"]
            rows_out.append(
                {
                    "runner": runner,
                    "accepted": counts["accepted"],
                    "rejected": counts["rejected"],
                    "decided": decided,
                    # The sample count travels with the rate so a 1-of-1
                    # runner never reads as a 100% model.
                    "sample_count": decided,
                    "acceptance_rate": _rate(counts["accepted"], decided),
                }
            )
        result[key] = {
            "runners": rows_out,
            "runner_count": len(ordered),
            "truncated": truncated,
        }

    return {
        "measured": True,
        "query_ref": "model_outcomes",
        "counting_unit": "distinct_card_latest_decision",
        "sample_note": (
            "acceptance_rate is unknown, not zero, when a runner has decided "
            "no cards; sample_count travels with every rate"
        ),
        **result,
    }


def _card_dimension_series(
    rows: list[tuple[Any, ...]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Series 7 and 8: latency distributions and risk-tier coverage."""
    queue: dict[str, list[int]] = {key: [] for key in _POPULATION_KEYS}
    run: dict[str, list[int]] = {key: [] for key in _POPULATION_KEYS}
    anomalies = {"negative_queue": 0, "negative_run": 0}
    tiers: dict[str, dict[str, int]] = {key: {} for key in _POPULATION_KEYS}
    totals = {key: 0 for key in _POPULATION_KEYS}
    tier_known = {key: 0 for key in _POPULATION_KEYS}
    cards = 0

    for population, risk_tier, queue_seconds, run_seconds in rows:
        population = str(population or "unknown_topic")
        cards += 1
        totals[population] = totals.get(population, 0) + 1

        tier = str(risk_tier or "")
        if tier:
            tier_known[population] = tier_known.get(population, 0) + 1
            bucket = tiers.setdefault(population, {})
            bucket[tier] = bucket.get(tier, 0) + 1

        if queue_seconds is not None:
            value = int(queue_seconds)
            # A negative duration is a clock anomaly, not a fast card. It is
            # counted as its own fact and kept out of the distribution.
            if value < 0:
                anomalies["negative_queue"] += 1
            else:
                queue.setdefault(population, []).append(value)
        if run_seconds is not None:
            value = int(run_seconds)
            if value < 0:
                anomalies["negative_run"] += 1
            else:
                run.setdefault(population, []).append(value)

    latency = {
        "measured": True,
        "query_ref": "card_dimensions",
        "counting_unit": "task_card",
        "cards": cards,
        "queue_latency": {
            "definition": "created_at -> started_at",
            "by_population": {
                key: _percentiles(queue.get(key, [])) for key in _POPULATION_KEYS
            },
        },
        "run_latency": {
            "definition": "started_at -> completed_at",
            "by_population": {
                key: _percentiles(run.get(key, [])) for key in _POPULATION_KEYS
            },
        },
        "anomalies": {
            **anomalies,
            "note": (
                "durations where the later timestamp precedes the earlier "
                "one; counted, never clamped to zero and never included in "
                "a percentile"
            ),
        },
    }

    risk = {
        "measured": True,
        "query_ref": "card_dimensions",
        "counting_unit": "task_card",
        "cards": cards,
        "by_population": {
            key: {
                "cards": totals.get(key, 0),
                "risk_tier_known": tier_known.get(key, 0),
                "risk_tier_unknown": max(
                    0, totals.get(key, 0) - tier_known.get(key, 0)
                ),
                "coverage": _rate(tier_known.get(key, 0), totals.get(key, 0)),
                "tiers": dict(
                    sorted(
                        tiers.get(key, {}).items(), key=lambda kv: (-kv[1], kv[0])
                    )
                ),
            }
            for key in _POPULATION_KEYS
        },
        # risk_tier is only derived at create as of today, so most historical
        # cards carry none. Those cards are reported as unknown coverage and
        # are never defaulted into a tier bucket.
        "coverage_note": (
            "risk_tier is derived at card creation, so cards created before "
            "that became a create-time field carry no tier; they are counted "
            "as risk_tier_unknown and never defaulted into a tier"
        ),
        "absent_metrics_are_unknown_not_zero": True,
    }
    return latency, risk
