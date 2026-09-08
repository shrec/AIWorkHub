#!/usr/bin/env python3
"""Bounded ``task_events`` rows for one card.

Replaces the single most-typed ad-hoc script in the manager transcripts: open
``.aiworkhub/tasking/task_queue.sqlite``, select this card's events, print them.
517 of 4,962 measured Bash calls targeted that database by hand.

Read-only: an ``immutable=1`` connection that neither locks nor replays the WAL.

    python -m aiworkhub.recipes.task_events --task-id ID [--event KIND] [--limit N]

``--event all`` (the default) applies no kind filter. Payloads larger than
``--payload-chars`` come back as their key list and size rather than trimmed
into something that still looks whole.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from . import _common as common

SCHEMA_ID = "aiworkhub.recipe.task_events.v1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task-id", required=True, help="exact canonical task id")
    parser.add_argument(
        "--event",
        default=common.NO_FILTER,
        help="exact event kind, or 'all' for no filter (default: all)",
    )
    parser.add_argument(
        "--limit", type=int, default=50, help="max rows, newest first (default: 50)"
    )
    parser.add_argument(
        "--payload-chars",
        type=int,
        default=2000,
        help="per-event payload size above which only keys are returned",
    )
    args = parser.parse_args(argv)

    limit = max(1, min(int(args.limit), 500))
    payload_chars = max(0, min(int(args.payload_chars), 20_000))
    task_id = str(args.task_id).strip()
    if not task_id:
        return common.fail(SCHEMA_ID, "invalid_task_id", "task id must be non-empty")

    try:
        conn = common.task_queue_readonly()
    except (FileNotFoundError, sqlite3.Error) as exc:
        return common.fail(SCHEMA_ID, "task_queue_unavailable", str(exc))
    conn.row_factory = sqlite3.Row
    try:
        where = "task_id=?"
        params: list[object] = [task_id]
        if args.event != common.NO_FILTER:
            where += " AND event=?"
            params.append(str(args.event))
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM task_events WHERE {where}", params
            ).fetchone()[0]
        )
        rows = conn.execute(
            f"SELECT event_id,task_id,event,runner,payload_json,created_at "
            f"FROM task_events WHERE {where} ORDER BY event_id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        task = conn.execute(
            "SELECT status,worker_status,runner,topic,mode,created_at,updated_at "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        return common.fail(SCHEMA_ID, "query_failed", str(exc))
    finally:
        conn.close()

    events = [
        {
            "event_id": row["event_id"],
            "event": row["event"],
            "runner": row["runner"],
            "created_at": row["created_at"],
            "payload": common.bounded_payload(row["payload_json"], payload_chars),
        }
        for row in rows
    ]
    return common.emit(
        {
            "schema_id": SCHEMA_ID,
            "task_id": task_id,
            "event_filter": args.event,
            "limit": limit,
            "total_count": total,
            "returned_count": len(events),
            "truncated": total > len(events),
            "task": dict(task) if task is not None else None,
            "events": events,
        }
    )


if __name__ == "__main__":
    sys.exit(main())
