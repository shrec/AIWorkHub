#!/usr/bin/env python3
"""Backfill escaped-defect attribution for converted NeedFix rows.

Dry-run by default: computes and prints the JSON summary without writing.
Pass --apply to persist the results through the validated write-once path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aiworkhub import defect_attribution, needfix_store, sdlc_outcome_metrics, task_store
from aiworkhub.sqlite_readonly import connect_readonly


def _accepted_receipts_corpus(readiness) -> list[dict]:
    """The same event source ``sdlc_outcome_metrics.read_repository_metrics`` reads."""
    task_conn = connect_readonly(readiness.canonical_db)
    try:
        event_rows, _cohort = sdlc_outcome_metrics.read_decided_task_cohort(
            task_conn, sdlc_outcome_metrics.MAX_LIMIT
        )
    finally:
        task_conn.close()
    receipts = []
    for event in event_rows:
        identity = sdlc_outcome_metrics.accepted_outcome_identity(event, readiness.repo_id)
        if identity is not None:
            receipts.append(identity)
    return receipts


def _make_verifier(repo_root: Path, repository_id: str):
    def verify(identity):
        task_id = str(identity.get("task_id") or "")
        if not task_id:
            return None
        events = task_store.get_task_events(repo_root, task_id, limit=100)
        for event in events:
            candidate = sdlc_outcome_metrics.accepted_outcome_identity(
                {**event, "task_id": task_id}, repository_id
            )
            if candidate == identity:
                return {**identity, "outcome": "accepted"}
        return None

    return verify


def _all_converted_needfix_ids(repo_root: Path) -> list[str]:
    ids: list[str] = []
    offset = 0
    limit = needfix_store.MAX_LIST_LIMIT
    while True:
        page = needfix_store.list_needfix(
            repo_root, include_archived=True, limit=limit, offset=offset,
        )
        ids.extend(
            str(row["id"]) for row in page if str(row.get("converted_task_id") or "").strip()
        )
        if len(page) < limit:
            break
        offset += limit
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_root", type=Path, help="Repository root containing the NeedFix store")
    parser.add_argument("--apply", action="store_true", help="Persist results (default: dry-run)")
    parser.add_argument("--max-workers", type=int, default=None)
    args = parser.parse_args(argv)

    readiness = task_store.storage_readiness(args.repo_root)
    if not readiness.ready:
        print(json.dumps({"error": "storage_not_ready", "reason": readiness.reason}))
        return 2
    repository_id = readiness.repo_id

    accepted_receipts = _accepted_receipts_corpus(readiness)
    verify_accepted_outcome = _make_verifier(args.repo_root, repository_id)
    needfix_ids = _all_converted_needfix_ids(args.repo_root)

    results = defect_attribution.attribute_many(
        args.repo_root,
        needfix_ids,
        repository_id=repository_id,
        accepted_receipts=accepted_receipts,
        verify_accepted_outcome=verify_accepted_outcome,
        max_workers=args.max_workers,
        dry_run=not args.apply,
    )

    summary = {
        "dry_run": not args.apply,
        "repository_id": repository_id,
        "total": len(results),
        "attributed": sum(1 for r in results if r["outcome"] == "attributed"),
        "non_card": sum(1 for r in results if r["outcome"] == "non_card"),
        "unknown": sum(1 for r in results if r["outcome"] == "unknown"),
        "skipped": sum(1 for r in results if r["outcome"] == "skipped"),
        "results": results,
    }
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
