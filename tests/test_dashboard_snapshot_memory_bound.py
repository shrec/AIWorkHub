from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_TOOL_ROOT = Path(__file__).resolve().parents[1]
_SRC = _TOOL_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import dashboard, dashboard_mcp_app  # noqa: E402


class PoisonRows(list):
    def __init__(self, status: str, total: int, poison_after: int) -> None:
        super().__init__(
            {
                "task_id": f"TASK_{status.upper()}_{index:04d}",
                "status": status,
                "topic": "runtime-memory",
                "runner": "codex",
            }
            for index in range(total)
        )
        self.status = status
        self.total = total
        self.poison_after = poison_after
        self.constructed = 0
        self.peak_live = 0

    def __len__(self) -> int:
        return self.total

    def __iter__(self):
        for index, row in enumerate(super().__iter__()):
            if index >= self.poison_after:
                raise AssertionError(f"{self.status} rows iterated past visible bound")
            self.constructed += 1
            self.peak_live = max(self.peak_live, self.constructed)
            yield row


class CountingProvider:
    def __init__(self, limit: int = 37) -> None:
        self.repo_root = Path("/")
        self.task_limit = limit
        self.rows = {
            status: PoisonRows(status, total=2000 + offset, poison_after=limit)
            for offset, status in enumerate(dashboard.ACTIVE_STATUSES)
        }
        self.cache_stats = {"hit": 0, "miss": 0}

    def get_storage_readiness(self):
        return SimpleNamespace(ready=True, reason="", repo_id="repo_test")

    def snapshot_read_scope(self):
        return dashboard.nullcontext()

    def snapshot_cache_stats(self):
        return dict(self.cache_stats)

    def list_tasks(self, status: str):
        return self.rows[status]

    def get_completion_inbox(self):
        return {
            "review_queue": [],
            "stale_processing": [],
            "runner_mismatch_warnings": [],
            "latest_validation_facts": [],
            "read_errors": [],
        }

    def get_collision_report(self):
        return {"collision_free": True, "active_cards": 0, "collision_count": 0, "file_collisions": []}

    def get_exact_status_counts(self):
        return {
            "pending": 2000,
            "processing": 2001,
            "review": 2002,
            "blocked": 800,
            "superseded": 700,
            "finished": 600,
            "archived": 500,
        }

    def get_manager_decision_counts(self):
        return {"accepted": 11, "rejected": 13}


def _patch_secondary_reads(monkeypatch):
    monkeypatch.setattr(dashboard.storage_observability, "snapshot", lambda _root: {})
    monkeypatch.setattr(dashboard.source_graph_daemon, "daemon_health", lambda _root: {})
    monkeypatch.setattr(dashboard.context_graph, "status", lambda _root: {})
    monkeypatch.setattr(dashboard, "_protocol_alert_telemetry", lambda _root: {})
    monkeypatch.setattr(dashboard.dashboard_kpis, "build_kpi_snapshot", lambda **_kwargs: {"kpis": []})
    monkeypatch.setattr(dashboard, "_coding_foundation_projections", lambda *_a, **_k: {
        "development_rules": {},
        "skills": {},
        "tool_recipes": {},
    })


def test_summary_snapshot_bounds_visible_rows_before_projection(monkeypatch):
    _patch_secondary_reads(monkeypatch)
    provider = CountingProvider(limit=37)

    snapshot = dashboard.build_snapshot(provider, summary_only=True)

    assert snapshot["status_counts"]["pending"] == 2000
    assert snapshot["status_counts"]["active"] == 6003
    assert snapshot["row_counts"]["pending"] == {"returned": 37, "exact": 2000, "truncated": True}
    assert snapshot["read_bounds"]["tasks.pending"]["configured_limit"] == 37
    assert snapshot["read_bounds"]["tasks.pending"]["authoritative_count"] == 2000
    assert snapshot["warnings"]["stale"] == []
    for rows in provider.rows.values():
        assert rows.constructed == 37
        assert rows.peak_live == 37


def test_full_snapshot_bounds_rows_and_preserves_schema_groups(monkeypatch):
    _patch_secondary_reads(monkeypatch)
    provider = CountingProvider(limit=23)
    provider.get_cost_ledger = lambda: {
        "totals": {},
        "aggregates": {},
        "tasks": [],
        "read_bounds": {
            "source": "cost_ledger.tasks",
            "configured_limit": 0,
            "returned_count": 0,
            "scanned_count": 0,
            "authoritative_count": "aggregate_only",
            "truncated": False,
            "cache": {"hit": 0, "miss": 0},
        },
    }
    provider.get_agent_processes = lambda: {"ok": True, "processes": [], "read_bounds": {}}
    provider.get_kpi_processes = lambda: {"ok": True, "processes": [], "read_bounds": {}}
    provider.get_adapter_readiness = lambda: {}
    provider.get_environment_preflight = lambda: {}
    provider.get_workforce_catalog = lambda: {}
    provider.get_callback_bridge_health = lambda: {"bound_task_count": 0, "unbound_task_count": 0, "by_state": {}}
    provider.get_task_plan = lambda: {}
    provider.get_needfix_snapshot = lambda: {"available": True, "items": [], "total": 0, "open": 0}
    provider.get_roadmap_snapshot = lambda: {"available": True, "items": [], "total": 0, "active": 0}

    snapshot = dashboard.build_snapshot(provider, summary_only=False)

    assert set(snapshot["tasks"]) == {"pending", "processing", "review", "stale"}
    assert len(snapshot["tasks"]["review"]) == 23
    assert snapshot["summaries"]["topics"][0]["name"] == "runtime-memory"
    assert snapshot["outcome_counts"]["accepted"] == 11
    assert snapshot["callback_bridge_health"]["bound_task_count"] == 0
    assert snapshot["needfix"]["available"] is True
    assert snapshot["roadmap"]["available"] is True
    assert snapshot["read_bounds"]["tasks.review"]["truncated"] is True
    for rows in provider.rows.values():
        assert rows.constructed == 23


def test_process_log_reader_retains_only_visible_process_rows(tmp_path):
    path = tmp_path / "process.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for index in range(1000):
            handle.write(json.dumps({
                "request_id": f"req-{index}",
                "timestamp": f"2026-09-05T00:{index % 60:02d}:00+00:00",
                "state": "review_ready",
                "task_id": f"TASK_{index}",
            }) + "\n")

    report = dashboard.read_process_runs(process_log_path=path, limit=9, max_bytes=1024 * 1024)

    assert len(report["processes"]) == 9
    assert report["total_requests"] == 1000
    assert report["read_bounds"]["configured_limit"] == 9
    assert report["read_bounds"]["returned_count"] == 9
    assert report["read_bounds"]["scanned_count"] == 1000
    assert report["read_bounds"]["truncated"] is True


def test_process_log_reader_byte_truncated_tail_exact_limit_marks_read_bounds(tmp_path):
    path = tmp_path / "process.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "request_id": "old-request-outside-byte-window",
            "timestamp": "2026-09-04T23:59:00+00:00",
            "state": "review_ready",
            "task_id": "TASK_OLD",
            "padding": "x" * 3000,
        }) + "\n")
        for index in range(9):
            handle.write(json.dumps({
                "request_id": f"tail-{index}",
                "timestamp": f"2026-09-05T00:0{index}:00+00:00",
                "state": "review_ready",
                "task_id": f"TASK_{index}",
            }, separators=(",", ":")) + "\n")

    report = dashboard.read_process_runs(process_log_path=path, limit=9, max_bytes=1024)

    assert len(report["processes"]) == 9
    assert report["truncated"] is True
    assert report["read_bounds"]["configured_limit"] == 9
    assert report["read_bounds"]["returned_count"] == 9
    assert report["read_bounds"]["scanned_count"] == 9
    assert report["read_bounds"]["authoritative_count"] == "unknown"
    assert report["read_bounds"]["truncated"] is True


def test_process_log_reader_newest_ai_infra_and_status_path_win(tmp_path, monkeypatch):
    path = tmp_path / "process.jsonl"
    older_status_path = str(tmp_path / "older.status.json")
    newer_status_path = str(tmp_path / "newer.status.json")
    events = [
        {
            "request_id": "req-shared",
            "timestamp": "2026-09-05T00:00:00+00:00",
            "state": "processing",
            "task_id": "TASK_SHARED",
            "supervisor_status_path": older_status_path,
            "project_context": {
                "sections": [
                    {
                        "name": "ai_memory",
                        "requested": True,
                        "executed": True,
                        "hit_count": 3,
                        "bytes": 45,
                    }
                ],
            },
            "usage": {"observed_model": "old", "model_observed": True},
        },
        {
            "request_id": "req-shared",
            "timestamp": "2026-09-05T00:01:00+00:00",
            "state": "processing",
            "task_id": "TASK_SHARED",
            "supervisor_status_path": newer_status_path,
            "usage": {"observed_model": "new", "model_observed": True},
        },
    ]
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")

    seen_status_paths: list[str] = []

    def fake_liveness(_row: dict[str, Any], status_path: str) -> dict[str, Any]:
        seen_status_paths.append(status_path)
        return {"status_path_seen": status_path}

    monkeypatch.setattr(dashboard.process_launcher, "ACTIVE_PROCESS_STATES", {"processing"})
    monkeypatch.setattr(dashboard, "_process_liveness_fields", fake_liveness)
    monkeypatch.setattr(dashboard.process_launcher, "_pid_matches", lambda *_args: False)

    report = dashboard.read_process_runs(process_log_path=path, limit=5, max_bytes=1024 * 1024)

    assert len(report["processes"]) == 1
    row = report["processes"][0]
    assert row["timestamp"] == "2026-09-05T00:01:00+00:00"
    assert row["status_path_seen"] == newer_status_path
    assert seen_status_paths == [newer_status_path]
    ai_infra = row["ai_infra_context"]
    assert ai_infra["usage"]["observed_model"] == "new"
    assert ai_infra["ai_memory"]["hit_count"] == 3


def test_mcp_snapshot_passes_previous_snapshot_contract(monkeypatch):
    calls: list[dict[str, Any]] = []
    previous = {"skills": {"ownership": "full", "state": "measured"}}

    def fake_build_snapshot(**kwargs):
        calls.append(kwargs)
        return {
            "schema_version": 1,
            "generated_at": "2026-09-05T00:00:00+00:00",
            "readonly": True,
            "storage": {"ready": True},
            "health": {"ok": True},
            "status_counts": {},
            "row_counts": {},
            "warnings": {},
            "errors": [],
        }

    monkeypatch.setattr(dashboard_mcp_app.dashboard, "build_snapshot", fake_build_snapshot)
    monkeypatch.setattr(dashboard_mcp_app.core, "manager_bootstrap", lambda: {})
    monkeypatch.setattr(dashboard_mcp_app.core, "dispatcher_health", lambda: {"status": "running"})
    monkeypatch.setattr(dashboard_mcp_app.shared_router, "list_known_repositories", lambda **_kwargs: {})
    monkeypatch.setattr(dashboard_mcp_app.core, "repo_root", lambda: Path("/"))
    monkeypatch.setattr(dashboard_mcp_app.core, "read_selected_coordinator_target", lambda _root: {})
    monkeypatch.setattr(dashboard_mcp_app.storage_observability, "snapshot", lambda _root: {})

    result = dashboard_mcp_app.snapshot_view(full=False, previous_snapshot=previous)

    assert calls == [{"summary_only": True, "previous": previous}]
    assert result["snapshot_mode"] == "summary"
    assert "manager_identity" in result
