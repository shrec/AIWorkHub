#!/usr/bin/env python3
"""Aggregated ``usage_record`` rows from the canonical task queue.

Replaces the cost/token roll-up a manager types by hand whenever a routing or
budget question comes up.

``usage_record`` is an EVENT KIND in ``task_events``, not a table -- measured
6,660 rows on this repository's queue -- so the aggregation reads
``task_events WHERE event='usage_record'`` and sums the parsed payload. That
distinction is the whole reason this script exists: a hand-written
``SELECT ... FROM usage_record`` fails, and the next attempt guesses.

Read-only: an ``immutable=1`` connection that neither locks nor replays the WAL.

    python -m aiworkhub.recipes.usage_rollup [--task-id ID] [--since ISO]
        [--group-by task|runner|topic|model|role|day] [--limit N]

``--task-id all`` and ``--since all`` (the defaults) apply no filter.
``records`` in a payload is an integer count of merged sub-records, so
``merged_records`` and ``event_count`` are reported separately rather than one
being inferred from the other.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from . import _common as common

SCHEMA_ID = "aiworkhub.recipe.usage_rollup.v1"

GROUP_KEYS = ("task", "runner", "topic", "model", "role", "day")

_NUMERIC_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "reasoning_output_tokens",
    "cost_usd",
)


def _group_value(group_by: str, row: sqlite3.Row, payload: dict, topic: str) -> str:
    if group_by == "task":
        return str(row["task_id"] or "")
    if group_by == "day":
        return str(row["created_at"] or "")[:10]
    if group_by == "runner":
        return str(payload.get("runner") or row["runner"] or "")
    if group_by == "topic":
        return str(payload.get("topic") or topic or "")
    if group_by == "model":
        return str(payload.get("observed_model") or payload.get("model") or "")
    return str(payload.get("role") or "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--task-id", default=common.NO_FILTER, help="exact task id, or 'all'"
    )
    parser.add_argument(
        "--since",
        default=common.NO_FILTER,
        help="ISO-8601 lower bound on created_at, or 'all'",
    )
    parser.add_argument("--group-by", default="runner", choices=GROUP_KEYS)
    parser.add_argument(
        "--limit", type=int, default=25, help="max groups, largest cost first"
    )
    parser.add_argument(
        "--scan-limit",
        type=int,
        default=20_000,
        help="max usage events read before the scan reports itself bounded",
    )
    args = parser.parse_args(argv)

    limit = max(1, min(int(args.limit), 200))
    scan_limit = max(1, min(int(args.scan_limit), 200_000))

    try:
        conn = common.task_queue_readonly()
    except (FileNotFoundError, sqlite3.Error) as exc:
        return common.fail(SCHEMA_ID, "task_queue_unavailable", str(exc))
    conn.row_factory = sqlite3.Row
    try:
        where = "e.event='usage_record'"
        params: list[object] = []
        if args.task_id != common.NO_FILTER:
            where += " AND e.task_id=?"
            params.append(str(args.task_id))
        if args.since != common.NO_FILTER:
            where += " AND e.created_at>=?"
            params.append(str(args.since))
        matched = int(
            conn.execute(
                f"SELECT COUNT(*) FROM task_events e WHERE {where}", params
            ).fetchone()[0]
        )
        rows = conn.execute(
            f"SELECT e.task_id,e.runner,e.created_at,e.payload_json,"
            f"COALESCE(t.topic,'') AS topic "
            f"FROM task_events e LEFT JOIN tasks t ON t.task_id=e.task_id "
            f"WHERE {where} ORDER BY e.event_id DESC LIMIT ?",
            [*params, scan_limit],
        ).fetchall()
    except sqlite3.Error as exc:
        return common.fail(SCHEMA_ID, "query_failed", str(exc))
    finally:
        conn.close()

    groups: dict[str, dict[str, float]] = {}
    unparsable = 0
    for row in rows:
        payload = common.bounded_payload(row["payload_json"], 20_000)
        if not isinstance(payload, dict) or payload.get("_oversized") or payload.get(
            "_unparsable"
        ):
            unparsable += 1
            continue
        key = _group_value(args.group_by, row, payload, row["topic"])
        bucket = groups.setdefault(
            key, {"event_count": 0.0, "merged_records": 0.0, **{f: 0.0 for f in _NUMERIC_FIELDS}}
        )
        bucket["event_count"] += 1
        merged = payload.get("records")
        bucket["merged_records"] += float(merged) if isinstance(merged, int) else 1.0
        for field in _NUMERIC_FIELDS:
            value = payload.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                bucket[field] += float(value)

    ordered = sorted(
        groups.items(), key=lambda kv: (-kv[1]["cost_usd"], -kv[1]["event_count"], kv[0])
    )
    projected = [
        {
            "group": key,
            "event_count": int(values["event_count"]),
            "merged_records": int(values["merged_records"]),
            "cost_usd": round(values["cost_usd"], 6),
            **{
                field: int(values[field])
                for field in _NUMERIC_FIELDS
                if field != "cost_usd"
            },
        }
        for key, values in ordered[:limit]
    ]
    return common.emit(
        {
            "schema_id": SCHEMA_ID,
            "group_by": args.group_by,
            "task_id_filter": args.task_id,
            "since_filter": args.since,
            "matched_event_count": matched,
            "scanned_event_count": len(rows),
            "scan_truncated": matched > len(rows),
            "unparsable_payloads": unparsable,
            "group_count": len(groups),
            "returned_count": len(projected),
            "totals": {
                "cost_usd": round(sum(v["cost_usd"] for v in groups.values()), 6),
                "total_tokens": int(sum(v["total_tokens"] for v in groups.values())),
            },
            "groups": projected,
        }
    )


if __name__ == "__main__":
    sys.exit(main())
