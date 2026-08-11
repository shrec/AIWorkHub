from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest


_TOOL_ROOT = Path(__file__).resolve().parents[1]
_SRC = _TOOL_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _ensure_deepseek_credentials_stub() -> None:
    """Bridge a pre-existing, out-of-scope worktree gap: some isolated Task
    MCP worktrees are missing ``deepseek_credentials.py`` entirely (an
    uncommitted file on the trusted host, absent from this worktree's git
    history) even though ``process_launcher.py``/``dashboard.py`` already
    import it at module scope. Only installs a stub when the real module is
    genuinely unimportable -- a host where the file exists imports it
    normally and this is a no-op."""
    import importlib
    import types

    try:
        importlib.import_module("aiworkhub.deepseek_credentials")
        return
    except ImportError:
        pass

    stub = types.ModuleType("aiworkhub.deepseek_credentials")

    class CredentialError(Exception):
        def __init__(self, reason: str = "deepseek_credential_stub_environment") -> None:
            super().__init__(reason)
            self.reason = reason

    def load_credential(repo=None):  # noqa: ANN001, ARG001
        raise CredentialError("deepseek_credential_stub_environment")

    def adapter_readiness(repo=None):  # noqa: ANN001, ARG001
        return {"ok": True, "readonly": True, "adapters": []}

    stub.CredentialError = CredentialError
    stub.load_credential = load_credential
    stub.adapter_readiness = adapter_readiness
    sys.modules["aiworkhub.deepseek_credentials"] = stub


_ensure_deepseek_credentials_stub()

from aiworkhub import core, dashboard, task_store  # noqa: E402


NOW = "2026-07-21T00:00:00+00:00"


def _init_canonical_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    result = task_store.initialize_repository(root)
    assert result["ok"], result
    return root


def _canonical_db(root: Path) -> Path:
    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    return Path(readiness.canonical_db)


def test_default_repo_root_tracks_manager_process_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dashboard reads must follow the same live repo authority as MCP tools."""
    original = _init_canonical_repo(tmp_path, "original")
    switched = _init_canonical_repo(tmp_path, "switched")
    monkeypatch.setenv("AIWORKHUB_REPO", str(original))
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    monkeypatch.setattr(core, "_PROCESS_REPO_ROOT_OVERRIDE", switched)

    assert core.repo_root() == switched.resolve()
    assert dashboard._default_repo_root() == switched.resolve()
    provider = dashboard.DashboardProvider()
    assert provider.repo_root.resolve() == switched.resolve()
    assert provider.get_storage_readiness().repo_id == task_store.storage_readiness(
        switched
    ).repo_id


def _insert_canonical_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str = "pending",
    worker_status: str = "unclaimed",
    runner: str = "r",
    topic: str = "task_mcp",
) -> None:
    card = {
        "task_id": task_id,
        "status": status,
        "worker_status": worker_status,
        "runner": runner,
        "topic": topic,
    }
    conn.execute(
        "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
        "objective, card_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            runner,
            topic,
            "solo",
            status,
            worker_status,
            "normal",
            "objective",
            json.dumps(card, sort_keys=True),
            NOW,
            NOW,
        ),
    )


def _chmod_blocked_by_sandbox() -> bool:
    import tempfile

    with tempfile.TemporaryDirectory() as name:
        try:
            os.chmod(name, 0o700)
        except PermissionError:
            return True
    return False


@pytest.fixture(autouse=True)
def _bridge_chmod_sandbox_restriction(monkeypatch: pytest.MonkeyPatch) -> None:
    """See test_process_launcher_security.py's identical fixture docstring:
    neutralizes ``os.chmod``/``os.fchmod`` ONLY when this exact sandbox
    genuinely rejects the bare syscall (probed once); a no-op elsewhere."""
    if _chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.task_rows = {
            "pending": [
                {
                    "task_id": "TASK_PENDING_B12_V1",
                    "status": "pending",
                    "topic": "imagery",
                    "runner": "runner_a_b12",
                }
            ],
            "processing": [
                {
                    "task_id": "TASK_STALE_B13_V1",
                    "status": "processing",
                    "topic": "mapping",
                    "runner": "runner_b_b12",
                }
            ],
            "review": [
                {
                    "task_id": "TASK_REVIEW_B12_V1",
                    "status": "review",
                    "topic": "imagery",
                    "runner": "runner_a_b12",
                }
            ],
            "blocked": [
                {
                    "task_id": "TASK_BLOCKED_B12_V1",
                    "status": "blocked",
                    "topic": "routing",
                    "runner": "runner_c_b12",
                }
            ],
            "finished": [],
            "archived": [],
        }
        self.cards = {
            "TASK_PENDING_B12_V1": {
                "task_id": "TASK_PENDING_B12_V1",
                "status": "pending",
                "topic": "imagery",
                "runner": "runner_a_b12",
                "objective": "Prepare imagery fixtures",
                "allowed_writes": ["fixtures/imagery.json"],
            },
            "TASK_REVIEW_B12_V1": {
                "task_id": "TASK_REVIEW_B12_V1",
                "status": "review",
                "topic": "imagery",
                "runner": "runner_a_b12",
                "objective": "Review imagery fixtures",
                "validation_status": "PASS",
                "result": {"tests": "passed"},
            },
        }

    def list_tasks(self, status: str):
        self.calls.append(("list_tasks", status))
        return [dict(row) for row in self.task_rows[status]]

    def get_task(self, task_id: str):
        self.calls.append(("get_task", task_id))
        card = self.cards.get(task_id)
        return dict(card) if card else None

    def get_completion_inbox(self):
        self.calls.append(("get_completion_inbox", None))
        return {
            "review_queue": [
                {
                    "task_id": "TASK_REVIEW_B12_V1",
                    "runner": "runner_a_b12",
                    "topic": "imagery",
                    "priority": "high",
                    "objective": "Review imagery fixtures",
                    "validation_status": "PASS",
                    "updated_at": "2026-07-10T10:00:00+00:00",
                }
            ],
            "stale_processing": [
                {
                    "task_id": "TASK_STALE_B13_V1",
                    "runner": "runner_b_b12",
                    "topic": "mapping",
                    "stale_hours": 31.5,
                    "last_activity_at": "2026-07-09T02:00:00+00:00",
                }
            ],
            "runner_mismatch_warnings": [
                {
                    "task_id": "TASK_STALE_B13_V1",
                    "runner": "runner_b_b12",
                    "topic": "mapping",
                    "warning": "RUNNER_TASK_BATCH_MISMATCH",
                }
            ],
            "latest_validation_facts": [
                {
                    "task_id": "TASK_STALE_B13_V1",
                    "lifecycle_state": "processing",
                    "validation_status": "FAIL",
                    "validation_error": "worker heartbeat expired",
                    "last_activity_at": "2026-07-09T02:00:00+00:00",
                }
            ],
            "read_errors": [],
        }

    def get_cost_ledger(self):
        self.calls.append(("get_cost_ledger", None))
        return {
            "aggregates": {
                "by_runner": {
                    "runner_a_b12": {
                        "records": 2,
                        "input_tokens": 100,
                        "output_tokens": 40,
                        "total_tokens": 140,
                        "cost_usd": 0.03,
                    },
                    "runner_b_b12": {
                        "records": 1,
                        "input_tokens": 20,
                        "output_tokens": 10,
                        "total_tokens": 30,
                        "cost_usd": 0.01,
                    },
                }
            },
            "source_status": {"usage_report_ok": True, "launch_log_ok": True},
        }

    def get_collision_report(self):
        self.calls.append(("get_collision_report", None))
        return {
            "collision_free": False,
            "active_cards": 4,
            "collision_count": 1,
            "file_collisions": [
                {
                    "file": "fixtures/imagery.json",
                    "conflicting_tasks": ["TASK_PENDING_B12_V1", "TASK_REVIEW_B12_V1"],
                }
            ],
        }

    def get_agent_processes(self):
        self.calls.append(("get_agent_processes", None))
        return {
            "ok": True,
            "total_requests": 1,
            "processes": [{
                "request_id": "run-1",
                "task_id": "TASK_REVIEW_B12_V1",
                "runner": "runner_a_b12",
                "topic": "imagery",
                "adapter_id": "claude_cli",
                "state": "review_ready",
                "exit_code": 0,
            }],
        }

    def get_callback_bridge_health(self):
        self.calls.append(("get_callback_bridge_health", None))
        return {
            "total": 3,
            "by_state": {"pending": 0, "inflight": 1, "delivered": 2, "dead_letter": 0, "superseded": 0},
            "bound_task_count": 3,
            "unbound_task_count": 0,
            "last_delivered_task_id": "TASK_REVIEW_B12_V1",
            "last_delivered_transition": "review_ready",
            "last_delivered_at": "2026-07-15T00:00:00+00:00",
            "last_dead_letter_task_id": "",
            "last_dead_letter_transition": "",
            "last_dead_letter_at": "",
            "last_dead_letter_error": "",
            "batches": {
                "total": 1,
                "by_state": {"pending": 0, "inflight": 1, "delivered": 0, "dead_letter": 0, "superseded": 0},
                "inflight_batch_member_count": 3,
                "oldest_pending_batch_age_seconds": 0.0,
                "last_dead_letter_batch_member_count": 0,
                "last_dead_letter_batch_at": "",
                "last_dead_letter_batch_error": "",
            },
        }


class PartiallyFailingProvider(FakeProvider):
    def list_tasks(self, status: str):
        if status == "processing":
            raise TimeoutError("processing read timed out")
        return super().list_tasks(status)

    def get_completion_inbox(self):
        raise RuntimeError("completion inbox unavailable")

    def get_cost_ledger(self):
        raise RuntimeError("usage source unavailable")


def test_build_snapshot_combines_read_sources_and_operational_summaries():
    provider = FakeProvider()

    snapshot = dashboard.build_snapshot(provider)

    assert snapshot["readonly"] is True
    assert snapshot["health"] == {"ok": True, "degraded": False, "provider_error_count": 0}
    assert snapshot["status_counts"] == {
        "pending": 1,
        "processing": 1,
        "review": 1,
        "blocked": 1,
        "finished": 0,
        "archived": 0,
        "stale": 1,
        "active": 3,
    }
    assert snapshot["tasks"]["processing"][0]["stale"] is True
    assert snapshot["tasks"]["processing"][0]["validation_status"] == "FAIL"
    assert snapshot["tasks"]["review"][0]["objective"] == "Review imagery fixtures"
    assert snapshot["cost_usage"]["totals"]["total_tokens"] == 170
    assert snapshot["cost_usage"]["totals"]["cost_usd"] == pytest.approx(0.04)
    assert snapshot["warnings"]["collisions"][0]["file"] == "fixtures/imagery.json"
    assert snapshot["warnings"]["runner_mismatches"][0]["task_id"] == "TASK_STALE_B13_V1"
    assert snapshot["agent_processes"]["processes"][0]["state"] == "review_ready"
    assert snapshot["callback_bridge_health"]["batches"]["inflight_batch_member_count"] == 3
    assert snapshot["callback_bridge_health"]["by_state"]["inflight"] == 1
    assert "project_context_telemetry" in snapshot

    topic_stats = {row["name"]: row for row in snapshot["summaries"]["topics"]}
    assert topic_stats["imagery"]["total"] == 2
    assert topic_stats["mapping"]["stale"] == 1
    assert [call for call in provider.calls if call[0] == "list_tasks"] == [
        ("list_tasks", "pending"),
        ("list_tasks", "processing"),
        ("list_tasks", "review"),
        ("list_tasks", "blocked"),
        ("list_tasks", "finished"),
        ("list_tasks", "archived"),
    ]


def test_project_context_telemetry_uses_latest_run_per_task():
    report = {
        "processes": [
            {
                "task_id": "TASK_A",
                "ai_infra_context": {
                    "session_current_state": {"requested": True, "executed": True, "hit_count": 8, "bytes": 1200},
                    "ai_memory": {"requested": True, "executed": True, "hit_count": 3, "bytes": 400},
                    "kb": {"requested": True, "executed": True, "hit_count": 2, "bytes": 300},
                },
            },
            {"task_id": "TASK_A", "ai_infra_context": {"ai_memory": {"hit_count": 99}}},
            {
                "task_id": "TASK_B",
                "ai_infra_context": {
                    "session_current_state": {"requested": True, "executed": True, "hit_count": 4, "bytes": 600},
                    "ai_memory": {"requested": True, "executed": True, "hit_count": 0, "bytes": 0},
                    "kb": {"requested": True, "executed": False, "hit_count": 0, "bytes": 0, "degraded_reason": "unavailable"},
                },
            },
        ]
    }
    telemetry = dashboard._project_context_telemetry(report)  # noqa: SLF001
    assert telemetry["observed_tasks"] == 2
    assert telemetry["session_current_state"]["hit_count"] == 12
    assert telemetry["ai_memory"]["hit_count"] == 3
    assert telemetry["kb"]["degraded_tasks"] == 1


def test_callback_bridge_health_batch_stats_never_expose_full_thread_id(tmp_path, monkeypatch):
    """B402: real end-to-end proof (not a mocked provider) that a live
    inflight batch's origin_thread_id never reaches the dashboard -- only
    redacted counts/ages, via the REAL canonical DashboardProvider/task_store
    stack, not FakeProvider."""
    root = _init_canonical_repo(tmp_path)
    conn = sqlite3.connect(_canonical_db(root))
    thread_id = "11111111-2222-4333-8444-555555555555"
    batch_id = "batch-redacted-health"
    for i in range(3):
        task_id = f"REAL_BATCH_{i}"
        _insert_canonical_task(
            conn, task_id, status="review", worker_status="review"
        )
        conn.execute(
            "INSERT INTO callback_outbox "
            "(task_id, origin_thread_id, episode_id, batch_id, transition, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'review_ready', 'inflight', ?, ?)",
            (task_id, thread_id, f"episode-{i}", batch_id, NOW, NOW),
        )
    conn.execute(
        "INSERT INTO callback_batches "
        "(batch_id, origin_thread_id, state, created_at, updated_at) "
        "VALUES (?, ?, 'inflight', ?, ?)",
        (batch_id, thread_id, NOW, NOW),
    )
    conn.commit()
    conn.close()

    provider = dashboard.DashboardProvider(repo_root=root)
    health = provider.get_callback_bridge_health()
    serialized = json.dumps(health)
    assert thread_id not in serialized
    assert health["batches"]["by_state"]["inflight"] == 1
    assert health["batches"]["inflight_batch_member_count"] == 3
    assert health["bound_task_count"] == 3
    assert health["backlog_count"] == 3
    assert health["attempts_total"] == 0
    assert health["retry_count"] == 0
    assert health["oldest_pending_at"] == NOW
    assert health["last_delivered_at"] == ""

    snapshot = dashboard.build_snapshot(provider)
    assert thread_id not in json.dumps(snapshot)
    assert snapshot["callback_bridge_health"]["batches"]["inflight_batch_member_count"] == 3


def test_callback_bridge_health_does_not_project_historical_dead_letter_as_current(tmp_path):
    root = _init_canonical_repo(tmp_path, "callback-current-truth")
    conn = sqlite3.connect(_canonical_db(root))
    conn.row_factory = sqlite3.Row
    _insert_canonical_task(conn, "OLD_FAILURE", status="blocked")
    _insert_canonical_task(conn, "NEW_SUCCESS", status="finished")
    conn.execute(
        "INSERT INTO callback_outbox "
        "(task_id, origin_thread_id, transition, episode_id, state, "
        "created_at, updated_at, last_error) VALUES (?,?,?,?,?,?,?,?)",
        (
            "OLD_FAILURE", "019f-old", "validation_failed", "1",
            "dead_letter", "2026-08-06T00:00:00+00:00",
            "2026-08-06T00:00:00+00:00", "direct app-server input is not allowed",
        ),
    )
    conn.execute(
        "INSERT INTO callback_outbox "
        "(task_id, origin_thread_id, transition, episode_id, state, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (
            "NEW_SUCCESS", "019f-current", "review_ready", "1",
            "delivered", "2026-08-09T00:00:00+00:00", "2026-08-09T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    health = task_store.callback_bridge_health(root)
    assert health["by_state"]["dead_letter"] == 1
    assert health["last_dead_letter_error"] == "direct app-server input is not allowed"
    assert health["current_delivery_status"] == "healthy"
    assert health["current_delivery_error"] == ""
    assert health["recovered_after_last_dead_letter"] is True

    conn = sqlite3.connect(_canonical_db(root))
    conn.execute(
        "INSERT INTO callback_outbox "
        "(task_id, origin_thread_id, transition, episode_id, state, "
        "created_at, updated_at, last_error) VALUES (?,?,?,?,?,?,?,?)",
        (
            "OLD_FAILURE", "019f-current", "validation_failed", "2",
            "dead_letter", "2026-08-10T00:00:00+00:00",
            "2026-08-10T00:00:00+00:00", "new current failure",
        ),
    )
    conn.commit()
    conn.close()

    degraded = task_store.callback_bridge_health(root)
    assert degraded["current_delivery_status"] == "degraded"
    assert degraded["current_delivery_error"] == "new current failure"
    assert degraded["recovered_after_last_dead_letter"] is False


def test_exact_status_counts_reports_totals_past_default_task_limit(tmp_path, monkeypatch):
    """B455: authoritative finished cards can exceed DEFAULT_TASK_LIMIT
    (500). exact_status_counts must report the true total via one narrow
    SQLite aggregate over (archived_at, status, worker_status) -- never by
    fetching/rendering the finished row list -- and build_snapshot's
    status_counts/row_counts must reflect that exact total even though task
    rows stay bounded. A real canonical task store, not a mocked provider."""
    root = _init_canonical_repo(tmp_path)
    db_path = _canonical_db(root)
    with sqlite3.connect(db_path) as conn:
        for i in range(501):
            _insert_canonical_task(
                conn,
                f"FINISHED_B455_{i}",
                status="finished",
                worker_status="done",
            )
        _insert_canonical_task(conn, "PENDING_B455_0")
        before_total = int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
    assert before_total == 502

    counts = dashboard.exact_status_counts(root)
    assert counts["finished"] == 501
    assert counts["finished"] > dashboard.DEFAULT_TASK_LIMIT
    assert counts["pending"] == 1
    assert sum(counts.values()) == 502

    class ExactOnlyProvider(FakeProvider):
        def get_exact_status_counts(self):
            return dashboard.exact_status_counts(root)

    snapshot = dashboard.build_snapshot(ExactOnlyProvider())
    assert snapshot["status_counts"]["finished"] == 501
    assert snapshot["status_counts"]["finished"] > dashboard.DEFAULT_TASK_LIMIT
    assert snapshot["row_counts"]["finished"] == {"returned": 0, "exact": 501, "truncated": True}
    assert snapshot["row_counts"]["archived"] == {"returned": 0, "exact": 0, "truncated": False}
    assert snapshot["status_counts"]["active"] == (
        snapshot["status_counts"]["pending"]
        + snapshot["status_counts"]["processing"]
        + snapshot["status_counts"]["review"]
    )

    # A pure read: the snapshot build must never mutate the queue.
    with sqlite3.connect(db_path) as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]) == before_total


def test_snapshot_isolates_provider_failures_without_hiding_healthy_groups():
    snapshot = dashboard.build_snapshot(PartiallyFailingProvider())

    assert snapshot["health"]["degraded"] is True
    assert snapshot["status_counts"]["processing"] == 0
    assert snapshot["status_counts"]["pending"] == 1
    assert snapshot["status_counts"]["blocked"] == 1
    assert snapshot["completion_inbox"] == {}
    assert snapshot["cost_usage"]["totals"]["available"] is False
    assert {error["source"] for error in snapshot["errors"]} >= {
        "tasks.processing",
        "completion_inbox",
        "cost_ledger",
    }


class _TransientExactCountsProvider(FakeProvider):
    """exact_status_counts fails once, then succeeds on retry."""

    def __init__(self):
        super().__init__()
        self._attempt = 0

    def get_exact_status_counts(self) -> dict[str, int]:
        self._attempt += 1
        if self._attempt == 1:
            raise OSError("database is locked (transient)")
        return {"pending": 3, "processing": 0, "review": 0,
                "blocked": 1, "finished": 0, "archived": 0,
                "superseded": 0, "stale": 0}


def test_exact_status_counts_recovers_from_transient_read_source_failure():
    """exact_status_counts retries once: transient lock/timeout
    should not degrade the snapshot or produce partial counts."""
    snapshot = dashboard.build_snapshot(_TransientExactCountsProvider())

    assert snapshot["health"]["degraded"] is False
    assert not snapshot["errors"]
    # Counts come from the successful retry, not bounded list_tasks.
    assert snapshot["status_counts"]["pending"] == 3
    assert snapshot["status_counts"]["blocked"] == 1
    assert snapshot["status_counts"]["finished"] == 0
    # Verify row_counts reflect exact source totals, not bounded list fallback.
    assert snapshot["row_counts"]["pending"]["exact"] == 3
    assert snapshot["row_counts"]["blocked"]["exact"] == 1


class _PersistentExactCountsProvider(FakeProvider):
    """exact_status_counts fails on both attempts."""

    def __init__(self):
        super().__init__()
        self._attempt = 0

    def get_exact_status_counts(self) -> dict[str, int]:
        self._attempt += 1
        raise RuntimeError("persistent storage unavailable")


def test_exact_status_counts_persistent_failure_stays_degraded():
    """Persistent exact_status_counts failure degrades truthfully
    and falls back to bounded task_limit counts."""
    snapshot = dashboard.build_snapshot(_PersistentExactCountsProvider())

    assert snapshot["health"]["degraded"] is True
    assert any(
        e["source"] == "exact_status_counts"
        for e in snapshot["errors"]
    )
    # Bounded counts from list_tasks, not fabricated.
    assert snapshot["status_counts"]["pending"] == 1
    assert snapshot["status_counts"]["blocked"] == 1
    assert snapshot["status_counts"]["finished"] == 0


def test_read_efficiency_telemetry_separates_observed_from_unobserved_tasks():
    process_report = {
        "processes": [
            {
                "task_id": "TASK_A",
                "adapter_id": "codex_cli",
                "ai_infra_context": {
                    "read_efficiency": {
                        "schema_id": "aiworkhub.provider_read_efficiency.v2",
                        "evidence_observed": True,
                        "provider_records_scanned": 12,
                        "recognized_read_events": 4,
                        "recognized_source_graph_events": 1,
                        "total_reads": 4,
                        "bounded_reads": 3,
                        "unbounded_reads": 1,
                        "exact_rereads": 1,
                        "read_bytes_observed": 3,
                        "total_read_bytes": 900,
                        "bounded_read_bytes": 600,
                        "unbounded_read_bytes": 300,
                    },
                },
            },
            {
                "task_id": "TASK_B",
                "adapter_id": "vscode_lm",
                "ai_infra_context": {
                    "read_efficiency": {"evidence_observed": False},
                },
            },
        ],
    }

    report = dashboard._read_efficiency_telemetry(process_report)

    assert report["observed_tasks"] == 2
    assert report["evidence_observed_tasks"] == 1
    assert report["evidence_unobserved_tasks"] == 1
    assert report["total_reads"] == 4
    assert report["bounded_read_rate"] == 75.0
    assert report["exact_reread_rate"] == 25.0
    assert report["read_byte_coverage_rate"] == 75.0
    assert report["total_read_bytes"] == 900
    assert report["by_adapter"]["vscode_lm"]["evidence_observed_tasks"] == 0


def test_read_efficiency_telemetry_excludes_legacy_codex_double_count_rows():
    process_report = {
        "processes": [
            {
                "task_id": "LEGACY",
                "adapter_id": "codex_cli",
                "ai_infra_context": {
                    "read_efficiency": {
                        "schema_id": "aiworkhub.provider_read_efficiency.v1",
                        "evidence_observed": True,
                        "total_reads": 2,
                        "unknown_repetitions": 1,
                    },
                },
            },
            {
                "task_id": "CURRENT",
                "adapter_id": "codex_cli",
                "ai_infra_context": {
                    "read_efficiency": {
                        "schema_id": "aiworkhub.provider_read_efficiency.v2",
                        "evidence_observed": True,
                        "total_reads": 1,
                        "bounded_reads": 1,
                    },
                },
            },
        ],
    }

    report = dashboard._read_efficiency_telemetry(process_report)

    assert report["schema_id"] == "aiworkhub.read_efficiency.telemetry.v2"
    assert report["evidence_observed_tasks"] == 1
    assert report["legacy_evidence_tasks"] == 1
    assert report["total_reads"] == 1
    assert report["unknown_repetitions"] == 0
    assert report["by_adapter"]["codex_cli"]["legacy_evidence_tasks"] == 1


def test_compact_ai_infra_keeps_only_path_free_read_aggregates():
    compact = dashboard._compact_ai_infra({
        "read_efficiency": {
            "schema_id": "aiworkhub.provider_read_efficiency.v2",
            "evidence_observed": True,
            "provider_records_scanned": 4,
            "recognized_read_events": 2,
            "total_reads": 2,
            "bounded_reads": 1,
            "total_read_bytes": 123,
            "events": [{"path": "/secret/repository/file.py"}],
            "recommendations": ["raw provider detail"],
            "measurement_label": "observed",
        },
    })

    assert compact["read_efficiency"]["total_reads"] == 2
    assert compact["read_efficiency"]["schema_id"] == "aiworkhub.provider_read_efficiency.v2"
    assert compact["read_efficiency"]["total_read_bytes"] == 123
    serialized = json.dumps(compact, sort_keys=True)
    assert "/secret/repository/file.py" not in serialized
    assert "raw provider detail" not in serialized


def test_compact_ai_infra_keeps_only_path_free_semantic_edit_aggregates():
    compact = dashboard._compact_ai_infra({
        "semantic_edit": {
            "schema_id": "aiworkhub.semantic_edit_runtime_evidence.v1",
            "observed": True,
            "file_count": 1,
            "range_count": 2,
            "file_bytes": 8_453,
            "old_region_bytes": 112,
            "replacement_bytes": 91,
            "model_reemitted_old_bytes": 0,
            "path": "/secret/repository/file.py",
            "replacement": "secret source text",
            "token_savings_claimed": True,
        },
    })

    evidence = compact["semantic_edit"]
    assert evidence["file_bytes"] == 8_453
    assert evidence["range_count"] == 2
    assert evidence["token_savings_claimed"] is False
    serialized = json.dumps(compact, sort_keys=True)
    assert "/secret/repository/file.py" not in serialized
    assert "secret source text" not in serialized


def test_production_provider_uses_only_existing_read_paths(monkeypatch, tmp_path):
    calls = []
    root = _init_canonical_repo(tmp_path)

    def fake_list_tasks(repo_root, status="pending", limit=80):
        calls.append(("task_store.list_tasks", repo_root, status, limit))
        if status is None:
            # get_cost_ledger scans the full task store with status=None.
            return []
        return [{
            "task_id": f"TASK_{status.upper()}_B12_V1",
            "status": status,
            "topic": "imagery",
            "runner": "runner_a_b12",
        }]

    def fake_get_task(repo_root, task_id):
        calls.append(("task_store.get_task", repo_root, task_id))
        return {"task_id": task_id}

    def fake_cost_ledger(*, repo_root, include_tasks):
        calls.append(("cost_ledger.build_cost_ledger", repo_root, include_tasks))
        return {
            "schema_id": "aiworkhub.cost_ledger.v1",
            "aggregates": {
                "by_runner": {
                    "runner_a": {"total_tokens": 123, "cost_usd": 0.5},
                },
            },
            "source_status": {"launch_log_ok": True},
        }

    def forbidden_legacy_provider(*_args, **_kwargs):
        raise AssertionError("canonical dashboard must not call a legacy provider")

    monkeypatch.setattr(dashboard.task_store, "list_tasks", fake_list_tasks)
    monkeypatch.setattr(dashboard.task_store, "get_task", fake_get_task)
    monkeypatch.setattr(
        dashboard.completion_inbox, "build_completion_inbox", forbidden_legacy_provider
    )
    monkeypatch.setattr(dashboard.cost_ledger, "build_cost_ledger", fake_cost_ledger)
    monkeypatch.setattr(dashboard.core, "collision_guard", forbidden_legacy_provider)

    provider = dashboard.DashboardProvider(
        task_limit=25, stale_processing_hours=6, repo_root=root
    )
    assert provider.list_tasks("pending")[0]["task_id"] == "TASK_PENDING_B12_V1"
    assert provider.get_task("TASK_PENDING_B12_V1") == {"task_id": "TASK_PENDING_B12_V1"}
    assert provider.get_completion_inbox()["review_queue"][0]["task_id"] == "TASK_REVIEW_B12_V1"
    ledger = provider.get_cost_ledger()
    assert ledger["aggregates"]["by_runner"]["runner_a"]["total_tokens"] == 123
    assert ledger["schema_id"] == "aiworkhub.dashboard.canonical_cost_ledger.v1"
    assert provider.get_collision_report()["collision_free"] is True

    assert ("task_store.list_tasks", root, "pending", 25) in calls
    assert ("task_store.get_task", root, "TASK_PENDING_B12_V1") in calls
    assert ("cost_ledger.build_cost_ledger", root, False) in calls
    assert all(
        call[0].startswith("task_store.")
        or call[0] == "cost_ledger.build_cost_ledger"
        for call in calls
    )


def test_process_run_reader_uses_latest_allowlisted_events_without_manager_side_effects(
    tmp_path,
    monkeypatch,
):
    process_log = tmp_path / "process_events.jsonl"
    events = [
        {
            "schema_id": "aiworkhub.task_mcp.process_event.v1",
            "timestamp": "2026-07-10T10:00:00+00:00",
            "request_id": "run-1",
            "task_id": "TASK_REVIEW_B12_V1",
            "runner": "runner_a_b12",
            "topic": "imagery",
            "adapter_id": "claude_cli",
            "state": "running",
            "started_at": "2026-07-10T10:00:00+00:00",
            "secret_environment": "must-not-be-exposed",
        },
        {
            "schema_id": "aiworkhub.task_mcp.process_event.v1",
            "timestamp": "2026-07-10T10:05:00+00:00",
            "request_id": "run-1",
            "state": "review_ready",
            "exit_code": 0,
            "finished_at": "2026-07-10T10:05:00+00:00",
        },
    ]
    process_log.write_text(
        "\n".join([*(json.dumps(event) for event in events), "not-json"]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(dashboard.process_launcher.PROCESS_LOG_ENV, str(process_log))

    def fail_default_manager():
        raise AssertionError("dashboard process reads must not construct ProcessManager")

    monkeypatch.setattr(dashboard.process_launcher, "default_manager", fail_default_manager)
    report = dashboard.DashboardProvider().get_agent_processes()

    assert report["readonly"] is True
    assert report["source"] == "process_event_log"
    assert report["total_requests"] == 1
    assert report["invalid_records"] == 1
    assert report["processes"] == [{
        "request_id": "run-1",
        "task_id": "TASK_REVIEW_B12_V1",
        "runner": "runner_a_b12",
        "topic": "imagery",
        "adapter_id": "claude_cli",
        "state": "review_ready",
        "started_at": "2026-07-10T10:00:00+00:00",
        "timestamp": "2026-07-10T10:05:00+00:00",
        "exit_code": 0,
        "finished_at": "2026-07-10T10:05:00+00:00",
    }]


def test_agent_processes_expose_derived_liveness_never_the_raw_status_path(tmp_path, monkeypatch):
    """B412: a running request whose supervisor heartbeat file is present
    surfaces bounded, derived liveness fields (state/ages/model/runtime) --
    but the dashboard never exposes the raw status file PATH itself,
    matching the existing policy of never exposing stdout_path/stderr_path."""
    process_log = tmp_path / "process_events.jsonl"
    status_path = tmp_path / "req-1.supervisor.json"
    now = time.time()
    fd = os.open(status_path, os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({
            "state": "running",
            "heartbeat_at_epoch": now - 1.0,
            "last_output_change_epoch": now - 2.0,
            "started_at_epoch": now - 30.0,
            "heartbeat_seq": 5,
            "child_pid": os.getpid(),
            "child_pid_start_ticks": dashboard.process_launcher._pid_start_ticks(os.getpid()),
        }, fh)

    own_ticks = dashboard.process_launcher._pid_start_ticks(os.getpid())
    events = [{
        "schema_id": "aiworkhub.task_mcp.process_event.v1",
        "timestamp": "2026-07-10T10:00:00+00:00",
        "request_id": "req-1",
        "task_id": "TASK_LIVE_B412_V1",
        "runner": "runner_a_b12",
        "topic": "coding",
        "adapter_id": "claude_cli",
        "model": "claude-sonnet-5",
        "state": "running",
        "pid": os.getpid(),
        "pid_start_ticks": own_ticks,
        "started_at": "2026-07-10T10:00:00+00:00",
        "supervisor_status_path": str(status_path),
    }]
    process_log.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    monkeypatch.setenv(dashboard.process_launcher.PROCESS_LOG_ENV, str(process_log))

    report = dashboard.read_process_runs()
    row = report["processes"][0]
    assert row["liveness_state"] == "alive"
    assert row["supervisor_alive"] is True
    assert row["child_alive"] is True
    assert row["heartbeat_seq"] == 5
    assert row["heartbeat_age_seconds"] == pytest.approx(1.0, abs=0.5)
    assert "last_activity_at" in row
    assert "supervisor_status_path" not in row
    assert "metadata_path" not in row


def test_terminal_review_ready_process_is_not_mislabeled_lost(tmp_path, monkeypatch):
    process_log = tmp_path / "process_events.jsonl"
    status_path = tmp_path / "req-terminal.supervisor.json"
    status_path.write_text(
        json.dumps({
            "state": "exited",
            "heartbeat_at_epoch": time.time() - 30.0,
            "child_pid": 99999999,
        }),
        encoding="utf-8",
    )
    status_path.chmod(0o600)
    events = [
        {
            "request_id": "req-terminal",
            "task_id": "TASK_TERMINAL_REVIEW_V1",
            "state": "running",
            "supervisor_status_path": str(status_path),
            "pid": 99999999,
            "timestamp": "2026-07-10T10:00:00+00:00",
        },
        {
            "request_id": "req-terminal",
            "task_id": "TASK_TERMINAL_REVIEW_V1",
            "state": "review_ready",
            "exit_code": 0,
            "finished_at": "2026-07-10T10:05:00+00:00",
            "timestamp": "2026-07-10T10:05:00+00:00",
        },
    ]
    process_log.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(dashboard.process_launcher.PROCESS_LOG_ENV, str(process_log))

    row = dashboard.read_process_runs()["processes"][0]
    assert row["state"] == "review_ready"
    assert row["exit_code"] == 0
    assert "liveness_state" not in row
    assert "process_alive" not in row
    assert "observed_state" not in row


def test_task_rows_receive_last_activity_and_liveness_from_process_report(monkeypatch):
    """B412: the Activity column must show a real timestamp -- not
    "Unknown" -- for a task actively driven by an isolated worker, sourced
    from the bounded process-liveness report, not from raw process
    existence."""

    class LiveProvider(FakeProvider):
        def get_agent_processes(self):
            return {
                "ok": True,
                "total_requests": 1,
                "processes": [{
                    "request_id": "req-live",
                    "task_id": "TASK_STALE_B13_V1",
                    "state": "running",
                    "model": "claude-sonnet-5",
                    "liveness_state": "quiet",
                    "heartbeat_age_seconds": 2.0,
                    "activity_age_seconds": 1200.0,
                    "supervisor_alive": True,
                    "child_alive": True,
                    "runtime_seconds": 300.0,
                    "last_activity_at": "2026-07-15T14:00:00+00:00",
                }],
            }

    snapshot = dashboard.build_snapshot(LiveProvider())
    processing_row = snapshot["tasks"]["processing"][0]
    assert processing_row["task_id"] == "TASK_STALE_B13_V1"
    assert processing_row["liveness_state"] == "quiet"
    assert processing_row["supervisor_alive"] is True
    assert processing_row["runtime_seconds"] == 300.0
    # last_activity_at from the more-specific completion_inbox fact
    # (latest_validation_facts) is preserved, not clobbered by the
    # process-liveness merge.
    assert processing_row["last_activity_at"] == "2026-07-09T02:00:00+00:00"


def test_task_row_uses_newest_retry_instead_of_older_launch_failure():
    groups = {
        "processing": [{"task_id": "TASK_RETRY", "status": "processing"}],
    }
    dashboard._merge_process_liveness_into_tasks(
        groups,
        {
            "processes": [
                {
                    "request_id": "new-request",
                    "task_id": "TASK_RETRY",
                    "state": "running",
                    "liveness_state": "alive",
                    "supervisor_alive": True,
                },
                {
                    "request_id": "old-request",
                    "task_id": "TASK_RETRY",
                    "state": "launch_failed",
                    "liveness_state": "lost",
                    "supervisor_alive": False,
                },
            ],
        },
    )

    assert groups["processing"][0]["liveness_state"] == "alive"
    assert groups["processing"][0]["supervisor_alive"] is True


def test_task_row_exposes_structured_provider_root_error_without_secrets():
    class FailedProvider(FakeProvider):
        def get_agent_processes(self):
            return {
                "ok": True,
                "processes": [{
                    "task_id": "TASK_PENDING_B12_V1",
                    "state": "launch_failed",
                    "adapter_id": "claude_cli",
                    "model": "claude-sonnet-5",
                    "blocked_reason": (
                        "401 authentication_failed token=super-secret-provider-token"
                    ),
                }],
            }

    snapshot = dashboard.build_snapshot(FailedProvider())
    row = snapshot["tasks"]["pending"][0]
    terminal = row["provider_terminal"]
    assert terminal["state"] == "launch_failed"
    assert terminal["category"] == "provider_authentication_failed"
    assert terminal["retryable"] is True
    assert terminal["recommended_action"] == "repair_provider_authentication_then_retry"
    assert terminal["adapter_id"] == "claude_cli"
    assert "super-secret-provider-token" not in json.dumps(terminal)


def test_validation_failed_terminal_recommends_residual_rework():
    terminal = dashboard._provider_terminal_status({
        "state": "validation_failed",
        "reason": "focused validation failed",
    })

    assert terminal["retryable"] is True
    assert terminal["recommended_action"] == "reject_review_to_residual_rework"


def test_task_detail_uses_actual_process_model_and_adapter():
    class ModelProvider(FakeProvider):
        def get_agent_processes(self):
            return {
                "ok": True,
                "processes": [{
                    "task_id": "TASK_PENDING_B12_V1",
                    "model": "deepseek-v4-pro",
                    "adapter_id": "deepseek_copilot_cli",
                }],
            }

    detail = dashboard.build_task_detail("TASK_PENDING_B12_V1", ModelProvider())
    assert detail is not None
    assert detail["task"]["model"] == "deepseek-v4-pro"
    assert detail["task"]["adapter_id"] == "deepseek_copilot_cli"


def test_task_detail_builds_bounded_portable_review_evidence_bundle():
    class EvidenceProvider:
        def get_task(self, task_id):
            assert task_id == "TASK_REVIEW_EVIDENCE_B13_V1"
            return {
                "task_id": task_id,
                "status": "review",
                "worker_status": "review",
                "completion_summary": (
                    "validated /home/shrek/private/result.json token=super-secret-value"
                ),
                "artifacts": [
                    "eval/evidence.json",
                    "/home/shrek/private/raw-output.json",
                ],
                "terminal_review": {
                    "substatus": "review_ready",
                    "evidence": {
                        "changed_paths": ["src/aiworkhub/dashboard.py"],
                        "changed_path_hashes": {"src/aiworkhub/dashboard.py": "a" * 64},
                        "promoted_paths": ["src/aiworkhub/dashboard.py"],
                        "validation": [
                            {
                                "command": "python /home/shrek/private/check.py",
                                "ok": True,
                                "returncode": 0,
                                "summary": "passed password=hunter2",
                            }
                        ],
                        "required_outputs": [
                            {
                                "path": "eval/evidence.json",
                                "sha256": "b" * 64,
                                "bytes": 42,
                            }
                        ],
                        "quality_gate": {
                            "schema_id": "aiworkhub.completion_quality_gate.v1",
                            "passed": False,
                            "blocking_checks": ["reviewer:security:unsafe-path"],
                            "checks": [
                                {
                                    "check_id": "tests",
                                    "kind": "test",
                                    "status": "passed",
                                    "summary": "focused suite passed",
                                }
                            ],
                            "quality_verdict": {
                                "schema_id": "aiworkhub.quality_verdict.v2",
                                "status": "unverified",
                                "passed": False,
                                "refine_required": True,
                                "risk_profile": {"effective_tier": "high"},
                                "blocking_evidence": ["reviewer:security:unsafe-path"],
                                "lenses": [
                                    {
                                        "lens": "security",
                                        "status": "failed",
                                        "evidence_ids": ["security-scan"],
                                        "finding_ids": ["reviewer:security:unsafe-path"],
                                    }
                                ],
                            },
                        },
                        "error": "",
                    },
                },
            }

        def get_agent_processes(self):
            return {
                "ok": True,
                "processes": [
                    {
                        "task_id": "TASK_REVIEW_EVIDENCE_B13_V1",
                        "state": "completed",
                        "exit_code": 0,
                        "stdout_path": "/home/shrek/private/stdout.jsonl",
                        "stderr_path": "/home/shrek/private/stderr.log",
                        "usage": {"input_tokens": 12, "output_tokens": 5},
                    }
                ],
            }

        def get_task_events(self, task_id):
            assert task_id == "TASK_REVIEW_EVIDENCE_B13_V1"
            return [
                {
                    "event": "terminal_review",
                    "runner": "runner_b13",
                    "created_at": "2026-07-30T13:00:00+00:00",
                    "payload": json.dumps(
                        {
                            "from_state": "processing",
                            "to_state": "review",
                            "reason": "ready /home/shrek/private/task.json",
                        }
                    ),
                }
            ]

    detail = dashboard.build_task_detail(
        "TASK_REVIEW_EVIDENCE_B13_V1", EvidenceProvider()
    )
    assert detail is not None
    bundle = detail["task"]["review_evidence_bundle"]
    assert bundle["schema_id"] == "aiworkhub.review_evidence_bundle.v1"
    assert bundle["terminal"]["substatus"] == "review_ready"
    assert bundle["diff"]["changed_paths"] == ["src/aiworkhub/dashboard.py"]
    assert bundle["tests"][0]["ok"] is True
    assert bundle["tests"][0]["command"] == "python <host-path>"
    assert bundle["tests"][0]["summary"] == "passed password=<redacted>"
    assert bundle["required_outputs"][0]["bytes"] == 42
    quality = detail["task"]["quality_gate"]
    assert quality["passed"] is False
    assert quality["quality_verdict"] == {
        "schema_id": "aiworkhub.quality_verdict.v2",
        "status": "unverified",
        "passed": False,
        "refine_required": True,
        "risk_tier": "high",
        "blocking_evidence": ["reviewer:security:unsafe-path"],
        "lenses": [
            {
                "lens": "security",
                "status": "failed",
                "evidence_count": 1,
                "finding_count": 1,
            }
        ],
    }
    assert bundle["artifacts"] == [
        "eval/evidence.json",
        "<host-path>/raw-output.json",
    ]
    assert bundle["approvals"][0] == {
        "event": "terminal_review",
        "timestamp": "2026-07-30T13:00:00+00:00",
        "actor": "runner_b13",
        "from_state": "processing",
        "to_state": "review",
        "reason": "ready <host-path>",
    }
    serialized = json.dumps(bundle, sort_keys=True)
    assert "/home/shrek" not in serialized
    assert "super-secret-value" not in serialized
    assert "hunter2" not in serialized
    assert "stdout_path" not in serialized
    assert "stderr_path" not in serialized


def test_task_detail_prefers_verified_reviewer_findings_over_terminal_prose():
    class ReviewerEvidenceProvider:
        def get_task(self, task_id):
            assert task_id == "REVIEW_TASK_TRUTH_B13_V1"
            return {
                "task_id": task_id,
                "status": "review",
                "worker_status": "review",
                "completion_summary": (
                    "Security review completed. No code changes required."
                ),
                "terminal_review": {
                    "substatus": "review_ready",
                    "evidence": {
                        "quality_review_receipt": {
                            "schema_id": "aiworkhub.quality_reviewer_receipt.v1",
                            "authority": {
                                "process_identity_verified": True,
                                "audit_verified": True,
                                "terminal_state": "review_ready",
                            },
                            "report": {
                                "lens": "security",
                                "read_only": True,
                                "can_mutate_repo": False,
                                "findings": [
                                    {"severity": "low"},
                                    {"severity": "low"},
                                ],
                            },
                        }
                    },
                },
            }

        def get_agent_processes(self):
            return {"ok": True, "processes": []}

        def get_task_events(self, task_id):
            return []

    detail = dashboard.build_task_detail(
        "REVIEW_TASK_TRUTH_B13_V1", ReviewerEvidenceProvider()
    )
    assert detail is not None
    logs = detail["task"]["review_evidence_bundle"]["logs"]
    assert logs["result_summary_source"] == "verified_quality_review_receipt"
    expected = "Verified security review: 2 findings (low: 2). Refinement required."
    assert logs["result_summary"] == expected
    assert logs["quality_review"] == {
        "source": "verified_quality_review_receipt",
        "text": expected,
        "lens": "security",
        "finding_count": 2,
        "severity_counts": {"critical": 0, "high": 0, "medium": 0, "low": 2},
        "refinement_required": True,
        "blocking_finding_count": 0,
    }
    assert "No code changes required" not in json.dumps(logs)
