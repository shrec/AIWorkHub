#!/usr/bin/env python3
"""
MCP Batch Collision Guard B119 V1
Validates task lists to ensure allowed_writes do not collide before or after wave execution.

Usage:
  python3 tools/geoai-task-mcp/scripts/build_mcp_batch_collision_guard_b119_v1.py --cards path/to.jsonl --mode [pre_wave|post_wave] --out out.json
"""

import argparse
import sys
import json
from collections import defaultdict
from datetime import datetime, timezone
import os

ACTIVE_STATUSES = {"pending", "processing", "review", "blocked"}

def canonical_status(card: dict) -> str:
    status = str(card.get("status") or "").strip().lower()
    worker_status = str(card.get("worker_status") or "").strip().lower()
    if status in {"finished", "completed", "stale_already_done"} or worker_status == "done":
        return "finished"
    if status.startswith("blocked") or worker_status.startswith(("blocked", "deferred")):
        return "blocked"
    if status in {"review", "ready_for_review", "codex_review", "awaiting_review"} or worker_status in {
        "review", "ready_for_review", "codex_review", "awaiting_review",
    }:
        return "review"
    if status in {"processing", "in_progress"} or worker_status in {"claimed", "in_progress"}:
        return "processing"
    return "pending"

def scan_collisions(cards: list[dict], mode: str) -> dict:
    timestamp = datetime.now(timezone.utc).isoformat()
    active_cards = [c for c in cards if canonical_status(c) in ACTIVE_STATUSES]
    
    file_map = defaultdict(list)
    for c in active_cards:
        tid = c.get("task_id", "?")
        rites = c.get("allowed_writes", [])
        if isinstance(rites, list):
            for w in rites:
                file_map[str(w)].append(tid)

    collisions = []
    for file_path, tasks in file_map.items():
        unique_tasks = list(set(tasks))
        if len(unique_tasks) > 1:
            collisions.append({
                "file": file_path,
                "conflicting_tasks": sorted(unique_tasks)
            })

    collision_free = len(collisions) == 0
    
    result = {
        "mode": mode,
        "scan_timestamp": timestamp,
        "total_cards_scanned": len(cards),
        "active_cards_count": len(active_cards),
        "collision_free": collision_free,
        "collision_count": len(collisions),
        "file_collisions": sorted(collisions, key=lambda x: x["file"]),
    }
    
    if not collision_free:
        result["blocked_action"] = "BLOCKED Action: De-duplicate allowed_writes across pending tasks before dispatching."

    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cards", required=True)
    parser.add_argument("--mode", choices=["pre_wave", "post_wave"], required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    try:
        with open(args.cards, "r", encoding="utf-8") as f:
            cards = [json.loads(line) for line in f if line.strip()]
    except Exception as e:
        print(f"Failed to read cards: {e}")
        return 1

    result = scan_collisions(cards, args.mode)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    if not result["collision_free"]:
        print(f"⚠️  Collisions detected in mode {args.mode}")
        if "blocked_action" in result:
            print(result["blocked_action"])
        return 1
    
    print(f"✅ No collisions detected in mode {args.mode}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
