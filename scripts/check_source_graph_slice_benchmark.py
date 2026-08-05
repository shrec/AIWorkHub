#!/usr/bin/env python3
"""Reproduce the exact-symbol Source Graph slice precision fixture."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiworkhub import source_graph
from aiworkhub import source_graph_insights
from aiworkhub.repository_state import bootstrap_repository


SNAPSHOT = ROOT / "benchmarks" / "source-graph-slice-precision-v1.json"


def measure() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="aiworkhub-slice-benchmark-") as raw:
        repo = Path(raw) / "repo"
        repo.mkdir()
        bootstrap_repository(repo, repo_name="source-graph-slice-benchmark")
        parts = ["def helper_target():\n    return 1\n"]
        for index in range(80):
            parts.append(
                f"def helper_{index}():\n    return {index}\n\n"
                f"def entry_{index}():\n    return helper_{index}()\n"
            )
        parts.append("def target_entry():\n    return helper_target()\n")
        target_file = repo / "pkg" / "large.py"
        target_file.parent.mkdir(parents=True)
        target_file.write_text("\n".join(parts), encoding="utf-8")
        source_graph.build_index(repo, incremental=False)

        focused = source_graph.focus(repo, "target_entry", 32)
        target = str(focused["ranked_symbols"][0]["qualname"])
        sliced = source_graph.slice_(
            repo, "change target behavior", 32, target=target,
        )
        conn = source_graph.connect(source_graph.resolve_db_path(repo), read_only=True)
        try:
            legacy_outgoing, legacy_incoming = source_graph_insights.call_edges(
                conn, ["pkg/large.py"], limit=50,
            )
        finally:
            conn.close()

        legacy = {
            "outgoing_calls": legacy_outgoing,
            "incoming_calls": legacy_incoming,
        }
        current = {
            "outgoing_calls": sliced["outgoing_calls"],
            "incoming_calls": sliced["incoming_calls"],
        }
        legacy_bytes = len(json.dumps(
            legacy, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8"))
        current_bytes = len(json.dumps(
            current, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8"))
        return {
            "target": target,
            "legacy_file_scoped_edge_rows": len(legacy_outgoing) + len(legacy_incoming),
            "current_symbol_scoped_edge_rows": (
                len(sliced["outgoing_calls"]) + len(sliced["incoming_calls"])
            ),
            "legacy_edge_bytes": legacy_bytes,
            "current_edge_bytes": current_bytes,
            "structural_reduction_percent": round(
                (1.0 - current_bytes / legacy_bytes) * 100.0, 3,
            ),
            "structural_ratio": round(legacy_bytes / current_bytes, 2),
        }


def check() -> list[str]:
    try:
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"source_graph_slice_snapshot_unreadable:{exc}"]
    observed = measure()
    errors = [
        f"source_graph_slice_mismatch:{key}:{snapshot.get('result', {}).get(key)}:{value}"
        for key, value in observed.items()
        if snapshot.get("result", {}).get(key) != value
    ]
    claim = snapshot.get("claim_boundary") or {}
    if claim.get("provider_token_savings_claimed") is not False:
        errors.append("source_graph_slice_unsafe_token_claim")
    if claim.get("accepted_task_quality_measured") is not False:
        errors.append("source_graph_slice_unsafe_quality_claim")
    return errors


def main() -> int:
    errors = check()
    if errors:
        for error in errors:
            print(error)
        return 1
    print("source_graph_slice_benchmark_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
