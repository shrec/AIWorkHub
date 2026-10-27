"""NF-2026-00633: an idle dashboard panel must not burn a core.

(d) the snapshot read set reuses one module-level pool, (e) the rendered task
collections never decode archived/superseded cards while every rendered value
stays identical, and (f) ``snapshot_view`` never sleeps waiting for a scan.
"""

from __future__ import annotations

import copy
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_TOOL_ROOT = Path(__file__).resolve().parents[1]
_SRC = _TOOL_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, dashboard, dashboard_mcp_app, task_store  # noqa: E402


# ---------------------------------------------------------------- (d) pool


@pytest.fixture
def fresh_read_pool(monkeypatch):
    """Start each case without a pool and shut down whatever it created."""
    monkeypatch.setattr(dashboard, "_SNAPSHOT_READ_POOL", None)
    yield
    pool = dashboard._SNAPSHOT_READ_POOL  # noqa: SLF001
    if pool is not None:
        pool.shutdown(wait=True)


def _count_read_thread_starts(monkeypatch) -> list[str]:
    started: list[str] = []
    original_start = threading.Thread.start

    def counting_start(self, *args, **kwargs):
        if self.name.startswith("aiworkhub-dashboard-read"):
            started.append(self.name)
        return original_start(self, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", counting_start)
    return started


def test_parallel_reads_reuse_one_module_pool(monkeypatch, fresh_read_pool):
    monkeypatch.setattr(dashboard.os, "cpu_count", lambda: 8)
    started = _count_read_thread_starts(monkeypatch)
    constructed: list[int] = []
    real_executor = dashboard.ThreadPoolExecutor

    def counting_executor(*args, **kwargs):
        constructed.append(kwargs.get("max_workers"))
        return real_executor(*args, **kwargs)

    monkeypatch.setattr(dashboard, "ThreadPoolExecutor", counting_executor)
    operations = {
        f"read{index}": (f"source{index}", lambda: threading.get_ident(), None)
        for index in range(8)
    }

    pools = set()
    executing_threads: set[int] = set()
    for _ in range(50):
        errors: list[dict[str, str]] = []
        result = dashboard._parallel_snapshot_reads(operations, errors)  # noqa: SLF001
        assert errors == []
        assert list(result) == list(operations)
        executing_threads.update(result.values())
        pools.add(id(dashboard._SNAPSHOT_READ_POOL))  # noqa: SLF001

    # Same observed-core formula as before: cpu_count - 2, minimum one.
    assert constructed == [6]
    assert len(pools) == 1
    assert len(started) <= 6
    assert len(executing_threads) <= 6
    assert threading.get_ident() not in executing_threads


def test_parallel_reads_match_the_sequential_path(monkeypatch, fresh_read_pool):
    def operations() -> dict[str, tuple[str, Any, Any]]:
        def slow_fail():
            time.sleep(0.05)
            raise RuntimeError("slow")

        def fast_fail():
            raise ValueError("fast")

        def slow_value():
            time.sleep(0.03)
            return {"slow": True}

        return {
            "a": ("source.a", slow_fail, "fallback-a"),
            "b": ("source.b", slow_value, {}),
            "c": ("source.c", fast_fail, "fallback-c"),
            "d": ("source.d", lambda: [1, 2, 3], []),
        }

    monkeypatch.setattr(dashboard.os, "cpu_count", lambda: 1)
    sequential_errors: list[dict[str, str]] = []
    sequential = dashboard._parallel_snapshot_reads(operations(), sequential_errors)  # noqa: SLF001
    assert dashboard._SNAPSHOT_READ_POOL is None  # noqa: SLF001

    monkeypatch.setattr(dashboard.os, "cpu_count", lambda: 8)
    for _ in range(3):
        pooled_errors: list[dict[str, str]] = []
        pooled = dashboard._parallel_snapshot_reads(operations(), pooled_errors)  # noqa: SLF001
        assert pooled == sequential
        assert pooled_errors == sequential_errors
    assert [error["source"] for error in sequential_errors] == ["source.a", "source.c"]


@pytest.mark.parametrize("cores", [None, 1, 2, 3])
def test_one_core_hosts_stay_sequential(monkeypatch, fresh_read_pool, cores):
    monkeypatch.setattr(dashboard.os, "cpu_count", lambda: cores)
    caller = threading.get_ident()
    errors: list[dict[str, str]] = []

    result = dashboard._parallel_snapshot_reads(  # noqa: SLF001
        {
            "first": ("first", threading.get_ident, None),
            "second": ("second", threading.get_ident, None),
        },
        errors,
    )

    assert result == {"first": caller, "second": caller}
    assert dashboard._SNAPSHOT_READ_POOL is None  # noqa: SLF001


def test_pool_creation_is_thread_safe(monkeypatch, fresh_read_pool):
    monkeypatch.setattr(dashboard.os, "cpu_count", lambda: 8)
    barrier = threading.Barrier(8)
    seen: list[int] = []

    def grab() -> None:
        barrier.wait()
        seen.append(id(dashboard._snapshot_read_pool()))  # noqa: SLF001

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(seen) == 8
    assert len(set(seen)) == 1


def test_nested_read_set_inside_a_pool_thread_runs_inline(monkeypatch, fresh_read_pool):
    # cpu_count 4 -> a two-worker pool; two outer reads that each start an
    # inner read set would deadlock a shared bounded pool without the guard.
    monkeypatch.setattr(dashboard.os, "cpu_count", lambda: 4)

    def outer(tag: str):
        inner_errors: list[dict[str, str]] = []
        return dashboard._parallel_snapshot_reads(  # noqa: SLF001
            {
                "x": (f"{tag}.x", lambda: f"{tag}-x", None),
                "y": (f"{tag}.y", lambda: f"{tag}-y", None),
            },
            inner_errors,
        )

    errors: list[dict[str, str]] = []
    result = dashboard._parallel_snapshot_reads(  # noqa: SLF001
        {
            "one": ("one", lambda: outer("one"), None),
            "two": ("two", lambda: outer("two"), None),
        },
        errors,
    )

    assert errors == []
    assert result == {
        "one": {"x": "one-x", "y": "one-y"},
        "two": {"x": "two-x", "y": "two-y"},
    }


# ------------------------------------------- (e) archived rows never decoded


_NOW = "2026-10-27T10:00:00+00:00"


def _card(task_id: str, updated_at: str, *, status: str, writes: list[str], **extra: Any) -> dict[str, Any]:
    card = {
        "task_id": task_id,
        "runner": "codex",
        "topic": "dashboard",
        "mode": "",
        "status": status,
        "worker_status": extra.pop("worker_status", "unclaimed"),
        "priority": "normal",
        "objective": f"objective {task_id}",
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": updated_at,
        "claimed_by": None,
        "claimed_at": None,
        "started_at": None,
        "completed_at": None,
        "archived_at": extra.pop("archived_at", ""),
        "allowed_writes": writes,
    }
    card.update(extra)
    card["status"] = task_store.canonical_status(card)
    return card


_CARDS: tuple[dict[str, Any], ...] = (
    _card("T-ACTIVE-P1", "2026-10-27T09:00:00+00:00", status="pending", writes=["src/a.py"]),
    _card("T-ACTIVE-P2", "2026-10-27T08:00:00+00:00", status="pending", writes=["src/a.py", "src/b.py"]),
    _card(
        "T-ACTIVE-X1", "2026-10-27T07:00:00+00:00",
        status="processing", worker_status="claimed", writes=["src/c.py"],
    ),
    _card("T-ACTIVE-R1", "2026-10-27T06:00:00+00:00", status="review", writes=["src/b.py"]),
    _card(
        "T-ARCHIVED-1", "2026-10-27T09:30:00+00:00",
        status="finished", writes=["src/a.py"], archived_at="2026-10-27T09:30:00+00:00",
    ),
    _card(
        "T-ARCHIVED-2", "2026-10-27T05:00:00+00:00",
        status="pending", writes=["src/c.py"], archived_at="2026-10-26T00:00:00+00:00",
    ),
    _card(
        "T-SUPERSEDED-1", "2026-10-27T09:45:00+00:00",
        status="superseded", writes=["src/c.py"], superseded_by="T-ACTIVE-X1",
    ),
    _card("T-FINISHED-1", "2026-10-27T04:00:00+00:00", status="finished", writes=["src/c.py"]),
)
_ACTIVE_IDS = {"T-ACTIVE-P1", "T-ACTIVE-P2", "T-ACTIVE-X1", "T-ACTIVE-R1"}
_ROW_COLUMNS = (
    "task_id", "runner", "topic", "mode", "status", "worker_status", "priority",
    "objective", "created_at", "updated_at", "claimed_by", "claimed_at",
    "started_at", "completed_at", "archived_at",
)


class _FakeStore:
    """Canonical store double: narrow rows vs. decoded card reads are counted."""

    def __init__(self) -> None:
        self.row_scans: list[str] = []
        self.decoded_card_ids: list[str] = []
        self.window_reads = 0

    def list_tasks(self, _root, *, status=None, limit=500):
        self.row_scans.append(status)
        rows = [
            {column: card[column] for column in _ROW_COLUMNS}
            for card in sorted(_CARDS, key=lambda c: c["updated_at"], reverse=True)
            if task_store.canonical_status(card) == status
        ]
        return rows[:limit]

    def list_task_cards(self, _root, *, limit=500):
        self.window_reads += 1
        cards = sorted(_CARDS, key=lambda c: c["updated_at"], reverse=True)[:limit]
        self.decoded_card_ids.extend(card["task_id"] for card in cards)
        return [dict(card) for card in cards]

    def get_tasks(self, _root, task_ids, *, readiness=None):
        found = {}
        for task_id in dict.fromkeys(task_ids):
            for card in _CARDS:
                if card["task_id"] == task_id:
                    self.decoded_card_ids.append(task_id)
                    found[task_id] = dict(card)
        return found

    def get_task(self, _root, task_id, **_kwargs):
        return self.get_tasks(_root, [task_id]).get(task_id)


class _HermeticProvider(dashboard.DashboardProvider):
    """Real provider readers; only non-task external sources are fixed."""

    def __init__(self, repo_root: Path, history_inputs: dict[str, list[list[str]]]) -> None:
        super().__init__(repo_root=repo_root)
        self._history_inputs = history_inputs

    def get_storage_readiness(self):
        return SimpleNamespace(ready=True, reason="ready", repo_id="repo_nf00633")

    def get_exact_status_counts(self):
        counts = {status: 0 for status in dashboard.ALL_CANONICAL_STATUSES}
        for card in _CARDS:
            counts[task_store.canonical_status(card)] += 1
        return counts

    def get_manager_decision_counts(self):
        return {"accepted": 5, "rejected": 2}

    def get_work_card_decision_counts(self):
        return None

    def get_history_series(self):
        return None

    def get_callback_bridge_health(self):
        return {"bound_task_count": 0, "unbound_task_count": 0, "by_state": {}}

    def get_adapter_readiness(self):
        return {"ok": True, "readonly": True, "adapters": []}

    def get_needfix_snapshot(self):
        self._history_inputs["needfix"].append(sorted(c["task_id"] for c in self._task_cards()))
        return {"available": True, "items": []}

    def get_roadmap_snapshot(self):
        self._history_inputs["roadmap"].append(sorted(c["task_id"] for c in self._task_cards()))
        return {"available": True, "items": []}


class _LegacyProvider(_HermeticProvider):
    """The pre-NF-2026-00633 rendered-collection readers, kept as the oracle."""

    def list_tasks(self, status: str) -> list[dict[str, Any]]:
        return task_store.list_tasks(self.repo_root, status=status, limit=self.task_limit)

    def get_collision_report(self) -> dict[str, Any]:
        active: list[dict[str, Any]] = []
        for status in dashboard.ACTIVE_STATUSES:
            active.extend(task_store.list_tasks(self.repo_root, status=status, limit=self.task_limit))
        owners_by_path: dict[str, list[str]] = defaultdict(list)
        cards_by_id = {str(card.get("task_id") or ""): card for card in self._task_cards()}
        for row in active:
            task_id = str(row.get("task_id") or "")
            card = cards_by_id.get(task_id) or task_store.get_task(self.repo_root, task_id) or {}
            for path in card.get("allowed_writes") or []:
                normalized = str(path).strip()
                if normalized and task_id not in owners_by_path[normalized]:
                    owners_by_path[normalized].append(task_id)
        collisions = [
            {"path": path, "task_ids": owners}
            for path, owners in sorted(owners_by_path.items())
            if len(owners) > 1
        ]
        return {
            "collision_free": not collisions,
            "active_cards": len(active),
            "collision_count": len(collisions),
            "file_collisions": collisions,
        }


def _hermetic_sources(monkeypatch, store: _FakeStore, history_inputs) -> None:
    monkeypatch.setattr(dashboard.task_store, "list_tasks", store.list_tasks)
    monkeypatch.setattr(dashboard.task_store, "list_task_cards", store.list_task_cards)
    monkeypatch.setattr(dashboard.task_store, "get_tasks", store.get_tasks)
    monkeypatch.setattr(dashboard.task_store, "get_task", store.get_task)
    monkeypatch.setattr(dashboard.storage_observability, "snapshot", lambda _root: {})
    monkeypatch.setattr(dashboard.source_graph_daemon, "daemon_health", lambda _root: {})
    monkeypatch.setattr(dashboard.context_graph, "status", lambda _root: {})
    monkeypatch.setattr(dashboard, "_protocol_alert_telemetry", lambda _root=None: {})
    monkeypatch.setattr(
        dashboard,
        "read_process_runs",
        lambda **_kwargs: {"ok": True, "processes": [], "total_requests": 0},
    )
    monkeypatch.setattr(
        dashboard.cost_ledger,
        "build_cost_ledger",
        lambda **_kwargs: {"tasks": [], "totals": {"total_tokens": 42}, "cost_per_accepted_outcome": {}},
    )
    monkeypatch.setattr(dashboard.repo_policy, "build_preflight", lambda _root: {"providers": []})

    def workforce(_root, **kwargs):
        history_inputs["workforce"].append(sorted(c["task_id"] for c in kwargs["cards"]))
        return {"workers": []}

    monkeypatch.setattr(dashboard.workforce_catalog, "build_catalog", workforce)


_RENDERED_KEYS = (
    "tasks",
    "status_counts",
    "row_counts",
    "outcome_counts",
    "work_card_outcomes",
    "kpi_analytics",
    "completion_inbox",
    "collision_report",
    "warnings",
    "task_plan",
    "cost_usage",
    "summaries",
)


def _rendered(snapshot: dict[str, Any]) -> dict[str, Any]:
    rendered = copy.deepcopy({key: snapshot.get(key) for key in _RENDERED_KEYS})
    # Cache counters are snapshot-cache instrumentation, which legitimately
    # differs between the cached and uncached readers.
    read_bounds = ((rendered.get("cost_usage") or {}).get("ledger") or {}).get("read_bounds")
    if isinstance(read_bounds, dict):
        read_bounds.pop("cache", None)
    return rendered


def test_rendered_task_collections_never_decode_archived_or_superseded_cards(monkeypatch, tmp_path):
    history_inputs: dict[str, list[list[str]]] = defaultdict(list)
    store = _FakeStore()
    _hermetic_sources(monkeypatch, store, history_inputs)
    provider = _HermeticProvider(tmp_path, history_inputs)

    errors: list[dict[str, str]] = []
    with provider.snapshot_read_scope():
        reads = dashboard._parallel_snapshot_reads(  # noqa: SLF001
            {
                **{
                    f"tasks.{status}": (f"tasks.{status}", lambda s=status: provider.list_tasks(s), [])
                    for status in dashboard.ACTIVE_STATUSES
                },
                "completion_inbox": ("completion_inbox", provider.get_completion_inbox, {}),
                "collision_guard": ("collision_guard", provider.get_collision_report, {}),
            },
            errors,
        )

    assert errors == []
    # Card payloads were decoded for active cards only, and the full-history
    # card window was never touched by the rendered collections.
    assert store.window_reads == 0
    assert set(store.decoded_card_ids) == _ACTIVE_IDS
    # One narrow scan per active status, shared by every consumer.
    assert sorted(store.row_scans) == sorted(dashboard.ACTIVE_STATUSES)
    rendered_ids = {
        row["task_id"]
        for status in dashboard.ACTIVE_STATUSES
        for row in reads[f"tasks.{status}"]
    }
    assert rendered_ids == _ACTIVE_IDS
    assert reads["collision_guard"]["file_collisions"] == [
        {"path": "src/a.py", "task_ids": ["T-ACTIVE-P1", "T-ACTIVE-P2"]},
        {"path": "src/b.py", "task_ids": ["T-ACTIVE-P2", "T-ACTIVE-R1"]},
    ]
    assert reads["collision_guard"]["active_cards"] == 4
    assert [row["task_id"] for row in reads["completion_inbox"]["review_queue"]] == ["T-ACTIVE-R1"]
    # Consumers get their own row copies: mutating one never leaks into another.
    reads["tasks.pending"][0]["task_id"] = "mutated"
    assert provider.list_tasks("pending")[0]["task_id"] == "T-ACTIVE-P1"


def test_full_snapshot_values_are_unchanged_against_the_legacy_readers(monkeypatch, tmp_path):
    results = {}
    raw = {}
    history = {}
    stores = {}
    for name, provider_class in (("legacy", _LegacyProvider), ("current", _HermeticProvider)):
        history_inputs: dict[str, list[list[str]]] = defaultdict(list)
        store = _FakeStore()
        _hermetic_sources(monkeypatch, store, history_inputs)
        snapshot = dashboard.build_snapshot(provider_class(tmp_path, history_inputs))
        raw[name] = snapshot
        results[name] = _rendered(snapshot)
        history[name] = dict(history_inputs)
        stores[name] = store

    assert results["current"] == results["legacy"]
    # Consumers of the active-status scans share one cached read.
    assert raw["current"]["cost_usage"]["ledger"]["read_bounds"]["cache"]["hit"] >= 1
    current = results["current"]
    assert current["status_counts"]["archived"] == 2
    assert current["status_counts"]["superseded"] == 1
    assert current["status_counts"]["finished"] == 1
    assert current["status_counts"]["active"] == 4
    assert current["outcome_counts"]["archived"] == 2
    assert current["outcome_counts"]["superseded"] == 1
    assert {
        row["task_id"] for status in dashboard.ACTIVE_STATUSES for row in current["tasks"][status]
    } == _ACTIVE_IDS
    assert [w["path"] for w in current["warnings"]["collisions"]] == ["src/a.py", "src/b.py"]

    # History/KPI readers keep exactly their inputs: the full card window,
    # archived and superseded cards included.
    every_id = sorted(card["task_id"] for card in _CARDS)
    for reader in ("needfix", "roadmap", "workforce"):
        assert history["current"][reader] == history["legacy"][reader] == [every_id]

    # The legacy path re-scanned the active statuses once per consumer.
    assert len(stores["legacy"].row_scans) == 8
    assert sorted(stores["current"].row_scans) == sorted(dashboard.ACTIVE_STATUSES)


# ---------------------------------------------------- (f) no settle sleep


def test_full_snapshot_reports_an_in_progress_scan_without_sleeping(monkeypatch):
    built = {
        "schema_version": 1,
        "storage": {"ready": True},
        "storage_usage": {"scan_status": "scanning", "repo_data_bytes": 0},
        "tasks": {"pending": []},
    }
    samples: list[int] = []

    def live_cache(_root):
        samples.append(1)
        return {"scan_status": "scanning", "repo_data_bytes": 7}

    def forbidden_sleep(_seconds):
        raise AssertionError("snapshot_view must not wait for the storage scan")

    monkeypatch.setattr(dashboard, "build_snapshot", lambda **_kwargs: dict(built))
    monkeypatch.setattr(core, "manager_bootstrap", lambda: {})
    monkeypatch.setattr(core, "dispatcher_health", lambda: {"ok": True})
    monkeypatch.setattr(core, "repo_root", lambda: Path("/"))
    monkeypatch.setattr(core, "read_selected_coordinator_target", lambda _root: {})
    monkeypatch.setattr(
        dashboard_mcp_app.shared_router, "list_known_repositories", lambda **_kwargs: {}
    )
    monkeypatch.setattr(dashboard_mcp_app.storage_observability, "snapshot", live_cache)
    monkeypatch.setattr(dashboard_mcp_app.time, "sleep", forbidden_sleep)

    started = time.perf_counter()
    result = dashboard_mcp_app.snapshot_view(full=True)
    elapsed = time.perf_counter() - started

    assert result["snapshot_mode"] == "full"
    assert result["storage_usage"]["scan_status"] == "scanning"
    assert result["storage_usage"]["repo_data_bytes"] == 7
    assert samples == [1]
    assert elapsed < 1.0
