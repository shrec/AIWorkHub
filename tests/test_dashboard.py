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

    def get_manager_decision_counts(self):
        self.calls.append(("get_manager_decision_counts", None))
        return {"accepted": 7, "rejected": 2, "total": 9}


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
    assert snapshot["outcome_counts"] == {
        "accepted": 7,
        "rejected": 2,
        "archived": 0,
        "superseded": 0,
        "finished": 0,
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
    # Independent status reads run concurrently; completion/call order is not
    # a contract, while the exact requested status set remains deterministic.
    assert sorted(call for call in provider.calls if call[0] == "list_tasks") == sorted([
        ("list_tasks", "pending"),
        ("list_tasks", "processing"),
        ("list_tasks", "review"),
        ("list_tasks", "blocked"),
        ("list_tasks", "finished"),
        ("list_tasks", "archived"),
    ])


def test_summary_snapshot_skips_full_webview_reads_without_changing_counts():
    full_provider = FakeProvider()
    summary_provider = FakeProvider()

    full = dashboard.build_snapshot(full_provider)
    summary = dashboard.build_snapshot(summary_provider, summary_only=True)

    assert summary["status_counts"] == full["status_counts"]
    assert summary["outcome_counts"] == full["outcome_counts"]
    assert summary["row_counts"] == full["row_counts"]
    assert summary["warnings"] == full["warnings"]
    assert summary["health"] == full["health"]
    called = {name for name, _argument in summary_provider.calls}
    assert "get_cost_ledger" not in called
    assert "get_agent_processes" not in called
    assert "get_callback_bridge_health" not in called
    assert summary["cost_usage"] == {}
    assert summary["agent_processes"] == {}


@pytest.mark.parametrize("malformed", ["n/a", "NaN", "Infinity", float("inf")])
def test_malformed_numeric_telemetry_does_not_blank_healthy_fields(malformed):
    compact = dashboard._compact_ai_infra({  # noqa: SLF001
        "usage": {
            "input_tokens": 12,
            "output_tokens": "bad",
            "observed_model": "provider/model",
            "model_observed": True,
            "usage_observed": True,
            "cost_usd": malformed,
            "cost_observed": True,
        }
    })

    assert compact["usage"]["input_tokens"] == 12
    assert compact["usage"]["output_tokens"] == 0
    assert compact["usage"]["observed_model"] == "provider/model"
    assert compact["usage"]["cost_usd"] == 0.0
    assert compact["usage"]["cost_observed"] is False

    totals = dashboard._cost_totals({  # noqa: SLF001
        "aggregates": {"by_runner": {
            "healthy": {"records": 1, "total_tokens": 12, "cost_usd": 0.25},
            "malformed": {
                "records": "bad",
                "total_tokens": float("inf"),
                "cost_usd": malformed,
            },
        }}
    })
    assert totals["records"] == 1
    assert totals["total_tokens"] == 12
    assert totals["cost_usd"] == 0.25


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


def test_snapshot_health_healthy_when_superseded_rows_exist(tmp_path, monkeypatch):
    """Dashboard snapshot health remains healthy when persisted superseded
    rows exist, the superseded exact count is exposed as its own bucket, and
    superseded is not folded into pending, active, processing, review, or
    finished counts."""
    root = _init_canonical_repo(tmp_path)
    db_path = _canonical_db(root)
    with sqlite3.connect(db_path) as conn:
        _insert_canonical_task(
            conn, "TASK_SUPERSEDED_NF159",
            status="superseded", worker_status="superseded",
        )

    class SupersededSnapshotProvider(FakeProvider):
        def get_exact_status_counts(self):
            return dashboard.exact_status_counts(root)

    snapshot = dashboard.build_snapshot(SupersededSnapshotProvider())

    assert snapshot["health"] == {"ok": True, "degraded": False, "provider_error_count": 0}
    assert not snapshot["errors"]
    assert snapshot["status_counts"]["superseded"] == 1
    # Superseded must not be folded into active or any lifecycle bucket.
    assert snapshot["status_counts"]["pending"] == 0
    assert snapshot["status_counts"]["processing"] == 0
    assert snapshot["status_counts"]["review"] == 0
    assert snapshot["status_counts"]["finished"] == 0
    assert snapshot["status_counts"]["active"] == 0


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
    assert ("cost_ledger.build_cost_ledger", root, True) in calls
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
                    "validated /home/shrek/private/result.json token=super-secret-value "
                    "Authorization: Bearer sk-or-v1-abc123def456"
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
                            },
                            {
                                "path": "credential: nf167-secret-value-xyz123/result.json",
                                "sha256": "c" * 64,
                                "bytes": 99,
                            },
                            {
                                "path": "Authorization: Bearer nf167-bearer-secret-token/result.json",
                                "sha256": "d" * 64,
                                "bytes": 101,
                            },
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
    assert "sk-or-v1-abc123def456" not in serialized
    assert "hunter2" not in serialized
    assert "nf167-secret-value-xyz123" not in serialized
    assert "nf167-bearer-secret-token" not in serialized
    assert "stdout_path" not in serialized
    assert "stderr_path" not in serialized


def test_task_detail_review_evidence_preserves_explicit_validation_truth():
    class TruthProvider:
        def get_task(self, task_id):
            assert task_id == "TASK_TRUTH_B13_V1"
            return {
                "task_id": task_id,
                "status": "review",
                "worker_status": "review",
                "terminal_review": {
                    "substatus": "review_ready",
                    "evidence": {
                        "validation": [
                            {"command": "ok-true", "ok": True, "returncode": 1},
                            {"command": "ok-false", "ok": False, "returncode": 0},
                            {"command": "passed-true", "passed": True, "returncode": 1},
                            {"command": "passed-false", "passed": False, "returncode": 0},
                            {"command": "rc-zero", "returncode": 0},
                            {"command": "rc-nonzero", "returncode": 2},
                            {"command": "timed-out", "returncode": None, "timed_out": True},
                            {
                                "command": "launch-failed",
                                "returncode": None,
                                "timed_out": False,
                                "launch_error": "FileNotFoundError",
                            },
                        ],
                    },
                },
            }

        def get_agent_processes(self):
            return {"ok": True, "processes": []}

        def get_task_events(self, task_id):
            return []

    detail = dashboard.build_task_detail("TASK_TRUTH_B13_V1", TruthProvider())
    assert detail is not None
    tests = detail["task"]["review_evidence_bundle"]["tests"]
    by_command = {row["command"]: row for row in tests}
    assert by_command["ok-true"]["ok"] is True
    assert by_command["ok-false"]["ok"] is False
    assert by_command["passed-true"]["ok"] is True
    assert by_command["passed-false"]["ok"] is False
    assert by_command["rc-zero"]["ok"] is True
    assert by_command["rc-nonzero"]["ok"] is False
    assert by_command["timed-out"]["ok"] is False
    assert by_command["launch-failed"]["ok"] is False
    assert all(row["history"] is False for row in tests)


def test_task_detail_review_evidence_binds_current_request_and_labels_history():
    class HistoryProvider:
        def get_task(self, task_id):
            assert task_id == "TASK_HISTORY_B13_V1"
            return {
                "task_id": task_id,
                "status": "review",
                "worker_status": "review",
                "terminal_review": {
                    "substatus": "review_ready",
                    "evidence": {
                        "request_identity": {
                            "request_id": "req-current",
                            "task_id": task_id,
                            "runner": "runner-current",
                            "topic": "topic-current",
                        },
                        "validation": [{"command": "current-check", "returncode": 0}],
                    },
                },
            }

        def get_agent_processes(self):
            return {"ok": True, "processes": []}

        def get_task_events(self, task_id):
            return [
                {
                    "event": "terminal_review",
                    "runner": "runner-pred",
                    "created_at": "2026-07-30T12:00:00+00:00",
                    "payload": json.dumps(
                        {
                            "substatus": "validation_failed",
                            "evidence": {
                                "request_identity": {
                                    "request_id": "req-pred",
                                    "task_id": task_id,
                                },
                                "validation": [
                                    {"command": "pred-check", "returncode": 1}
                                ],
                            },
                        }
                    ),
                }
            ]

    detail = dashboard.build_task_detail("TASK_HISTORY_B13_V1", HistoryProvider())
    assert detail is not None
    bundle = detail["task"]["review_evidence_bundle"]
    assert bundle["request_identity"]["request_id"] == "req-current"
    assert bundle["request_identity"]["runner"] == "runner-current"
    assert bundle["tests"][0]["ok"] is True
    assert bundle["tests"][0]["history"] is False
    assert len(bundle["tests"]) == 1
    assert len(bundle["validation_history"]) == 1
    history = bundle["validation_history"][0]
    assert history["command"] == "pred-check"
    assert history["ok"] is False
    assert history["history"] is True
    assert history["request_id"] == "req-pred"


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


def _write_protocol_alert_record(root: Path, *, count: int, reason: str) -> None:
    alert_dir = root / ".aiworkhub" / "runtime"
    alert_dir.mkdir(parents=True, exist_ok=True)
    (alert_dir / "mcp_protocol_alerts.json").write_text(
        json.dumps({
            "schema_id": "aiworkhub.mcp_control_plane.protocol_alert.v1",
            "count": count,
            "latest": {
                "method": "thread/resume",
                "request_id": None,
                "repo_identity": root.name,
                "boundary": "app_server_mux_sideband",
                "reason": reason,
                "timestamp": NOW,
            },
        }),
        encoding="utf-8",
    )


def test_protocol_alert_telemetry_empty_without_repo_root():
    telemetry = dashboard._protocol_alert_telemetry(None)
    assert telemetry == {
        "schema_id": "aiworkhub.mcp_control_plane.protocol_alert_telemetry.v1",
        "alert_count": 0,
        "latest_method": None,
        "latest_boundary": None,
        "latest_reason": None,
        "latest_timestamp": None,
    }


def test_protocol_alert_telemetry_empty_when_no_durable_record_yet(tmp_path):
    telemetry = dashboard._protocol_alert_telemetry(tmp_path)
    assert telemetry["alert_count"] == 0
    assert telemetry["latest_reason"] is None


def test_protocol_alert_telemetry_defaults_safely_on_malformed_record(tmp_path):
    alert_dir = tmp_path / ".aiworkhub" / "runtime"
    alert_dir.mkdir(parents=True)
    (alert_dir / "mcp_protocol_alerts.json").write_text("{not json", encoding="utf-8")

    telemetry = dashboard._protocol_alert_telemetry(tmp_path)

    assert telemetry["alert_count"] == 0
    assert telemetry["latest_reason"] is None


def test_protocol_alert_telemetry_reports_truthful_count_and_latest_reason(tmp_path):
    _write_protocol_alert_record(tmp_path, count=4, reason="invalid_params:empty_string")

    telemetry = dashboard._protocol_alert_telemetry(tmp_path)

    assert telemetry["alert_count"] == 4
    assert telemetry["latest_method"] == "thread/resume"
    assert telemetry["latest_boundary"] == "app_server_mux_sideband"
    assert telemetry["latest_reason"] == "invalid_params:empty_string"
    assert telemetry["latest_timestamp"] == NOW


def test_build_snapshot_includes_protocol_alert_telemetry_for_ready_repo(tmp_path):
    root = _init_canonical_repo(tmp_path, "protocol-alert-repo")
    provider = dashboard.DashboardProvider(repo_root=root)

    snapshot = dashboard.build_snapshot(provider)
    assert snapshot["protocol_alert_telemetry"]["alert_count"] == 0

    _write_protocol_alert_record(root, count=2, reason="invalid_params:non_object:list")

    snapshot = dashboard.build_snapshot(provider)
    telemetry = snapshot["protocol_alert_telemetry"]
    assert telemetry["alert_count"] == 2
    assert telemetry["latest_reason"] == "invalid_params:non_object:list"
    assert telemetry["latest_boundary"] == "app_server_mux_sideband"


def _rules_manifest_mapping() -> dict:
    from aiworkhub import development_rules as dr

    return {
        "schema": dr.SCHEMA_ID,
        "schema_version": dr.SCHEMA_VERSION,
        "languages": ["python"],
        "rules": [
            {
                "id": "forbidden_pattern_rule",
                "kind": "forbidden_pattern",
                "applicability": {},
                "forbid": ["eval_usage"],
                "payload": {"severity": "error", "rationale_id": "security_risk"},
            },
            {
                "id": "coding_convention_rule",
                "kind": "coding_convention",
                "applicability": {"paths": ["src/**/*.py"]},
                "payload": {"guideline_ids": ["naming_snake_case"], "severity": "warning"},
            },
        ],
    }


def _skill_mapping(**overrides: object) -> dict:
    data = {
        "identity": "commit-msg-check",
        "version": "1.0.0",
        "scope": "repository",
        "task_family": "commit",
        "path_or_symbol": "src/aiworkhub/skill_registry.py",
        "risk": "medium",
        "stage": "post-edit",
        "triggers": ["commit"],
        "confidence": 0.9,
    }
    data.update(overrides)
    return data


def _echo_recipe():
    from aiworkhub import tool_recipes as tr

    return tr.Recipe(id="echo-tool", version="1.0.0", argv=(tr.lit("echo"),))


class _FoundationProvider(FakeProvider):
    def __init__(self, **inputs: object) -> None:
        super().__init__()
        self._inputs = inputs

    def get_development_rules_projection_input(self):
        if "development_rules" not in self._inputs:
            raise AttributeError("development_rules")
        return self._inputs["development_rules"]

    def get_skills_projection_input(self):
        if "skills" not in self._inputs:
            raise AttributeError("skills")
        return self._inputs["skills"]

    def get_tool_recipes_projection_input(self):
        if "tool_recipes" not in self._inputs:
            raise AttributeError("tool_recipes")
        return self._inputs["tool_recipes"]


def test_coding_foundation_default_snapshot_is_not_wired() -> None:
    snapshot = dashboard.build_snapshot(FakeProvider())
    for key in ("development_rules", "skills", "tool_recipes"):
        projection = snapshot[key]
        assert projection["state"] == "not_wired"
        assert projection["availability"] == "not_wired"
        assert "declared_rule_count" not in projection
        assert "registry_count" not in projection
        assert projection["ownership"] == "full"


def test_dashboard_provider_projects_repository_development_rules(tmp_path) -> None:
    manifest_path = tmp_path / ".aiworkhub" / "config" / "development_rules.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(_rules_manifest_mapping()), encoding="utf-8")

    provider = dashboard.DashboardProvider(repo_root=tmp_path)
    rules = dashboard._coding_foundation_projections(
        provider, ownership="full"
    )["development_rules"]
    assert rules["state"] == "measured"
    assert rules["availability"] == "available"
    assert rules["declared_rule_count"] == 2
    assert rules["resolved_rule_count"] == 1
    assert rules["violation_evidence_state"] == "no_sample"


def test_coding_foundation_positive_measured_projections() -> None:
    provider = _FoundationProvider(
        development_rules={
            "manifest": _rules_manifest_mapping(),
            "resolve": {"language": "python"},
            "violations": [{"rule_id": "forbidden_pattern_rule", "result": "fail"}],
        },
        skills={
            "records": [
                _skill_mapping(),
                _skill_mapping(
                    identity="review-gate",
                    version="1.1.0",
                    lifecycle_state="active",
                ),
            ],
            "selections": [{"identity": "review-gate"}],
            "invocations": [{"identity": "review-gate", "result": "ok"}],
            "outcomes": [{"identity": "review-gate", "outcome": "accepted"}],
            "selection_denominator": 4,
            "invocation_denominator": 4,
            "outcome_denominator": 4,
        },
        tool_recipes={
            "recipes": [_echo_recipe()],
            "receipts": [
                {
                    "recipe_id": "echo-tool",
                    "version": "1.0.0",
                    "cache_eligible": False,
                    "context_bytes": 12,
                }
            ],
        },
    )

    snapshot = dashboard.build_snapshot(provider)
    rules = snapshot["development_rules"]
    assert rules["state"] == "measured"
    assert rules["availability"] == "available"
    assert rules["version"] == "1.0.0"
    assert isinstance(rules["digest_prefix"], str)
    assert len(rules["digest_prefix"]) == 12
    assert len(rules["digest_prefix"]) < 64
    assert rules["declared_rule_count"] == 2
    assert rules["resolved_rule_count"] == 1
    assert rules["violation_evidence_state"] == "measured"
    assert rules["violations"][0]["rule_id"] == "forbidden_pattern_rule"

    skills = snapshot["skills"]
    assert skills["state"] == "measured"
    assert skills["lifecycle"] == {"proposed": 1, "active": 1, "retired": 0}
    assert skills["selection"]["state"] == "measured"
    assert skills["selection"]["count"] == 1
    assert skills["selection"]["denominator"] == 4
    assert skills["invocation"]["state"] == "measured"
    assert skills["outcome"]["state"] == "measured"

    recipes = snapshot["tool_recipes"]
    assert recipes["state"] == "measured"
    assert recipes["registry_count"] == 1
    assert recipes["discovery_count"] == 1
    assert recipes["invocation"]["state"] == "measured"
    assert recipes["cache"]["state"] == "measured"
    assert recipes["cache"]["ineligible_count"] == 1
    assert recipes["context"]["state"] == "unknown"

def test_coding_foundation_malformed_inputs_are_invalid() -> None:
    provider = _FoundationProvider(
        development_rules={"manifest": {"schema": "nope"}},
        skills={"records": [{"identity": 1}]},
        tool_recipes={"recipes": ["not-a-recipe"]},
    )
    snapshot = dashboard.build_snapshot(provider)
    assert snapshot["development_rules"]["state"] == "invalid"
    assert "declared_rule_count" not in snapshot["development_rules"]
    assert snapshot["skills"]["state"] == "invalid"
    assert "lifecycle" not in snapshot["skills"]
    assert snapshot["tool_recipes"]["state"] == "invalid"
    assert "registry_count" not in snapshot["tool_recipes"]


def test_coding_foundation_unavailable_is_not_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard, "_load_development_rules_api", lambda: None)
    monkeypatch.setattr(dashboard, "_load_skill_registry_api", lambda: None)
    monkeypatch.setattr(dashboard, "_load_tool_recipes_api", lambda: None)
    provider = _FoundationProvider(
        development_rules={"manifest": _rules_manifest_mapping()},
        skills={"records": [_skill_mapping()]},
        tool_recipes={"recipes": [_echo_recipe()]},
    )
    snapshot = dashboard.build_snapshot(provider)
    for key in ("development_rules", "skills", "tool_recipes"):
        assert snapshot[key]["state"] == "unavailable"
        assert snapshot[key]["availability"] == "unavailable"
        assert 0 not in snapshot[key].values()


def test_coding_foundation_no_sample_and_unknown_denominators() -> None:
    provider = _FoundationProvider(
        development_rules={"manifest": _rules_manifest_mapping(), "violations": "unknown"},
        skills={
            "records": [_skill_mapping()],
            "selections": "unknown",
            "invocation_denominator": "unknown",
        },
        tool_recipes={"recipes": [_echo_recipe()], "receipts": "unknown"},
    )
    snapshot = dashboard.build_snapshot(provider)
    assert snapshot["development_rules"]["state"] == "measured"
    assert snapshot["development_rules"]["violation_evidence_state"] == "unknown"
    assert "violation_count" not in snapshot["development_rules"]
    assert snapshot["skills"]["selection"]["state"] == "unknown"
    assert snapshot["skills"]["selection"]["denominator"] == "unknown"
    assert snapshot["skills"]["invocation"]["state"] == "no_sample"
    assert snapshot["skills"]["outcome"]["state"] == "no_sample"
    assert snapshot["tool_recipes"]["invocation"]["state"] == "unknown"
    assert snapshot["tool_recipes"]["cache"]["denominator"] == "unknown"
    assert snapshot["tool_recipes"]["context"]["state"] == "unknown"


def test_coding_foundation_partial_summary_does_not_erase_rich_evidence() -> None:
    full_provider = _FoundationProvider(
        development_rules={
            "manifest": _rules_manifest_mapping(),
            "violations": [{"rule_id": "forbidden_pattern_rule", "result": "fail"}],
        },
        skills={
            "records": [_skill_mapping()],
            "selections": [{"identity": "commit-msg-check"}],
            "selection_denominator": 3,
        },
        tool_recipes={
            "recipes": [_echo_recipe()],
            "receipts": [
                {"recipe_id": "echo-tool", "cache_eligible": True, "context_bytes": 4}
            ],
        },
    )
    full = dashboard.build_snapshot(full_provider)
    assert full["development_rules"]["violations"]
    assert full["skills"]["selection"]["items"]
    assert full["tool_recipes"]["receipts"]

    summary_provider = _FoundationProvider(
        development_rules={"manifest": _rules_manifest_mapping()},
        skills={"records": [_skill_mapping()]},
        tool_recipes={"recipes": [_echo_recipe()]},
    )
    summary = dashboard.build_snapshot(summary_provider, summary_only=True)
    assert summary["development_rules"]["ownership"] == "summary"
    assert "violations" not in summary["development_rules"]
    assert "items" not in summary["skills"]["selection"]
    assert "receipts" not in summary["tool_recipes"]

    merged = dashboard.build_snapshot(
        summary_provider, summary_only=True, previous=full
    )
    assert merged["development_rules"]["ownership"] == "full"
    assert merged["development_rules"]["violations"][0]["rule_id"] == "forbidden_pattern_rule"
    assert merged["skills"]["selection"]["items"][0]["identity"] == "commit-msg-check"
    assert merged["tool_recipes"]["receipts"][0]["recipe_id"] == "echo-tool"
    assert merged["development_rules"]["violation_evidence_state"] == "measured"
    assert merged["development_rules"]["violation_count"] == 1
    assert merged["skills"]["selection"]["state"] == "measured"
    assert merged["skills"]["selection"]["count"] == 1
    assert merged["skills"]["selection"]["denominator"] == 3
    assert merged["tool_recipes"]["invocation"]["state"] == "measured"
    assert merged["tool_recipes"]["invocation"]["count"] == 1


def test_coding_foundation_summary_keeps_rules_state_with_violations() -> None:
    full = dashboard.build_snapshot(
        _FoundationProvider(
            development_rules={
                "manifest": _rules_manifest_mapping(),
                "violations": [{"rule_id": "forbidden_pattern_rule", "result": "fail"}],
            }
        )
    )
    merged = dashboard.build_snapshot(
        _FoundationProvider(development_rules={"manifest": _rules_manifest_mapping()}),
        summary_only=True,
        previous=full,
    )
    rules = merged["development_rules"]
    assert rules["violations"]
    assert rules["violation_evidence_state"] == "measured"
    assert rules["violation_count"] == len(full["development_rules"]["violations"])
    assert rules["violation_evidence_state"] not in {"no_sample", "unknown", "invalid"}


def test_coding_foundation_summary_keeps_skills_state_with_items() -> None:
    full = dashboard.build_snapshot(
        _FoundationProvider(
            skills={
                "records": [_skill_mapping()],
                "selections": [{"identity": "commit-msg-check"}],
                "selection_denominator": 3,
            }
        )
    )
    merged = dashboard.build_snapshot(
        _FoundationProvider(skills={"records": [_skill_mapping()]}),
        summary_only=True,
        previous=full,
    )
    selection = merged["skills"]["selection"]
    assert selection["items"]
    assert selection["state"] == "measured"
    assert selection["count"] == 1
    assert selection["denominator"] == 3
    assert selection["state"] not in {"no_sample", "unknown", "invalid"}


def test_coding_foundation_summary_keeps_recipes_state_with_receipts() -> None:
    full = dashboard.build_snapshot(
        _FoundationProvider(
            tool_recipes={
                "recipes": [_echo_recipe()],
                "receipts": [
                    {"recipe_id": "echo-tool", "cache_eligible": True, "context_bytes": 4}
                ],
            }
        )
    )
    merged = dashboard.build_snapshot(
        _FoundationProvider(tool_recipes={"recipes": [_echo_recipe()]}),
        summary_only=True,
        previous=full,
    )
    recipes = merged["tool_recipes"]
    assert recipes["receipts"]
    assert recipes["invocation"]["state"] == "measured"
    assert recipes["invocation"]["count"] == 1
    assert recipes["invocation"]["state"] not in {"no_sample", "unknown", "invalid"}
    assert recipes["invocation"]["items"]


def test_coding_foundation_oversized_and_secret_fields_are_bounded() -> None:
    secret_digest = "ab" * 32
    oversized = [
        {
            "rule_id": f"rule-{index}",
            "token": "secret=super-secret-value",
            "path": "/home/shrek/hidden/file.py",
            "digest": secret_digest,
        }
        for index in range(20)
    ]
    provider = _FoundationProvider(
        development_rules={
            "manifest": _rules_manifest_mapping(),
            "violations": oversized,
        },
        skills={
            "records": [_skill_mapping()],
            "selections": oversized,
            "selection_denominator": True,
        },
        tool_recipes={
            "recipes": [_echo_recipe()],
            "receipts": oversized,
        },
    )
    snapshot = dashboard.build_snapshot(provider)
    rules = snapshot["development_rules"]
    assert rules["violations_truncated"] is True
    assert len(rules["violations"]) == dashboard._PROJECTION_LIST_LIMIT
    assert rules["returned_count"] == dashboard._PROJECTION_LIST_LIMIT
    assert rules["violation_count"] == "unknown"
    leaked = json.dumps(snapshot)
    assert "super-secret-value" not in leaked
    assert "/home/shrek/hidden/file.py" not in leaked
    assert secret_digest not in leaked
    selection = snapshot["skills"]["selection"]
    assert selection["denominator"] == "unknown"
    assert selection["truncated"] is True
    assert selection["returned_count"] == dashboard._PROJECTION_LIST_LIMIT
    assert selection["count"] == "unknown"
    recipes = snapshot["tool_recipes"]
    assert recipes["receipts_truncated"] is True
    assert recipes["invocation"]["returned_count"] == dashboard._PROJECTION_LIST_LIMIT
    assert recipes["invocation"]["count"] == "unknown"

    mixed = list(oversized)
    mixed[0] = "not-a-row"
    unknown_snapshot = dashboard.build_snapshot(
        _FoundationProvider(
            development_rules={
                "manifest": _rules_manifest_mapping(),
                "violations": mixed,
            },
            skills={
                "records": [_skill_mapping()],
                "selections": mixed,
            },
            tool_recipes={
                "recipes": [_echo_recipe()],
                "receipts": mixed,
            },
        )
    )
    assert unknown_snapshot["development_rules"]["violation_count"] == "unknown"
    assert unknown_snapshot["development_rules"]["returned_count"] == 7
    assert unknown_snapshot["skills"]["selection"]["count"] == "unknown"
    assert unknown_snapshot["skills"]["selection"]["returned_count"] == 7
    assert unknown_snapshot["tool_recipes"]["invocation"]["count"] == "unknown"
    assert unknown_snapshot["tool_recipes"]["invocation"]["returned_count"] == 7


def test_coding_foundation_structured_credential_fields_redacted() -> None:
    plaintext = {
        "token": "structured-token-secret-xyz",
        "access_token": "structured-access-token-xyz",
        "apiKey": "structured-camel-apikey-xyz",
        "api_key": "structured-snake-apikey-xyz",
        "authorization": "plain-authorization-header-xyz",
        "password": "structured-password-secret-xyz",
        "secret": "structured-secret-value-xyz",
        "credential": "structured-credential-value-xyz",
        "note": "Authorization: Bearer inline-bearer-secret-xyz",
        "rule_id": "keep-rule-id",
        "identity": "keep-identity",
        "version": "1.2.3",
    }
    snapshot = dashboard.build_snapshot(
        _FoundationProvider(
            development_rules={
                "manifest": _rules_manifest_mapping(),
                "violations": [plaintext],
            },
            skills={
                "records": [_skill_mapping()],
                "selections": [plaintext],
                "invocations": [plaintext],
                "outcomes": [plaintext],
                "selection_denominator": True,
            },
            tool_recipes={
                "recipes": [_echo_recipe()],
                "receipts": [plaintext],
            },
        )
    )
    leaked = json.dumps(snapshot)
    for secret in (
        "structured-token-secret-xyz",
        "structured-access-token-xyz",
        "structured-camel-apikey-xyz",
        "structured-snake-apikey-xyz",
        "plain-authorization-header-xyz",
        "structured-password-secret-xyz",
        "structured-secret-value-xyz",
        "structured-credential-value-xyz",
        "inline-bearer-secret-xyz",
        "Bearer inline-bearer-secret-xyz",
    ):
        assert secret not in leaked
    rules_item = snapshot["development_rules"]["violations"][0]
    skills_item = snapshot["skills"]["selection"]["items"][0]
    recipes_item = snapshot["tool_recipes"]["receipts"][0]
    for item in (rules_item, skills_item, recipes_item):
        assert item["token"] == dashboard._CREDENTIAL_FIELD_REDACTION_MARKER
        assert item["api_key"] == dashboard._CREDENTIAL_FIELD_REDACTION_MARKER
        assert item["authorization"] == dashboard._CREDENTIAL_FIELD_REDACTION_MARKER
        assert item["password"] == dashboard._CREDENTIAL_FIELD_REDACTION_MARKER
        assert item["rule_id"] == "keep-rule-id"
        assert item["identity"] == "keep-identity"
        assert item["version"] == "1.2.3"


def test_coding_foundation_access_key_fields_redacted_from_serialized_snapshot() -> None:
    plaintext = {
        "secret_access_key": "structured-secret-access-key-xyz",
        "aws_secret_access_key": "structured-aws-secret-access-key-xyz",
        "access_key": "structured-access-key-xyz",
        "s3_access_key": "structured-s3-access-key-xyz",
        "rule_id": "keep-rule-id",
        "identity": "keep-identity",
        "cache_key": "keep-cache-key",
        "access_id": "keep-access-id",
    }
    snapshot = dashboard.build_snapshot(
        _FoundationProvider(
            development_rules={
                "manifest": _rules_manifest_mapping(),
                "violations": [plaintext],
            },
            skills={
                "records": [_skill_mapping()],
                "selections": [plaintext],
                "invocations": [plaintext],
                "outcomes": [plaintext],
                "selection_denominator": True,
            },
            tool_recipes={
                "recipes": [_echo_recipe()],
                "receipts": [plaintext],
            },
        )
    )
    leaked = json.dumps(snapshot)
    for secret in (
        "structured-secret-access-key-xyz",
        "structured-aws-secret-access-key-xyz",
        "structured-access-key-xyz",
        "structured-s3-access-key-xyz",
    ):
        assert secret not in leaked
    rules_item = snapshot["development_rules"]["violations"][0]
    skills_item = snapshot["skills"]["selection"]["items"][0]
    recipes_item = snapshot["tool_recipes"]["receipts"][0]
    marker = dashboard._CREDENTIAL_FIELD_REDACTION_MARKER
    for item in (rules_item, skills_item, recipes_item):
        assert item["secret_access_key"] == marker
        assert item["aws_secret_access_key"] == marker
        assert item["access_key"] == marker
        assert item["s3_access_key"] == marker
        assert item["rule_id"] == "keep-rule-id"
        assert item["identity"] == "keep-identity"
        assert item["cache_key"] == "keep-cache-key"
        assert item["access_id"] == "keep-access-id"


def test_coding_foundation_compound_assignment_secrets_redacted_from_serialized_snapshot() -> None:
    secrets = (
        ("private_key", "compound-private-key-xyz"),
        ("client_secret", "compound-client-secret-xyz"),
        ("id_token", "compound-id-token-xyz"),
        ("refresh_token", "compound-refresh-token-xyz"),
        ("aws_secret_access_key", "compound-aws-secret-access-key-xyz"),
    )
    rows = [
        {
            "note": f"{name}={value}",
            "message": f"{name}:{value}",
            "rule_id": "keep-rule-id",
        }
        for name, value in secrets
    ]
    snapshot = dashboard.build_snapshot(
        _FoundationProvider(
            development_rules={
                "manifest": _rules_manifest_mapping(),
                "violations": rows,
            },
            skills={
                "records": [_skill_mapping()],
                "selections": rows,
                "invocations": rows,
                "outcomes": rows,
                "selection_denominator": True,
            },
            tool_recipes={
                "recipes": [_echo_recipe()],
                "receipts": rows,
            },
        )
    )
    leaked = json.dumps(snapshot)
    for _name, value in secrets:
        assert value not in leaked
    rules_items = snapshot["development_rules"]["violations"]
    skills_items = snapshot["skills"]["selection"]["items"]
    recipes_items = snapshot["tool_recipes"]["receipts"]
    for items in (rules_items, skills_items, recipes_items):
        assert len(items) == len(secrets)
        for item, (name, _value) in zip(items, secrets, strict=True):
            assert item["note"] == f"{name}=<redacted>"
            assert item["message"] == f"{name}:<redacted>"
            assert item["rule_id"] == "keep-rule-id"


def test_coding_foundation_labeled_and_0x_digest_fields_are_prefixed() -> None:
    hex64 = "ab" * 32
    labeled = f"sha256:{hex64}"
    hex_0x = f"0x{hex64}"
    plaintext = {
        "digest": labeled,
        "sha256": hex_0x,
        "recipe_digest": labeled,
        "hash": labeled,
        "rule_id": "keep-rule-id",
    }
    snapshot = dashboard.build_snapshot(
        _FoundationProvider(
            development_rules={
                "manifest": _rules_manifest_mapping(),
                "violations": [plaintext],
            },
            skills={
                "records": [_skill_mapping()],
                "selections": [plaintext],
                "selection_denominator": True,
            },
            tool_recipes={
                "recipes": [_echo_recipe()],
                "receipts": [plaintext],
            },
        )
    )
    leaked = json.dumps(snapshot)
    assert labeled not in leaked
    assert hex_0x not in leaked
    assert hex64 not in leaked
    assert f'"hash": "{labeled}"' not in leaked
    assert f'"hash": "{hex_0x}"' not in leaked
    assert f'"hash": "{hex64}"' not in leaked
    rules_item = snapshot["development_rules"]["violations"][0]
    skills_item = snapshot["skills"]["selection"]["items"][0]
    recipes_item = snapshot["tool_recipes"]["receipts"][0]
    expected_labeled = dashboard._digest_prefix(labeled)
    expected_0x = dashboard._digest_prefix(hex_0x)
    assert expected_labeled is not None
    assert expected_0x is not None
    assert len(expected_labeled) <= dashboard._DIGEST_PREFIX_LEN
    assert len(expected_0x) <= dashboard._DIGEST_PREFIX_LEN
    assert dashboard._projection_string(labeled) == expected_labeled
    assert dashboard._projection_string(hex_0x) == expected_0x
    assert dashboard._projection_string("keep-rule-id") == "keep-rule-id"
    for item in (rules_item, skills_item, recipes_item):
        assert item["digest"] == expected_labeled
        assert item["sha256"] == expected_0x
        assert item["recipe_digest"] == expected_labeled
        assert item["hash"] == expected_labeled
        assert item["rule_id"] == "keep-rule-id"
        assert hex64 not in item["digest"]
        assert hex64 not in item["sha256"]
        assert hex64 not in item["recipe_digest"]
        assert hex64 not in item["hash"]
        assert labeled not in item["hash"]
        assert hex_0x not in item["hash"]

def test_coding_foundation_summary_zero_or_different_counts_keep_prior_governing() -> None:
    full = dashboard.build_snapshot(
        _FoundationProvider(
            development_rules={
                "manifest": _rules_manifest_mapping(),
                "violations": [{"rule_id": "forbidden_pattern_rule", "result": "fail"}],
            },
            skills={
                "records": [_skill_mapping()],
                "selections": [{"identity": "commit-msg-check"}],
                "selection_denominator": 3,
            },
            tool_recipes={
                "recipes": [_echo_recipe()],
                "receipts": [
                    {"recipe_id": "echo-tool", "cache_eligible": True, "context_bytes": 4}
                ],
            },
        )
    )
    zero = dashboard.build_snapshot(
        _FoundationProvider(
            development_rules={"manifest": _rules_manifest_mapping(), "violations": []},
            skills={"records": [_skill_mapping()], "selections": []},
            tool_recipes={"recipes": [_echo_recipe()], "receipts": []},
        ),
        summary_only=True,
        previous=full,
    )
    assert zero["development_rules"]["violations"][0]["rule_id"] == "forbidden_pattern_rule"
    assert zero["development_rules"]["violation_count"] == 1
    assert zero["development_rules"]["violation_evidence_state"] == "measured"
    assert zero["skills"]["selection"]["items"][0]["identity"] == "commit-msg-check"
    assert zero["skills"]["selection"]["count"] == 1
    assert zero["skills"]["selection"]["denominator"] == 3
    assert zero["tool_recipes"]["receipts"][0]["recipe_id"] == "echo-tool"
    assert zero["tool_recipes"]["invocation"]["count"] == 1
    assert zero["tool_recipes"]["invocation"]["state"] == "measured"

    different = dashboard.build_snapshot(
        _FoundationProvider(
            development_rules={
                "manifest": _rules_manifest_mapping(),
                "violations": [
                    {"rule_id": "a", "result": "fail"},
                    {"rule_id": "b", "result": "fail"},
                ],
            },
            skills={
                "records": [_skill_mapping()],
                "selections": [{"identity": "a"}, {"identity": "b"}],
            },
            tool_recipes={
                "recipes": [_echo_recipe()],
                "receipts": [
                    {"recipe_id": "a", "cache_eligible": True, "context_bytes": 1},
                    {"recipe_id": "b", "cache_eligible": False, "context_bytes": 2},
                ],
            },
        ),
        summary_only=True,
        previous=full,
    )
    assert different["development_rules"]["violation_count"] == 1
    assert len(different["development_rules"]["violations"]) == 1
    assert different["skills"]["selection"]["count"] == 1
    assert len(different["skills"]["selection"]["items"]) == 1
    assert different["tool_recipes"]["invocation"]["count"] == 1
    assert len(different["tool_recipes"]["receipts"]) == 1


def test_coding_foundation_summary_unavailable_or_no_sample_keeps_prior_governing() -> None:
    full = dashboard.build_snapshot(
        _FoundationProvider(
            development_rules={
                "manifest": _rules_manifest_mapping(),
                "violations": [{"rule_id": "forbidden_pattern_rule", "result": "fail"}],
            },
            skills={
                "records": [_skill_mapping()],
                "selections": [{"identity": "commit-msg-check"}],
                "selection_denominator": 3,
            },
            tool_recipes={
                "recipes": [_echo_recipe()],
                "receipts": [
                    {"recipe_id": "echo-tool", "cache_eligible": True, "context_bytes": 4}
                ],
            },
        )
    )
    for key, rich_path in (
        ("development_rules", ("violations",)),
        ("skills", ("selection", "items")),
        ("tool_recipes", ("receipts",)),
    ):
        prior = full[key]
        assert prior["state"] == "measured"
        assert dashboard._mapping_has_rich_evidence(prior), sorted(prior)
        assert "state" in dashboard._TOP_LEVEL_EVIDENCE_GOVERNING_KEYS
        assert dashboard._should_preserve_governing("state", prior)
        for state in ("unavailable", "no_sample"):
            current = {
                "schema_id": prior["schema_id"],
                "state": state,
                "ownership": "summary",
                "availability": state,
            }
            merged = dashboard._merge_coding_foundation_projection(prior, current)
            assert merged["state"] == "measured"
            assert merged["availability"] == "available"
            assert merged["ownership"] == "full"
            cursor: object = merged
            for part in rich_path:
                assert isinstance(cursor, dict)
                cursor = cursor[part]
            assert cursor


def test_coding_foundation_projection_items_bound_inspection_and_invalid_rows() -> None:
    class _InspectedList(list):
        def __init__(self, rows: list) -> None:
            super().__init__(rows)
            self.inspected = 0

        def __iter__(self):
            for row in list.__iter__(self):
                self.inspected += 1
                yield row

    huge = _InspectedList([{"rule_id": f"rule-{index}"} for index in range(10_000)])
    items, truncated, total = dashboard._bounded_projection_items(huge)
    assert truncated is True
    assert len(items) == dashboard._PROJECTION_LIST_LIMIT
    assert total == "unknown"
    assert huge.inspected <= dashboard._PROJECTION_LIST_LIMIT

    empty_ok = [{}, {"rule_id": "ok"}, "nope"]
    items, truncated, total = dashboard._bounded_projection_items(empty_ok)
    assert items == [{"rule_id": "ok"}]
    assert truncated is False
    assert total == "unknown"

    empty, empty_truncated, empty_total = dashboard._bounded_projection_items([])
    assert empty == []
    assert empty_truncated is False
    assert empty_total == 0

    snapshot = dashboard.build_snapshot(
        _FoundationProvider(
            development_rules={
                "manifest": _rules_manifest_mapping(),
                "violations": _InspectedList(
                    [{"rule_id": f"rule-{index}"} for index in range(10_000)]
                ),
            },
            skills={
                "records": [_skill_mapping()],
                "selections": [{}, {"identity": "ok"}],
            },
            tool_recipes={
                "recipes": [_echo_recipe()],
                "receipts": [{}, {"recipe_id": "echo-tool"}],
            },
        )
    )
    assert snapshot["development_rules"]["violation_count"] == "unknown"
    assert snapshot["skills"]["selection"]["count"] == "unknown"
    assert snapshot["tool_recipes"]["invocation"]["count"] == "unknown"
    assert snapshot["development_rules"]["violations"]
    assert snapshot["skills"]["selection"]["items"] == [{"identity": "ok"}]
    assert snapshot["tool_recipes"]["receipts"] == [{"recipe_id": "echo-tool"}]


def test_cross_sibling_summary_full_evidence_merge_regression() -> None:
    full_selection = dashboard.build_snapshot(
        _FoundationProvider(
            skills={
                "records": [_skill_mapping()],
                "selections": [{"identity": "commit-msg-check"}],
                "selection_denominator": 3,
            }
        )
    )
    assert full_selection["skills"]["selection"]["items"]
    assert full_selection["skills"]["invocation"]["state"] == "no_sample"
    invocation_summary = _FoundationProvider(
        skills={
            "records": [_skill_mapping()],
            "invocations": [{"identity": "review-gate", "result": "ok"}],
            "invocation_denominator": 5,
        }
    )
    current_invocation = dashboard.build_snapshot(
        invocation_summary, summary_only=True
    )
    assert current_invocation["skills"]["invocation"]["state"] == "measured"
    assert current_invocation["skills"]["invocation"]["denominator"] == 5
    merged = dashboard.build_snapshot(
        invocation_summary, summary_only=True, previous=full_selection
    )
    assert merged["skills"]["selection"]["items"][0]["identity"] == "commit-msg-check"
    assert merged["skills"]["selection"]["state"] == "measured"
    assert merged["skills"]["selection"]["count"] == 1
    assert merged["skills"]["selection"]["denominator"] == 3
    assert merged["skills"]["invocation"]["state"] == "measured"
    assert merged["skills"]["invocation"]["count"] == 1
    assert merged["skills"]["invocation"]["denominator"] == 5
    assert "items" not in merged["skills"]["invocation"]

    full_invocation = dashboard.build_snapshot(
        _FoundationProvider(
            skills={
                "records": [_skill_mapping()],
                "invocations": [{"identity": "review-gate", "result": "ok"}],
                "invocation_denominator": 4,
            }
        )
    )
    selection_summary = _FoundationProvider(
        skills={
            "records": [_skill_mapping()],
            "selections": [{"identity": "commit-msg-check"}],
            "selection_denominator": 5,
        }
    )
    merged = dashboard.build_snapshot(
        selection_summary, summary_only=True, previous=full_invocation
    )
    assert merged["skills"]["invocation"]["items"][0]["identity"] == "review-gate"
    assert merged["skills"]["invocation"]["state"] == "measured"
    assert merged["skills"]["invocation"]["denominator"] == 4
    assert merged["skills"]["selection"]["state"] == "measured"
    assert merged["skills"]["selection"]["count"] == 1
    assert merged["skills"]["selection"]["denominator"] == 5

    full_outcome = dashboard.build_snapshot(
        _FoundationProvider(
            skills={
                "records": [_skill_mapping()],
                "outcomes": [{"identity": "review-gate", "outcome": "accepted"}],
                "outcome_denominator": 2,
            }
        )
    )
    merged = dashboard.build_snapshot(
        invocation_summary, summary_only=True, previous=full_outcome
    )
    assert merged["skills"]["outcome"]["items"][0]["identity"] == "review-gate"
    assert merged["skills"]["outcome"]["state"] == "measured"
    assert merged["skills"]["outcome"]["denominator"] == 2
    assert merged["skills"]["invocation"]["state"] == "measured"
    assert merged["skills"]["invocation"]["denominator"] == 5
    merged = dashboard.build_snapshot(
        _FoundationProvider(
            skills={
                "records": [_skill_mapping()],
                "outcomes": [{"identity": "review-gate", "outcome": "accepted"}],
                "outcome_denominator": 5,
            }
        ),
        summary_only=True,
        previous=full_selection,
    )
    assert merged["skills"]["selection"]["items"][0]["identity"] == "commit-msg-check"
    assert merged["skills"]["selection"]["denominator"] == 3
    assert merged["skills"]["outcome"]["state"] == "measured"
    assert merged["skills"]["outcome"]["denominator"] == 5

    prior_recipes = {
        "schema_id": dashboard._PROJECTION_SCHEMA_IDS["tool_recipes"],
        "state": "measured",
        "availability": "available",
        "ownership": "full",
        "receipts": [{"recipe_id": "echo-tool"}],
        "invocation": {
            "state": "measured",
            "count": 1,
            "items": [{"recipe_id": "echo-tool"}],
        },
        "cache": {"state": "no_sample"},
        "context": {"state": "no_sample"},
    }
    current_recipes = {
        "schema_id": dashboard._PROJECTION_SCHEMA_IDS["tool_recipes"],
        "state": "measured",
        "availability": "available",
        "ownership": "summary",
        "invocation": {"state": "no_sample"},
        "cache": {"state": "measured", "eligible_count": 1, "ineligible_count": 0},
        "context": {"state": "measured", "receipt_count": 1},
    }
    merged_recipes = dashboard._merge_coding_foundation_projection(
        prior_recipes, current_recipes
    )
    assert merged_recipes["receipts"] == [{"recipe_id": "echo-tool"}]
    assert merged_recipes["invocation"]["state"] == "measured"
    assert merged_recipes["invocation"]["count"] == 1
    assert merged_recipes["invocation"]["items"] == [{"recipe_id": "echo-tool"}]
    assert merged_recipes["cache"] == {
        "state": "measured",
        "eligible_count": 1,
        "ineligible_count": 0,
    }
    assert merged_recipes["context"] == {"state": "measured", "receipt_count": 1}

    prior_cache_only = {
        "schema_id": dashboard._PROJECTION_SCHEMA_IDS["tool_recipes"],
        "state": "measured",
        "availability": "available",
        "ownership": "full",
        "receipts": [{"recipe_id": "echo-tool"}],
        "invocation": {"state": "no_sample"},
        "cache": {"state": "measured", "eligible_count": 1, "ineligible_count": 0},
        "context": {"state": "no_sample"},
    }
    current_invocation_only = {
        "schema_id": dashboard._PROJECTION_SCHEMA_IDS["tool_recipes"],
        "state": "measured",
        "availability": "available",
        "ownership": "summary",
        "invocation": {"state": "measured", "count": 1, "denominator": 5},
    }
    merged_recipes = dashboard._merge_coding_foundation_projection(
        prior_cache_only, current_invocation_only
    )
    assert merged_recipes["receipts"] == [{"recipe_id": "echo-tool"}]
    assert merged_recipes["invocation"] == {
        "state": "measured",
        "count": 1,
        "denominator": 5,
    }
    assert merged_recipes["cache"] == {
        "state": "measured",
        "eligible_count": 1,
        "ineligible_count": 0,
    }


def test_coding_foundation_default_provider_is_wired_no_sample(tmp_path: Path) -> None:
    root = _init_canonical_repo(tmp_path)
    provider = dashboard.DashboardProvider(repo_root=root)
    assert provider.get_development_rules_projection_input() is None
    skills = provider.get_skills_projection_input()
    assert len(list(skills)) == 0
    recipes = provider.get_tool_recipes_projection_input()
    assert len(recipes) == 0
    snapshot = dashboard.build_snapshot(provider)
    for key in ("development_rules", "skills", "tool_recipes"):
        projection = snapshot[key]
        assert projection["state"] == "no_sample"
        assert projection["availability"] == "no_sample"
        assert projection["state"] != "not_wired"


def test_coding_foundation_projects_real_invocation_receipt_and_cache_decision() -> None:
    from aiworkhub import tool_recipes as tr

    recipe = _echo_recipe()
    receipt = tr.build_receipt(tr.validate_invocation(recipe, {}))
    decision = tr.cache_eligibility(recipe)
    assert isinstance(receipt, tr.InvocationReceipt)
    assert isinstance(decision, tr.CacheDecision)
    assert decision.eligible is False
    snapshot = dashboard.build_snapshot(
        _FoundationProvider(tool_recipes={"recipes": [recipe], "receipts": [receipt]})
    )
    recipes = snapshot["tool_recipes"]
    assert recipes["invocation"]["state"] == "measured"
    assert recipes["invocation"]["count"] == 1
    assert recipes["receipts"][0]["recipe_id"] == "echo-tool"
    assert isinstance(recipes["receipts"][0].get("digest_prefix"), str)
    assert recipes["cache"]["state"] == "measured"
    assert recipes["cache"]["eligible_count"] == 0
    assert recipes["cache"]["ineligible_count"] == 1
    assert recipes["context"]["state"] == "unknown"
    assert "receipt_count" not in recipes["context"]


def test_coding_foundation_invalid_only_receipts_stay_unknown() -> None:
    snapshot = dashboard.build_snapshot(
        _FoundationProvider(
            tool_recipes={"recipes": [_echo_recipe()], "receipts": [{}, ""]}
        )
    )
    recipes = snapshot["tool_recipes"]
    assert recipes["invocation"]["count"] == "unknown"
    assert recipes["cache"]["state"] == "unknown"
    assert recipes["context"]["state"] == "unknown"
    assert "eligible_count" not in recipes["cache"]
    assert "ineligible_count" not in recipes["cache"]
    assert "receipt_count" not in recipes["context"]


def test_coding_foundation_exact_empty_receipts_are_measured_zero() -> None:
    empty = dashboard.build_snapshot(
        _FoundationProvider(tool_recipes={"recipes": [_echo_recipe()], "receipts": []})
    )
    recipes = empty["tool_recipes"]
    assert recipes["invocation"]["state"] == "measured"
    assert recipes["invocation"]["count"] == 0
    assert recipes["cache"] == {
        "state": "measured",
        "eligible_count": 0,
        "ineligible_count": 0,
    }
    assert recipes["context"] == {"state": "measured", "receipt_count": 0}
    missing = dashboard.build_snapshot(
        _FoundationProvider(tool_recipes={"recipes": [_echo_recipe()]})
    )
    assert missing["tool_recipes"]["invocation"]["state"] == "no_sample"
    assert missing["tool_recipes"]["cache"]["state"] == "no_sample"
    assert missing["tool_recipes"]["context"]["state"] == "no_sample"


def test_coding_foundation_full_to_summary_keeps_nested_cache_context_coherent() -> None:
    full = dashboard.build_snapshot(
        _FoundationProvider(
            tool_recipes={
                "recipes": [_echo_recipe()],
                "receipts": [
                    {"recipe_id": "echo-tool", "cache_eligible": True, "context_bytes": 4}
                ],
            }
        )
    )
    assert full["tool_recipes"]["cache"]["state"] == "measured"
    assert full["tool_recipes"]["cache"]["eligible_count"] == 0
    assert full["tool_recipes"]["cache"]["ineligible_count"] == 1
    assert full["tool_recipes"]["context"]["state"] == "unknown"
    assert full["tool_recipes"]["receipts"]
    summary = dashboard.build_snapshot(
        _FoundationProvider(tool_recipes={"recipes": [_echo_recipe()]}),
        summary_only=True,
    )
    assert summary["tool_recipes"]["cache"]["state"] == "no_sample"
    assert summary["tool_recipes"]["context"]["state"] == "no_sample"
    assert "receipts" not in summary["tool_recipes"]
    merged = dashboard.build_snapshot(
        _FoundationProvider(tool_recipes={"recipes": [_echo_recipe()]}),
        summary_only=True,
        previous=full,
    )
    recipes = merged["tool_recipes"]
    assert recipes["receipts"][0]["recipe_id"] == "echo-tool"
    assert recipes["cache"] == {
        "state": "measured",
        "eligible_count": 0,
        "ineligible_count": 1,
    }
    assert recipes["context"]["state"] == "unknown"
    assert "eligible_count" not in recipes["context"]
    prior_unit = {
        "schema_id": dashboard._PROJECTION_SCHEMA_IDS["tool_recipes"],
        "state": "measured",
        "availability": "available",
        "ownership": "full",
        "receipts": [{"recipe_id": "echo-tool"}],
        "invocation": {
            "state": "measured",
            "count": 1,
            "items": [{"recipe_id": "echo-tool"}],
        },
        "cache": {"state": "measured", "eligible_count": 1, "ineligible_count": 0},
        "context": {"state": "unknown", "denominator": "unknown"},
    }
    current_unit = {
        "schema_id": dashboard._PROJECTION_SCHEMA_IDS["tool_recipes"],
        "state": "measured",
        "availability": "available",
        "ownership": "summary",
        "invocation": {"state": "no_sample"},
        "cache": {"state": "no_sample"},
        "context": {"state": "no_sample"},
    }
    merged_unit = dashboard._merge_coding_foundation_projection(prior_unit, current_unit)
    assert merged_unit["receipts"] == [{"recipe_id": "echo-tool"}]
    assert merged_unit["cache"] == {
        "state": "measured",
        "eligible_count": 1,
        "ineligible_count": 0,
    }
    assert merged_unit["context"] == {"state": "unknown", "denominator": "unknown"}
    assert merged_unit["invocation"]["state"] == "measured"


def test_coding_foundation_full_to_summary_empty_receipts_stay_atomic() -> None:
    full = dashboard.build_snapshot(
        _FoundationProvider(
            tool_recipes={
                "recipes": [_echo_recipe()],
                "receipts": [
                    {"recipe_id": "echo-tool", "cache_eligible": True, "context_bytes": 4}
                ],
            }
        )
    )
    prior = full["tool_recipes"]
    assert prior["receipts"]
    assert prior["invocation"]["state"] == "measured"
    assert prior["invocation"]["count"] == 1
    assert prior["cache"] == {
        "state": "measured",
        "eligible_count": 0,
        "ineligible_count": 1,
    }
    assert prior["context"]["state"] == "unknown"
    empty_summary_provider = _FoundationProvider(
        tool_recipes={"recipes": [_echo_recipe()], "receipts": []}
    )
    summary = dashboard.build_snapshot(empty_summary_provider, summary_only=True)
    assert "receipts" not in summary["tool_recipes"]
    assert summary["tool_recipes"]["invocation"] == {
        "state": "measured",
        "count": 0,
        "returned_count": 0,
    }
    assert summary["tool_recipes"]["cache"] == {
        "state": "measured",
        "eligible_count": 0,
        "ineligible_count": 0,
    }
    assert summary["tool_recipes"]["context"] == {"state": "measured", "receipt_count": 0}
    merged = dashboard.build_snapshot(
        empty_summary_provider, summary_only=True, previous=full
    )
    recipes = merged["tool_recipes"]
    assert recipes["receipts"][0]["recipe_id"] == "echo-tool"
    assert recipes["invocation"]["state"] == prior["invocation"]["state"]
    assert recipes["invocation"]["count"] == prior["invocation"]["count"]
    assert recipes["cache"] == prior["cache"]
    assert recipes["context"] == prior["context"]
    assert recipes["invocation"]["count"] == len(recipes["receipts"])
    current_empty = {
        "schema_id": dashboard._PROJECTION_SCHEMA_IDS["tool_recipes"],
        "state": "measured",
        "availability": "available",
        "ownership": "summary",
        "receipts": [],
        "invocation": {"state": "measured", "count": 0},
        "cache": {"state": "measured", "eligible_count": 0, "ineligible_count": 0},
        "context": {"state": "measured", "receipt_count": 0},
    }
    replaced = dashboard._merge_coding_foundation_projection(prior, current_empty)
    assert "receipts" not in replaced
    assert replaced["invocation"] == {"state": "measured", "count": 0}
    assert replaced["cache"] == {
        "state": "measured",
        "eligible_count": 0,
        "ineligible_count": 0,
    }
    assert replaced["context"] == {"state": "measured", "receipt_count": 0}


def test_coding_foundation_resolve_uses_raw_hex_and_long_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hex_path = "0123456789abcdef0123456789abcdef"
    long_path = "src/" + ("seg" * 40) + "/module.py"
    assert len(hex_path) == 32
    assert all(character in "0123456789abcdef" for character in hex_path)
    assert len(long_path) > 120
    assert dashboard._projection_string(hex_path, 120) != hex_path
    assert dashboard._projection_string(long_path, 120) != long_path
    real = dashboard._load_development_rules_api()
    assert real is not None

    class _Resolved:
        def __init__(self, count: int) -> None:
            self.rules = (None,) * count

    class _Api:
        parse_manifest = staticmethod(real.parse_manifest)
        canonical_digest = staticmethod(real.canonical_digest)

        @staticmethod
        def resolve(manifest, **kwargs):  # noqa: ANN001, ARG004
            path = kwargs.get("path")
            if path == hex_path:
                return _Resolved(1)
            if path == long_path:
                return _Resolved(2)
            return _Resolved(0)

    monkeypatch.setattr(dashboard, "_load_development_rules_api", lambda: _Api)
    hex_snapshot = dashboard.build_snapshot(
        _FoundationProvider(
            development_rules={
                "manifest": _rules_manifest_mapping(),
                "resolve": {"path": hex_path},
            }
        )
    )
    assert hex_snapshot["development_rules"]["resolved_rule_count"] == 1
    long_snapshot = dashboard.build_snapshot(
        _FoundationProvider(
            development_rules={
                "manifest": _rules_manifest_mapping(),
                "resolve": {"path": long_path},
            }
        )
    )
    assert long_snapshot["development_rules"]["resolved_rule_count"] == 2


class _InspectedPrimary(list):
    def __init__(self, rows: list) -> None:
        super().__init__(rows)
        self.inspected = 0

    def __iter__(self):
        for row in list.__iter__(self):
            self.inspected += 1
            yield row


def test_coding_foundation_primary_skills_and_recipes_cap_inspection() -> None:
    from aiworkhub import tool_recipes as tr

    skill_rows = _InspectedPrimary(
        [
            _skill_mapping(identity=f"skill-{index}", version="1.0.0")
            for index in range(20)
        ]
    )
    recipe_rows = _InspectedPrimary(
        [
            tr.Recipe(id=f"echo-{index}", version="1.0.0", argv=(tr.lit("echo"),))
            for index in range(20)
        ]
    )
    snapshot = dashboard.build_snapshot(
        _FoundationProvider(
            skills={"records": skill_rows},
            tool_recipes={"recipes": recipe_rows},
        )
    )
    skills = snapshot["skills"]
    assert skills["state"] == "measured"
    assert skills["truncated"] is True
    assert skills["returned_count"] == dashboard._PROJECTION_LIST_LIMIT
    assert skills["count"] == "unknown"
    assert skill_rows.inspected <= dashboard._PROJECTION_LIST_LIMIT
    recipes = snapshot["tool_recipes"]
    assert recipes["state"] == "measured"
    assert recipes["truncated"] is True
    assert recipes["returned_count"] == dashboard._PROJECTION_LIST_LIMIT
    assert recipes["count"] == "unknown"
    assert "registry_count" not in recipes
    assert recipe_rows.inspected <= dashboard._PROJECTION_LIST_LIMIT


def test_coding_foundation_empty_skill_registry_keeps_measured_nested_evidence() -> None:
    snapshot = dashboard.build_snapshot(
        _FoundationProvider(
            skills={
                "records": [],
                "selections": [{"identity": "commit-msg-check"}],
                "invocations": [{"identity": "commit-msg-check", "result": "ok"}],
                "outcomes": [{"identity": "commit-msg-check", "outcome": "accepted"}],
                "selection_denominator": 3,
            }
        )
    )
    skills = snapshot["skills"]
    assert skills["state"] == "measured"
    assert skills["availability"] == "available"
    assert skills["state"] != "no_sample"
    assert skills["selection"]["state"] == "measured"
    assert skills["invocation"]["state"] == "measured"
    assert skills["outcome"]["state"] == "measured"
    assert skills["selection"]["count"] == 1
    assert "lifecycle" not in skills


def test_coding_foundation_cache_eligible_echo_matches_recomputed_section() -> None:
    snapshot = dashboard.build_snapshot(
        _FoundationProvider(
            tool_recipes={
                "recipes": [_echo_recipe()],
                "receipts": [
                    {"recipe_id": "echo-tool", "cache_eligible": True, "context_bytes": 4}
                ],
            }
        )
    )
    recipes = snapshot["tool_recipes"]
    assert recipes["cache"]["state"] == "measured"
    assert recipes["cache"]["eligible_count"] == 0
    assert recipes["cache"]["ineligible_count"] == 1
    echoed = [item.get("cache_eligible") for item in recipes["receipts"]]
    assert True not in echoed
    assert echoed == [False]
    assert recipes["cache"]["eligible_count"] == sum(1 for flag in echoed if flag is True)
    assert recipes["cache"]["ineligible_count"] == sum(
        1 for flag in echoed if flag is False
    )


class _NotReadyReadiness:
    ready = False
    reason = "uninitialized"
    repo_id = "not-ready"


class _NotReadyProvider(_FoundationProvider):
    def get_storage_readiness(self) -> _NotReadyReadiness:
        return _NotReadyReadiness()


def test_coding_foundation_measured_full_survives_storage_not_ready() -> None:
    full = dashboard.build_snapshot(
        _FoundationProvider(
            development_rules={
                "manifest": _rules_manifest_mapping(),
                "violations": [{"rule_id": "forbidden_pattern_rule", "result": "fail"}],
            },
            skills={
                "records": [_skill_mapping()],
                "selections": [{"identity": "commit-msg-check"}],
                "selection_denominator": 3,
            },
            tool_recipes={
                "recipes": [_echo_recipe()],
                "receipts": [
                    {"recipe_id": "echo-tool", "cache_eligible": True, "context_bytes": 4}
                ],
            },
        )
    )
    assert full["development_rules"]["violations"]
    assert full["skills"]["selection"]["items"]
    assert full["tool_recipes"]["receipts"]
    assert hasattr(_NotReadyProvider(), "get_development_rules_projection_input")
    assert hasattr(_NotReadyProvider(), "get_skills_projection_input")
    assert hasattr(_NotReadyProvider(), "get_tool_recipes_projection_input")

    not_ready = dashboard.build_snapshot(_NotReadyProvider(), previous=full)
    for key in ("development_rules", "skills", "tool_recipes"):
        projection = not_ready[key]
        assert projection["state"] == "unavailable"
        assert projection["availability"] == "unavailable"
        assert projection["reason"] == "storage_not_ready"
        assert projection["state"] != "not_wired"
        assert projection["availability"] != "not_wired"
    assert not_ready["development_rules"]["violations"][0]["rule_id"] == "forbidden_pattern_rule"
    assert not_ready["skills"]["selection"]["items"][0]["identity"] == "commit-msg-check"
    assert not_ready["tool_recipes"]["receipts"][0]["recipe_id"] == "echo-tool"


def test_coding_foundation_truncated_skills_omit_prefix_lifecycle() -> None:
    rows = [
        _skill_mapping(identity=f"skill-{index}", version="1.0.0")
        for index in range(dashboard._PROJECTION_LIST_LIMIT)
    ]
    rows.append(_skill_mapping(identity="retired-late", lifecycle_state="retired"))
    snapshot = dashboard.build_snapshot(_FoundationProvider(skills={"records": rows}))
    skills = snapshot["skills"]
    assert skills["truncated"] is True
    assert skills["count"] == "unknown"
    lifecycle = skills.get("lifecycle")
    assert lifecycle is None or lifecycle.get("state") == "unknown"
    if isinstance(lifecycle, dict):
        assert lifecycle.get("retired") != 0


def test_coding_foundation_summary_two_records_keeps_nested_rich_atomic_counts() -> None:
    from aiworkhub import tool_recipes as tr

    full = dashboard.build_snapshot(
        _FoundationProvider(
            skills={
                "records": [_skill_mapping()],
                "selections": [{"identity": "commit-msg-check"}],
                "selection_denominator": 3,
            },
            tool_recipes={
                "recipes": [_echo_recipe()],
                "receipts": [
                    {"recipe_id": "echo-tool", "cache_eligible": True, "context_bytes": 4}
                ],
            },
        )
    )
    assert full["skills"]["returned_count"] == 1
    assert full["tool_recipes"]["returned_count"] == 1
    merged = dashboard.build_snapshot(
        _FoundationProvider(
            skills={
                "records": [
                    _skill_mapping(),
                    _skill_mapping(
                        identity="review-gate",
                        version="1.1.0",
                        lifecycle_state="active",
                    ),
                ]
            },
            tool_recipes={
                "recipes": [
                    _echo_recipe(),
                    tr.Recipe(id="echo-2", version="1.0.0", argv=(tr.lit("echo"),)),
                ]
            },
        ),
        summary_only=True,
        previous=full,
    )
    skills = merged["skills"]
    assert skills["returned_count"] == 2
    assert skills["count"] == 2
    assert skills["lifecycle"] == {"proposed": 1, "active": 1, "retired": 0}
    assert skills["selection"]["items"][0]["identity"] == "commit-msg-check"
    recipes = merged["tool_recipes"]
    assert recipes["returned_count"] == 2
    assert recipes["count"] == 2
    assert recipes["registry_count"] == 2
    assert recipes["receipts"][0]["recipe_id"] == "echo-tool"


def test_coding_foundation_secret_key_variants_redacted_from_serialized_snapshot() -> None:
    plaintext = {
        "secret_key": "structured-secret-key-xyz",
        "SECRET_KEY": "structured-SECRET-KEY-xyz",
        "secret-key": "structured-secret-hyphen-key-xyz",
        "cache_key": "keep-cache-key",
        "rule_id": "keep-rule-id",
        "identity": "keep-identity",
        "note": "secret_key=free-text-secret-key-xyz",
        "message": "SECRET_KEY:free-text-SECRET-KEY-xyz",
        "detail": "secret-key=free-text-secret-hyphen-xyz",
        "cache_note": "cache_key=keep-cache-assignment",
    }
    snapshot = dashboard.build_snapshot(
        _FoundationProvider(
            development_rules={
                "manifest": _rules_manifest_mapping(),
                "violations": [plaintext],
            },
            skills={
                "records": [_skill_mapping()],
                "selections": [plaintext],
                "invocations": [plaintext],
                "outcomes": [plaintext],
                "selection_denominator": True,
            },
            tool_recipes={
                "recipes": [_echo_recipe()],
                "receipts": [plaintext],
            },
        )
    )
    leaked = json.dumps(snapshot)
    for secret in (
        "structured-secret-key-xyz",
        "structured-SECRET-KEY-xyz",
        "structured-secret-hyphen-key-xyz",
        "free-text-secret-key-xyz",
        "free-text-SECRET-KEY-xyz",
        "free-text-secret-hyphen-xyz",
    ):
        assert secret not in leaked
    assert "keep-cache-key" in leaked
    assert "keep-cache-assignment" in leaked
    rules_item = snapshot["development_rules"]["violations"][0]
    skills_item = snapshot["skills"]["selection"]["items"][0]
    recipes_item = snapshot["tool_recipes"]["receipts"][0]
    marker = dashboard._CREDENTIAL_FIELD_REDACTION_MARKER
    for item in (rules_item, skills_item, recipes_item):
        assert item["secret_key"] == marker
        assert item["SECRET_KEY"] == marker
        assert item["secret-key"] == marker
        assert item["cache_key"] == "keep-cache-key"
        assert item["rule_id"] == "keep-rule-id"
        assert item["identity"] == "keep-identity"
        assert item["note"] == "secret_key=<redacted>"
        assert item["message"] == "SECRET_KEY:<redacted>"
        assert item["detail"] == "secret-key=<redacted>"
        assert item["cache_note"] == "cache_key=keep-cache-assignment"


def test_coding_foundation_primary_collection_generator_limit_and_misleading_len() -> None:
    limit = dashboard._PROJECTION_LIST_LIMIT
    exact = dashboard._bounded_primary_collection(iter(range(limit)))
    assert exact is not None
    items, truncated, total = exact
    assert items == list(range(limit))
    assert truncated is False
    assert total == limit

    oversized = dashboard._bounded_primary_collection(iter(range(limit + 1)))
    assert oversized is not None
    items, truncated, total = oversized
    assert items == list(range(limit))
    assert truncated is True
    assert total == "unknown"

    class _ShortLen:
        def __len__(self) -> int:
            return limit

        def __iter__(self):
            yield from range(limit + 1)

    class _LongLen:
        def __len__(self) -> int:
            return limit + 5

        def __iter__(self):
            yield from range(limit)

    class _LieList(list):
        def __len__(self) -> int:
            return 3

    short = dashboard._bounded_primary_collection(_ShortLen())
    assert short is not None
    items, truncated, total = short
    assert items == list(range(limit))
    assert truncated is True
    assert total == "unknown"

    long = dashboard._bounded_primary_collection(_LongLen())
    assert long is not None
    items, truncated, total = long
    assert items == list(range(limit))
    assert truncated is False
    assert total == limit

    lie_list = dashboard._bounded_primary_collection(_LieList(range(limit)))
    assert lie_list is not None
    items, truncated, total = lie_list
    assert items == list(range(limit))
    assert truncated is False
    assert total == limit
