from __future__ import annotations

import copy
import inspect
import json
import random
import sys
import threading
import time
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
    result = dashboard_mcp_app.snapshot_view(full=True)

    assert calls == [((), {"summary_only": False})]
    assert result["status_counts"] == FAKE_SNAPSHOT["status_counts"]
    assert result["tasks"] == FAKE_SNAPSHOT["tasks"]
    assert result["callback_delivery"]["status"] == "running"
    assert result["callback_delivery"]["dispatcher_running"] is True
    assert result["server_tool"] == "aiworkhub_dashboard_snapshot"
    assert result["snapshot_mode"] == "full"
    assert result["authority_flags"]["readonly"] is True
    assert result["authority_flags"]["queue_write"] is False
    assert "transport_truncated_fields" not in result


def test_snapshot_view_serializes_allocation_heavy_builds(monkeypatch):
    barrier = threading.Barrier(3)
    state_lock = threading.Lock()
    active = 0
    peak = 0

    def fake_build_snapshot(**_kwargs):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with state_lock:
            active -= 1
        return dict(FAKE_SNAPSHOT)

    monkeypatch.setattr(dashboard, "build_snapshot", fake_build_snapshot)

    def run_snapshot():
        barrier.wait()
        dashboard_mcp_app.snapshot_view(full=True)

    threads = [threading.Thread(target=run_snapshot) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert peak == 1


def test_snapshot_identity_does_not_hide_stopped_callback_dispatcher(monkeypatch):
    monkeypatch.setattr(
        dashboard, "build_snapshot", lambda **_kwargs: dict(FAKE_SNAPSHOT)
    )
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


def test_snapshot_default_is_bounded_manager_summary(monkeypatch):
    calls: list[dict[str, Any]] = []

    def fake_build_snapshot(**kwargs):
        calls.append(kwargs)
        return dict(FAKE_SNAPSHOT)

    monkeypatch.setattr(dashboard, "build_snapshot", fake_build_snapshot)

    result = dashboard_mcp_app.snapshot_view()

    assert calls == [{"summary_only": True}]
    assert result["snapshot_mode"] == "summary"
    assert result["full_snapshot_available"] is True
    assert result["status_counts"] == FAKE_SNAPSHOT["status_counts"]
    assert result["row_counts"] == FAKE_SNAPSHOT["row_counts"]
    assert "tasks" not in result
    assert "agent_processes" not in result
    assert "tasks" in result["omitted_fields"]


def test_snapshot_view_bounds_oversized_secondary_sections(monkeypatch):
    oversized = dict(FAKE_SNAPSHOT)
    oversized["agent_processes"] = {"processes": ["x" * 1024 for _ in range(6000)]}
    monkeypatch.setattr(
        dashboard, "build_snapshot", lambda **_kwargs: dict(oversized)
    )

    result = dashboard_mcp_app.snapshot_view(full=True)

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
    monkeypatch.setattr(dashboard, "build_snapshot", lambda **_kwargs: dict(huge))

    result = dashboard_mcp_app.snapshot_view(full=True)

    assert dashboard_mcp_app._byte_len(result) <= dashboard_mcp_app.MAX_SNAPSHOT_RESPONSE_BYTES
    truncated = result["transport_truncated_fields"]
    assert truncated
    assert truncated == list(dashboard_mcp_app._SNAPSHOT_TRIM_ORDER[: len(truncated)])
    for field in truncated:
        assert result[field] == {"transport_truncated": True}


# ---------------------------------------------------------------------------
# snapshot_view: bounded quarantine history and known-contract suppression
# ---------------------------------------------------------------------------

_QUARANTINE_ARRAYS = (
    "quarantine_batches",
    "terminal_log_quarantine_batches",
    "task_retention_batches",
)
# Every retention provider lists at most its newest 100 batches, so a mature
# repository hands the snapshot 100 rows in each of the three arrays.
_PROVIDER_BATCH_CAP = 100


def _quarantine_stamp(index: int) -> str:
    return f"2026-07-{1 + index // 24:02d}T{index % 24:02d}:30:00+00:00"


def _quarantine_row(array: str, index: int) -> dict[str, Any]:
    """One batch row in the shape the array's retention provider lists."""

    batch_id = f"{array[:4]}-{index:04d}"
    deadline = "2026-08-20T00:00:00+00:00"
    if array == "task_retention_batches":
        return {
            "batch_id": batch_id,
            "task_count": 7,
            "bytes": 41_000 + index,
            "quarantined_at": _quarantine_stamp(index),
            "restore_deadline": deadline,
            "restored": False,
            "purge_eligible": False,
        }
    row: dict[str, Any] = {
        "batch_id": batch_id,
        "created_at": _quarantine_stamp(index),
        "restore_deadline": deadline,
        "status": "quarantined",
        "quarantined_count": 3,
        "restored_count": 0,
        "bytes": 1_048_576 + index,
        "purge_eligible": False,
        "reapable_empty": False,
    }
    if array == "terminal_log_quarantine_batches":
        row.update(
            {
                "recorded_bytes": 1_048_576 + index,
                "on_disk_bytes": 1_048_576 + index,
                "unclaimed": False,
            }
        )
    return row


def _storage_usage(rows: int = _PROVIDER_BATCH_CAP, *, reverse: bool = False) -> dict[str, Any]:
    """A storage_observability.snapshot() payload whose batch arrays are unsorted."""

    usage: dict[str, Any] = {
        "schema_version": 1,
        "readonly": True,
        "disk_total_bytes": 512_000_000_000,
        "disk_free_bytes": 200_000_000_000,
        "scan_status": "ready",
        "repo_data_bytes": 9_100_000_000,
        "quarantine_bytes": 123_456_789,
        "managed_total_bytes": 19_900_000_000,
        "task_retention": {"ok": True, "candidate_count": 4, "archived_total": 812},
        "storage_bounds": {"terminal_unclaimed_count": 0},
        "errors": [],
    }
    for array in _QUARANTINE_ARRAYS:
        order = list(range(rows))
        random.Random(857).shuffle(order)
        if reverse:
            order.reverse()
        usage[array] = [_quarantine_row(array, index) for index in order]
    return usage


def _manager_reply() -> dict[str, Any]:
    """A manager_bootstrap route-gate reply: identity facts plus the contract prose."""

    reply: dict[str, Any] = {
        "ok": True,
        "role": "manager",
        "provider": "codex",
        "repo_id": "repo_canon",
        "storage_ready": True,
        "manager_verified": True,
        "manager_route": {"thread_id": "019f5097-6dbe-7172-870a-945afc5f3bfa"},
        "task_health": {"pending": 1, "processing": 1},
        "contract_version": core.MANAGER_CONTRACT_VERSION,
        "contract_sha256": core.MANAGER_CONTRACT_SHA256,
        "contract_delivered": True,
        "contract_delivery_reason": "route_gate_call",
    }
    reply.update(copy.deepcopy(core.MANAGER_CONTRACT))
    return reply


def _stub_snapshot_sources(
    monkeypatch,
    *,
    storage_usage: dict[str, Any] | None = None,
    manager: dict[str, Any] | None = None,
) -> None:
    """Serve one hermetic snapshot: no SQLite, repository scan or router read."""

    snapshot = dict(FAKE_SNAPSHOT)
    if storage_usage is not None:
        snapshot["storage_usage"] = storage_usage
    monkeypatch.setattr(dashboard, "build_snapshot", lambda **_kwargs: dict(snapshot))
    monkeypatch.setattr(core, "manager_bootstrap", lambda: copy.deepcopy(manager or {}))
    monkeypatch.setattr(core, "dispatcher_health", lambda: {"ok": True, "status": "running"})
    monkeypatch.setattr(core, "repo_root", lambda: Path("/"))
    monkeypatch.setattr(core, "read_selected_coordinator_target", lambda _root: {})
    monkeypatch.setattr(
        dashboard_mcp_app.shared_router, "list_known_repositories", lambda **_kwargs: {}
    )
    monkeypatch.setattr(dashboard_mcp_app.storage_observability, "snapshot", lambda _root: {})


def test_snapshot_default_bounds_quarantine_rows_and_states_the_full_count(monkeypatch):
    usage = _storage_usage()
    _stub_snapshot_sources(monkeypatch, storage_usage=usage)

    bounded = dashboard_mcp_app.snapshot_view()["storage_usage"]

    assert dashboard_mcp_app.MAX_SNAPSHOT_QUARANTINE_ROWS == 5
    for array in _QUARANTINE_ARRAYS:
        assert bounded[array]["total_count"] == 100
        assert bounded[array]["returned_count"] == 5
        assert bounded[array]["truncated"] is True
        assert bounded[array]["rows"] == [
            _quarantine_row(array, index) for index in range(99, 94, -1)
        ]
    # Only the three history arrays change; every other storage fact stays whole.
    assert set(bounded) == set(usage)
    for key in set(usage) - set(_QUARANTINE_ARRAYS):
        assert bounded[key] == usage[key]
    # The builder's cached arrays are read, never consumed by the projection.
    assert all(len(usage[array]) == 100 for array in _QUARANTINE_ARRAYS)


def test_snapshot_default_quarantine_rows_do_not_depend_on_input_order(monkeypatch):
    payloads = []
    for reverse in (False, True):
        _stub_snapshot_sources(monkeypatch, storage_usage=_storage_usage(reverse=reverse))
        payloads.append(json.dumps(dashboard_mcp_app.snapshot_view()["storage_usage"]))

    assert payloads[0] == payloads[1]


def test_snapshot_default_quarantine_recency_is_the_newest_instant_then_batch_id(monkeypatch):
    rows = [
        {"batch_id": "offset-older", "created_at": "2026-07-10T12:00:00+05:00"},
        {"batch_id": "utc-newer", "created_at": "2026-07-10T08:00:00+00:00"},
        {"batch_id": "tie-a", "created_at": "2026-07-09T00:00:00+00:00"},
        {"batch_id": "tie-c", "created_at": "2026-07-09T00:00:00Z"},
        {"batch_id": "tie-b", "created_at": "2026-07-09T00:00:00+00:00"},
        {"batch_id": "undated"},
        {"batch_id": "garbled", "created_at": "not-a-time"},
    ]
    _stub_snapshot_sources(monkeypatch, storage_usage={"quarantine_batches": rows})

    bound = dashboard_mcp_app.snapshot_view()["storage_usage"]["quarantine_batches"]

    # 12:00+05:00 is 07:00Z: an hour older than 08:00Z although it sorts after it as text.
    assert [row["batch_id"] for row in bound["rows"]] == [
        "utc-newer",
        "offset-older",
        "tie-c",
        "tie-b",
        "tie-a",
    ]
    assert (bound["total_count"], bound["returned_count"], bound["truncated"]) == (7, 5, True)


@pytest.mark.parametrize("count", [0, 3, 5, 6])
def test_snapshot_default_quarantine_counts_are_true_at_the_bound_edges(monkeypatch, count):
    _stub_snapshot_sources(monkeypatch, storage_usage=_storage_usage(rows=count))

    bounded = dashboard_mcp_app.snapshot_view()["storage_usage"]

    for array in _QUARANTINE_ARRAYS:
        assert bounded[array]["total_count"] == count
        assert bounded[array]["returned_count"] == min(count, 5) == len(bounded[array]["rows"])
        assert bounded[array]["truncated"] is (count > 5)


def test_snapshot_default_leaves_absent_or_unexpected_storage_shapes_alone(monkeypatch):
    usage = {
        "scan_status": "scanning",
        "quarantine_batches": None,
        "task_retention_batches": {"unexpected": True},
    }
    _stub_snapshot_sources(monkeypatch, storage_usage=usage)

    bounded = dashboard_mcp_app.snapshot_view()["storage_usage"]

    assert bounded == usage
    assert "terminal_log_quarantine_batches" not in bounded


def test_snapshot_full_keeps_every_quarantine_row_and_the_whole_contract(monkeypatch):
    usage = _storage_usage()
    manager = _manager_reply()
    _stub_snapshot_sources(monkeypatch, storage_usage=usage, manager=manager)

    # Neither the row bound nor the contract digest applies to the Webview's full shape.
    result = dashboard_mcp_app.snapshot_view(
        full=True, known_contract_sha256=core.MANAGER_CONTRACT_SHA256
    )

    assert result["snapshot_mode"] == "full"
    assert result["storage_usage"] == usage
    assert all(isinstance(result["storage_usage"][array], list) for array in _QUARANTINE_ARRAYS)
    assert result["manager_identity"] == manager
    assert result["tasks"] == FAKE_SNAPSHOT["tasks"]
    assert set(result) == set(FAKE_SNAPSHOT) | {
        "storage_usage",
        "manager_identity",
        "callback_delivery",
        "known_repositories",
        "manager_identity_target",
        "server_tool",
        "authority_flags",
        "snapshot_mode",
    }


@pytest.mark.parametrize("digest", [None, "", "   "])
def test_snapshot_default_without_a_digest_keeps_the_whole_contract(monkeypatch, digest):
    manager = _manager_reply()
    _stub_snapshot_sources(monkeypatch, manager=manager)

    assert dashboard_mcp_app.snapshot_view()["manager_identity"] == manager
    result = dashboard_mcp_app.snapshot_view(known_contract_sha256=digest)
    assert result["manager_identity"] == manager


@pytest.mark.parametrize(
    "spell",
    [str, str.upper, lambda digest: f"  {digest}\n"],
    ids=["exact", "uppercase", "padded"],
)
def test_snapshot_default_known_contract_digest_suppresses_the_unchanged_prose(monkeypatch, spell):
    manager = _manager_reply()
    _stub_snapshot_sources(monkeypatch, manager=manager)

    identity = dashboard_mcp_app.snapshot_view(
        known_contract_sha256=spell(core.MANAGER_CONTRACT_SHA256)
    )["manager_identity"]

    assert not set(core.MANAGER_CONTRACT) & set(identity)
    assert identity["contract_omitted_fields"] == sorted(core.MANAGER_CONTRACT)
    assert identity["contract_sha256"] == core.MANAGER_CONTRACT_SHA256
    assert identity["contract_delivered"] is False
    assert identity["contract_delivery_reason"] == "caller_holds_current_contract"
    assert "known_contract_sha256" in identity["contract_recall"]
    delivery_facts = {"contract_delivered", "contract_delivery_reason"}
    for key, value in manager.items():
        if key not in core.MANAGER_CONTRACT and key not in delivery_facts:
            assert identity[key] == value


@pytest.mark.parametrize("stale", ["0" * 64, "deadbeef", "not a digest"])
def test_snapshot_default_stale_contract_digest_returns_the_contract(monkeypatch, stale):
    _stub_snapshot_sources(monkeypatch, manager=_manager_reply())

    identity = dashboard_mcp_app.snapshot_view(known_contract_sha256=stale)["manager_identity"]

    for key, value in core.MANAGER_CONTRACT.items():
        assert identity[key] == value
    assert identity["contract_sha256"] == core.MANAGER_CONTRACT_SHA256
    assert identity["contract_delivered"] is True
    assert identity["contract_delivery_reason"] == "contract_sha_changed"
    assert "contract_omitted_fields" not in identity


def test_snapshot_default_digest_cannot_suppress_a_reply_that_holds_no_contract(monkeypatch):
    _stub_snapshot_sources(monkeypatch)

    def failing_bootstrap():
        raise RuntimeError("bootstrap unavailable")

    monkeypatch.setattr(core, "manager_bootstrap", failing_bootstrap)

    identity = dashboard_mcp_app.snapshot_view(
        known_contract_sha256=core.MANAGER_CONTRACT_SHA256
    )["manager_identity"]

    assert identity["ok"] is False
    assert identity["reason"] == "manager_bootstrap_failed:RuntimeError"
    assert "contract_omitted_fields" not in identity
    assert "contract_delivered" not in identity


def test_snapshot_default_payload_is_materially_smaller_on_a_mature_repository(monkeypatch):
    usage = _storage_usage()
    manager = _manager_reply()
    _stub_snapshot_sources(monkeypatch, storage_usage=usage, manager=manager)

    bounded = dashboard_mcp_app.snapshot_view()
    known = dashboard_mcp_app.snapshot_view(known_contract_sha256=core.MANAGER_CONTRACT_SHA256)
    # The same default response before the bound: raw provider arrays, whole reply.
    legacy = {**bounded, "storage_usage": usage, "manager_identity": manager}
    legacy_bytes = dashboard_mcp_app._byte_len(legacy)
    bounded_bytes = dashboard_mcp_app._byte_len(bounded)
    contract_bytes = dashboard_mcp_app._byte_len(core.MANAGER_CONTRACT)

    assert legacy_bytes > 70_000
    assert bounded_bytes < legacy_bytes * 0.25
    assert bounded_bytes < 20_000
    assert bounded_bytes - dashboard_mcp_app._byte_len(known) > contract_bytes * 0.8


def test_snapshot_tool_signature_gains_only_the_optional_contract_digest():
    parameters = inspect.signature(dashboard_mcp_app.snapshot_view).parameters

    assert list(parameters) == ["full", "previous", "previous_snapshot", "known_contract_sha256"]
    assert parameters["known_contract_sha256"].default == ""


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
        dashboard_mcp_app.SKILLS_TOOL_NAME,
        dashboard_mcp_app.SETTINGS_TOOL_NAME,
        dashboard_mcp_app.STORAGE_RETENTION_PREVIEW_TOOL_NAME,
        dashboard_mcp_app.TERMINAL_LOG_RETENTION_PREVIEW_TOOL_NAME,
        dashboard_mcp_app.TASK_RETENTION_PREVIEW_TOOL_NAME,
        dashboard_mcp_app.INITIALIZE_TOOL_NAME,
    }
    expected_names.update(dashboard_mcp_app.SETTINGS_UPDATE_TOOLS)
    expected_names.update(dashboard_mcp_app.MODEL_SETTINGS_UPDATE_TOOLS)
    expected_names.update(dashboard_mcp_app.SOURCE_GRAPH_SETTINGS_UPDATE_TOOLS)
    expected_names.update(dashboard_mcp_app.STORAGE_RETENTION_WRITE_TOOLS)
    expected_names.update(dashboard_mcp_app.TERMINAL_LOG_RETENTION_WRITE_TOOLS)
    expected_names.update(dashboard_mcp_app.TASK_RETENTION_WRITE_TOOLS)
    expected_names.update(dashboard_mcp_app.NEEDFIX_READ_TOOLS)
    expected_names.update(dashboard_mcp_app.NEEDFIX_WRITE_TOOLS)
    expected_names.update(dashboard_mcp_app.ROADMAP_READ_TOOLS)
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
        lambda _root, **_kwargs: {
            "ok": True,
            "candidate_count": 0,
            "preview_digest": "b" * 64,
        },
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


# ---------------------------------------------------------------------------
# _model_policy_view compact catalog bounding
# ---------------------------------------------------------------------------

def _fake_model_settings(models: dict[str, Any] | None = None) -> dict[str, Any]:
    """Same shape model_settings.load() publishes, without touching disk."""

    return {
        "ok": True,
        "schema_id": dashboard_mcp_app.model_settings.SCHEMA_ID,
        "revision": 4,
        "configured": True,
        "default_enabled": True,
        "providers": {},
        "adapters": {},
        "models": models or {},
        "updated_at": "2026-09-14T00:00:00+00:00",
    }


# Sized relative to the real compact bound rather than a literal, so this
# fixture stays oversized (and the pinned-routes test below still has enough
# real rows to pin) whatever MAX_MODEL_POLICY_CATALOG_ROWS is configured to.
_OVERSIZED_ROWS_PER_PROVIDER = dashboard_mcp_app.MAX_MODEL_POLICY_CATALOG_ROWS // 2 + 20


def _catalog_with_81_workers() -> dict[str, Any]:
    """N anthropic + N copilot routes, then xai past the compact row cut.

    xai sorts last, so under a global head slice of the sorted catalog it was
    the whole provider that disappeared -- not merely some of its routes.
    """

    rows: list[dict[str, Any]] = []
    for provider, adapter in (("anthropic", "claude_cli"), ("copilot", "vscode_lm")):
        for index in range(_OVERSIZED_ROWS_PER_PROVIDER):
            rows.append(
                {
                    "worker_id": f"{provider}-{index:02d}",
                    "provider": provider,
                    "adapter_id": adapter,
                    "model": f"{provider}-model-{index:02d}",
                    "enabled": True,
                }
            )
    rows.append(
        {
            "worker_id": "xai-grok",
            "provider": "xai",
            "adapter_id": "grok_kilo_cli",
            "model": "grok-4.6",
            "enabled": True,
        }
    )
    return {"workers": rows}


@pytest.fixture
def _oversized_model_catalog(monkeypatch):
    monkeypatch.setattr(
        dashboard_mcp_app.workforce_catalog,
        "load_catalog",
        lambda _root: _catalog_with_81_workers(),
    )
    monkeypatch.setattr(
        dashboard_mcp_app.vscode_lm_bridge,
        "bridge_readiness",
        lambda _root, **_kwargs: {
            "observed_models": [],
            "launchable": False,
            "blocker_reason": "",
        },
    )
    monkeypatch.setattr(
        dashboard_mcp_app.workforce_catalog,
        "opencode_identities_from_preflight",
        lambda _preflight: [],
    )
    return monkeypatch


def test_compact_model_catalog_never_drops_a_whole_provider(
    monkeypatch, _oversized_model_catalog
):
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings, "load", lambda _root: _fake_model_settings()
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]
    total = 2 * _OVERSIZED_ROWS_PER_PROVIDER + 1

    assert catalog["worker_count"] == total
    assert catalog["configured_worker_count"] == total
    assert catalog["truncated"] is True
    # Still bounded: the response never grows to the full catalog.
    assert catalog["returned_worker_count"] == len(catalog["workers"])
    assert catalog["returned_worker_count"] < total
    assert (
        catalog["returned_worker_count"]
        <= dashboard_mcp_app.MAX_MODEL_POLICY_CATALOG_ROWS
    )
    # Every configured provider reaches the Webview, including the one that
    # sorts last and used to fall off the end of the global slice.
    assert {row["provider"] for row in catalog["workers"]} == {
        "anthropic",
        "copilot",
        "xai",
    }
    xai_routes = [row for row in catalog["workers"] if row["provider"] == "xai"]
    assert [row["model"] for row in xai_routes] == ["grok-4.6"]
    # Toggleable: the Webview disables a route control only when the catalog
    # itself disabled the row or its provider is off.
    assert xai_routes[0]["catalog_enabled"] is True
    assert xai_routes[0]["effective_enabled"] is True


def test_compact_model_catalog_reports_per_provider_count_truth(
    monkeypatch, _oversized_model_catalog
):
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings, "load", lambda _root: _fake_model_settings()
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]
    counts = {entry["provider"]: entry for entry in catalog["provider_counts"]}

    assert set(counts) == {"anthropic", "copilot", "xai"}
    # Nothing was lost to the ingestion cap in this fixture and no producer cut
    # above it, so every count is unqualified: the provider's total is the
    # ingested population, it is a measurement rather than a floor, and the
    # enabled figure really is provider-wide.
    assert counts["xai"] == {
        "provider": "xai",
        "total": 1,
        "total_is_lower_bound": False,
        "ingested": 1,
        "returned": 1,
        "truncated": False,
        "enabled_total": 1,
        "enabled_counted_over": 1,
        "enabled_returned": 1,
    }
    for provider in ("anthropic", "copilot"):
        assert counts[provider]["total"] == _OVERSIZED_ROWS_PER_PROVIDER
        assert counts[provider]["ingested"] == _OVERSIZED_ROWS_PER_PROVIDER
        assert 0 < counts[provider]["returned"] < _OVERSIZED_ROWS_PER_PROVIDER
        assert counts[provider]["truncated"] is True
        # Nothing is disabled here, so the provider's enabled total is its
        # whole row count -- a number the truncated row list cannot state.
        assert counts[provider]["enabled_total"] == _OVERSIZED_ROWS_PER_PROVIDER
        assert counts[provider]["enabled_counted_over"] == _OVERSIZED_ROWS_PER_PROVIDER
        assert (
            counts[provider]["enabled_returned"] == counts[provider]["returned"]
        )
    assert sum(entry["returned"] for entry in counts.values()) == len(
        catalog["workers"]
    )
    assert sum(entry["total"] for entry in counts.values()) == catalog["worker_count"]
    # No ingestion loss to report, so the section is empty rather than padded
    # with zero rows.
    assert catalog["source_ingestion_loss"] == []


def test_explicitly_configured_route_outranks_the_compact_bound(
    monkeypatch, _oversized_model_catalog
):
    # A route the repository owner named in models.json must stay visible, or
    # the only way to switch it back is to hand-edit the settings file.
    last = _OVERSIZED_ROWS_PER_PROVIDER - 1
    pinned_model = f"anthropic-model-{last:02d}"
    unconfigured_neighbour = f"anthropic-model-{last - 1:02d}"
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings,
        "load",
        lambda _root: _fake_model_settings(
            {"anthropic": {"claude_cli": {pinned_model: True}}}
        ),
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]
    models = [row["model"] for row in catalog["workers"]]

    assert pinned_model in models
    # Its unconfigured neighbour is still past this provider's fair share, so
    # the pin -- not a raised bound -- is what carried the configured route.
    assert unconfigured_neighbour not in models
    pinned = next(row for row in catalog["workers"] if row["model"] == pinned_model)
    assert pinned["effective_enabled"] is True


def test_sixty_four_pinned_routes_cannot_starve_a_later_provider(
    monkeypatch, _oversized_model_catalog
):
    # The defect this pins down: explicit routes used to be paid out of the
    # same budget as everything else, in sorted order. Pins sized to exactly
    # fill the compact bound and all sorting before "xai" therefore used to
    # consume the whole budget before the per-provider round-robin ran even
    # once, and the last provider vanished exactly as it did under the
    # original global head slice.
    bound = dashboard_mcp_app.MAX_MODEL_POLICY_CATALOG_ROWS
    half = bound // 2
    pins = {
        "anthropic": {
            "claude_cli": {
                f"anthropic-model-{index:02d}": True for index in range(half)
            }
        },
        "copilot": {
            "vscode_lm": {
                f"copilot-model-{index:02d}": True for index in range(half)
            }
        },
    }
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings,
        "load",
        lambda _root: _fake_model_settings(pins),
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]
    models = {row["model"] for row in catalog["workers"]}

    assert catalog["worker_count"] == 2 * _OVERSIZED_ROWS_PER_PROVIDER + 1
    # Every provider still reaches the Webview, the last-sorting one included.
    assert {row["provider"] for row in catalog["workers"]} == {
        "anthropic",
        "copilot",
        "xai",
    }
    # Every one of the pinned leaves survived; none was dropped silently.
    for provider in ("anthropic", "copilot"):
        assert {
            f"{provider}-model-{index:02d}" for index in range(half)
        } <= models
    # And the explicitly enabled routes are still toggleable rather than
    # merely present.
    for row in catalog["workers"]:
        if row["model"] in {"anthropic-model-00", "copilot-model-00"}:
            assert row["catalog_enabled"] is True
            assert row["effective_enabled"] is True
    xai_routes = [row for row in catalog["workers"] if row["provider"] == "xai"]
    assert [row["model"] for row in xai_routes] == ["grok-4.6"]
    assert xai_routes[0]["effective_enabled"] is True

    # Still bounded. The floor raised the honoured limit by exactly the slots
    # correctness required -- the pins plus one route for the provider the
    # pins left unrepresented -- and never to the whole catalog.
    assert catalog["truncated"] is True
    assert catalog["returned_worker_count"] == 2 * half + 1
    assert catalog["returned_worker_count"] < catalog["worker_count"]
    assert catalog["row_limit"] == dashboard_mcp_app.MAX_MODEL_POLICY_CATALOG_ROWS
    assert catalog["row_limit_honoured"] == 2 * half + 1


def test_provider_counts_state_the_enabled_total_the_bound_cannot_show(
    monkeypatch, _oversized_model_catalog
):
    # Ten anthropic routes are switched off, and the compact bound returns all
    # ten alongside only part of the enabled remainder. An enabled count taken
    # from the returned rows alone is therefore not this provider's enabled
    # total, so the payload has to publish both numbers.
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings,
        "load",
        lambda _root: _fake_model_settings(
            {
                "anthropic": {
                    "claude_cli": {
                        f"anthropic-model-{index:02d}": False
                        for index in range(30, 40)
                    }
                }
            }
        ),
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]
    counts = {entry["provider"]: entry for entry in catalog["provider_counts"]}
    shown = [row for row in catalog["workers"] if row["provider"] == "anthropic"]

    assert counts["anthropic"]["total"] == _OVERSIZED_ROWS_PER_PROVIDER
    assert counts["anthropic"]["truncated"] is True
    assert counts["anthropic"]["enabled_total"] == _OVERSIZED_ROWS_PER_PROVIDER - 10
    assert counts["anthropic"]["enabled_returned"] == sum(
        1 for row in shown if row["effective_enabled"]
    )
    # The distinction is the point: the shown count understates the provider,
    # so a label built from it alone would misstate the provider's truth.
    assert (
        counts["anthropic"]["enabled_returned"]
        < counts["anthropic"]["enabled_total"]
    )
    assert counts["copilot"]["enabled_total"] == _OVERSIZED_ROWS_PER_PROVIDER
    assert counts["xai"]["enabled_total"] == 1
    assert counts["xai"]["enabled_returned"] == 1


def _stub_model_policy_sources(
    monkeypatch,
    *,
    catalog_rows: list[dict[str, Any]],
    opencode_identities: list[str] | None = None,
    observed_models: list[str] | None = None,
) -> None:
    """Point _model_policy_view at in-memory sources instead of the repo."""

    identities = list(opencode_identities or [])
    observed = list(observed_models or [])
    monkeypatch.setattr(
        dashboard_mcp_app.workforce_catalog,
        "load_catalog",
        lambda _root: {"workers": catalog_rows},
    )
    monkeypatch.setattr(
        dashboard_mcp_app.vscode_lm_bridge,
        "bridge_readiness",
        lambda _root, **_kwargs: {
            "observed_models": list(observed),
            "launchable": False,
            "blocker_reason": "",
        },
    )
    monkeypatch.setattr(
        dashboard_mcp_app.workforce_catalog,
        "opencode_identities_from_preflight",
        lambda _preflight: list(identities),
    )


@pytest.mark.parametrize(
    "written_identity, owner_decision, expected_enabled",
    [
        # The identity a Webview toggle writes: the canonical policy owner the
        # row carries, which is also the one the launcher consults. Both
        # answers survive the round trip intact.
        (("opencode", "opencode_cli"), True, True),
        (("opencode", "opencode_cli"), False, False),
        # The same route named under the vendor spelling it was discovered as.
        # The launcher still evaluates the canonical identity, finds no
        # override there and falls back to the OpenCode identity default, which
        # refuses a non-opencode vendor identity. Display must report that same
        # False rather than promise a route the repository will not run.
        (("xai", "opencode_cli"), True, False),
        (("xai", "opencode_cli"), False, False),
    ],
)
def test_explicit_opencode_decision_survives_the_bound_under_either_spelling(
    monkeypatch, written_identity, owner_decision, expected_enabled
):
    # models.json may name an OpenCode decision under the vendor provider the
    # identity was discovered as -- "xai"/"opencode_cli" -- while the row that
    # decision governs carries the canonical policy owner, "opencode". Matched
    # on one spelling only, the pin recognised nothing, and the route somebody
    # explicitly configured was dropped past the bound like any unconfigured
    # leaf, leaving no control to switch it back on.
    #
    # Visibility and enforcement are separate questions, and conflating them is
    # what the previous round got wrong. A decision under EITHER spelling still
    # pins the row, because a named route must stay drawable whatever the bound
    # costs elsewhere. Whether that route is ENABLED is not the pin's business:
    # that answer comes from the launch gate, and display now returns the gate's
    # own conjunction instead of a more generous reading that let a vendor leaf
    # tick a box the launcher would have refused.
    written_provider, written_adapter = written_identity
    catalog_rows = [
        {
            "worker_id": f"anthropic-{index:02d}",
            "provider": "anthropic",
            "adapter_id": "claude_cli",
            "model": f"anthropic-model-{index:02d}",
            "enabled": True,
        }
        for index in range(20)
    ]
    bound = dashboard_mcp_app.MAX_MODEL_POLICY_CATALOG_ROWS
    # Comfortably larger than the bound minus anthropic's fixed 20, so the
    # scenario below stays "well past the compact bound" whatever the bound is
    # configured to, the same way the original fixed 80/64 pairing was.
    opencode_plain_count = bound + 39
    # Zero-padded to a fixed width so the catalog's alphabetic model sort
    # agrees with numeric order all the way past two digits -- otherwise
    # "model-100-free" sorts ahead of "model-99-free" and the boundary below
    # no longer names the row the fair-share cutoff actually excludes.
    identities = [
        f"opencode/model-{index:03d}-free" for index in range(opencode_plain_count)
    ]
    identities.append("xai/grok-4.6")
    _stub_model_policy_sources(
        monkeypatch,
        catalog_rows=catalog_rows,
        opencode_identities=identities,
    )
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings,
        "load",
        lambda _root: _fake_model_settings(
            {written_provider: {written_adapter: {"xai/grok-4.6": owner_decision}}}
        ),
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]
    models = {row["model"] for row in catalog["workers"]}

    # Well past the compact bound: anthropic's fixed 20 plus the OpenCode pool.
    assert catalog["worker_count"] == 20 + opencode_plain_count + 1
    assert catalog["truncated"] is True
    assert catalog["returned_worker_count"] == bound
    # Pinned under either spelling -- a route nobody can draw is a route nobody
    # can switch back on.
    assert "xai/grok-4.6" in models
    # It was the pin that carried it, not a raised bound: the unconfigured
    # OpenCode leaf sorting immediately before it is past this provider's fair
    # share and did not survive.
    assert f"opencode/model-{opencode_plain_count - 1:03d}-free" not in models
    pinned = next(
        row for row in catalog["workers"] if row["model"] == "xai/grok-4.6"
    )
    # Grouping is unchanged. The route still hangs under the OpenCode policy
    # owner while keeping the exact vendor identity it was discovered as.
    assert pinned["provider"] == "opencode"
    assert pinned["adapter"] == "opencode_cli"
    assert pinned["vendor_provider"] == "xai"
    assert {row["provider"] for row in catalog["workers"]} == {
        "anthropic",
        "opencode",
    }
    # Drawn with a live control, and carrying the decision the launcher will
    # apply to this exact route rather than a second, more generous reading of
    # the same file.
    assert pinned["catalog_enabled"] is True
    assert pinned["effective_enabled"] is expected_enabled


def _tail_provider_catalog(anthropic_rows: int) -> list[dict[str, Any]]:
    """Many anthropic routes, then a single xai route as the very last row."""

    rows: list[dict[str, Any]] = [
        {
            "worker_id": f"anthropic-{index:03d}",
            "provider": "anthropic",
            "adapter_id": "claude_cli",
            "model": f"anthropic-model-{index:03d}",
            "enabled": True,
        }
        for index in range(anthropic_rows)
    ]
    rows.append(
        {
            "worker_id": "xai-grok",
            "provider": "xai",
            "adapter_id": "grok_kilo_cli",
            "model": "grok-4.6",
            "enabled": True,
        }
    )
    return rows


def test_provider_past_the_ingestion_cap_still_reaches_the_webview(monkeypatch):
    # The render bound can only be fair if it has seen every provider, so the
    # ingestion cap cannot be a head slice either: a provider whose only row
    # sits at index 600 of a 601-row catalog was deleted before any grouping
    # ran, and no downstream fairness could bring it back.
    _stub_model_policy_sources(
        monkeypatch, catalog_rows=_tail_provider_catalog(600)
    )
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings, "load", lambda _root: _fake_model_settings()
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]

    assert catalog["configured_worker_count"] == 601
    # Ingestion stays bounded by the declared cap -- it is not raised to fit.
    assert (
        catalog["source_rows_ingested"]
        == dashboard_mcp_app.MAX_MODEL_POLICY_SOURCE_ROWS
    )
    # Nothing raised the floor here, so the honoured bound is the declared one.
    # This is the counterpart the raised-floor case is told apart from.
    assert catalog["source_row_limit_honoured"] == catalog["source_row_limit"]
    assert catalog["worker_count"] == catalog["source_rows_ingested"]
    assert {row["provider"] for row in catalog["workers"]} == {"anthropic", "xai"}
    xai_routes = [row for row in catalog["workers"] if row["provider"] == "xai"]
    assert [row["model"] for row in xai_routes] == ["grok-4.6"]
    assert xai_routes[0]["effective_enabled"] is True
    # The loss the cap did cause is published exactly, per provider, so nobody
    # has to infer which provider a shortfall came out of. The provider that
    # lost nothing is absent rather than reported as a zero.
    loss = {entry["provider"]: entry for entry in catalog["source_ingestion_loss"]}
    assert set(loss) == {"anthropic"}
    assert loss["anthropic"] == {
        "provider": "anthropic",
        "total": 600,
        "ingested": 511,
        "dropped": 89,
        # No other source describes these routes, so every refusal is also a
        # row the tree is missing and the two counts agree. They are still
        # separate fields, because the case where they diverge is exactly the
        # one a single number cannot state.
        "absent_routes": 89,
        # The configured catalog is read directly and has no producer above it,
        # so none of its loss can be charged to an upstream cap. The field is
        # still published as a zero rather than omitted: a consumer splitting
        # the two must not have to guess which sources report the split.
        "upstream_absent_routes": 0,
        "vendor_providers": ["anthropic"],
    }
    assert (
        loss["anthropic"]["ingested"] + 1 == catalog["source_rows_ingested"]
    )
    # The count truth beside it states the provider's own size, not the
    # ingestion cap's arithmetic. Taken from the surviving rows this said
    # "511 of 511" for a provider that has 600 routes, and the tree drew a
    # truncated provider as complete.
    counts = {entry["provider"]: entry for entry in catalog["provider_counts"]}
    assert counts["anthropic"]["total"] == 600
    assert counts["anthropic"]["ingested"] == 511
    assert counts["anthropic"]["returned"] < 511
    assert counts["anthropic"]["truncated"] is True
    # And the enabled figure names the population it was actually counted
    # over: the rows that survived ingestion were the only ones ever evaluated
    # against the policy, so it is not a provider-wide claim.
    assert counts["anthropic"]["enabled_counted_over"] == 511
    assert counts["anthropic"]["enabled_total"] == 511
    assert (
        counts["anthropic"]["enabled_counted_over"] < counts["anthropic"]["total"]
    )
    # The provider that lost nothing carries an unqualified total, so the two
    # cases stay distinguishable rather than both reading as partial.
    assert counts["xai"]["total"] == 1
    assert counts["xai"]["enabled_counted_over"] == counts["xai"]["total"]


def test_explicit_route_past_the_ingestion_cap_is_still_ingested(monkeypatch):
    # A named decision must outrank its own position in the catalog file, or
    # the ingestion cap silently reintroduces the defect the render bound was
    # fixed for.
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings,
        "load",
        lambda _root: _fake_model_settings(
            {"anthropic": {"claude_cli": {"anthropic-model-599": True}}}
        ),
    )
    _stub_model_policy_sources(
        monkeypatch, catalog_rows=_tail_provider_catalog(600)
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]
    models = {row["model"] for row in catalog["workers"]}

    assert "anthropic-model-599" in models
    # Its neighbour, one row earlier and unconfigured, never made it through
    # ingestion -- so the pin, not a widened cap, is what carried the route.
    assert "anthropic-model-598" not in models
    assert "xai" in {row["provider"] for row in catalog["workers"]}


def test_raised_ingestion_floor_publishes_the_limit_it_actually_honoured(
    monkeypatch,
):
    # Pins are reserved before the ingestion budget is spent, so 520 explicitly
    # configured routes raise the floor above the declared cap. That is correct
    # -- a named decision has to survive its own catalog position -- but while
    # the honoured figure was discarded, source_rows_ingested simply came back
    # larger than source_row_limit with nothing in the payload to say why, which
    # reads exactly like a bound that failed to hold.
    pins = {
        "anthropic": {
            "claude_cli": {
                f"anthropic-model-{index:03d}": True for index in range(520)
            }
        }
    }
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings,
        "load",
        lambda _root: _fake_model_settings(pins),
    )
    _stub_model_policy_sources(
        monkeypatch, catalog_rows=_tail_provider_catalog(600)
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]

    # The floor is exactly what correctness required: 520 pins plus one row for
    # the provider those pins left unrepresented.
    assert (
        catalog["source_row_limit"]
        == dashboard_mcp_app.MAX_MODEL_POLICY_SOURCE_ROWS
    )
    assert catalog["source_rows_ingested"] == 521
    assert catalog["source_row_limit_honoured"] == 521
    assert catalog["source_row_limit_honoured"] > catalog["source_row_limit"]
    # Raised to that floor and no further, so the payload is still bounded
    # rather than quietly promoted to the whole catalog.
    assert (
        catalog["source_row_limit_honoured"] < catalog["configured_worker_count"]
    )
    # And the raise accounts for itself: the pinned population is what was
    # ingested beyond the cap, and the unpinned tail was still dropped.
    loss = {entry["provider"]: entry for entry in catalog["source_ingestion_loss"]}
    assert loss["anthropic"]["ingested"] == 520
    assert loss["anthropic"]["dropped"] == 80
    assert "xai" in {row["provider"] for row in catalog["workers"]}


def _discovery_identities(opencode_rows: int) -> list[str]:
    """Many opencode identities, then one xai identity as the very last."""

    identities = [
        f"opencode/model-{index:03d}-free" for index in range(opencode_rows)
    ]
    identities.append("xai/grok-4.6")
    return identities


def test_explicit_opencode_identity_past_the_discovery_cap_is_still_ingested(
    monkeypatch,
):
    # The discovery probe was the last head slice left. "opencode models"
    # returns one flat sequence covering every vendor it can reach, so an
    # identity past the cap was deleted before the tree grouped anything, pins
    # were never consulted there at all, and the counts beside it were taken
    # after the slice -- so the payload published the survivors as the source's
    # total and the loss was not merely unfixed but invisible.
    pins = {
        "opencode": {
            "opencode_cli": {
                "opencode/model-599-free": True,
                "xai/grok-4.6": True,
            }
        }
    }
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings,
        "load",
        lambda _root: _fake_model_settings(pins),
    )
    _stub_model_policy_sources(
        monkeypatch,
        catalog_rows=[],
        opencode_identities=_discovery_identities(600),
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]
    models = {row["model"] for row in catalog["workers"]}
    source = catalog["opencode_source"]

    # Exact returned/total/truncated truth about the discovery source itself,
    # published rather than inferred from the identities that became rows.
    assert source["total"] == 601
    assert source["returned"] == dashboard_mcp_app.MAX_MODEL_POLICY_SOURCE_ROWS
    assert source["truncated"] is True
    assert source["row_limit"] == dashboard_mcp_app.MAX_MODEL_POLICY_SOURCE_ROWS
    assert source["row_limit_honoured"] == source["row_limit"]
    # Reported in the canonical key space the tree groups by, so the number is
    # reachable from a family the Webview actually draws.
    assert source["ingestion_loss"] == [
        {
            "provider": "opencode",
            "total": 601,
            "ingested": 512,
            "dropped": 89,
            # Nothing else supplies these identities here, so the probe's
            # refusals and the tree's missing rows are the same 89.
            "absent_routes": 89,
            # And all 89 were refused by this module's bound: the producer
            # delivered everything it had, so none of them may be charged to a
            # cap above it.
            "upstream_absent_routes": 0,
            "vendor_providers": ["opencode"],
        }
    ]
    # The vendor listed last is not the one that was cut: ingestion reserved it
    # a slot before the budget was spent.
    assert "xai/grok-4.6" in models
    # The explicitly configured tail identity outranks its own discovery
    # position, while its unconfigured neighbour one place earlier does not --
    # so it was the pin that carried it, not a widened cap.
    assert "opencode/model-599-free" in models
    assert "opencode/model-598-free" not in models
    # Drawn with a live control rather than merely present.
    grok = next(
        row for row in catalog["workers"] if row["model"] == "xai/grok-4.6"
    )
    assert grok["provider"] == "opencode"
    assert grok["vendor_provider"] == "xai"
    assert grok["catalog_enabled"] is True
    assert grok["effective_enabled"] is True
    # What the discovery cap cost is folded into the provider's own size, so the
    # tree cannot draw a truncated provider as complete.
    counts = {entry["provider"]: entry for entry in catalog["provider_counts"]}
    assert counts["opencode"]["total"] == 601
    assert counts["opencode"]["ingested"] == 512
    assert counts["opencode"]["truncated"] is True
    assert catalog["truncated"] is True


def test_discovery_cap_cannot_delete_a_whole_vendor_without_a_pin(monkeypatch):
    # The provider-fairness half, stated without any explicit decision helping:
    # the vendor sorting last in the discovery sequence still reaches the tree,
    # and the loss is charged to the vendor that actually lost identities.
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings,
        "load",
        lambda _root: _fake_model_settings(),
    )
    _stub_model_policy_sources(
        monkeypatch,
        catalog_rows=[],
        opencode_identities=_discovery_identities(600),
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]
    source = catalog["opencode_source"]

    assert source["total"] == 601
    assert source["returned"] == dashboard_mcp_app.MAX_MODEL_POLICY_SOURCE_ROWS
    # The vendor listed last kept every identity it had: it is absent from the
    # loss, so ingestion spent the shortfall on the crowded vendor instead of
    # deleting the sparse one whole, which is what a head slice did here.
    assert source["ingestion_loss"][0]["vendor_providers"] == ["opencode"]
    assert "xai" not in source["ingestion_loss"][0]["vendor_providers"]
    # The discovery counts describe the identities that became rows, and the
    # source's own total is the separate number beside them -- the two used to
    # be the same figure, which is how the slice stayed invisible.
    assert catalog["opencode_discovered_model_count"] == source["returned"]
    assert catalog["opencode_discovered_model_count"] < source["total"]


def _duplicating_discovery(catalog_models: list[str]) -> list[str]:
    """A discovery sequence whose dropped tail mostly repeats the catalog.

    500 identities the catalog does not carry, then the vendor sorting last,
    then every configured model again, then one genuinely new identity as the
    very last. Under a 512-row bound the refused tail is 89 repeats of rows
    already rendered plus exactly one route the payload really is missing --
    the two cases a raw per-source drop count cannot tell apart.
    """

    identities = [
        f"opencode/model-{index:03d}-free" for index in range(100, 600)
    ]
    identities.append("xai/grok-4.6")
    identities.extend(catalog_models)
    identities.append("opencode/model-900-free")
    return identities


def test_discovery_drops_that_repeat_catalog_rows_do_not_inflate_the_total(
    monkeypatch,
):
    # A provider's size is a count of distinct launch routes. The catalog and
    # the discovery probe describe overlapping populations, so adding each
    # source's raw drop count to the rendered rows counted a route the payload
    # already holds a second time: an identity "opencode models" listed and the
    # bound refused, which is the same route as a configured opencode_cli row
    # sitting right there in the tree. The denominator grew, every shown-of-
    # total label shrank against it, and the provider read as far more
    # truncated than it was.
    catalog_models = [
        f"opencode/model-{index:03d}-free" for index in range(100)
    ]
    catalog_rows = [
        {
            "worker_id": f"opencode-{index:03d}",
            "provider": "opencode",
            "adapter_id": "opencode_cli",
            "model": model,
            "enabled": True,
        }
        for index, model in enumerate(catalog_models)
    ]
    # The vendor sorting last is explicitly configured, so the render bound
    # keeps it too and the fixture states both halves on one payload: the
    # route stays visible and toggleable, and the denominator it is counted
    # against is the deduped one.
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings,
        "load",
        lambda _root: _fake_model_settings(
            {"opencode": {"opencode_cli": {"xai/grok-4.6": True}}}
        ),
    )
    _stub_model_policy_sources(
        monkeypatch,
        catalog_rows=catalog_rows,
        opencode_identities=_duplicating_discovery(catalog_models),
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]
    models = {row["model"] for row in catalog["workers"]}
    source = catalog["opencode_source"]
    counts = {entry["provider"]: entry for entry in catalog["provider_counts"]}

    assert catalog["configured_worker_count"] == 100
    # The discovery source still reports its own ingestion honestly: it refused
    # 90 of the identities it was handed, and that number is about the probe.
    assert source["total"] == 602
    assert source["returned"] == dashboard_mcp_app.MAX_MODEL_POLICY_SOURCE_ROWS
    assert source["truncated"] is True
    assert source["ingestion_loss"] == [
        {
            "provider": "opencode",
            "total": 602,
            "ingested": 512,
            "dropped": 90,
            # The same entry states what those refusals cost the tree, which is
            # the only one of the two counts that belongs beside a deduped
            # provider total: 89 of the 90 repeat rows already rendered from the
            # configured catalog, so exactly one route is absent. Publishing
            # only `dropped` is what let the Webview print "90 not loaded"
            # against a denominator short by one.
            "absent_routes": 1,
            # The producer handed over everything it parsed here, so this
            # module's own bound refused all 90 and no part of the loss belongs
            # to a cap above it.
            "upstream_absent_routes": 0,
            "vendor_providers": ["opencode"],
        }
    ]
    # 601 distinct routes reached the tree: 100 configured rows, 500 discovered
    # identities the catalog did not carry, and the vendor sorting last.
    assert catalog["worker_count"] == 601
    assert "xai/grok-4.6" in models
    # And exactly one of the 90 refusals was a route nothing else supplied, so
    # the provider's size is 602 rather than the 691 a summed drop count gave.
    assert counts["opencode"]["ingested"] == 601
    assert counts["opencode"]["total"] == 602
    assert (
        counts["opencode"]["total"]
        < catalog["worker_count"] + source["ingestion_loss"][0]["dropped"]
    )
    # The absent count and the denominator are one population: what the tree is
    # missing is precisely total minus ingested, and the raw refusal count is
    # not. A displayed "not loaded" clause may only be built from the former.
    assert source["ingestion_loss"][0]["absent_routes"] == (
        counts["opencode"]["total"] - counts["opencode"]["ingested"]
    )
    assert source["ingestion_loss"][0]["dropped"] > (
        source["ingestion_loss"][0]["absent_routes"]
    )
    # Still bounded and still honest about being bounded -- the dedup narrows
    # the denominator, it does not claim the payload is complete.
    assert counts["opencode"]["truncated"] is True
    assert catalog["truncated"] is True


def _observed_copilot_models(count: int) -> list[str]:
    """More Copilot identities than the bridge bound admits, in host order."""

    return [f"copilot-model-{index:03d}" for index in range(count)]


def test_explicit_editor_identity_past_the_bridge_cap_is_still_ingested(
    monkeypatch,
):
    # The editor bridge was the last head slice. A Copilot host reporting more
    # identities than the cap lost whichever it listed last -- including one the
    # owner had explicitly configured, which then had no checkbox left to switch
    # it back -- and the counts beside it were taken after the slice, so the
    # payload published the survivors as the host's total and the loss was not
    # merely unfixed but invisible.
    pins = {"copilot": {"vscode_lm": {"copilot-model-599": True}}}
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings,
        "load",
        lambda _root: _fake_model_settings(pins),
    )
    _stub_model_policy_sources(
        monkeypatch,
        catalog_rows=[],
        observed_models=_observed_copilot_models(600),
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]
    models = {row["model"] for row in catalog["workers"]}
    source = catalog["editor_source"]

    # Exact returned/total/truncated truth about the bridge itself, published
    # rather than inferred from the identities that became rows.
    assert source["total"] == 600
    assert source["returned"] == dashboard_mcp_app.MAX_MODEL_POLICY_SOURCE_ROWS
    assert source["truncated"] is True
    assert source["row_limit"] == dashboard_mcp_app.MAX_MODEL_POLICY_SOURCE_ROWS
    assert source["row_limit_honoured"] == source["row_limit"]
    assert source["ingestion_loss"] == [
        {
            "provider": "copilot",
            "total": 600,
            "ingested": 512,
            "dropped": 88,
            # Only the bridge describes copilot/vscode_lm identities, so its
            # refusals are also the rows the tree is missing.
            "absent_routes": 88,
            # The bridge returns a slice and no total, so no producer-refused
            # identity is recoverable here and none of the 88 may be charged to
            # a cap above this module. The host's own truncation is reported by
            # total_is_lower_bound instead, which is a hedge and not a count.
            "upstream_absent_routes": 0,
            "vendor_providers": ["copilot"],
        }
    ]
    # The explicitly configured tail identity outranks its own position in the
    # host's list, while its unconfigured neighbour one place earlier does not
    # -- so it was the pin that carried it, not a widened cap.
    assert "copilot-model-599" in models
    assert "copilot-model-598" not in models
    # The discovery count describes the identities that became rows; the host's
    # own total is the separate number beside it. The two used to be the same
    # figure, which is how the slice stayed invisible.
    assert catalog["discovered_model_count"] == source["returned"]
    assert catalog["discovered_model_count"] < source["total"]
    # What the bridge cap cost is folded into the provider's own size, so the
    # tree cannot draw a truncated provider as complete.
    counts = {entry["provider"]: entry for entry in catalog["provider_counts"]}
    assert counts["copilot"]["total"] == 600
    assert counts["copilot"]["ingested"] == 512
    assert counts["copilot"]["truncated"] is True
    assert catalog["truncated"] is True


# ---------------------------------------------------------------------------
# Settings display and launch enforcement decide one route the same way
# ---------------------------------------------------------------------------

def _repo_with_opencode_route(tmp_path: Path) -> Path:
    """A real repository carrying one xai route declared under opencode_cli."""

    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    dashboard_mcp_app.workforce_catalog.upsert_worker(
        repo,
        {
            "worker_id": "grok_opencode",
            "provider": "xai",
            "adapter_id": "opencode_cli",
            "model": "xai/grok-4.6",
            "enabled": True,
            "supports": ["code"],
            "max_context_tokens": 128000,
        },
        actor={"role": "manager", "actor_id": "nf865"},
    )
    return repo


_OPENCODE_PREFLIGHT: dict[str, Any] = {
    "providers": [
        {
            "adapter_id": "opencode_cli",
            "installed": True,
            "installed_version": "1.0",
            "status": "ready",
            "launchable": True,
            "access_observed": True,
        }
    ]
}

# The seeded catalog already ships an "xai/grok-4.6" identity under
# grok_kilo_cli, which is a different launch route with the same model string
# and the wrong policy owner for this test. Both sides are selected by
# worker_id so the two halves are compared on one route rather than on
# whichever row happened to sort first.
_OPENCODE_WORKER_ID = "grok_opencode"


def _shown_and_enforced(repo: Path, monkeypatch) -> tuple[dict[str, Any], dict[str, Any]]:
    """The Settings row and the launch catalog row for the same exact route."""

    monkeypatch.setattr(
        dashboard_mcp_app.vscode_lm_bridge,
        "bridge_readiness",
        lambda _root, **_kwargs: {
            "observed_models": [],
            "launchable": False,
            "blocker_reason": "",
        },
    )
    shown = next(
        row
        for row in dashboard_mcp_app._model_policy_view(repo, _OPENCODE_PREFLIGHT)[
            "catalog"
        ]["workers"]
        if row["worker_id"] == _OPENCODE_WORKER_ID
    )
    enforced = next(
        row
        for row in dashboard_mcp_app.workforce_catalog.build_catalog(
            repo,
            cards=[],
            process_rows=[],
            usage_rows=[],
            preflight=_OPENCODE_PREFLIGHT,
        )["workers"]
        if row["worker_id"] == _OPENCODE_WORKER_ID
    )
    # Same launch route on both sides, named the same way.
    assert shown["model"] == enforced["model"] == "xai/grok-4.6"
    assert shown["adapter"] == enforced["effective_adapter_id"] == "opencode_cli"
    return shown, enforced


@pytest.mark.parametrize("owner_decision", [True, False])
def test_checked_settings_route_is_the_route_the_launcher_will_run(
    tmp_path, monkeypatch, owner_decision
):
    # End to end across the two halves that had drifted: what Settings draws
    # and what the launch catalog enforces. The Webview writes a route toggle
    # under the canonical policy owner its row carries, so that is the identity
    # exercised here, and both halves must read the same decision out of it.
    # A checkbox is a promise about what will run; a UI that renders "on" over
    # a launch gate that says "off" breaks that promise more quietly than a
    # missing row does, because nothing about a checked box admits the
    # disagreement.
    repo = _repo_with_opencode_route(tmp_path)
    model_settings = dashboard_mcp_app.model_settings
    model_settings.update(
        repo,
        provider="opencode",
        adapter="opencode_cli",
        model="xai/grok-4.6",
        enabled=owner_decision,
        expected_revision=model_settings.load(repo)["revision"],
    )

    shown, enforced = _shown_and_enforced(repo, monkeypatch)

    # Both halves, and the same answer either way round: a test that only ever
    # asserts "off" cannot tell a route that honours its owner from one that is
    # merely disabled by default. ``policy_enabled`` is the launch catalog's
    # own name for the decision the Settings checkbox draws.
    assert shown["effective_enabled"] is owner_decision
    assert enforced["policy_enabled"] is owner_decision
    assert shown["effective_enabled"] == enforced["policy_enabled"]
    # The catalog row is enabled, so the launcher's combined verdict is the
    # policy decision and nothing else.
    assert enforced["enabled"] is owner_decision
    # And an enabled route is genuinely launch-eligible rather than merely
    # drawn with a tick, which is the property the UI was actually claiming.
    assert enforced["launch_eligible"] is owner_decision
    # Still drawn and still toggleable in both states -- a route nobody can
    # draw is a route nobody can switch back on.
    assert shown["catalog_enabled"] is True
    assert shown["provider"] == "opencode"
    assert shown["vendor_provider"] == "xai"


def test_vendor_keyed_true_is_reported_exactly_as_the_launcher_treats_it(
    tmp_path, monkeypatch
):
    # A decision hand-written under the vendor spelling -- "xai"/"opencode_cli"
    # -- rather than the canonical owner the launcher consults. Display used to
    # let that leaf win on its own and rendered the route enabled, while
    # workforce_catalog still evaluated the canonical "opencode" identity,
    # found no override, fell back to the OpenCode identity default and refused
    # to launch it. The row was drawn checked against a repository that would
    # not run it. Display now returns the launcher's own answer.
    repo = _repo_with_opencode_route(tmp_path)
    model_settings = dashboard_mcp_app.model_settings
    model_settings.update(
        repo,
        provider="xai",
        adapter="opencode_cli",
        model="xai/grok-4.6",
        enabled=True,
        expected_revision=model_settings.load(repo)["revision"],
    )

    shown, enforced = _shown_and_enforced(repo, monkeypatch)

    assert shown["effective_enabled"] == enforced["policy_enabled"]
    assert shown["effective_enabled"] is False
    assert enforced["enabled"] is False
    assert enforced["launch_eligible"] is False
    # The route is still rendered and still toggleable, so the owner can turn
    # it on through the control that writes the identity the launcher reads.
    assert shown["catalog_enabled"] is True


def _opencode_preflight(identities: list[str]) -> dict[str, Any]:
    """A provider-status snapshot shaped exactly as repo_policy publishes one.

    ``provider_observed_models`` is the field the real
    ``opencode_identities_from_preflight`` reads, so a snapshot built this way
    drives the shipped discovery path -- parser included -- rather than a stub
    standing in for it.
    """

    return {
        "providers": [
            {
                "adapter_id": "opencode_cli",
                "installed": True,
                "installed_version": "1.0",
                "status": "ready",
                "launchable": True,
                "access_observed": True,
                "provider_observed_models": list(identities),
            }
        ]
    }


def _declared_route_view(repo: Path, monkeypatch, identities: list[str]):
    """The Settings catalog for a repo whose owner enabled xai/grok-4.6."""

    model_settings = dashboard_mcp_app.model_settings
    model_settings.update(
        repo,
        provider="opencode",
        adapter="opencode_cli",
        model="xai/grok-4.6",
        enabled=True,
        expected_revision=model_settings.load(repo)["revision"],
    )
    # The only stub here is the editor bridge, because a test host has no VS
    # Code window to answer it. The OpenCode discovery path is NOT stubbed:
    # opencode_identities_from_preflight and parse_opencode_models_output both
    # run for real, which is the whole point -- a stub handed this view a list
    # no producer would ever have handed it.
    monkeypatch.setattr(
        dashboard_mcp_app.vscode_lm_bridge,
        "bridge_readiness",
        lambda _root, **_kwargs: {
            "observed_models": [],
            "launchable": False,
            "blocker_reason": "",
        },
    )
    preflight = _opencode_preflight(identities)
    return (
        dashboard_mcp_app._model_policy_view(repo, preflight)["catalog"],
        dashboard_mcp_app.workforce_catalog.opencode_identities_from_preflight(
            preflight
        ),
    )


def test_enabled_route_past_the_upstream_discovery_cut_is_still_toggleable(
    tmp_path, monkeypatch
):
    # The defect this closes is a PRODUCER-side one, so nothing here stubs the
    # producer. A host listing 201 OpenCode identities is handed to the shipped
    # discovery path, and the enabled route sits at position 201 -- past the
    # 64-row cap parse_opencode_models_output applies inside
    # opencode_identities_from_preflight. MAX_MODEL_POLICY_SOURCE_ROWS = 512
    # therefore never binds on this source and has nothing to reserve the pin
    # against, which is exactly why a pin-aware ingestion bound alone did not
    # fix this: the identity was gone before the bound could protect it.
    #
    # The row is drawn either way -- models.json named that exact route, which
    # is evidence in its own right -- but WHICH row it is drawn as is the second
    # half of the fix. The snapshot the producer parsed is reachable from the
    # view, so the identities its cap refused are recoverable: this route was
    # offered by the host and dropped by a bound, not absent from the host, and
    # the payload must not report the two as the same thing.
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    identities = [f"opencode/model-{index:03d}" for index in range(200)]
    identities.append("xai/grok-4.6")

    catalog, discovered = _declared_route_view(repo, monkeypatch, identities)

    # The producer handed this module strictly less than the host listed, and
    # the enabled identity is not in what arrived. Asserted rather than assumed:
    # if an upstream bound is ever raised, this is the line that says so.
    assert len(discovered) < len(identities)
    assert "xai/grok-4.6" not in discovered
    # The upstream cut is now the source's own reported truth. While `total` was
    # taken from the delivered list this read 64 of 64 and `truncated` was
    # structurally False -- a 201-identity host publishing itself as complete,
    # because a 512-row bound cannot bind on a list already cut to 64.
    source = catalog["opencode_source"]
    assert source["delivered"] == len(discovered)
    assert source["total"] == len(identities)
    assert source["upstream_refused"] == len(identities) - len(discovered)
    assert source["truncated"] is True
    # Named identities, not an unqualified floor: this boundary can say exactly
    # what it lost, so it does not fall back to "at least".
    assert source["total_is_lower_bound"] is False

    row = next(
        item for item in catalog["workers"] if item["model"] == "xai/grok-4.6"
        and item["adapter"] == "opencode_cli"
    )
    # Drawn, and drawn as the owner's decision: enabled, and with a live
    # checkbox rather than a disabled one, because a route nobody can toggle is
    # the failure this card is about.
    assert row["effective_enabled"] is True
    assert row["catalog_enabled"] is True
    # Grouped under the OpenCode policy owner, which is what the tree draws it
    # under and what makes the row reachable at all.
    assert row["provider"] == "opencode"
    assert row["model"] == "xai/grok-4.6"
    # And it is labelled for what it is. The host DID list this identity -- it
    # is in provider_observed_models -- so calling the row `declared_only`
    # asserted that no source offered a route the snapshot plainly carries. It
    # is an inventory-only row a bound refused, which is the same class as any
    # other truncated-source row and is counted as one.
    assert row["inventory_only"] is True
    assert row["source_truncated"] is True
    assert row["discovered_from_opencode"] is True
    assert row.get("declared_only") is not True
    assert catalog["declared_only_model_count"] == 0
    assert catalog["source_truncated_model_count"] == 1
    # The provider's size is stated over the host's population and not over the
    # parser's. 64 of 64 was the understatement; 201 is what the host offered.
    counts = {entry["provider"]: entry for entry in catalog["provider_counts"]}
    assert counts["opencode"]["total"] == len(identities)
    assert counts["opencode"]["truncated"] is True
    assert counts["opencode"]["total_is_lower_bound"] is False


def test_producer_refused_opencode_routes_are_not_charged_to_this_bound(
    tmp_path, monkeypatch
):
    # The same 201-identity host as above, read for a different fact: WHICH
    # bound the missing rows are charged to. Every one of them was cut by the
    # producer's own 64-identity ceiling before this module was offered
    # anything, and MAX_MODEL_POLICY_SOURCE_ROWS = 512 cannot bind on a list of
    # 64 -- so this ingestion refused nothing at all. Published as a single
    # `absent_routes` figure, the Webview named this module's discovery bound
    # as the cause, and an owner raising that limit would have recovered none
    # of the rows. The producer's share is marked instead, as a strict subset
    # of the same deduped population and never as a second count beside it.
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    identities = [f"opencode/model-{index:03d}" for index in range(200)]
    identities.append("xai/grok-4.6")

    catalog, discovered = _declared_route_view(repo, monkeypatch, identities)

    source = catalog["opencode_source"]
    loss = {item["provider"]: item for item in source["ingestion_loss"]}
    entry = loss["opencode"]

    # Rows really are missing, so the split below is not vacuously true.
    assert entry["absent_routes"] > 0
    # And all of them are the producer's. The subtraction the tree does to
    # name its own bound therefore yields zero, not the whole shortfall.
    assert entry["upstream_absent_routes"] == entry["absent_routes"]
    assert entry["absent_routes"] - entry["upstream_absent_routes"] == 0
    # A subset, never an addend: adding the two would restate the same double
    # count that the deduped `absent_routes` exists to prevent.
    assert entry["upstream_absent_routes"] <= entry["dropped"]
    # The route the declaration brought back is excluded from both, because a
    # row that is drawn is not a row anybody is missing.
    assert "xai/grok-4.6" not in discovered
    assert "xai/grok-4.6" in {row["model"] for row in catalog["workers"]}
    # The configured catalog is read directly and has no producer above it, so
    # its own loss entries can never claim an upstream share.
    assert all(
        item["upstream_absent_routes"] == 0
        for item in catalog["source_ingestion_loss"]
    )


def test_declared_route_the_producer_did_supply_is_reported_as_discovered(
    tmp_path, monkeypatch
):
    # The control for the test above. With the same enabled route inside what
    # the producer actually returns, nothing is materialised: the row comes from
    # discovery, is labelled as discovered, and declared_only_model_count stays
    # zero. Without this, a materialisation that fired unconditionally would
    # duplicate every discovered route and inflate the counts the tree divides
    # by, and the test above could not tell the two apart.
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)

    catalog, discovered = _declared_route_view(
        repo, monkeypatch, ["xai/grok-4.6", "opencode/model-000"]
    )

    assert "xai/grok-4.6" in discovered
    rows = [
        item for item in catalog["workers"] if item["model"] == "xai/grok-4.6"
        and item["adapter"] == "opencode_cli"
    ]
    # Exactly one row for one launch route: the decision pinned the discovered
    # row rather than adding a second one beside it.
    assert len(rows) == 1
    assert rows[0]["effective_enabled"] is True
    assert rows[0]["discovered_from_opencode"] is True
    assert rows[0].get("declared_only") is not True
    assert catalog["declared_only_model_count"] == 0


def _mixed_adapter_vendor_catalog(opencode_rows: int) -> list[dict[str, Any]]:
    """One vendor spelling, two adapters, therefore two rendered providers.

    ``policy_route_identity`` sends every ``opencode_cli`` route to the
    canonical ``opencode`` owner and leaves ``grok_kilo_cli`` under ``xai``, so
    a single "xai" vendor group spans two of the providers the tree draws.
    """

    rows: list[dict[str, Any]] = [
        {
            "worker_id": f"xai-opencode-{index:03d}",
            "provider": "xai",
            "adapter_id": "opencode_cli",
            "model": f"xai/model-{index:03d}",
            "enabled": True,
        }
        for index in range(opencode_rows)
    ]
    rows.append(
        {
            "worker_id": "xai-grok",
            "provider": "xai",
            "adapter_id": "grok_kilo_cli",
            "model": "grok-4.6",
            "enabled": True,
        }
    )
    return rows


def test_one_vendor_across_two_adapters_keeps_both_rendered_providers(
    monkeypatch,
):
    # Ingestion fairness was allocated per vendor spelling while the rendered
    # rows, provider_counts and the ingestion loss all speak the canonical
    # provider. Those are not one partition nested inside the other: "xai" is a
    # single vendor group and two rendered providers -- opencode through
    # opencode_cli, xai through grok_kilo_cli. The vendor floor was therefore
    # satisfied by one opencode row, the round-robin spent the remaining 511
    # inside that same group, and the whole rendered "xai" provider was ingested
    # away. Its loss entry was published in the canonical key space, naming a
    # family the tree then had no row to draw -- reported and unreachable at
    # once, which is the exact failure the canonical keying was meant to end.
    _stub_model_policy_sources(
        monkeypatch, catalog_rows=_mixed_adapter_vendor_catalog(512)
    )
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings, "load", lambda _root: _fake_model_settings()
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]

    assert catalog["configured_worker_count"] == 513
    # Still bounded by the declared ingestion cap, not widened to fit.
    assert (
        catalog["source_rows_ingested"]
        == dashboard_mcp_app.MAX_MODEL_POLICY_SOURCE_ROWS
    )
    assert catalog["source_row_limit_honoured"] == catalog["source_row_limit"]
    # Both rendered providers survive a bound spent between vendor spellings.
    assert {row["provider"] for row in catalog["workers"]} == {"opencode", "xai"}
    xai_routes = [row for row in catalog["workers"] if row["provider"] == "xai"]
    assert [row["model"] for row in xai_routes] == ["grok-4.6"]
    assert xai_routes[0]["adapter"] == "grok_kilo_cli"
    assert xai_routes[0]["vendor_provider"] == "xai"
    # Visible and toggleable, which is the pair of claims this card is about.
    assert xai_routes[0]["catalog_enabled"] is True
    assert xai_routes[0]["effective_enabled"] is True

    # Every loss entry names a provider the tree actually draws, so a published
    # shortfall is a reachable one rather than a number filed under a family
    # that no longer has a row.
    drawn = {entry["provider"] for entry in catalog["provider_counts"]}
    loss = {entry["provider"]: entry for entry in catalog["source_ingestion_loss"]}
    assert set(loss) <= drawn
    assert set(loss) == {"opencode"}
    # And it names the vendor spelling underneath, so canonical keying never
    # hides that it was the xai half of OpenCode that lost rows.
    assert loss["opencode"]["vendor_providers"] == ["xai"]
    assert loss["opencode"]["total"] == 512
    assert loss["opencode"]["dropped"] == 512 - loss["opencode"]["ingested"]


def test_declared_leaves_cannot_lift_a_row_bound_past_its_hard_ceiling(
    monkeypatch,
):
    # Every leaf in models.json becomes a pin and every pin reserves a row, so
    # the floor both bounds are raised to came out of a file this module does
    # not control and nothing bounded. One declared leaf per catalog row
    # therefore lifted effective_limit past 64 and past 512 to the whole
    # catalog: a payload that had stopped being bounded while still publishing a
    # row_limit beside itself. The ceiling is the number that floor may not
    # pass, and what it refuses is counted rather than quietly absent.
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings,
        "load",
        lambda _root: _fake_model_settings(
            {
                "anthropic": {
                    "claude_cli": {
                        f"anthropic-model-{index:03d}": True
                        for index in range(1400)
                    }
                }
            }
        ),
    )
    _stub_model_policy_sources(
        monkeypatch, catalog_rows=_tail_provider_catalog(1400)
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]

    # The read of models.json is itself bounded, and reports the file's own leaf
    # count rather than only what it managed to read -- a decision that was
    # never read is otherwise indistinguishable from one that was refused.
    assert catalog["declared_leaf_count"] == 1400
    assert (
        catalog["declared_leaf_limit"]
        == dashboard_mcp_app.MAX_MODEL_POLICY_DECLARED_LEAVES
    )
    assert catalog["declared_leaves_truncated"] is True

    # Neither bound reached the catalog; both stop at their declared ceiling.
    assert (
        catalog["source_row_limit_honoured"]
        <= dashboard_mcp_app.MAX_MODEL_POLICY_SOURCE_ROW_CEILING
    )
    assert catalog["source_rows_ingested"] < catalog["configured_worker_count"]
    assert (
        catalog["row_limit_honoured"]
        == dashboard_mcp_app.MAX_MODEL_POLICY_CATALOG_ROW_CEILING
    )
    assert catalog["row_limit_honoured"] > catalog["row_limit"]
    assert (
        catalog["returned_worker_count"]
        == dashboard_mcp_app.MAX_MODEL_POLICY_CATALOG_ROW_CEILING
    )
    assert catalog["returned_worker_count"] < catalog["worker_count"]
    assert catalog["truncated"] is True

    # The pins the ceiling refused are stated. Silently short is the one outcome
    # a bounded payload may not have here: a configured route with no row has no
    # control, and nothing else in the payload would say so.
    assert catalog["pinned_routes_refused"] > 0
    assert catalog["source_pinned_routes_refused"] >= 0

    # Representation still outranks the pins, so the provider sorting last is
    # drawn even when every remaining slot was already spoken for.
    assert {row["provider"] for row in catalog["workers"]} == {
        "anthropic",
        "xai",
    }
    xai_routes = [row for row in catalog["workers"] if row["provider"] == "xai"]
    assert [row["model"] for row in xai_routes] == ["grok-4.6"]
    assert xai_routes[0]["effective_enabled"] is True


def test_a_declaration_within_the_ceiling_still_raises_the_floor_untouched(
    monkeypatch,
):
    # The control for the test above. The ceiling must bind only when the floor
    # actually exceeds it; a fixed 256-row payload would be the same defect with
    # the opposite sign, refusing pins that fit perfectly well. Pin count is
    # sized relative to the compact bound so the floor keeps exceeding it (and
    # stays under the ceiling) whatever the bound is configured to.
    pin_count = dashboard_mcp_app.MAX_MODEL_POLICY_CATALOG_ROWS + 20
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings,
        "load",
        lambda _root: _fake_model_settings(
            {
                "anthropic": {
                    "claude_cli": {
                        f"anthropic-model-{index:03d}": True
                        for index in range(pin_count)
                    }
                }
            }
        ),
    )
    _stub_model_policy_sources(
        monkeypatch, catalog_rows=_tail_provider_catalog(600)
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]

    assert catalog["declared_leaves_truncated"] is False
    assert catalog["declared_leaf_count"] == pin_count
    # The pins plus one row for the provider they left unrepresented.
    assert catalog["row_limit_honoured"] == pin_count + 1
    assert (
        catalog["row_limit_honoured"]
        < dashboard_mcp_app.MAX_MODEL_POLICY_CATALOG_ROW_CEILING
    )
    assert catalog["pinned_routes_refused"] == 0
    assert catalog["source_pinned_routes_refused"] == 0
    assert "xai" in {row["provider"] for row in catalog["workers"]}


def _ceiling_refused_source_catalog() -> list[dict[str, Any]]:
    """1024 declared anthropic rows plus one xai row: the ceiling refuses one.

    Every anthropic row is declared in models.json, so every one is a pin the
    ingestion bound reserves, and the trailing xai row's representation pushes
    the floor to exactly one past MAX_MODEL_POLICY_SOURCE_ROW_CEILING. The
    ceiling admits representation first and then the reserved rows in catalog
    order, so the refused row is the declared anthropic row sitting last.

    That row is given the model name that sorts *first* on purpose. Ingestion
    refuses by catalog position while the render bound admits by sorted
    position, so the same route can be refused by one bound and still be the
    representative the other one draws -- which is what lets this fixture assert
    on the rebuilt row itself rather than only on a count.
    """

    rows: list[dict[str, Any]] = [
        {
            "worker_id": f"anthropic-{index:04d}",
            "provider": "anthropic",
            "adapter_id": "claude_cli",
            "model": f"anthropic-model-{index:04d}",
            "enabled": True,
        }
        for index in range(1, 1024)
    ]
    # Last in catalog order, first in sort order, and switched off in the
    # catalog -- the exact value a declaration-built rebuild overwrote with a
    # hardcoded True.
    rows.append(
        {
            "worker_id": "anthropic-refused",
            "provider": "anthropic",
            "adapter_id": "claude_cli",
            "model": "anthropic-model-0000",
            "enabled": False,
        }
    )
    rows.append(
        {
            "worker_id": "xai-grok",
            "provider": "xai",
            "adapter_id": "grok_kilo_cli",
            "model": "grok-4.6",
            "enabled": True,
        }
    )
    return rows


def test_ceiling_refused_configured_row_keeps_its_own_identity_and_enabled(
    monkeypatch,
):
    # A route the ingestion ceiling refused is still a route the catalog
    # supplied. Rebuilt from the declaration alone it came back with a blank
    # worker_id, catalog_enabled hardcoded True and declared_only set -- so the
    # Webview said discovery never offered a row the catalog had listed, and the
    # "enabled": False the catalog stated about it was overwritten by the
    # rebuild that was meant to rescue it. A row drawn toggleable against a
    # catalog entry that is switched off is the same broken promise as a checked
    # box over a launcher that refuses: the payload has the evidence and states
    # the opposite.
    #
    # Only genuinely declared-only routes -- named by the owner and supplied by
    # nothing -- may claim that label, so both counts are asserted, not just the
    # row.
    declared_models = {"anthropic-model-0000": True}
    declared_models.update(
        {f"anthropic-model-{index:04d}": True for index in range(1, 1024)}
    )
    monkeypatch.setattr(
        dashboard_mcp_app.model_settings,
        "load",
        lambda _root: _fake_model_settings(
            {"anthropic": {"claude_cli": declared_models}}
        ),
    )
    _stub_model_policy_sources(
        monkeypatch, catalog_rows=_ceiling_refused_source_catalog()
    )

    catalog = dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]

    # The premise, asserted rather than assumed: the ingestion ceiling really
    # bound, and it really refused a pin. Without this the row below could be
    # passing for the ordinary reason that nothing was ever dropped.
    assert catalog["declared_leaves_truncated"] is False
    assert (
        catalog["source_rows_ingested"]
        == dashboard_mcp_app.MAX_MODEL_POLICY_SOURCE_ROW_CEILING
    )
    assert catalog["source_pinned_routes_refused"] == 1

    row = next(
        item
        for item in catalog["workers"]
        if item["model"] == "anthropic-model-0000"
    )
    # The source row's own identity, carried past the bound that refused it.
    assert row["worker_id"] == "anthropic-refused"
    # And the source row's own enabled truth. The catalog switched this route
    # off; a rebuild may report that, never overwrite it.
    assert row["catalog_enabled"] is False
    assert row["effective_enabled"] is False
    # It is a configured route, so it claims neither of the two labels that
    # would deny the source which supplied it.
    assert row.get("declared_only") is not True
    assert row["inventory_only"] is False
    # What it does claim is the truth the reader needs: present, and present
    # only because the declaration reserved it past a source that came up short.
    assert row["source_truncated"] is True
    assert catalog["source_truncated_model_count"] == 1
    # No declared-only claim anywhere in the payload: every leaf here names a
    # route the catalog supplied.
    assert catalog["declared_only_model_count"] == 0
    assert not [
        item for item in catalog["workers"] if item.get("declared_only")
    ]


def test_opencode_below_its_producer_ceiling_claims_no_upstream_loss(
    tmp_path, monkeypatch
):
    # The control for the 201-identity case above, and the reason the recovery
    # is gated on the producer's ceiling rather than run unconditionally. With
    # 63 identities the 64-row cap never engaged, so the parser examined every
    # candidate and anything it left out was rejected on identity grounds --
    # not refused by a bound. Reporting those as lost routes would invent a
    # shortfall, inflate the provider's size and print a "not loaded" clause
    # for rows nothing ever lost, which is the same class of wrong number this
    # card exists to remove, only in the opposite direction.
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    identities = [f"opencode/model-{index:03d}" for index in range(63)]

    catalog, discovered = _declared_route_view(repo, monkeypatch, identities)

    # The premise: the producer's cap did not bind, so everything arrived.
    assert len(discovered) == len(identities)
    assert len(identities) < dashboard_mcp_app.OPENCODE_UPSTREAM_IDENTITY_CEILING
    source = catalog["opencode_source"]
    assert source["total"] == len(identities)
    assert source["delivered"] == len(identities)
    assert source["upstream_refused"] == 0
    assert source["truncated"] is False
    assert source["total_is_lower_bound"] is False
    # And the declared route the host genuinely never listed keeps the label it
    # has earned. "No source offered this" is a true claim here, and withholding
    # it everywhere would trade one unsupported statement for another.
    row = next(
        item for item in catalog["workers"] if item["model"] == "xai/grok-4.6"
        and item["adapter"] == "opencode_cli"
    )
    assert row["declared_only"] is True
    assert row.get("source_truncated") is not True
    assert catalog["declared_only_model_count"] == 1
    assert catalog["upstream_truncated_model_count"] == 0


def _declared_copilot_route_view(monkeypatch, observed: list[str]):
    """Settings for an owner who enabled a Copilot route the bridge cut.

    ``bridge_readiness`` head-slices at 128 and publishes no total beside the
    slice, so a host offering more is indistinguishable here from one offering
    exactly 128. The stub returns the slice, which is all the real bridge ever
    hands over.
    """

    monkeypatch.setattr(
        dashboard_mcp_app.model_settings,
        "load",
        lambda _root: _fake_model_settings(
            {"copilot": {"vscode_lm": {"copilot-model-999": True}}}
        ),
    )
    _stub_model_policy_sources(
        monkeypatch, catalog_rows=[], observed_models=observed
    )
    return dashboard_mcp_app._model_policy_view(Path("/repo"))["catalog"]


def test_editor_host_at_the_bridge_ceiling_reports_a_floor_not_a_total(
    monkeypatch,
):
    # The second ceiling, and the one whose input cannot be recovered. The
    # bridge cuts at 128 and reports nothing, so MAX_MODEL_POLICY_SOURCE_ROWS =
    # 512 cannot bind on this source either: `truncated` was structurally False
    # and 128 was published as the host's catalog size. The difference from the
    # OpenCode boundary is what can honestly be said about it -- the identities
    # past the cut are unreachable, so the total is a floor and the payload says
    # so instead of claiming a count it never measured.
    observed = _observed_copilot_models(
        dashboard_mcp_app.EDITOR_UPSTREAM_MODEL_CEILING
    )

    catalog = _declared_copilot_route_view(monkeypatch, observed)

    source = catalog["editor_source"]
    assert source["delivered"] == dashboard_mcp_app.EDITOR_UPSTREAM_MODEL_CEILING
    assert source["total_is_lower_bound"] is True
    assert source["truncated"] is True
    # The remainder is unknown, so none is invented: a floor is published, never
    # a guessed count added to the provider's size.
    assert source["upstream_refused"] == 0
    counts = {entry["provider"]: entry for entry in catalog["provider_counts"]}
    assert counts["copilot"]["total_is_lower_bound"] is True
    assert counts["copilot"]["truncated"] is True

    row = next(
        item for item in catalog["workers"]
        if item["model"] == "copilot-model-999"
    )
    # Visible and toggleable, which is the pair of claims this card is about.
    assert row["effective_enabled"] is True
    assert row["catalog_enabled"] is True
    # And labelled for what the evidence supports. The row is absent from a list
    # that was itself cut, so "no host offers this route" is not a fact in
    # evidence -- calling it declared_only asserted a measurement of a
    # population nobody here can see.
    assert row.get("declared_only") is not True
    assert row["upstream_truncated"] is True
    assert row["source_truncated"] is True
    assert catalog["declared_only_model_count"] == 0
    assert catalog["upstream_truncated_model_count"] == 1
    assert catalog["truncated"] is True


def test_editor_host_below_the_bridge_ceiling_reports_an_exact_total(
    monkeypatch,
):
    # The control. One identity short of the bridge's slice means the bridge
    # returned everything the host had, so the total is a measurement and the
    # declared route really is one no source offered. Without this the fix could
    # pass by marking every copilot provider a floor and every declared route
    # unknowable, which states nothing and is never wrong.
    observed = _observed_copilot_models(
        dashboard_mcp_app.EDITOR_UPSTREAM_MODEL_CEILING - 1
    )

    catalog = _declared_copilot_route_view(monkeypatch, observed)

    source = catalog["editor_source"]
    assert source["total"] == len(observed)
    assert source["total_is_lower_bound"] is False
    assert source["truncated"] is False
    counts = {entry["provider"]: entry for entry in catalog["provider_counts"]}
    assert counts["copilot"]["total_is_lower_bound"] is False

    row = next(
        item for item in catalog["workers"]
        if item["model"] == "copilot-model-999"
    )
    assert row["effective_enabled"] is True
    assert row["declared_only"] is True
    assert row.get("upstream_truncated") is not True
    assert catalog["declared_only_model_count"] == 1
    assert catalog["upstream_truncated_model_count"] == 0


def test_ceiling_represents_each_group_with_a_reserved_row_it_already_holds():
    # Three groups of three rows, a pin inside every one of them, and a ceiling
    # one row below the floor those pins raise. Representation has to be paid
    # for -- a provider with no row at all is the one failure the Webview offers
    # no remedy for -- but it does not have to be paid for twice. Rebuilding the
    # admission order from an empty ``seen`` took each group's FIRST row, which
    # is not pinned, and only then offered the pins three slots that were
    # already gone: every explicit decision refused, and three rows nobody asked
    # for drawn in their place. A group is represented by a pin it already
    # contains, so one slot does both jobs.
    group_keys = ["a", "a", "a", "b", "b", "b", "c", "c", "c"]
    reserved = {1, 2, 4, 7}

    selected, honoured, refused = dashboard_mcp_app._bounded_provider_selection(
        group_keys,
        limit=1,
        ceiling=3,
        reserved=reserved,
    )

    # The premise, asserted rather than assumed: every group holds a pin, so the
    # floor is exactly the four reserved rows and it really does exceed the
    # ceiling. Without this the branch under test never runs and the rest passes
    # for the ordinary reason that nothing was ever refused.
    assert len(reserved) > 3
    assert honoured == 3
    assert len(selected) == 3

    # Representation still outranks the decisions: every group keeps a row.
    assert {group_keys[index] for index in selected} == {"a", "b", "c"}
    # And not one of the three slots went to a row nobody pinned. This is the
    # finding itself -- the admitted rows are three of the four decisions rather
    # than three unpinned first rows that displaced all four.
    assert selected <= reserved
    # So the refusal is the arithmetic minimum. Four decisions cannot fit under
    # a ceiling of three; exactly one is refused, not all four.
    assert refused == len(reserved) - 3
    assert refused == 1


def test_capped_producer_past_the_recovery_window_reports_a_floor(
    tmp_path, monkeypatch
):
    # The 201-identity case above recovers the producer's entire input, so its
    # total is a measurement and ``total_is_lower_bound`` is False -- that test
    # is this one's control. The recovery is itself bounded, though:
    # _opencode_offered_identities reads the snapshot only as far as
    # MAX_MODEL_POLICY_SOURCE_ROW_CEILING. A host listing more than that trips
    # the producer's 64-row cap AND leaves a tail past the recovery window, so
    # the identities named here are a subset of what the host offered and a
    # total counted over them is a floor. Detecting the producer's cap and then
    # publishing an exact count over a population nobody here has seen is the
    # same understatement the detection exists to remove, one layer in.
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    window = dashboard_mcp_app.MAX_MODEL_POLICY_SOURCE_ROW_CEILING
    identities = [
        f"opencode/model-{index:04d}" for index in range(window + 64)
    ]
    # The enabled route sits past the producer's cap but inside the window the
    # recovery can still read, so it arrives as a named refused identity rather
    # than as part of the unseen tail. The tail is what makes the total a floor;
    # this row is what shows the floor cost the owner no control.
    identities[100] = "xai/grok-4.6"

    catalog, discovered = _declared_route_view(repo, monkeypatch, identities)

    # The premises: the producer's cap really bound, and the host really listed
    # more identities than the recovery window can reach.
    assert (
        len(discovered)
        >= dashboard_mcp_app.OPENCODE_UPSTREAM_IDENTITY_CEILING
    )
    assert len(identities) > window

    source = catalog["opencode_source"]
    # Everything the window could reach, and strictly less than the host has.
    assert source["total"] == window
    assert source["total"] < len(identities)
    assert source["truncated"] is True
    # Therefore a floor. While this read False the payload stated an exact 1024
    # about a host that had offered more, which is not a bounded answer but a
    # wrong one -- the same defect the producer-cap detection had just fixed one
    # boundary over.
    assert source["total_is_lower_bound"] is True

    # And the flag reaches the numbers the tree actually divides by. Published
    # on the source alone, a reader looking at the provider row still saw a
    # count presented as exact.
    counts = {entry["provider"]: entry for entry in catalog["provider_counts"]}
    assert counts["opencode"]["total_is_lower_bound"] is True
    assert counts["opencode"]["truncated"] is True
    assert counts["opencode"]["total"] < len(identities)

    # Visible and toggleable, which is the pair of claims this card is about: a
    # floor is a statement about a count, never a reason to drop a row.
    row = next(
        item for item in catalog["workers"] if item["model"] == "xai/grok-4.6"
        and item["adapter"] == "opencode_cli"
    )
    assert row["provider"] == "opencode"
    assert row["effective_enabled"] is True
    assert row["catalog_enabled"] is True
    # Labelled for what it is: a route a bound refused, not one no host offers.
    assert row["source_truncated"] is True
    assert row.get("declared_only") is not True


def test_opencode_snapshot_at_the_parser_cap_reports_a_floor(
    tmp_path, monkeypatch
):
    # The cap this boundary compares against has to be one that can actually
    # bind. repo_policy publishes provider_observed_models as
    # parse_opencode_models_output's 64-row read of ``opencode models``, so the
    # recovered snapshot can never reach MAX_MODEL_POLICY_SOURCE_ROW_CEILING:
    # compared to that 1024 the floor was unreachable on any real host, and a
    # snapshot sitting exactly at its producer's cap published its own length as
    # the host's exact total -- the same understatement the recovery was added
    # to remove, one layer in and invisible to the recovery-window case above,
    # which only reaches the floor through a snapshot no producer would write.
    #
    # Every offered identity parsed here, so the recovery can name nothing that
    # was lost and the only honest statement about the size is the weaker one.
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    cap = dashboard_mcp_app.OPENCODE_UPSTREAM_IDENTITY_CEILING
    identities = [f"opencode/model-{index:03d}" for index in range(cap)]
    # The owner's enabled route is inside the snapshot, so this test states the
    # count truth alone and never depends on how an absent route is labelled.
    identities[30] = "xai/grok-4.6"

    catalog, discovered = _declared_route_view(repo, monkeypatch, identities)

    # The premises: the producer's cap bound exactly, the snapshot length is one
    # a real producer writes, and it is nowhere near the ceiling the floor used
    # to be tested against -- which is why that test could never fire here.
    assert len(discovered) == cap
    assert cap in dashboard_mcp_app.OPENCODE_UPSTREAM_OFFERED_CEILINGS
    assert cap < dashboard_mcp_app.MAX_MODEL_POLICY_SOURCE_ROW_CEILING

    source = catalog["opencode_source"]
    assert source["total"] == cap
    assert source["delivered"] == cap
    # Nothing past the cap was recoverable, so no shortfall is invented ...
    assert source["upstream_refused"] == 0
    # ... and the size is published as a floor rather than as the host's count.
    assert source["total_is_lower_bound"] is True
    # A floor says the population may be larger than the total beside it, so the
    # source is not whole even though this module's own bound refused nothing.
    assert source["returned"] == source["total"]
    assert source["truncated"] is True
    assert catalog["truncated"] is True

    counts = {entry["provider"]: entry for entry in catalog["provider_counts"]}
    assert counts["opencode"]["total_is_lower_bound"] is True
    assert counts["opencode"]["truncated"] is True

    # And the enabled route stays visible and toggleable. A floor is a statement
    # about a count and never a reason to drop or disable a row.
    row = next(
        item for item in catalog["workers"] if item["model"] == "xai/grok-4.6"
        and item["adapter"] == "opencode_cli"
    )
    assert row["provider"] == "opencode"
    assert row["catalog_enabled"] is True
    assert row["effective_enabled"] is True
    assert row["discovered_from_opencode"] is True


def test_capped_opencode_producer_leaves_an_absent_declared_route_unknowable(
    tmp_path, monkeypatch
):
    # The OpenCode twin of the editor-ceiling case, and the one this view kept
    # getting wrong: the parser stops at its 64-row cap, the snapshot it read
    # stops there too, so nothing past the cap is recoverable and the total is a
    # floor. The owner's configured xai/grok-4.6 is absent from every list that
    # arrived -- and absence from a list that was itself cut is not evidence
    # that no host offers the route.
    #
    # Only vscode_lm was treated as unknowable here, so this row fell through to
    # declared_only and the tree printed "not offered by discovery" over a
    # measurement the OpenCode producer was never in a position to make. The
    # control for it is the 63-identity test above, where the cap did not bind
    # and declared_only is the true label.
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    cap = dashboard_mcp_app.OPENCODE_UPSTREAM_IDENTITY_CEILING
    identities = [f"opencode/model-{index:03d}" for index in range(cap)]

    catalog, discovered = _declared_route_view(repo, monkeypatch, identities)

    # The premises, stated rather than assumed: the producer's cap bound
    # exactly, and the configured route really is absent from what it delivered.
    assert len(discovered) == cap
    assert "xai/grok-4.6" not in identities
    source = catalog["opencode_source"]
    assert source["total"] == cap
    assert source["upstream_refused"] == 0
    # The uncertainty the label has to be read from, unchanged by this fix.
    assert source["total_is_lower_bound"] is True
    counts = {entry["provider"]: entry for entry in catalog["provider_counts"]}
    assert counts["opencode"]["total_is_lower_bound"] is True

    row = next(
        item for item in catalog["workers"] if item["model"] == "xai/grok-4.6"
        and item["adapter"] == "opencode_cli"
    )
    # Visible and toggleable, which the relabelling may not cost.
    assert row["provider"] == "opencode"
    assert row["catalog_enabled"] is True
    assert row["effective_enabled"] is True
    # And labelled for what the evidence supports and no more: declared_only
    # would assert no host offers this route, a discovered_from_* flag would
    # assert the opposite, and neither was measured on this host.
    assert row.get("declared_only") is not True
    assert row.get("discovered_from_opencode") is not True
    assert row["upstream_truncated"] is True
    assert row["source_truncated"] is True
    # So the payload the Webview reads carries no declared-only claim at all,
    # which is what keeps "not offered by discovery" off this row.
    assert catalog["declared_only_model_count"] == 0
    assert not [
        item for item in catalog["workers"] if item.get("declared_only")
    ]
    assert catalog["upstream_truncated_model_count"] == 1


def test_opencode_snapshot_at_the_producer_slice_reports_a_floor(
    tmp_path, monkeypatch
):
    # The other real producer cap, on the path where the snapshot carries the
    # host's own list: repo_policy slices provider_observed_models at 128. The
    # parser then cuts that to 64, so the identities between the two caps are
    # recoverable and exact while anything past 128 is unseen -- and both facts
    # are published at once. The recovery is not weakened by the floor, and the
    # floor is not quietly filled in by the recovery.
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    parser_cap = dashboard_mcp_app.OPENCODE_UPSTREAM_IDENTITY_CEILING
    slice_cap = 128
    identities = [f"opencode/model-{index:03d}" for index in range(slice_cap)]
    # Past the parser's cap and inside the slice, so it arrives as a named
    # refused identity rather than as part of the tail nobody here can see.
    identities[100] = "xai/grok-4.6"

    catalog, discovered = _declared_route_view(repo, monkeypatch, identities)

    # The premises, again stated rather than assumed.
    assert len(discovered) == parser_cap
    assert slice_cap in dashboard_mcp_app.OPENCODE_UPSTREAM_OFFERED_CEILINGS
    assert slice_cap < dashboard_mcp_app.MAX_MODEL_POLICY_SOURCE_ROW_CEILING

    source = catalog["opencode_source"]
    # Everything the slice carried, which is more than the parser delivered ...
    assert source["total"] == slice_cap
    assert source["delivered"] == parser_cap
    assert source["upstream_refused"] == slice_cap - parser_cap
    assert source["truncated"] is True
    # ... and still a floor, because the slice that carried it may have cut a
    # tail of its own. An exact recovery inside a bounded window does not make
    # the window's own length a measurement of the host.
    assert source["total_is_lower_bound"] is True

    counts = {entry["provider"]: entry for entry in catalog["provider_counts"]}
    assert counts["opencode"]["total_is_lower_bound"] is True
    assert counts["opencode"]["truncated"] is True

    row = next(
        item for item in catalog["workers"] if item["model"] == "xai/grok-4.6"
        and item["adapter"] == "opencode_cli"
    )
    assert row["provider"] == "opencode"
    assert row["catalog_enabled"] is True
    assert row["effective_enabled"] is True
    # Named by the source that did offer it rather than labelled as a route no
    # host has: the floor is about the tail, not about this row.
    assert row["source_truncated"] is True
    assert row["discovered_from_opencode"] is True
    assert row.get("declared_only") is not True


# ---------------------------------------------------------------------------
# Skill selection / injection coverage panel
# ---------------------------------------------------------------------------


def test_skills_view_reports_measured_coverage_without_raw_rows(
    tmp_path, monkeypatch
) -> None:
    """Counts, a streak and reason tallies -- never a list of skills or cards."""
    from aiworkhub import skill_registry_store

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(dashboard_mcp_app.core, "repo_root", lambda: repo)
    skill_registry_store.record_selection(
        repo,
        task_id="TASK_EMPTY",
        packet={"version": 1, "skills": []},
        empty_reason="no_vocabulary_match",
    )

    result = dashboard_mcp_app.skills_view()
    assert result["ok"] is True
    assert result["server_tool"] == "aiworkhub_dashboard_skills"
    assert result["measured"] is True
    assert result["skills"]["total"] == 0
    assert result["skills"]["injectable"] == 0
    assert result["selection"]["selection_count"] == 0
    assert result["selection"]["injection_count"] == 0
    assert result["selection"]["consecutive_empty_streak"] == 1
    assert result["selection"]["all_empty"] is True
    assert result["selection"]["empty_reasons"] == {"no_vocabulary_match": 1}
    # Bounded by construction: no key in the payload carries a row list.
    assert not any(
        isinstance(value, list)
        for block in ("skills", "selection")
        for value in result[block].values()
    )


def test_skills_view_reports_an_absent_store_as_unmeasured_not_zero(
    tmp_path, monkeypatch
) -> None:
    """A skill panel that renders "nobody looked" as 0 is the original defect."""
    repo = tmp_path / "bare"
    repo.mkdir()
    monkeypatch.setattr(dashboard_mcp_app.core, "repo_root", lambda: repo)

    result = dashboard_mcp_app.skills_view()
    assert result["measured"] is False
    assert result["unavailable_reason"] == "skill_store_absent"
    assert result["skills"] == {}
    assert result["selection"] == {}
    assert result["schema_id"] == (
        dashboard_mcp_app.skill_registry_store.COVERAGE_SCHEMA_ID
    )


def test_skills_view_keeps_one_unmeasured_shape_however_the_reading_failed(
    tmp_path, monkeypatch
) -> None:
    """A failure ABOVE the projection answers in the projection's own shape.

    Dropping ``schema_id``/``unavailable_reason``/``skills``/``selection`` on
    this path makes a renderer branch on WHICH layer failed before it can tell
    whether the surface was measured, and a missing block reads as an absent
    one -- the exact confusion "measured: False" was added to end.
    """
    repo = tmp_path / "unreadable"
    repo.mkdir()
    monkeypatch.setattr(dashboard_mcp_app.core, "repo_root", lambda: repo)

    def boom(*args, **kwargs):
        raise OSError("skills.sqlite is not readable")

    monkeypatch.setattr(
        dashboard_mcp_app.skill_registry_store, "skill_coverage", boom
    )

    result = dashboard_mcp_app.skills_view()
    assert result["ok"] is False
    assert result["error"] == "skill_coverage_unavailable:OSError"
    assert result["measured"] is False
    # Same shape as the absent-store reading, key for key.
    assert result["schema_id"] == (
        dashboard_mcp_app.skill_registry_store.COVERAGE_SCHEMA_ID
    )
    assert result["unavailable_reason"] == result["error"]
    assert result["skills"] == {}
    assert result["selection"] == {}


def test_the_skills_tool_is_registered_on_the_readonly_surface() -> None:
    recorded: list[str] = []

    class _Recorder:
        def tool(self, *, name):
            recorded.append(name)
            return lambda fn: fn

    names = dashboard_mcp_app.register(_Recorder())
    assert dashboard_mcp_app.SKILLS_TOOL_NAME == "aiworkhub_dashboard_skills"
    assert dashboard_mcp_app.SKILLS_TOOL_NAME in recorded
    assert dashboard_mcp_app.SKILLS_TOOL_NAME in names


# ---------------------------------------------------------------------------
# roadmap current-wave projection
# ---------------------------------------------------------------------------

_WAVE_GOALS = [
    {"id": "playbook", "label": "Playbook stage gates", "task_ids": ["TASK_PLAYBOOK_V1"]},
    {"id": "lsp", "label": "LSP index integration", "task_ids": ["TASK_LSP_V3"]},
    {"id": "delta-review", "label": "Delta review", "task_ids": ["TASK_DELTA_V2"]},
]
_WAVE_TASK_CARDS = {
    "TASK_PLAYBOOK_V1": {"task_id": "TASK_PLAYBOOK_V1", "status": "in_progress"},
    "TASK_LSP_V3": {"task_id": "TASK_LSP_V3", "status": "pending"},
    "TASK_DELTA_V2": {"task_id": "TASK_DELTA_V2", "status": "blocked_on_review"},
}


def _wave_repo(tmp_path, monkeypatch, cards=None):
    repo = tmp_path / "wave-repo"
    repo.mkdir()
    known = _WAVE_TASK_CARDS if cards is None else cards
    monkeypatch.setattr(dashboard_mcp_app.core, "repo_root", lambda: repo)
    monkeypatch.setattr(
        dashboard_mcp_app.task_store, "get_task", lambda _root, task_id: known.get(task_id)
    )
    return repo


def _add_wave(repo, milestone, goals, *, activate=True, title="Wave outcome"):
    store = dashboard_mcp_app.roadmap_store
    item = store.add_item(
        repo,
        title=title,
        outcome="Outcome text",
        milestone=milestone,
        acceptance=["criterion"],
        provenance={"wave_goals": goals},
    )
    if activate:
        store.transition_item(repo, item["id"], "approved", reason="approve")
        store.transition_item(repo, item["id"], "in_progress", reason="start")
    for goal in goals:
        for task_id in goal["task_ids"]:
            store.link_task(repo, item["id"], task_id)
    return item["id"]


def _roadmap_state(repo):
    store = dashboard_mcp_app.roadmap_store
    items = store.list_items(repo, include_archived=True, limit=500)
    return {
        "items": items,
        "events": {item["id"]: store.list_events(repo, item["id"], limit=500) for item in items},
    }


def _forbid_roadmap_writes(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("a dashboard Roadmap read must never write")

    for name in ("initialize_repository", "add_item", "transition_item", "link_task"):
        monkeypatch.setattr(dashboard_mcp_app.roadmap_store, name, boom)


def test_roadmap_views_project_the_current_wave_and_never_write(tmp_path, monkeypatch) -> None:
    repo = _wave_repo(tmp_path, monkeypatch)
    wave_id = _add_wave(repo, "0.11.51", _WAVE_GOALS)
    future_id = _add_wave(repo, "0.11.60", _WAVE_GOALS[:1], activate=False, title="Future wave")
    monkeypatch.setattr(dashboard_mcp_app, "__version__", "0.11.53")
    before = _roadmap_state(repo)
    _forbid_roadmap_writes(monkeypatch)

    listed = dashboard_mcp_app.roadmap_list_view(limit=200)
    detail = dashboard_mcp_app.roadmap_detail_view(wave_id)

    expected = {
        "state": "ready",
        "selection_reason": "unique_highest_active_wave",
        "wave_id": wave_id,
        "installed_version": "0.11.53",
        "target_milestone": "0.11.51",
        "overdue": True,
        "goals": [
            {
                "id": "playbook",
                "label": "Playbook stage gates",
                "state": "open",
                "tasks": [{"task_id": "TASK_PLAYBOOK_V1", "status": "processing"}],
            },
            {
                "id": "lsp",
                "label": "LSP index integration",
                "state": "open",
                "tasks": [{"task_id": "TASK_LSP_V3", "status": "pending"}],
            },
            {
                "id": "delta-review",
                "label": "Delta review",
                "state": "open",
                "tasks": [{"task_id": "TASK_DELTA_V2", "status": "blocked"}],
            },
        ],
    }
    assert listed["current_wave"] == expected
    assert detail["current_wave"] == expected
    assert _roadmap_state(repo) == before

    assert listed["ok"] is True
    assert listed["authority"] == "readonly"
    assert listed["server_tool"] == "aiworkhub_dashboard_roadmap_list"
    assert {entry["id"]: entry["status"] for entry in listed["entries"]} == {
        wave_id: "in_progress",
        future_id: "proposed",
    }
    assert all("provenance" not in entry for entry in listed["entries"])
    assert listed["count"] == 2
    assert listed["truncated"] is False
    assert listed["status_counts"]["in_progress"] == 1
    assert listed["status_counts"]["proposed"] == 1
    assert detail["ok"] is True
    assert detail["authority"] == "readonly"
    assert detail["item"]["id"] == wave_id
    assert detail["item"]["status"] == "in_progress"
    assert detail["item"]["milestone"] == "0.11.51"


def test_current_wave_ignores_list_filters_but_a_truncated_list_is_unknown(
    tmp_path, monkeypatch
) -> None:
    repo = _wave_repo(tmp_path, monkeypatch)
    wave_id = _add_wave(repo, "0.11.51", _WAVE_GOALS)
    future_id = _add_wave(repo, "0.11.60", _WAVE_GOALS[:1], activate=False, title="Future wave")
    monkeypatch.setattr(dashboard_mcp_app, "__version__", "0.11.53")

    filtered = dashboard_mcp_app.roadmap_list_view(status="proposed", limit=200)
    assert [entry["id"] for entry in filtered["entries"]] == [future_id]
    assert filtered["current_wave"]["state"] == "ready"
    assert filtered["current_wave"]["wave_id"] == wave_id

    paged = dashboard_mcp_app.roadmap_list_view(limit=200, offset=2)
    assert paged["entries"] == []
    assert paged["current_wave"]["wave_id"] == wave_id

    cut = dashboard_mcp_app.roadmap_list_view(limit=1)
    assert cut["truncated"] is True
    assert cut["current_wave"]["state"] == "UNKNOWN"
    assert cut["current_wave"]["selection_reason"] == "truncated_roadmap"
    assert cut["current_wave"]["installed_version"] == "0.11.53"

    other = dashboard_mcp_app.roadmap_detail_view(future_id)
    assert other["item"]["id"] == future_id
    assert other["current_wave"] == dashboard_mcp_app.roadmap_list_view(limit=200)["current_wave"]
    assert other["current_wave"]["wave_id"] == wave_id


def test_roadmap_views_report_unknown_for_ambiguous_or_unverifiable_waves(
    tmp_path, monkeypatch
) -> None:
    repo = _wave_repo(tmp_path, monkeypatch)
    first = _add_wave(repo, "0.11.51", _WAVE_GOALS)
    second = _add_wave(repo, "0.11.51", _WAVE_GOALS[:1], title="Second wave")
    monkeypatch.setattr(dashboard_mcp_app, "__version__", "0.11.53")

    listed = dashboard_mcp_app.roadmap_list_view(limit=200)
    detail = dashboard_mcp_app.roadmap_detail_view(first)
    for view in (listed, detail):
        assert view["current_wave"]["state"] == "UNKNOWN"
        assert view["current_wave"]["selection_reason"] == "ambiguous_active_wave"
        assert view["current_wave"]["wave_id"] is None
    assert {entry["id"] for entry in listed["entries"]} == {first, second}

    monkeypatch.setattr(dashboard_mcp_app, "__version__", "not-a-version")
    unverifiable = dashboard_mcp_app.roadmap_list_view(limit=200)
    assert unverifiable["ok"] is True
    assert len(unverifiable["entries"]) == 2
    assert unverifiable["current_wave"]["state"] == "UNKNOWN"
    assert unverifiable["current_wave"]["selection_reason"] == "invalid_installed_version"


def test_roadmap_views_report_unknown_when_an_active_wave_has_an_invalid_milestone(
    tmp_path, monkeypatch
) -> None:
    repo = _wave_repo(tmp_path, monkeypatch)
    valid = _add_wave(repo, "0.11.54", _WAVE_GOALS)
    unversioned = _add_wave(repo, "not-a-version", _WAVE_GOALS[:1], title="Unversioned wave")
    monkeypatch.setattr(dashboard_mcp_app, "__version__", "0.11.53")
    before = _roadmap_state(repo)
    _forbid_roadmap_writes(monkeypatch)

    listed = dashboard_mcp_app.roadmap_list_view(limit=200)
    detail = dashboard_mcp_app.roadmap_detail_view(valid)

    for view in (listed, detail):
        assert view["ok"] is True
        assert view["current_wave"] == {
            "state": "UNKNOWN",
            "selection_reason": "invalid_wave_version",
            "wave_id": None,
            "installed_version": "0.11.53",
            "target_milestone": None,
            "overdue": None,
            "goals": [],
        }
    assert {entry["id"]: entry["status"] for entry in listed["entries"]} == {
        valid: "in_progress",
        unversioned: "in_progress",
    }
    assert detail["item"]["id"] == valid
    assert detail["item"]["status"] == "in_progress"
    assert _roadmap_state(repo) == before


def test_current_wave_goal_states_follow_canonical_task_status(tmp_path, monkeypatch) -> None:
    cards = {
        "TASK_PLAYBOOK_V1": {"task_id": "TASK_PLAYBOOK_V1", "status": "finished"},
        "TASK_LSP_V3": {
            "task_id": "TASK_LSP_V3",
            "status": "finished",
            "archived_at": "2026-09-01T00:00:00+00:00",
        },
        "TASK_DELTA_V2": {"task_id": "TASK_DELTA_V2", "status": "blocked_on_review"},
    }
    repo = _wave_repo(tmp_path, monkeypatch, cards=cards)
    _add_wave(repo, "0.11.51", _WAVE_GOALS)
    monkeypatch.setattr(dashboard_mcp_app, "__version__", "0.11.53")

    wave = dashboard_mcp_app.roadmap_list_view(limit=200)["current_wave"]

    assert {goal["id"]: goal["state"] for goal in wave["goals"]} == {
        "playbook": "checked",
        "lsp": "open",
        "delta-review": "open",
    }
    assert {
        task["task_id"]: task["status"] for goal in wave["goals"] for task in goal["tasks"]
    } == {"TASK_PLAYBOOK_V1": "finished", "TASK_LSP_V3": "archived", "TASK_DELTA_V2": "blocked"}
    assert wave["overdue"] is True


def test_current_wave_uses_the_runtime_version_and_missing_task_cards_stay_unknown(
    tmp_path, monkeypatch
) -> None:
    import aiworkhub

    repo = _wave_repo(tmp_path, monkeypatch, cards={})
    _add_wave(repo, "0.0.1", _WAVE_GOALS)

    wave = dashboard_mcp_app.roadmap_list_view(limit=200)["current_wave"]

    assert wave["state"] == "ready"
    assert wave["installed_version"] == aiworkhub.__version__
    assert wave["target_milestone"] == "0.0.1"
    assert wave["overdue"] is True
    assert {goal["state"] for goal in wave["goals"]} == {"UNKNOWN"}
    assert {task["status"] for goal in wave["goals"] for task in goal["tasks"]} == {"missing"}


def test_roadmap_detail_invalid_id_keeps_its_error_shape() -> None:
    result = dashboard_mcp_app.roadmap_detail_view("not-a-roadmap-id")

    assert result["ok"] is False
    assert result["error"] == "invalid_roadmap_id"
    assert result["authority"] == "readonly"
