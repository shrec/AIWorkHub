"""Focused NeedFix registry and dashboard tests.

Verifies that ``dashboard.build_snapshot`` includes a bounded truthful
NeedFix snapshot using the existing ``needfix_store.list_needfix`` API and
canonical repository root. Preserves ``build_task_detail`` behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_TOOL_ROOT = Path(__file__).resolve().parents[1]
_SRC = _TOOL_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import dashboard, needfix_store  # noqa: E402


# ---------------------------------------------------------------------------
# _build_needfix_snapshot  (unit)
# ---------------------------------------------------------------------------

class TestBuildNeedfixSnapshot:
    def test_snapshot_has_correct_schema_and_structure(self, tmp_path: Path):
        """The snapshot object carries schema_id, open, total, items, limit,
        truncated and an explicit error (None when available)."""
        needfix_store.initialize_repository(tmp_path)
        needfix_store.capture_proposal(tmp_path, title="A", description="Desc A")

        snapshot = dashboard._build_needfix_snapshot(tmp_path)
        assert snapshot["schema_id"] == "aiworkhub.needfix_snapshot.v1"
        assert snapshot["available"] is True
        assert snapshot["error"] is None
        assert isinstance(snapshot["open"], int)
        assert isinstance(snapshot["total"], int)
        assert isinstance(snapshot["items"], list)
        assert isinstance(snapshot["limit"], int)
        assert isinstance(snapshot["truncated"], bool)

    def test_open_counts_exclude_resolved_archived_rejected_duplicate(
        self, tmp_path: Path,
    ):
        """Open count must distinguish closed/resolved statuses from active
        intake states (captured, triaged, accepted, task_planned,
        task_created, deferred)."""
        needfix_store.initialize_repository(tmp_path)

        def _dummy_create_task(card):
            return {"task_id": f"task-{card['title']}", "id": f"task-{card['title']}"}

        captured = needfix_store.capture_proposal(
            tmp_path, title="Captured", description="D1"
        )["id"]

        triaged = needfix_store.capture_proposal(
            tmp_path, title="Triaged", description="D2"
        )["id"]
        needfix_store.triage_needfix(tmp_path, triaged)

        accepted = needfix_store.capture_proposal(
            tmp_path, title="Accepted", description="D3"
        )["id"]
        needfix_store.triage_needfix(tmp_path, accepted)
        needfix_store.accept_needfix(tmp_path, accepted)

        task_planned = needfix_store.capture_proposal(
            tmp_path, title="TaskPlanned", description="D4"
        )["id"]
        needfix_store.triage_needfix(tmp_path, task_planned)
        needfix_store.accept_needfix(tmp_path, task_planned)
        needfix_store.mark_task_planned(tmp_path, task_planned)

        task_created = needfix_store.capture_proposal(
            tmp_path, title="TaskCreated", description="D5"
        )["id"]
        needfix_store.triage_needfix(tmp_path, task_created)
        needfix_store.accept_needfix(tmp_path, task_created)
        needfix_store.mark_task_planned(tmp_path, task_created)
        needfix_store.convert_needfix(
            tmp_path, task_created, create_task_fn=_dummy_create_task
        )

        deferred = needfix_store.capture_proposal(
            tmp_path, title="Deferred", description="D6"
        )["id"]
        needfix_store.triage_needfix(tmp_path, deferred)
        needfix_store.defer_needfix(tmp_path, deferred)

        resolved = needfix_store.capture_proposal(
            tmp_path, title="Resolved", description="D7"
        )["id"]
        needfix_store.triage_needfix(tmp_path, resolved)
        needfix_store.accept_needfix(tmp_path, resolved)
        needfix_store.mark_task_planned(tmp_path, resolved)
        needfix_store.convert_needfix(
            tmp_path, resolved, create_task_fn=_dummy_create_task
        )
        needfix_store.resolve_needfix(tmp_path, resolved)

        rejected = needfix_store.capture_proposal(
            tmp_path, title="Rejected", description="D8"
        )["id"]
        needfix_store.reject_needfix(tmp_path, rejected, reason="not applicable")

        duplicate = needfix_store.capture_proposal(
            tmp_path, title="Duplicate", description="D9"
        )["id"]
        needfix_store.mark_duplicate(tmp_path, duplicate, captured, reason="dup")

        snapshot = dashboard._build_needfix_snapshot(tmp_path)
        assert snapshot["available"] is True
        assert snapshot["total"] == 9
        # open = everything except resolved, archived, rejected, duplicate
        assert snapshot["open"] == 6

    def test_archived_items_excluded_by_default(self, tmp_path: Path):
        """Archived items should not appear in total or open count when
        include_archived=False (the default in _build_needfix_snapshot)."""
        needfix_store.initialize_repository(tmp_path)
        needfix_store.capture_proposal(tmp_path, title="Open", description="D1")
        r2 = needfix_store.capture_proposal(tmp_path, title="Archived", description="D2")
        needfix_store.archive_needfix(tmp_path, r2["id"])

        snapshot = dashboard._build_needfix_snapshot(tmp_path)
        assert snapshot["available"] is True
        assert snapshot["total"] == 1
        assert snapshot["open"] == 1
        assert len(snapshot["items"]) == 1
        assert snapshot["items"][0]["id"] != r2["id"]

    def test_snapshot_items_are_bounded_and_safe(self, tmp_path: Path):
        """Each item only exposes safe dashboard fields (id, title, status,
        kind, severity, created_at, updated_at, converted_task_id)."""
        needfix_store.initialize_repository(tmp_path)
        proposal = needfix_store.capture_proposal(tmp_path, title="Safe", description="D")

        snapshot = dashboard._build_needfix_snapshot(tmp_path)
        item = snapshot["items"][0]
        allowed = {"id", "title", "status", "kind", "severity",
                   "created_at", "updated_at", "converted_task_id"}
        assert set(item.keys()) == allowed
        assert item["id"] == proposal["id"]

    def test_truncated_true_when_total_hits_limit(self, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch):
        """When the number of rows equals or exceeds NEEDFIX_SNAPSHOT_LIMIT,
        truncated must be True."""
        monkeypatch.setattr(dashboard, "NEEDFIX_SNAPSHOT_LIMIT", 3)
        needfix_store.initialize_repository(tmp_path)
        for i in range(4):
            needfix_store.capture_proposal(
                tmp_path, title=f"T {i}", description=f"D {i}"
            )

        snapshot = dashboard._build_needfix_snapshot(tmp_path)
        assert snapshot["total"] == 3
        assert snapshot["truncated"] is True
        assert snapshot["limit"] == 3


# ---------------------------------------------------------------------------
# Uninitialized / degraded
# ---------------------------------------------------------------------------

class TestNeedfixUnavailable:
    def test_unavailable_function_returns_bounded_state(self):
        """_needfix_unavailable returns an explicit unavailable state with
        schema_id, available=False, error string, zero counts, empty items."""
        state = dashboard._needfix_unavailable()
        assert state["schema_id"] == "aiworkhub.needfix_snapshot.v1"
        assert state["available"] is False
        assert isinstance(state["error"], str)
        assert state["open"] == 0
        assert state["total"] == 0
        assert state["items"] == []
        assert state["truncated"] is False

    def test_build_snapshot_returns_unavailable_on_store_failure(
        self, tmp_path: Path,
    ):
        """When needfix_store.list_needfix raises, _build_needfix_snapshot
        returns an unavailable state with the error message surfaced, never
        a false zero."""
        non_existent = tmp_path / "nonexistent"
        snapshot = dashboard._build_needfix_snapshot(non_existent)
        assert snapshot["available"] is False
        assert snapshot["error"] != "needfix_store_unavailable"
        assert snapshot["open"] == 0
        assert snapshot["total"] == 0
        assert snapshot["items"] == []
        assert snapshot["truncated"] is False

    def test_degraded_build_snapshot_includes_needfix_unavailable(self):
        """A provider whose storage is not ready produces a degraded snapshot
        where needfix is explicitly unavailable, not a false zero."""

        class DegradedProvider:
            repo_root = None

            def get_storage_readiness(self):
                from aiworkhub.task_store import StorageReadiness
                return StorageReadiness(
                    ready=False,
                    reason="test_degraded",
                    repo_id="repo_test_degraded",
                    canonical_db="/nonexistent/.aiworkhub/tasking/tasks.sqlite",
                )

        snap = dashboard.build_snapshot(DegradedProvider())
        assert snap["health"]["degraded"] is True
        assert snap["needfix"]["available"] is False
        assert snap["needfix"]["error"] == "needfix_store_unavailable"
        assert snap["needfix"]["open"] == 0
        assert snap["needfix"]["total"] == 0
        assert snap["needfix"]["items"] == []


# ---------------------------------------------------------------------------
# Integration with build_snapshot via FakeProvider
# ---------------------------------------------------------------------------

class TestNeedfixInBuildSnapshot:
    def test_needfix_key_present_in_snapshot(self, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch):
        """build_snapshot returns a dict that always includes the needfix key."""
        needfix_store.initialize_repository(tmp_path)
        needfix_store.capture_proposal(tmp_path, title="T1", description="D1")

        class Provider:
            repo_root = tmp_path

            def get_storage_readiness(self):
                from aiworkhub.task_store import StorageReadiness
                return StorageReadiness(
                    ready=True,
                    reason="ok",
                    repo_id="repo_test_needfix",
                    canonical_db=str(
                        tmp_path / ".aiworkhub" / "tasking" / "tasks.sqlite"
                    ),
                )

            def list_tasks(self, status):
                return []

            def get_completion_inbox(self):
                return {}

            def get_cost_ledger(self):
                return {}

            def get_collision_report(self):
                return {}

            def get_agent_processes(self):
                return {"ok": True, "processes": [], "total_requests": 0}

            def get_adapter_readiness(self):
                return {"ok": True, "readonly": True, "adapters": []}

            def get_callback_bridge_health(self):
                return {"bound_task_count": 0, "unbound_task_count": 0, "by_state": {}}

            def get_task_plan(self):
                return {}

            def get_environment_preflight(self):
                return {}

            def get_workforce_catalog(self):
                return {}

        snap = dashboard.build_snapshot(Provider())
        assert "needfix" in snap
        assert snap["needfix"]["schema_id"] == "aiworkhub.needfix_snapshot.v1"
        assert snap["needfix"]["available"] is True
        assert snap["needfix"]["open"] == 1
        assert snap["needfix"]["total"] == 1

    def test_build_snapshot_needfix_reads_from_canonical_repo_root(
        self, tmp_path: Path,
    ):
        """The NeedFix snapshot is built from the canonical repository root
        via needfix_store.list_needfix, never from a provider method."""
        needfix_store.initialize_repository(tmp_path)
        needfix_store.capture_proposal(tmp_path, title="T1", description="D1")

        with patch.object(
            needfix_store, "list_needfix", wraps=needfix_store.list_needfix
        ) as mock_list:
            dashboard._build_needfix_snapshot(tmp_path)
            mock_list.assert_called_once()
            call_kwargs = mock_list.call_args.kwargs
            assert call_kwargs["include_archived"] is False
            assert call_kwargs["limit"] == dashboard.NEEDFIX_SNAPSHOT_LIMIT
            assert call_kwargs["order_by"] == "created_at"


# ---------------------------------------------------------------------------
# build_task_detail preserves existing behaviour
# ---------------------------------------------------------------------------

class TestBuildTaskDetailRegression:
    def test_build_task_detail_still_returns_result_object(self):
        """build_task_detail must still return its result object with the
        expected keys; the NeedFix integration must not alter its behaviour."""

        class Provider:
            def get_task(self, task_id):
                return {
                    "task_id": task_id,
                    "status": "review",
                    "worker_status": "review",
                    "topic": "test",
                    "runner": "test_runner",
                }

            def get_agent_processes(self):
                return {"ok": True, "processes": [], "total_requests": 0}

            def get_task_events(self, task_id):
                return []

        detail = dashboard.build_task_detail("TASK_DETAIL_REGRESSION_V1", Provider())
        assert detail is not None
        assert "generated_at" in detail
        assert "readonly" in detail
        assert "task" in detail
        assert detail["readonly"] is True
        assert detail["task"]["task_id"] == "TASK_DETAIL_REGRESSION_V1"
        assert detail["task"]["status"] == "review"

    def test_build_task_detail_returns_none_for_missing_task(self):
        """build_task_detail must still return None for missing tasks."""

        class Provider:
            def get_task(self, task_id):
                return None

        detail = dashboard.build_task_detail("NONEXISTENT", Provider())
        assert detail is None
