from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from http.client import HTTPConnection
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

from aiworkhub import dashboard, task_store  # noqa: E402


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


@contextmanager
def running_server(provider):
    server = dashboard.create_server(port=0, provider=provider)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(
    server,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
):
    host, port = server.server_address[:2]
    connection = HTTPConnection(host, port, timeout=3)
    try:
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        return response.status, dict(response.getheaders()), payload
    finally:
        connection.close()


def raw_request(server, method: str, path: str, headers: list[tuple[str, str]]):
    host, port = server.server_address[:2]
    connection = HTTPConnection(host, port, timeout=3)
    try:
        connection.putrequest(method, path, skip_host=True)
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        payload = response.read()
        return response.status, dict(response.getheaders()), payload
    finally:
        connection.close()


def decode_json(payload: bytes):
    return json.loads(payload.decode("utf-8"))


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

    snapshot = dashboard.build_snapshot(provider)
    assert thread_id not in json.dumps(snapshot)
    assert snapshot["callback_bridge_health"]["batches"]["inflight_batch_member_count"] == 3


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


def test_production_provider_uses_only_existing_read_paths(monkeypatch, tmp_path):
    calls = []
    root = _init_canonical_repo(tmp_path)

    def fake_list_tasks(repo_root, status="pending", limit=80):
        calls.append(("task_store.list_tasks", repo_root, status, limit))
        return [{
            "task_id": f"TASK_{status.upper()}_B12_V1",
            "status": status,
            "topic": "imagery",
            "runner": "runner_a_b12",
        }]

    def fake_get_task(repo_root, task_id):
        calls.append(("task_store.get_task", repo_root, task_id))
        return {"task_id": task_id}

    def forbidden_legacy_provider(*_args, **_kwargs):
        raise AssertionError("canonical dashboard must not call a legacy provider")

    monkeypatch.setattr(dashboard.task_store, "list_tasks", fake_list_tasks)
    monkeypatch.setattr(dashboard.task_store, "get_task", fake_get_task)
    monkeypatch.setattr(
        dashboard.completion_inbox, "build_completion_inbox", forbidden_legacy_provider
    )
    monkeypatch.setattr(dashboard.cost_ledger, "build_cost_ledger", forbidden_legacy_provider)
    monkeypatch.setattr(dashboard.core, "collision_guard", forbidden_legacy_provider)

    provider = dashboard.DashboardProvider(
        task_limit=25, stale_processing_hours=6, repo_root=root
    )
    assert provider.list_tasks("pending")[0]["task_id"] == "TASK_PENDING_B12_V1"
    assert provider.get_task("TASK_PENDING_B12_V1") == {"task_id": "TASK_PENDING_B12_V1"}
    assert provider.get_completion_inbox()["review_queue"][0]["task_id"] == "TASK_REVIEW_B12_V1"
    assert provider.get_cost_ledger()["aggregates"]["by_runner"] == {}
    assert provider.get_collision_report()["collision_free"] is True

    assert ("task_store.list_tasks", root, "pending", 25) in calls
    assert ("task_store.get_task", root, "TASK_PENDING_B12_V1") in calls
    assert all(call[0].startswith("task_store.") for call in calls)


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


@pytest.mark.parametrize("host", ["127.0.0.1", "127.10.20.30", "::1", "localhost", "[::1]"])
def test_loopback_hosts_are_allowed(host):
    assert dashboard.is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.9", "example.com", ""])
def test_non_loopback_hosts_are_rejected(host):
    assert dashboard.is_loopback_host(host) is False
    with pytest.raises(ValueError, match="loopback"):
        dashboard.create_server(host=host, port=0, provider=FakeProvider())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("localhost", ("localhost", None)),
        ("LOCALHOST:8765", ("localhost", 8765)),
        ("127.10.20.30:80", ("127.10.20.30", 80)),
        ("[::1]:8765", ("::1", 8765)),
        ("[0:0:0:0:0:0:0:1]", ("::1", None)),
    ],
)
def test_loopback_authorities_are_parsed_without_dns(value, expected):
    assert dashboard.parse_loopback_authority(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "example.com",
        "localhost.example.com",
        "0.0.0.0:8765",
        "[::]:8765",
        "localhost:",
        "user@127.0.0.1",
        "127.0.0.1/path",
    ],
)
def test_non_loopback_or_ambiguous_authorities_are_rejected(value):
    assert dashboard.parse_loopback_authority(value) is None


def test_static_path_resolution_is_allowlisted_and_traversal_safe():
    resolved = dashboard.resolve_static_path("/dashboard.css")
    assert resolved is not None
    assert resolved[0] == dashboard.STATIC_DIR / "dashboard.css"
    assert resolved[1].startswith("text/css")

    assert dashboard.resolve_static_path("/%2e%2e/dashboard.py") is None
    assert dashboard.resolve_static_path("/static/../dashboard.py") is None
    assert dashboard.resolve_static_path("/dashboard.py") is None
    assert dashboard.resolve_static_path("//dashboard.css") is None
    assert dashboard.resolve_static_path("/static/dashboard.js%00.txt") is None


def test_ephemeral_server_serves_assets_and_json_get_endpoints():
    provider = FakeProvider()
    with running_server(provider) as server:
        status, headers, payload = request(server, "GET", "/healthz")
        assert status == 200
        assert decode_json(payload)["readonly"] is True
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["Cross-Origin-Opener-Policy"] == "same-origin"
        assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
        assert headers["X-Permitted-Cross-Domain-Policies"] == "none"
        assert headers["Cache-Control"] == "no-store"
        assert "camera=()" in headers["Permissions-Policy"]
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert "Python" not in headers["Server"]

        status, headers, payload = request(server, "GET", "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert b"AIWorkHub Task Operations" in payload
        assert b'id="tab-usage"' in payload
        assert b'id="tab-returns"' in payload

        status, headers, payload = request(server, "GET", "/dashboard.js")
        assert status == 200
        assert headers["Content-Type"].startswith("text/javascript")
        assert b"/api/snapshot" in payload

        status, headers, payload = request(server, "GET", "/api/snapshot")
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        assert decode_json(payload)["status_counts"]["active"] == 3

        status, _, payload = request(server, "GET", "/api/task?id=TASK_REVIEW_B12_V1")
        assert status == 200
        assert decode_json(payload)["task"]["validation_status"] == "PASS"

        status, _, payload = request(server, "GET", "/api/task?id=TASK_UNKNOWN_B12_V1")
        assert status == 404
        assert decode_json(payload)["error"] == "task_not_found"


def test_request_host_must_be_an_unambiguous_loopback_authority():
    provider = FakeProvider()
    with running_server(provider) as server:
        for host_header in [
            "example.com",
            "localhost.example.com",
            "0.0.0.0:8765",
            "user@127.0.0.1",
        ]:
            status, _, payload = request(
                server,
                "GET",
                "/api/snapshot",
                headers={"Host": host_header},
            )
            assert status == 421
            assert decode_json(payload)["error"] == "invalid_host"

        status, _, payload = raw_request(server, "GET", "/healthz", [])
        assert status == 421
        assert decode_json(payload)["error"] == "invalid_host"

        port = server.server_address[1]
        status, _, payload = raw_request(
            server,
            "GET",
            "/healthz",
            [("Host", f"127.0.0.1:{port}"), ("Host", f"localhost:{port}")],
        )
        assert status == 421
        assert decode_json(payload)["error"] == "invalid_host"
        assert provider.calls == []


def test_origin_must_match_the_request_host_when_present():
    with running_server(FakeProvider()) as server:
        port = server.server_address[1]
        authority = f"localhost:{port}"
        status, _, _ = request(
            server,
            "GET",
            "/healthz",
            headers={"Host": authority, "Origin": f"http://{authority}"},
        )
        assert status == 200

        for origin in [
            f"http://127.0.0.1:{port}",
            f"https://{authority}",
            "http://example.com",
            "null",
        ]:
            status, _, payload = request(
                server,
                "GET",
                "/healthz",
                headers={"Host": authority, "Origin": origin},
            )
            assert status == 403
            assert decode_json(payload)["error"] == "invalid_origin"

        status, _, payload = raw_request(
            server,
            "GET",
            "/healthz",
            [
                ("Host", authority),
                ("Origin", f"http://{authority}"),
                ("Origin", f"http://{authority}"),
            ],
        )
        assert status == 403
        assert decode_json(payload)["error"] == "invalid_origin"


def test_ephemeral_server_rejects_bad_paths_queries_and_mutations():
    provider = FakeProvider()
    with running_server(provider) as server:
        for path in [
            "/%2e%2e/dashboard.py",
            "/static/../dashboard.py",
            "/dashboard.py",
        ]:
            status, _, payload = request(server, "GET", path)
            assert status == 404
            assert b"Local, read-only HTTP dashboard" not in payload

        status, _, payload = request(server, "GET", "/api/task")
        assert status == 400
        assert decode_json(payload)["error"] == "bad_request"

        status, _, payload = request(server, "GET", "/api/task?id=bad%2Ftask")
        assert status == 400
        assert decode_json(payload)["error"] == "bad_request"

        status, _, _ = request(server, "GET", "/api/snapshot?unexpected=1")
        assert status == 400

        calls_before = list(provider.calls)
        for method in ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "PROPFIND"]:
            status, headers, payload = request(server, method, "/api/task?id=TASK_REVIEW_B12_V1", b"{}")
            assert status == 405
            assert headers["Allow"] == "GET, HEAD"
            assert decode_json(payload)["message"] == "dashboard is read-only"
        assert provider.calls == calls_before


def test_ordinary_pages_and_api_routes_retain_strict_anti_framing_headers():
    """B455: adding the VS Code embed route must not weaken anti-framing
    anywhere except the one explicit VSCODE_EMBED_PATH document."""
    with running_server(FakeProvider()) as server:
        for path in [
            "/",
            "/dashboard.js",
            "/dashboard.css",
            "/healthz",
            "/api/snapshot",
            "/api/task?id=TASK_REVIEW_B12_V1",
        ]:
            status, headers, _ = request(server, "GET", path)
            assert status == 200
            assert headers["X-Frame-Options"] == "DENY"
            assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_vscode_embed_route_relaxes_frame_ancestors_only():
    with running_server(FakeProvider()) as server:
        status, headers, payload = request(server, "GET", dashboard.VSCODE_EMBED_PATH)
        assert status == 200
        assert "X-Frame-Options" not in headers
        csp = headers["Content-Security-Policy"]
        assert f"frame-ancestors {dashboard.VSCODE_EMBED_FRAME_ANCESTORS}" in csp
        assert "vscode-webview:" in csp
        assert "https://*.vscode-cdn.net" in csp
        # every other directive stays exactly as restrictive as normal pages
        assert "default-src 'none'" in csp
        assert "connect-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "object-src 'none'" in csp
        assert "base-uri 'none'" in csp
        assert "form-action 'none'" in csp
        # same underlying document as "/" -- no separate/copied frontend
        assert b"AIWorkHub Task Operations" in payload
        assert headers["X-Content-Type-Options"] == "nosniff"

        status, _, head_payload = request(server, "HEAD", dashboard.VSCODE_EMBED_PATH)
        assert status == 200
        assert head_payload == b""


def test_vscode_embed_route_rejects_query_and_never_leaks_to_other_paths():
    with running_server(FakeProvider()) as server:
        status, headers, _ = request(server, "GET", f"{dashboard.VSCODE_EMBED_PATH}?x=1")
        assert status == 400
        assert headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]

        for path in ["/", "/?embed=vscode", "/embed/vscode2", "/embed", "/EMBED/VSCODE"]:
            status, headers, _ = request(server, "GET", path)
            assert headers["X-Frame-Options"] == "DENY"
            assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
            assert "vscode-webview:" not in headers["Content-Security-Policy"]


def test_vscode_embed_route_still_enforces_host_and_origin_checks():
    """Arbitrary Host/Origin values must not relax anti-framing or bypass
    the existing loopback/origin gate on the new route."""
    with running_server(FakeProvider()) as server:
        port = server.server_address[1]
        status, _, payload = request(
            server, "GET", dashboard.VSCODE_EMBED_PATH,
            headers={"Host": "example.com"},
        )
        assert status == 421
        assert decode_json(payload)["error"] == "invalid_host"

        status, _, payload = request(
            server, "GET", dashboard.VSCODE_EMBED_PATH,
            headers={"Host": f"localhost:{port}", "Origin": "http://evil.example"},
        )
        assert status == 403
        assert decode_json(payload)["error"] == "invalid_origin"


def test_api_and_mutation_endpoints_keep_auth_unchanged_by_embed_route(tmp_path, monkeypatch):
    """B455: the VS Code embed route is a read-only static document; it must
    not alter /api/* authorization behavior in any way."""
    import importlib

    taskdb = importlib.import_module("AITools.taskdb")
    db_path = tmp_path / "queue.sqlite"
    monkeypatch.setattr(taskdb, "DEFAULT_DB", db_path)
    with taskdb.open_db(db_path) as conn:
        taskdb.init_db(conn)

    with running_server(FakeProvider()) as server:
        body = json.dumps({"task_id": "NOPE", "reason": "x"}).encode()
        status, headers, _ = request(server, "POST", "/api/archive", body)
        assert status == 401
        assert headers["X-Frame-Options"] == "DENY"

        status, headers, _ = request(server, "POST", dashboard.VSCODE_EMBED_PATH, body)
        assert status == 405
        assert headers["Allow"] == "GET, HEAD"


def test_archive_post_requires_capability_and_never_echoes_token(tmp_path, monkeypatch):
    import aiworkhub

    token = "dashboard-test-capability"
    token_path = tmp_path / "coordinator.token"
    token_path.write_text(token, encoding="utf-8")
    token_path.chmod(0o600)
    monkeypatch.setattr(aiworkhub, "coordinator_config", lambda: ("", str(token_path)))

    root = _init_canonical_repo(tmp_path, "archive-repo")
    with sqlite3.connect(_canonical_db(root)) as conn:
        _insert_canonical_task(
            conn,
            "ARCHIVE_HTTP_TEST",
            status="finished",
            worker_status="done",
            runner="test",
            topic="test",
        )

    body = json.dumps({"task_id": "ARCHIVE_HTTP_TEST", "reason": "cleanup"}).encode()
    provider = FakeProvider()
    provider.repo_root = root
    with running_server(provider) as server:
        status, _, payload = request(server, "POST", "/api/archive", body)
        assert status == 401
        assert token.encode() not in payload

        status, _, payload = request(
            server, "POST", "/api/archive", body,
            {"X-Coordinator-Token": "wrong-capability"},
        )
        assert status == 401
        assert token.encode() not in payload
        assert b"wrong-capability" not in payload

        status, _, payload = request(
            server, "POST", "/api/archive", body,
            {"X-Coordinator-Token": token},
        )
        assert status == 200
        assert decode_json(payload)["ok"] is True
        assert token.encode() not in payload

    assert task_store.get_task(root, "ARCHIVE_HTTP_TEST")["status"] == "archived"
    serialized_events = json.dumps(
        task_store.get_task_events(root, "ARCHIVE_HTTP_TEST"), ensure_ascii=False,
    )
    assert token not in serialized_events


def test_health_identity_get_exposes_bounded_non_secret_queue_identity(tmp_path, monkeypatch):
    root = _init_canonical_repo(tmp_path, "identity-repo")
    db_path = _canonical_db(root)
    provider = FakeProvider()
    provider.repo_root = root

    with running_server(provider) as server:
        status, _, payload = request(server, "GET", "/api/health-identity")
        assert status == 200
        identity = decode_json(payload)
        assert identity["ok"] is True
        assert identity["db_path"] == str(db_path.resolve())
        assert len(identity["db_identity_fingerprint"]) == 32
        serialized = json.dumps(identity).lower()
        assert "origin_thread" not in serialized
        assert "coordinator-token" not in serialized


def test_head_returns_headers_without_a_body():
    with running_server(FakeProvider()) as server:
        status, headers, payload = request(server, "HEAD", "/dashboard.css")
        assert status == 200
        assert int(headers["Content-Length"]) > 0
        assert payload == b""


def test_cli_no_open_passes_host_and_port_without_launching_browser(monkeypatch, capsys):
    observed = {}

    class StubServer:
        server_address = ("127.0.0.1", 43123)

        def serve_forever(self, poll_interval=0.25):
            observed["poll_interval"] = poll_interval
            raise KeyboardInterrupt

        def server_close(self):
            observed["closed"] = True

    def fake_create_server(host, port):
        observed["host"] = host
        observed["port"] = port
        return StubServer()

    def fail_browser_open(*_args, **_kwargs):
        raise AssertionError("browser must not open with --no-open")

    monkeypatch.setattr(dashboard, "create_server", fake_create_server)
    monkeypatch.setattr(dashboard.webbrowser, "open", fail_browser_open)

    dashboard.main(["--host", "127.0.0.1", "--port", "0", "--no-open"])

    assert observed == {
        "host": "127.0.0.1",
        "port": 0,
        "poll_interval": 0.25,
        "closed": True,
    }
    assert "http://127.0.0.1:43123/" in capsys.readouterr().out
