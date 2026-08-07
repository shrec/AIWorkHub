"""Full-contract regression tests for aiworkhub.needfix_store.

Covers:
- NF-YYYY-NNNNN stable ID generation
- Full lifecycle transitions and guards
- Captured/unverified convert rejection
- Duplicate-parent cycle guard
- Deduplication and idempotent add
- Bounded list filters and pagination
- Archive / restore / purge lifecycle
- Atomic / idempotent / compensating convert
- Concurrency guard (failed claim)
- Malformed input rejection
- Repo isolation (separate databases)
- Manager mutation vs worker proposal authority
- Existing task storage, blocked-rework, B852 lifecycle tests still pass
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from aiworkhub import needfix_store, task_store


@pytest.fixture
def repo_root():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def init_store(repo_root: Path):
    result = needfix_store.initialize_repository(repo_root)
    assert result["initialized"] is True
    return repo_root


class TestNeedFixIdGeneration:
    """NF-YYYY-NNNNN stable ID format."""

    def test_id_format(self, init_store: Path):
        result = needfix_store.capture_proposal(
            init_store, title="Test", description="Desc"
        )
        nfid = result["id"]
        assert needfix_store.NF_ID_RE.match(nfid), f"{nfid!r} does not match NF-YYYY-NNNNN"
        year = nfid.split("-")[1]
        num = nfid.split("-")[2]
        assert len(year) == 4
        assert len(num) == 5
        assert num.isdigit()

    def test_id_sequence_increments(self, init_store: Path):
        r1 = needfix_store.capture_proposal(init_store, title="A", description="First")
        r2 = needfix_store.capture_proposal(init_store, title="B", description="Second")
        n1 = int(r1["id"].split("-")[2])
        n2 = int(r2["id"].split("-")[2])
        assert n2 == n1 + 1

    def test_id_is_stable_and_parseable(self, init_store: Path):
        for i in range(5):
            r = needfix_store.capture_proposal(
                init_store, title=f"T{i}", description=f"D{i}"
            )
            assert needfix_store.NF_ID_RE.match(r["id"])


def test_task_store_initialization_creates_needfix_for_first_dashboard_read(
    tmp_path: Path,
):
    """Fresh canonical initialization includes the additive NeedFix schema."""
    result = task_store.initialize_repository(tmp_path)
    assert result["needfix_storage"]["initialized"] is True
    assert needfix_store.list_needfix(tmp_path) == []


@pytest.mark.parametrize(
    "kind",
    [
        "idea",
        "technical_debt",
        "optimization",
        "benchmark_gap",
        "documentation_drift",
        "security_risk",
        "investigation",
        "roadmap_candidate",
    ],
)
def test_dashboard_intake_kinds_round_trip(tmp_path: Path, kind: str):
    needfix_store.initialize_repository(tmp_path)
    row = needfix_store.capture_proposal(
        tmp_path,
        title=f"Kind {kind}",
        description="Dashboard contract round-trip",
        kind=kind,
    )
    assert row["kind"] == kind


class TestLifecycleTransitions:
    """Full lifecycle: captured->triaged->accepted->task_planned->task_created->resolved."""

    def test_captured_to_triaged(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        assert r["status"] == "captured"
        r2 = needfix_store.triage_needfix(init_store, r["id"], readiness_score=75)
        assert r2["status"] == "triaged"
        assert r2["readiness_score"] == 75

    def test_triaged_to_accepted(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        r2 = needfix_store.accept_needfix(init_store, r["id"])
        assert r2["status"] == "accepted"

    def test_triaged_to_rejected(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        r2 = needfix_store.reject_needfix(init_store, r["id"], reason="Not now")
        assert r2["status"] == "rejected"

    def test_triaged_to_deferred(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        r2 = needfix_store.defer_needfix(init_store, r["id"], reason="Later")
        assert r2["status"] == "deferred"

    def test_accepted_to_task_planned(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        r2 = needfix_store.mark_task_planned(init_store, r["id"])
        assert r2["status"] == "task_planned"
        assert r2["task_planned_at"] is not None

    def test_deferred_to_accepted(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.defer_needfix(init_store, r["id"])
        r2 = needfix_store.accept_needfix(init_store, r["id"])
        assert r2["status"] == "accepted"

    def test_reject_requires_reason(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        with pytest.raises(needfix_store.NeedFixValidationError):
            needfix_store.reject_needfix(init_store, r["id"], reason="  ")

    def test_invalid_transition_rejected(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        with pytest.raises(needfix_store.NeedFixConflictError):
            needfix_store.accept_needfix(init_store, r["id"])

    def test_cannot_skip_triage_for_accept(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        with pytest.raises(needfix_store.NeedFixConflictError):
            needfix_store.mark_task_planned(init_store, r["id"])


class TestCapturedUnverifiedGuard:
    """captured/unverified cannot convert -- must be accepted first."""

    def _dummy_create_task(self, card):
        return {"task_id": "task-001", "id": "task-001"}

    def test_captured_cannot_convert(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        with pytest.raises(needfix_store.NeedFixConflictError, match=r"captured"):
            needfix_store.convert_needfix(init_store, r["id"], self._dummy_create_task)

    def test_triaged_cannot_convert(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        with pytest.raises(needfix_store.NeedFixConflictError, match=r"triaged"):
            needfix_store.convert_needfix(init_store, r["id"], self._dummy_create_task)

    def test_accepted_can_convert(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        result = needfix_store.convert_needfix(init_store, r["id"], self._dummy_create_task)
        assert result["converted_task_id"] == "task-001"


class TestDuplicateCycleGuard:
    """Duplicate-parent cycle detection."""

    def test_self_duplicate_rejected(self, init_store: Path):
        r1 = needfix_store.capture_proposal(init_store, title="T1", description="D1")
        needfix_store.triage_needfix(init_store, r1["id"])
        with pytest.raises(needfix_store.NeedFixValidationError, match="itself"):
            needfix_store.mark_duplicate(init_store, r1["id"], r1["id"])

    def test_direct_cycle_rejected(self, init_store: Path):
        r1 = needfix_store.capture_proposal(init_store, title="T1", description="D1")
        r2 = needfix_store.capture_proposal(init_store, title="T2", description="D2")
        needfix_store.triage_needfix(init_store, r1["id"])
        needfix_store.triage_needfix(init_store, r2["id"])
        needfix_store.mark_duplicate(init_store, r1["id"], r2["id"])
        with pytest.raises(needfix_store.NeedFixValidationError, match="cycle"):
            needfix_store.mark_duplicate(init_store, r2["id"], r1["id"])

    def test_transitive_cycle_rejected(self, init_store: Path):
        r1 = needfix_store.capture_proposal(init_store, title="T1", description="D1")
        r2 = needfix_store.capture_proposal(init_store, title="T2", description="D2")
        r3 = needfix_store.capture_proposal(init_store, title="T3", description="D3")
        for rid in [r1["id"], r2["id"], r3["id"]]:
            needfix_store.triage_needfix(init_store, rid)
        needfix_store.mark_duplicate(init_store, r1["id"], r2["id"])
        needfix_store.mark_duplicate(init_store, r2["id"], r3["id"])
        with pytest.raises(needfix_store.NeedFixValidationError, match="cycle"):
            needfix_store.mark_duplicate(init_store, r3["id"], r1["id"])

    def test_duplicate_parent_not_found(self, init_store: Path):
        r1 = needfix_store.capture_proposal(init_store, title="T1", description="D1")
        needfix_store.triage_needfix(init_store, r1["id"])
        with pytest.raises(needfix_store.NeedFixNotFoundError):
            needfix_store.mark_duplicate(init_store, r1["id"], "NF-2099-99999")


class TestDeduplication:
    """Dedupe via title+description+scope hash."""

    def test_duplicate_capture_returns_existing(self, init_store: Path):
        r1 = needfix_store.capture_proposal(init_store, title="T", description="D", scope="S")
        r2 = needfix_store.capture_proposal(init_store, title="T", description="D", scope="S")
        assert r1["id"] == r2["id"]

    def test_different_scope_allows_new(self, init_store: Path):
        r1 = needfix_store.capture_proposal(init_store, title="T", description="D", scope="S1")
        r2 = needfix_store.capture_proposal(init_store, title="T", description="D", scope="S2")
        assert r1["id"] != r2["id"]

    def test_archived_duplicate_allows_new(self, init_store: Path):
        r1 = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.archive_needfix(init_store, r1["id"])
        r2 = needfix_store.capture_proposal(init_store, title="T", description="D")
        assert r1["id"] != r2["id"]


class TestBoundedListFilters:
    """Bounded list with filters and pagination."""

    def test_list_limit_bounded(self, init_store: Path):
        for i in range(10):
            needfix_store.capture_proposal(init_store, title=f"T{i}", description=f"D{i}")
        result = needfix_store.list_needfix(init_store, limit=3)
        assert len(result) == 3

    def test_list_offset(self, init_store: Path):
        for i in range(10):
            needfix_store.capture_proposal(init_store, title=f"T{i}", description=f"D{i}")
        full = needfix_store.list_needfix(init_store, limit=100)
        offset = needfix_store.list_needfix(init_store, limit=100, offset=5)
        assert len(offset) == len(full) - 5
        assert offset[0]["id"] == full[5]["id"]

    def test_list_filter_by_status(self, init_store: Path):
        r1 = needfix_store.capture_proposal(init_store, title="T1", description="D1")
        r2 = needfix_store.capture_proposal(init_store, title="T2", description="D2")
        needfix_store.triage_needfix(init_store, r2["id"])
        captured = needfix_store.list_needfix(init_store, status="captured")
        triaged = needfix_store.list_needfix(init_store, status="triaged")
        assert len(captured) == 1
        assert len(triaged) == 1

    def test_list_filter_by_kind_and_severity(self, init_store: Path):
        needfix_store.capture_proposal(init_store, title="T1", description="D1", kind="bug", severity="critical")
        needfix_store.capture_proposal(init_store, title="T2", description="D2", kind="feature", severity="low")
        bugs = needfix_store.list_needfix(init_store, kind="bug")
        criticals = needfix_store.list_needfix(init_store, severity="critical")
        assert len(bugs) == 1
        assert len(criticals) == 1

    def test_list_max_limit_enforced(self, init_store: Path):
        for i in range(600):
            needfix_store.capture_proposal(init_store, title=f"T{i}", description=f"D{i}")
        result = needfix_store.list_needfix(init_store, limit=9999)
        assert len(result) <= needfix_store.MAX_LIST_LIMIT


class TestArchiveRestorePurge:
    """Archive, restore, purge lifecycle."""

    def test_archive_and_restore(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        a = needfix_store.archive_needfix(init_store, r["id"], reason="Done")
        assert a["status"] == "archived"
        assert a["archived_at"] is not None
        restored = needfix_store.restore_needfix(init_store, r["id"], target_status="captured")
        assert restored["status"] == "captured"
        assert restored["archived_at"] is None

    def test_restore_invalid_target(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.archive_needfix(init_store, r["id"])
        with pytest.raises(needfix_store.NeedFixValidationError):
            needfix_store.restore_needfix(init_store, r["id"], target_status="resolved")

    def test_restore_only_archived(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        with pytest.raises(needfix_store.NeedFixConflictError):
            needfix_store.restore_needfix(init_store, r["id"])

    def test_purge_requires_archived(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        with pytest.raises(needfix_store.NeedFixConflictError):
            needfix_store.purge_needfix(init_store, r["id"], audit_reason="test")

    def test_purge_archived_succeeds(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.archive_needfix(init_store, r["id"])
        result = needfix_store.purge_needfix(init_store, r["id"], audit_reason="Cleanup")
        assert result["purged"] is True
        with pytest.raises(needfix_store.NeedFixNotFoundError):
            needfix_store.get_needfix(init_store, r["id"])

    def test_purge_requires_audit_reason(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.archive_needfix(init_store, r["id"])
        with pytest.raises(needfix_store.NeedFixValidationError):
            needfix_store.purge_needfix(init_store, r["id"], audit_reason="")


class TestConvertAtomicIdempotent:
    """Atomic, idempotent, compensating convert."""

    def _create_task_ok(self, card):
        return {"task_id": "task-ok", "id": "task-ok"}

    def _create_task_fail(self, card):
        raise RuntimeError("task creation failed")

    def test_convert_idempotent(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        c1 = needfix_store.convert_needfix(init_store, r["id"], self._create_task_ok)
        assert c1["already_converted"] is False
        c2 = needfix_store.convert_needfix(init_store, r["id"], self._create_task_fail)
        assert c2["already_converted"] is True
        assert c2["converted_task_id"] == c1["converted_task_id"]

    def test_convert_compensates_on_failure(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        original_status = r["status"]
        with pytest.raises(RuntimeError, match="task creation failed"):
            needfix_store.convert_needfix(init_store, r["id"], self._create_task_fail)
        nr = needfix_store.get_needfix(init_store, r["id"])
        assert nr["status"] == "accepted"  # compensated back
        assert nr["converted_task_id"] is None

    def test_convert_concurrency_guard(self, init_store: Path):
        """Simulate race by directly manipulating the DB to look like it's being claimed."""
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        conn = needfix_store._connect(init_store)
        try:
            conn.execute(
                "UPDATE needfix SET status = 'converting' WHERE id = ?", (r["id"],)
            )
        finally:
            conn.close()
        with pytest.raises(needfix_store.NeedFixConflictError):
            needfix_store.convert_needfix(init_store, r["id"], self._create_task_ok)

    def test_convert_resolved_returns_existing(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        c1 = needfix_store.convert_needfix(init_store, r["id"], self._create_task_ok)
        needfix_store.resolve_needfix(init_store, r["id"])
        c2 = needfix_store.convert_needfix(init_store, r["id"], self._create_task_ok)
        assert c2["already_converted"] is True
        assert c2.get("resolved") is True


class TestLinkExistingTask:
    """Explicit manager-only link of a NeedFix to an already-existing,
    same-repository, manager-accepted, finished canonical task."""

    def _tasks(self, **tasks):
        store = dict(tasks)

        def get_task_fn(task_id):
            return store.get(task_id)

        def canonical_status_fn(task):
            return task["status"]

        return get_task_fn, canonical_status_fn

    def _accepted_needfix(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        return needfix_store.accept_needfix(init_store, r["id"])

    def test_link_existing_task_success(self, init_store: Path):
        r = self._accepted_needfix(init_store)
        get_task_fn, status_fn = self._tasks(**{"task-1": {"status": "finished"}})
        result = needfix_store.link_existing_task(
            init_store, r["id"], "task-1", get_task_fn, status_fn
        )
        assert result["already_converted"] is False
        assert result["converted_task_id"] == "task-1"
        nr = needfix_store.get_needfix(init_store, r["id"])
        assert nr["status"] == "task_created"
        assert nr["converted_task_id"] == "task-1"

    def test_link_existing_task_idempotent_same_target(self, init_store: Path):
        r = self._accepted_needfix(init_store)
        get_task_fn, status_fn = self._tasks(**{"task-1": {"status": "finished"}})
        c1 = needfix_store.link_existing_task(init_store, r["id"], "task-1", get_task_fn, status_fn)
        assert c1["already_converted"] is False
        c2 = needfix_store.link_existing_task(init_store, r["id"], "task-1", get_task_fn, status_fn)
        assert c2["already_converted"] is True
        assert c2["converted_task_id"] == "task-1"

    def test_link_existing_task_resolved_returns_existing(self, init_store: Path):
        r = self._accepted_needfix(init_store)
        get_task_fn, status_fn = self._tasks(**{"task-1": {"status": "finished"}})
        needfix_store.link_existing_task(init_store, r["id"], "task-1", get_task_fn, status_fn)
        needfix_store.resolve_needfix(init_store, r["id"])
        c2 = needfix_store.link_existing_task(init_store, r["id"], "task-1", get_task_fn, status_fn)
        assert c2["already_converted"] is True
        assert c2.get("resolved") is True

    def test_link_existing_task_conflicting_retry_different_target(self, init_store: Path):
        r = self._accepted_needfix(init_store)
        get_task_fn, status_fn = self._tasks(
            **{"task-1": {"status": "finished"}, "task-2": {"status": "finished"}}
        )
        needfix_store.link_existing_task(init_store, r["id"], "task-1", get_task_fn, status_fn)
        with pytest.raises(needfix_store.NeedFixConflictError):
            needfix_store.link_existing_task(init_store, r["id"], "task-2", get_task_fn, status_fn)

    def test_link_existing_task_missing_fails_closed(self, init_store: Path):
        r = self._accepted_needfix(init_store)
        get_task_fn, status_fn = self._tasks()
        with pytest.raises(needfix_store.NeedFixValidationError):
            needfix_store.link_existing_task(
                init_store, r["id"], "does-not-exist", get_task_fn, status_fn
            )
        nr = needfix_store.get_needfix(init_store, r["id"])
        assert nr["status"] == "accepted"  # compensated back
        assert nr["converted_task_id"] is None

    def test_link_existing_task_foreign_fails_closed(self, init_store: Path):
        r = self._accepted_needfix(init_store)
        # ``task-in-other-repo`` never appears in this repo's task lookup,
        # simulating an id that only exists in a different repository.
        get_task_fn, status_fn = self._tasks(**{"other-repo-task-9": {"status": "finished"}})
        with pytest.raises(needfix_store.NeedFixValidationError):
            needfix_store.link_existing_task(
                init_store, r["id"], "task-in-other-repo", get_task_fn, status_fn
            )

    def test_link_existing_task_fabricated_fails_closed(self, init_store: Path):
        r = self._accepted_needfix(init_store)
        get_task_fn, status_fn = self._tasks(**{"task-1": {"status": "finished"}})
        with pytest.raises(needfix_store.NeedFixValidationError):
            needfix_store.link_existing_task(
                init_store, r["id"], "totally-fabricated-id", get_task_fn, status_fn
            )

    def test_link_existing_task_unfinished_fails_closed(self, init_store: Path):
        r = self._accepted_needfix(init_store)
        get_task_fn, status_fn = self._tasks(**{"task-1": {"status": "processing"}})
        with pytest.raises(needfix_store.NeedFixConflictError):
            needfix_store.link_existing_task(init_store, r["id"], "task-1", get_task_fn, status_fn)
        nr = needfix_store.get_needfix(init_store, r["id"])
        assert nr["status"] == "accepted"  # compensated back

    def test_link_existing_task_unaccepted_review_fails_closed(self, init_store: Path):
        r = self._accepted_needfix(init_store)
        get_task_fn, status_fn = self._tasks(**{"task-1": {"status": "review"}})
        with pytest.raises(needfix_store.NeedFixConflictError):
            needfix_store.link_existing_task(init_store, r["id"], "task-1", get_task_fn, status_fn)

    def test_link_existing_task_archived_without_acceptance_fails_closed(self, init_store: Path):
        r = self._accepted_needfix(init_store)
        get_task_fn, status_fn = self._tasks(**{"task-1": {"status": "archived"}})
        with pytest.raises(needfix_store.NeedFixConflictError):
            needfix_store.link_existing_task(init_store, r["id"], "task-1", get_task_fn, status_fn)
        nr = needfix_store.get_needfix(init_store, r["id"])
        assert nr["status"] == "accepted"  # compensated back

    def test_link_existing_task_captured_cannot_link(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        get_task_fn, status_fn = self._tasks(**{"task-1": {"status": "finished"}})
        with pytest.raises(needfix_store.NeedFixConflictError):
            needfix_store.link_existing_task(init_store, r["id"], "task-1", get_task_fn, status_fn)

    def test_link_existing_task_concurrency_guard(self, init_store: Path):
        """Simulate race by directly manipulating the DB to look like it's being claimed."""
        r = self._accepted_needfix(init_store)
        conn = needfix_store._connect(init_store)
        try:
            conn.execute(
                "UPDATE needfix SET status = 'converting' WHERE id = ?", (r["id"],)
            )
        finally:
            conn.close()
        get_task_fn, status_fn = self._tasks(**{"task-1": {"status": "finished"}})
        with pytest.raises(needfix_store.NeedFixConflictError):
            needfix_store.link_existing_task(init_store, r["id"], "task-1", get_task_fn, status_fn)

    def test_link_existing_task_allows_normal_resolve_transition(self, init_store: Path):
        r = self._accepted_needfix(init_store)
        get_task_fn, status_fn = self._tasks(**{"task-1": {"status": "finished"}})
        needfix_store.link_existing_task(init_store, r["id"], "task-1", get_task_fn, status_fn)
        resolved = needfix_store.resolve_needfix(init_store, r["id"])
        assert resolved["status"] == "resolved"


class TestExistingTaskLinkCanonicalDelegation:
    """Core, server MCP, and dashboard MCP call the same canonical implementation."""

    def test_server_delegates_to_core(self, monkeypatch):
        from aiworkhub import core, server

        calls = []

        def fake(needfix_id, existing_task_id):
            calls.append((needfix_id, existing_task_id))
            return {"ok": True}

        monkeypatch.setattr(core, "needfix_link_existing_task", fake)
        server.needfix_link_existing_task("NF-2026-00001", "task-1")
        assert calls == [("NF-2026-00001", "task-1")]

    def test_dashboard_delegates_to_core(self, monkeypatch):
        from aiworkhub import core, dashboard_mcp_app

        calls = []

        def fake(needfix_id, existing_task_id):
            calls.append((needfix_id, existing_task_id))
            return {
                "needfix_id": needfix_id,
                "converted_task_id": existing_task_id,
                "already_converted": False,
            }

        monkeypatch.setattr(core, "needfix_link_existing_task", fake)
        result = dashboard_mcp_app.needfix_link_existing_task_view(
            "NF-2026-00001", "task-1", confirm=True
        )
        assert calls == [("NF-2026-00001", "task-1")]
        assert result["ok"] is True

    def test_dashboard_requires_confirmation(self):
        from aiworkhub import dashboard_mcp_app

        result = dashboard_mcp_app.needfix_link_existing_task_view("NF-2026-00001", "task-1")
        assert result["ok"] is False


class TestManagerVsWorkerAuthority:
    """Manager mutation vs worker proposal authority."""

    def test_capture_always_lands_captured(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        assert r["status"] == "captured"
        assert r["provenance"]["verified"] is False

    def test_add_can_set_initial_status(self, init_store: Path):
        r = needfix_store.add_needfix(init_store, title="T", description="D", status="triaged")
        assert r["status"] == "triaged"
        assert r["provenance"]["verified"] is True

    def test_add_invalid_status_rejected(self, init_store: Path):
        with pytest.raises(needfix_store.NeedFixValidationError):
            needfix_store.add_needfix(init_store, title="T", description="D", status="nonsense")


class TestRepoIsolation:
    """Each repository gets its own isolated database with independent sequences."""

    def test_independent_sequences_and_isolation(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            repo1 = Path(td1)
            repo2 = Path(td2)
            needfix_store.initialize_repository(repo1)
            needfix_store.initialize_repository(repo2)

            # Both repos start at NF-YYYY-00001
            r1 = needfix_store.capture_proposal(repo1, title="Repo1-First", description="D")
            r2 = needfix_store.capture_proposal(repo2, title="Repo2-Only", description="D")
            assert r1["id"].endswith("-00001")
            assert r2["id"].endswith("-00001")

            # Each repo only sees its own titles
            list1 = needfix_store.list_needfix(repo1)
            list2 = needfix_store.list_needfix(repo2)
            titles1 = {item["title"] for item in list1}
            titles2 = {item["title"] for item in list2}
            assert "Repo1-First" in titles1
            assert "Repo2-Only" not in titles1
            assert "Repo2-Only" in titles2
            assert "Repo1-First" not in titles2

            # Create a second row in repo1; prove independent sequence
            r1b = needfix_store.capture_proposal(repo1, title="Repo1-Second", description="D2")
            assert r1b["id"].endswith("-00002")
            # Repo2 still has only one item (still 00001)
            list2_after = needfix_store.list_needfix(repo2)
            assert len(list2_after) == 1
            assert list2_after[0]["title"] == "Repo2-Only"
            # Repo1 now has two items
            list1_after = needfix_store.list_needfix(repo1)
            assert len(list1_after) == 2


class TestServerRegistration:
    """Verify server needfix_* module functions are present and callable."""

    _REQUIRED = (
        "needfix_list",
        "needfix_show",
        "needfix_add",
        "needfix_update",
        "needfix_archive",
        "needfix_restore",
        "needfix_purge",
        "needfix_count",
        "needfix_events",
        "needfix_preview_convert",
        "needfix_convert",
        "needfix_link_existing_task",
    )

    def test_required_functions_callable(self):
        from aiworkhub import server
        for name in self._REQUIRED:
            fn = getattr(server, name, None)
            assert fn is not None, f"server.{name} missing"
            assert callable(fn), f"server.{name} is not callable"

    def test_needfix_list_signature(self):
        import inspect
        from aiworkhub import server
        sig = inspect.signature(server.needfix_list)
        params = list(sig.parameters.keys())
        for p in ("status", "kind", "severity", "include_archived", "limit", "offset", "order_by", "order_dir"):
            assert p in params, f"Missing parameter {p} in needfix_list"

    def test_optional_registered_tools(self):
        """Optional: when FastMCP.tools is available, verify registration."""
        from aiworkhub import server
        mcp = getattr(server, "mcp", None)
        if mcp is None:
            pytest.skip("mcp not available")
        registered_tools = getattr(mcp, "tools", None)
        if registered_tools is None:
            pytest.skip("mcp.tools not available")
        tool_names = {tool.name for tool in registered_tools}
        missing = set(self._REQUIRED) - tool_names
        assert not missing, f"Missing MCP tools: {missing}"
class TestMalformedInput:
    """Malformed input fails closed."""

    def test_empty_title_rejected(self, init_store: Path):
        with pytest.raises(needfix_store.NeedFixValidationError, match="title"):
            needfix_store.capture_proposal(init_store, title="", description="D")

    def test_empty_description_rejected(self, init_store: Path):
        with pytest.raises(needfix_store.NeedFixValidationError, match="description"):
            needfix_store.capture_proposal(init_store, title="T", description="")

    def test_invalid_kind_rejected(self, init_store: Path):
        with pytest.raises(needfix_store.NeedFixValidationError, match="kind"):
            needfix_store.capture_proposal(init_store, title="T", description="D", kind="bogus")

    def test_invalid_severity_rejected(self, init_store: Path):
        with pytest.raises(needfix_store.NeedFixValidationError, match="severity"):
            needfix_store.capture_proposal(init_store, title="T", description="D", severity="extreme")

    def test_readiness_out_of_range_rejected(self, init_store: Path):
        with pytest.raises(needfix_store.NeedFixValidationError, match="readiness_score"):
            needfix_store.capture_proposal(init_store, title="T", description="D", readiness_score=999)

    def test_description_size_bounded(self, init_store: Path):
        big = "x" * (needfix_store.MAX_DESCRIPTION_BYTES + 1)
        with pytest.raises(needfix_store.NeedFixValidationError, match="bounded"):
            needfix_store.capture_proposal(init_store, title="T", description=big)


class TestCount:
    """Count with optional filters."""

    def test_count_all_active(self, init_store: Path):
        for i in range(5):
            needfix_store.capture_proposal(init_store, title=f"T{i}", description=f"D{i}")
        assert needfix_store.count_needfix(init_store) == 5

    def test_count_by_status(self, init_store: Path):
        needfix_store.capture_proposal(init_store, title="T1", description="D1")
        r2 = needfix_store.capture_proposal(init_store, title="T2", description="D2")
        needfix_store.triage_needfix(init_store, r2["id"])
        assert needfix_store.count_needfix(init_store, status="captured") == 1
        assert needfix_store.count_needfix(init_store, status="triaged") == 1

    def test_count_by_kind(self, init_store: Path):
        needfix_store.capture_proposal(init_store, title="T1", description="D1", kind="bug")
        needfix_store.capture_proposal(init_store, title="T2", description="D2", kind="bug")
        needfix_store.capture_proposal(init_store, title="T3", description="D3", kind="feature")
        assert needfix_store.count_needfix(init_store, kind="bug") == 2
        assert needfix_store.count_needfix(init_store, kind="feature") == 1


class TestEvents:
    """Event audit trail."""

    def test_events_recorded(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        events = needfix_store.list_events(init_store, r["id"])
        assert len(events) >= 1
        assert events[0]["event"] == "created"

    def test_events_include_lifecycle(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        events = needfix_store.list_events(init_store, r["id"])
        event_names = [e["event"] for e in events]
        assert "created" in event_names
        assert "triaged" in event_names
        assert "accepted" in event_names


class TestPreviewConvert:
    """Read-only preview of conversion eligibility."""

    def test_preview_captured_not_claimable(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        pv = needfix_store.preview_convert(init_store, r["id"])
        assert pv["claimable"] is False
        assert pv["unverified"] is True

    def test_preview_accepted_is_claimable(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        pv = needfix_store.preview_convert(init_store, r["id"])
        assert pv["claimable"] is True


class TestGetNeedfix:
    """Show/Get single NeedFix."""

    def test_get_existing(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        nr = needfix_store.get_needfix(init_store, r["id"])
        assert nr["id"] == r["id"]
        assert nr["title"] == "T"

    def test_get_missing_raises(self, init_store: Path):
        with pytest.raises(needfix_store.NeedFixNotFoundError):
            needfix_store.get_needfix(init_store, "NF-2099-00001")


class TestUpdateFields:
    """Update mutable fields."""

    def test_update_basic_fields(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="Old", description="OldD")
        u = needfix_store.update_needfix(init_store, r["id"], title="New", readiness_score=85)
        assert u["title"] == "New"
        assert u["readiness_score"] == 85

    def test_update_includes_extended_fields(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        u = needfix_store.update_needfix(
            init_store, r["id"],
            kind="security", severity="high",
            tags=["urgent", "cve"],
            scope_files=["src/main.py"],
            scope_symbols=["MyClass.do_work"],
            evidence_refs=["commit/abc123"],
        )
        assert u["kind"] == "security"
        assert u["severity"] == "high"
        assert u["tags"] == ["urgent", "cve"]
        assert u["scope_files"] == ["src/main.py"]
        assert u["scope_symbols"] == ["MyClass.do_work"]
        assert u["evidence_refs"] == ["commit/abc123"]


class TestInitializationIdempotent:
    """initialize_repository is idempotent and preserves existing data."""

    def test_double_init_preserves_data(self, init_store: Path):
        r1 = needfix_store.capture_proposal(init_store, title="T", description="D")
        result = needfix_store.initialize_repository(init_store)
        assert result["initialized"] is True
        assert result["existing_count"] >= 1
        r2 = needfix_store.get_needfix(init_store, r1["id"])
        assert r2["title"] == "T"

    def test_init_on_fresh_repo(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            result = needfix_store.initialize_repository(repo)
            assert result["initialized"] is True
            assert result["existing_count"] == 0
