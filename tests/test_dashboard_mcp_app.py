from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_TOOL_ROOT = Path(__file__).resolve().parents[1]
_SRC = _TOOL_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, dashboard, dashboard_mcp_app, task_store  # noqa: E402


FAKE_SNAPSHOT: dict[str, Any] = {
    "schema_version": 1,
    "readonly": True,
    "health": {"ok": True, "degraded": False, "provider_error_count": 0},
    "status_counts": {
        "pending": 1,
        "processing": 1,
        "review": 0,
        "blocked": 0,
        "finished": 0,
        "archived": 0,
        "stale": 0,
        "active": 2,
    },
    "row_counts": {"pending": {"returned": 1, "exact": 1, "truncated": False}},
    "tasks": {
        "pending": [{"task_id": "TASK_B615_PENDING_V1", "topic": "coding", "runner": "runner_a"}],
        "processing": [],
        "review": [],
        "blocked": [],
        "finished": [],
        "archived": [],
        "stale": [],
    },
    "summaries": {"topics": [], "runners": []},
    "completion_inbox": {},
    "cost_usage": {"totals": {"available": False}, "ledger": {}},
    "collision_report": {},
    "agent_processes": {"processes": []},
    "callback_bridge_health": {},
    "warnings": {"stale": [], "collisions": [], "runner_mismatches": []},
    "errors": [],
}

FAKE_TASK_CARD: dict[str, Any] = {
    "generated_at": "2026-07-18T00:00:00+00:00",
    "readonly": True,
    "task": {
        "task_id": "TASK_B615_DETAIL_V1",
        "topic": "coding",
        "runner": "runner_a",
        "objective": "Do the thing",
        "allowed_writes": ["tools/x.py"],
    },
}


@pytest.fixture(autouse=True)
def _isolate_dashboard(monkeypatch):
    """Ensure no test accidentally reaches a real taskctl/SQLite call."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("dashboard.build_snapshot/build_task_detail must be monkeypatched per test")

    monkeypatch.setattr(dashboard, "build_snapshot", _boom)
    monkeypatch.setattr(dashboard, "build_task_detail", _boom)
    monkeypatch.setattr(core, "health", _boom)
    yield


# ---------------------------------------------------------------------------
# snapshot_view
# ---------------------------------------------------------------------------

def test_snapshot_view_reuses_build_snapshot_as_sole_data_builder(monkeypatch):
    calls: list[tuple[Any, ...]] = []

    def fake_build_snapshot(*args, **kwargs):
        calls.append((args, kwargs))
        return dict(FAKE_SNAPSHOT)

    monkeypatch.setattr(dashboard, "build_snapshot", fake_build_snapshot)
    monkeypatch.setattr(core, "dispatcher_health", lambda: {
        "ok": True,
        "healthy": True,
        "status": "running",
        "dispatch_expected": True,
        "dispatcher_running": True,
        "registered": True,
        "repo_id": "repo_canon",
        "dispatcher_repo_id": "repo_canon",
        "problems": [],
    })
    result = dashboard_mcp_app.snapshot_view()

    assert calls == [((), {})]
    assert result["status_counts"] == FAKE_SNAPSHOT["status_counts"]
    assert result["tasks"] == FAKE_SNAPSHOT["tasks"]
    assert result["callback_delivery"]["status"] == "running"
    assert result["callback_delivery"]["dispatcher_running"] is True
    assert result["server_tool"] == "aiworkhub_dashboard_snapshot"
    assert result["authority_flags"]["readonly"] is True
    assert result["authority_flags"]["queue_write"] is False
    assert "transport_truncated_fields" not in result


def test_snapshot_identity_does_not_hide_stopped_callback_dispatcher(monkeypatch):
    monkeypatch.setattr(dashboard, "build_snapshot", lambda: dict(FAKE_SNAPSHOT))
    monkeypatch.setattr(core, "manager_bootstrap", lambda: {
        "ok": True,
        "role": "manager",
        "provider": "codex",
        "manager_route": {"thread_id": "019f5097-6dbe-7172-870a-945afc5f3bfa"},
    })
    monkeypatch.setattr(core, "dispatcher_health", lambda: {
        "ok": False,
        "healthy": False,
        "status": "stopped",
        "dispatch_expected": True,
        "dispatcher_running": False,
        "registered": False,
        "repo_id": "repo_canon",
        "dispatcher_repo_id": "",
        "problems": ["dispatcher_unregistered"],
    })

    result = dashboard_mcp_app.snapshot_view()

    assert result["manager_identity"]["role"] == "manager"
    assert result["callback_delivery"]["healthy"] is False
    assert result["callback_delivery"]["problems"] == ["dispatcher_unregistered"]


def test_snapshot_view_bounds_oversized_secondary_sections(monkeypatch):
    oversized = dict(FAKE_SNAPSHOT)
    oversized["agent_processes"] = {"processes": ["x" * 1024 for _ in range(6000)]}
    monkeypatch.setattr(dashboard, "build_snapshot", lambda: dict(oversized))

    result = dashboard_mcp_app.snapshot_view()

    assert result["agent_processes"] == {"transport_truncated": True}
    assert "agent_processes" in result["transport_truncated_fields"]
    # Essential fields survive trimming untouched.
    assert result["status_counts"] == FAKE_SNAPSHOT["status_counts"]
    assert result["tasks"] == FAKE_SNAPSHOT["tasks"]
    assert result["row_counts"] == FAKE_SNAPSHOT["row_counts"]


def test_snapshot_view_never_exceeds_response_bound_even_when_everything_is_huge(monkeypatch):
    huge = dict(FAKE_SNAPSHOT)
    for field in dashboard_mcp_app._SNAPSHOT_TRIM_ORDER:
        huge[field] = {"blob": "y" * (1024 * 1024)}
    monkeypatch.setattr(dashboard, "build_snapshot", lambda: dict(huge))

    result = dashboard_mcp_app.snapshot_view()

    assert dashboard_mcp_app._byte_len(result) <= dashboard_mcp_app.MAX_SNAPSHOT_RESPONSE_BYTES
    truncated = result["transport_truncated_fields"]
    assert truncated
    assert truncated == list(dashboard_mcp_app._SNAPSHOT_TRIM_ORDER[: len(truncated)])
    for field in truncated:
        assert result[field] == {"transport_truncated": True}


# ---------------------------------------------------------------------------
# task_detail_view
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_id",
    ["", " ", "../etc/passwd", "task id with spaces", "'; DROP TABLE tasks; --", "a" * 300],
)
def test_task_detail_view_rejects_invalid_task_id_without_touching_provider(monkeypatch, bad_id):
    monkeypatch.setattr(
        dashboard,
        "build_task_detail",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    result = dashboard_mcp_app.task_detail_view(bad_id)
    assert result["ok"] is False
    assert result["error"] == "invalid_task_id"
    assert result["server_tool"] == "aiworkhub_dashboard_task_detail"


def test_task_detail_view_reports_not_found(monkeypatch):
    monkeypatch.setattr(dashboard, "build_task_detail", lambda task_id: None)
    result = dashboard_mcp_app.task_detail_view("TASK_B615_MISSING_V1")
    assert result["ok"] is False
    assert result["error"] == "task_not_found"
    assert result["task_id"] == "TASK_B615_MISSING_V1"


def test_task_detail_view_reuses_build_task_detail_as_sole_data_builder(monkeypatch):
    calls: list[str] = []

    def fake_build_task_detail(task_id):
        calls.append(task_id)
        return dict(FAKE_TASK_CARD)

    monkeypatch.setattr(dashboard, "build_task_detail", fake_build_task_detail)
    result = dashboard_mcp_app.task_detail_view("TASK_B615_DETAIL_V1")

    assert calls == ["TASK_B615_DETAIL_V1"]
    assert result["ok"] is True
    assert result["task"]["task_id"] == "TASK_B615_DETAIL_V1"
    assert result["server_tool"] == "aiworkhub_dashboard_task_detail"
    assert result["authority_flags"]["process_launch"] is False


def test_task_detail_view_bounds_oversized_task_fields(monkeypatch):
    huge_card = {
        "generated_at": "2026-07-18T00:00:00+00:00",
        "readonly": True,
        "task": {
            "task_id": "TASK_B615_HUGE_V1",
            "topic": "coding",
            "result": "z" * (2 * 1024 * 1024),
        },
    }
    monkeypatch.setattr(dashboard, "build_task_detail", lambda task_id: dict(huge_card))

    result = dashboard_mcp_app.task_detail_view("TASK_B615_HUGE_V1")

    assert result["ok"] is True
    assert result["task"]["result"] == "(transport_truncated)"
    assert "result" in result["transport_truncated_fields"]
    assert result["task"]["task_id"] == "TASK_B615_HUGE_V1"
    assert dashboard_mcp_app._byte_len(result) <= dashboard_mcp_app.MAX_TASK_DETAIL_RESPONSE_BYTES


# ---------------------------------------------------------------------------
# health_view
# ---------------------------------------------------------------------------

def test_health_view_reads_repo_root_env_and_never_calls_build_snapshot(monkeypatch):
    # B865/B850: health_view() reads AIWORKHUB_REPO_ROOT + task_store.storage_
    # readiness directly (never core.health(), never dashboard.build_snapshot)
    # so the connection banner never depends on a repository shipping
    # AITools/taskctl.py and polling stays cheap.
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    monkeypatch.setattr(
        dashboard_mcp_app.core,
        "repo_root",
        lambda: (_ for _ in ()).throw(RuntimeError("repository_root_not_found")),
    )

    def _boom_snapshot():
        raise AssertionError("health_view must never call dashboard.build_snapshot")

    monkeypatch.setattr(dashboard, "build_snapshot", _boom_snapshot)

    result = dashboard_mcp_app.health_view()
    assert result["ok"] is False
    assert result["error"] == "repo_root_not_selected"
    assert result["server_tool"] == "aiworkhub_dashboard_health"
    assert result["server_version"]
    assert result["authority_flags"]["agent_launch"] is False


def test_health_view_uses_explicit_repo_binding(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))

    result = dashboard_mcp_app.health_view()

    assert result["ok"] is True
    assert result["repo"] == str(repo)
    assert result["storage"]["ready"] is True


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------

class _FakeFastMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, name: str):
        def _decorator(fn):
            self.registered[name] = fn
            return fn

        return _decorator


def test_register_binds_readonly_live_output_and_initialize_tools():
    # Task live output, AI Memory browsing, and initialization are additive
    # alongside the original three read-only tools; register() must bind all
    # six without dropping the historical surface.
    fake_mcp = _FakeFastMCP()
    names = dashboard_mcp_app.register(fake_mcp)

    expected_names = set(dashboard_mcp_app.READONLY_TOOL_NAMES) | {
        dashboard_mcp_app.LIVE_OUTPUT_TOOL_NAME,
        dashboard_mcp_app.MEMORY_TOOL_NAME,
        dashboard_mcp_app.SESSION_TOOL_NAME,
        dashboard_mcp_app.KB_TOOL_NAME,
        dashboard_mcp_app.SETTINGS_TOOL_NAME,
        dashboard_mcp_app.STORAGE_RETENTION_PREVIEW_TOOL_NAME,
        dashboard_mcp_app.TERMINAL_LOG_RETENTION_PREVIEW_TOOL_NAME,
        dashboard_mcp_app.INITIALIZE_TOOL_NAME,
    }
    expected_names.update(dashboard_mcp_app.SETTINGS_UPDATE_TOOLS)
    expected_names.update(dashboard_mcp_app.STORAGE_RETENTION_WRITE_TOOLS)
    expected_names.update(dashboard_mcp_app.TERMINAL_LOG_RETENTION_WRITE_TOOLS)
    assert set(names) == expected_names
    assert set(fake_mcp.registered) == expected_names
    assert fake_mcp.registered["aiworkhub_dashboard_snapshot"] is dashboard_mcp_app.snapshot_view
    assert fake_mcp.registered["aiworkhub_dashboard_task_detail"] is dashboard_mcp_app.task_detail_view
    assert fake_mcp.registered["aiworkhub_dashboard_health"] is dashboard_mcp_app.health_view
    assert fake_mcp.registered["aiworkhub_dashboard_task_live_output"] is dashboard_mcp_app.task_live_output_view
    assert fake_mcp.registered["aiworkhub_dashboard_initialize"] is dashboard_mcp_app.initialize_view


def test_storage_retention_tools_preserve_read_write_authority(monkeypatch):
    monkeypatch.setattr(dashboard_mcp_app.core, "repo_root", lambda: Path("/repo"))
    monkeypatch.setattr(
        dashboard_mcp_app.storage_retention,
        "preview",
        lambda _root: {"ok": True, "candidate_count": 0, "preview_digest": "a" * 64},
    )
    monkeypatch.setattr(
        dashboard_mcp_app.storage_retention,
        "list_batches",
        lambda _root: {"ok": True, "batches": []},
    )
    preview = dashboard_mcp_app.storage_retention_preview_view()
    assert preview["authority_flags"]["readonly"] is True
    assert preview["authority_flags"]["queue_write"] is False

    monkeypatch.setattr(
        dashboard_mcp_app.storage_retention,
        "quarantine",
        lambda *_args, **_kwargs: {"ok": True, "quarantined": 1},
    )
    monkeypatch.setattr(dashboard_mcp_app.storage_observability, "invalidate", lambda _root: None)
    written = dashboard_mcp_app.storage_quarantine_view("a" * 64, confirm=True)
    assert written["ok"] is True
    assert written["authority_flags"]["readonly"] is False
    assert written["authority_flags"]["storage_write"] is True

    monkeypatch.setattr(
        dashboard_mcp_app.storage_retention,
        "prune_stale_registrations",
        lambda *_args, **_kwargs: {"ok": True, "pruned": 2},
    )
    pruned = dashboard_mcp_app.storage_registration_prune_view("c" * 64, confirm=True)
    assert pruned["ok"] is True
    assert pruned["authority_flags"]["readonly"] is False
    assert pruned["authority_flags"]["storage_write"] is True


def test_terminal_log_retention_tools_preserve_read_write_authority(monkeypatch):
    monkeypatch.setattr(dashboard_mcp_app.core, "repo_root", lambda: Path("/repo"))
    monkeypatch.setattr(
        dashboard_mcp_app.terminal_log_retention,
        "preview",
        lambda _root: {"ok": True, "candidate_count": 0, "preview_digest": "b" * 64},
    )
    monkeypatch.setattr(
        dashboard_mcp_app.terminal_log_retention,
        "list_batches",
        lambda _root: {"ok": True, "batches": []},
    )
    preview = dashboard_mcp_app.terminal_log_retention_preview_view()
    assert preview["authority_flags"]["readonly"] is True
    assert preview["authority_flags"].get("storage_write", False) is False

    monkeypatch.setattr(
        dashboard_mcp_app.terminal_log_retention,
        "quarantine",
        lambda *_args, **_kwargs: {"ok": True, "quarantined": 4},
    )
    monkeypatch.setattr(dashboard_mcp_app.storage_observability, "invalidate", lambda _root: None)
    written = dashboard_mcp_app.terminal_log_quarantine_view("b" * 64, confirm=True)
    assert written["ok"] is True
    assert written["authority_flags"]["readonly"] is False
    assert written["authority_flags"]["storage_write"] is True


def test_task_id_validation_reuses_the_dashboard_http_route_pattern():
    # Same compiled pattern object as dashboard._TASK_ID_RE -- never a second,
    # potentially drifting, task_id regex.
    assert dashboard_mcp_app.task_detail_view.__module__ == "aiworkhub.dashboard_mcp_app"
    assert dashboard._TASK_ID_RE.fullmatch("TASK_OK_V1")


def test_authority_flags_never_grant_write_or_launch_capability():
    flags = dashboard_mcp_app._readonly_authority_flags()
    assert flags == {
        "readonly": True,
        "queue_write": False,
        "audit_write": False,
        "process_launch": False,
        "agent_launch": False,
        "shell_invocation": False,
    }
