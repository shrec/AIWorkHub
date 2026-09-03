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

from aiworkhub import needfix_ingest, needfix_store, task_store


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
        assert r["status"] == "captured"
        with pytest.raises(needfix_store.NeedFixConflictError) as exc_info:
            needfix_store.accept_needfix(init_store, r["id"])
        msg = str(exc_info.value)
        assert "captured" in msg
        for status in needfix_store.VALID_TRANSITIONS.get("captured", ()):
            assert status in msg

    def test_cannot_skip_triage_for_accept(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        with pytest.raises(needfix_store.NeedFixConflictError) as exc_info:
            needfix_store.accept_needfix(init_store, r["id"])
        msg = str(exc_info.value)
        assert "captured" in msg
        for status in needfix_store.VALID_TRANSITIONS.get("captured", ()):
            assert status in msg

    def test_invalid_create_status_guidance(self, init_store: Path):
        with pytest.raises(needfix_store.NeedFixValidationError) as exc_info:
            needfix_store.add_needfix(
                init_store, title="T", description="D", status="open"
            )
        msg = str(exc_info.value)
        assert "open" in msg
        for status in needfix_store.STATUSES:
            assert status in msg

class TestCapturedUnverifiedGuard:
    """captured/unverified cannot convert -- must be accepted first."""

    def _dummy_create_task(self, card):
        return {"ok": True, "returncode": 0, "command": [], "stdout": "", "stderr": "", "task_id": "task-001"}

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
        return {"ok": True, "returncode": 0, "command": [], "stdout": "", "stderr": "", "task_id": "task-ok"}

    def _create_task_fail(self, card):
        raise RuntimeError("task creation failed")

    def _create_task_ok_no_id_key(self, card):
        """A fake exercising the real production shape: 'id' is never present."""
        return {"ok": True, "task_id": "task-no-id-key", "created": True}

    def _create_task_receipt_false(self, card):
        """Mirrors the real create_task() validation-failure receipt shape."""
        return {
            "ok": False,
            "returncode": 2,
            "command": [],
            "stdout": "",
            "stderr": "read_only_declaration_required",
        }

    def _create_task_receipt_ok_missing_task_id(self, card):
        return {"ok": True, "created": True}

    def _create_task_receipt_not_a_mapping(self, card):
        return "not-a-mapping"

    def _create_task_capture(self, card):
        self.captured_card = card
        return {"ok": True, "task_id": "task-captured"}

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

    def test_convert_extracts_task_id_without_guessing_id_fallback(self, init_store: Path):
        """The canonical receipt never carries an 'id' key -- only 'task_id'."""
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        result = needfix_store.convert_needfix(
            init_store, r["id"], self._create_task_ok_no_id_key
        )
        assert result["converted_task_id"] == "task-no-id-key"

    def test_convert_rejects_ok_false_receipt_with_real_reason(self, init_store: Path):
        """A canonical failure receipt (ok=False) surfaces its stderr reason and
        fails closed without stranding the NeedFix as task_created."""
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        with pytest.raises(needfix_store.NeedFixError, match="read_only_declaration_required"):
            needfix_store.convert_needfix(init_store, r["id"], self._create_task_receipt_false)
        nr = needfix_store.get_needfix(init_store, r["id"])
        assert nr["status"] == "accepted"
        assert nr["converted_task_id"] is None

    def test_convert_rejects_ambiguous_ok_true_missing_task_id(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        with pytest.raises(needfix_store.NeedFixError, match="ambiguous"):
            needfix_store.convert_needfix(
                init_store, r["id"], self._create_task_receipt_ok_missing_task_id
            )
        nr = needfix_store.get_needfix(init_store, r["id"])
        assert nr["status"] == "accepted"
        assert nr["converted_task_id"] is None

    def test_convert_rejects_non_mapping_receipt(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        with pytest.raises(needfix_store.NeedFixError, match="malformed"):
            needfix_store.convert_needfix(
                init_store, r["id"], self._create_task_receipt_not_a_mapping
            )
        nr = needfix_store.get_needfix(init_store, r["id"])
        assert nr["status"] == "accepted"
        assert nr["converted_task_id"] is None

    def test_convert_retry_after_success_is_idempotent_no_duplicate(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        c1 = needfix_store.convert_needfix(init_store, r["id"], self._create_task_capture)
        c2 = needfix_store.convert_needfix(init_store, r["id"], self._create_task_capture)
        assert c2["already_converted"] is True
        assert c2["converted_task_id"] == c1["converted_task_id"] == "task-captured"

    # -- Typed required-field validation (mirrors core._create_task_fn) ----

    def _validating_create_fn(self, card):
        """Simulate core._create_task_fn's explicit required-field extraction:
        missing/empty task_id, title, or objective must raise NeedFixError,
        never a raw KeyError."""
        for key in ("task_id", "title", "objective"):
            value = card.get(key)
            if not isinstance(value, str) or not value.strip():
                raise needfix_store.NeedFixError(
                    f"conversion card is missing required field {key!r}"
                )
        return {"ok": True, "task_id": card["task_id"]}

    def _card_builder_missing(self, field):
        """Return a task_card_builder that omits *field* from the card."""
        def builder(snapshot):
            card = {
                "task_id": f"needfix-{snapshot['id']}",
                "title": snapshot["title"],
                "objective": snapshot["description"],
            }
            del card[field]
            return card
        return builder

    @pytest.mark.parametrize("missing_field", ["task_id", "title", "objective"])
    def test_convert_rejects_missing_required_card_field(self, init_store: Path, missing_field):
        """A conversion card missing any required field must raise NeedFixError
        and compensate the claim -- never a raw KeyError."""
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        with pytest.raises(needfix_store.NeedFixError, match="missing required field"):
            needfix_store.convert_needfix(
                init_store,
                r["id"],
                self._validating_create_fn,
                task_card_builder=self._card_builder_missing(missing_field),
            )
        nr = needfix_store.get_needfix(init_store, r["id"])
        assert nr["status"] == "accepted"  # compensated back
        assert nr["converted_task_id"] is None

    @pytest.mark.parametrize("field", ["task_id", "title", "objective"])
    def test_convert_rejects_empty_required_card_field(self, init_store: Path, field):
        """A conversion card with an empty/whitespace required field must fail
        closed with a typed error, just like a missing field."""
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])

        def empty_field_builder(snapshot):
            card = {
                "task_id": f"needfix-{snapshot['id']}",
                "title": snapshot["title"],
                "objective": snapshot["description"],
            }
            card[field] = "   "  # whitespace-only → invalid
            return card

        with pytest.raises(needfix_store.NeedFixError, match="missing required field"):
            needfix_store.convert_needfix(
                init_store,
                r["id"],
                self._validating_create_fn,
                task_card_builder=empty_field_builder,
            )


class TestDefaultTaskCard:
    """Scope defaults never guess launch identity or writable outputs."""

    def _create_task_capture(self, card):
        self.captured_card = card
        return {"ok": True, "task_id": "task-captured"}

    def test_default_writable_card_requires_explicit_launch_contract(self, init_store: Path):
        r = needfix_store.capture_proposal(
            init_store,
            title="T",
            description="D",
            scope_files=["src/aiworkhub/foo.py"],
        )
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        with pytest.raises(needfix_store.NeedFixValidationError, match="launch contract"):
            needfix_store.preview_convert(init_store, r["id"])

    def test_default_card_does_not_infer_required_outputs_from_scope_files(self, init_store: Path):
        """Scope files may include unchanged evidence/supporting context --
        forcing all of them to be required would produce false
        required_output_unchanged failures, so the default card leaves
        required_outputs unset entirely."""
        r = needfix_store.capture_proposal(
            init_store,
            title="T",
            description="D",
            scope_files=["src/aiworkhub/foo.py", "docs/evidence.md"],
        )
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        card = needfix_store.default_task_card(
            needfix_store.get_needfix(init_store, r["id"])
        )
        assert card["allowed_writes"] == ["src/aiworkhub/foo.py", "docs/evidence.md"]
        assert not card.get("required_outputs")

    def test_default_card_is_read_only_without_scope_files(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        card = needfix_store.normalize_task_plan(
            needfix_store.get_needfix(init_store, r["id"]),
            {"runner": "deepseek_v4-pro", "topic": "needfix_readonly"},
        )
        assert card["read_only"] is True
        assert not card.get("allowed_writes")
        assert not card.get("required_outputs")
        assert card.get("max_live_tokens") is None


class TestNeedFixConversionPublicPlanContract:
    """Public task_plan contract: strict validation, preview digest binding,
    default read/write vs required-output distinction, read_only intent,
    and uncapped-unless-explicit max_live_tokens -- exercised through the
    same ``needfix_store`` primitives ``core.needfix_preview_convert`` /
    ``core.needfix_convert`` delegate to."""

    def _create_task_capture(self, card):
        self.captured_card = card
        return {"ok": True, "task_id": "task-plan-committed"}

    def _accepted_with_scope(self, init_store: Path, scope_files=None):
        r = needfix_store.capture_proposal(
            init_store, title="T", description="D", scope_files=scope_files
        )
        needfix_store.triage_needfix(init_store, r["id"])
        return needfix_store.accept_needfix(init_store, r["id"])

    def test_preview_returns_normalized_card_and_digest(self, init_store: Path):
        r = self._accepted_with_scope(init_store, ["src/aiworkhub/foo.py"])
        pv = needfix_store.preview_convert(
            init_store,
            r["id"],
            {
                "runner": "deepseek_v4-pro",
                "topic": "needfix_fix",
                "required_outputs": ["src/aiworkhub/foo.py"],
            },
        )
        assert pv["claimable"] is True
        assert pv["task_plan"]["allowed_writes"] == ["src/aiworkhub/foo.py"]
        assert pv["task_plan"]["required_outputs"] == ["src/aiworkhub/foo.py"]
        assert pv["plan_digest"] == needfix_store.plan_digest(pv["task_plan"])

    def test_writable_preview_without_required_outputs_fails_before_task_creation(
        self, init_store: Path
    ):
        """NF-62: preview must reject the same card launch would reject."""

        r = self._accepted_with_scope(init_store, ["src/aiworkhub/foo.py"])
        with pytest.raises(
            needfix_store.NeedFixValidationError,
            match="writable needfix conversion requires explicit non-empty",
        ):
            needfix_store.preview_convert(
                init_store,
                r["id"],
                {
                    "runner": "deepseek_v4-pro",
                    "topic": "needfix_fix",
                },
            )
        unchanged = needfix_store.get_needfix(init_store, r["id"])
        assert unchanged["status"] == "accepted"
        assert unchanged["converted_task_id"] is None

    def test_preview_reflects_caller_supplied_task_plan_overrides(self, init_store: Path):
        r = self._accepted_with_scope(init_store)
        plan = {
            "acceptance": ["Custom acceptance"],
            "priority": "high",
            "runner": "deepseek_v4-pro",
            "topic": "needfix_research",
        }
        pv = needfix_store.preview_convert(init_store, r["id"], plan)
        assert pv["task_plan"]["acceptance"] == ["Custom acceptance"]
        assert pv["task_plan"]["priority"] == "high"

    def test_commit_forwards_full_task_plan_to_created_card(self, init_store: Path):
        r = self._accepted_with_scope(init_store)
        plan = {
            "allowed_writes": ["src/a.py", "src/b.py"],
            "required_outputs": ["src/a.py"],
            "acceptance": ["Do the thing"],
            "validation": ["python3 -m pytest -q tests/test_a.py"],
            "depends_on": ["other-task"],
            "runner": "codex",
            "topic": "custom_topic",
            "task_type": "code",
            "read_only": False,
        }
        pv = needfix_store.preview_convert(init_store, r["id"], plan)
        card = needfix_store.normalize_task_plan(
            needfix_store.get_needfix(init_store, r["id"]), plan
        )
        assert card == pv["task_plan"]
        needfix_store.convert_needfix(
            init_store,
            r["id"],
            self._create_task_capture,
            task_card_builder=lambda snapshot: needfix_store.normalize_task_plan(snapshot, plan),
        )
        committed = self.captured_card
        for field, value in plan.items():
            assert committed[field] == value
        assert committed["task_id"] == f"needfix-{r['id']}"

    def test_core_conversion_bridge_preserves_quality_contract(self, init_store, monkeypatch):
        from aiworkhub import core, task_templates

        r = self._accepted_with_scope(
            init_store,
            ["src/aiworkhub/foo.py", "tests/test_foo.py"],
        )
        plan = {
            "runner": "codex_gpt-5.6-sol",
            "topic": "needfix_fix",
            "required_outputs": ["src/aiworkhub/foo.py"],
            "validation": [
                "python3 -m pytest -q tests/test_foo.py",
                "git diff --check",
            ],
            "validation_roles": ["reproduction", "regression"],
            "work_kind": "bugfix",
            "risk_tier": "critical",
        }
        preview = needfix_store.preview_convert(init_store, r["id"], plan)
        captured = {}

        def fake_create_task(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "task_id": f"needfix-{r['id']}"}

        monkeypatch.setattr(core, "repo_root", lambda: init_store)
        monkeypatch.setattr(core, "create_task", fake_create_task)
        result = core.needfix_convert(
            r["id"],
            task_plan=plan,
            plan_digest=preview["plan_digest"],
        )

        assert result["converted_task_id"] == f"needfix-{r['id']}"
        assert captured["validation_roles"] == ["reproduction", "regression"]
        assert captured["work_kind"] == "bugfix"
        assert captured["risk_tier"] == "critical"
        assert captured["custom_template_escape"] == (
            task_templates.AUDITED_CUSTOM_ESCAPE
        )

    def test_malformed_plan_unknown_field_fails_closed_without_mutation(self, init_store: Path):
        r = self._accepted_with_scope(init_store)
        with pytest.raises(needfix_store.NeedFixValidationError, match="unsupported"):
            needfix_store.preview_convert(init_store, r["id"], {"task_id": "guessed-id"})
        nr = needfix_store.get_needfix(init_store, r["id"])
        assert nr["status"] == "accepted"

    def test_malformed_plan_wrong_type_fails_closed(self, init_store: Path):
        r = self._accepted_with_scope(init_store)
        with pytest.raises(needfix_store.NeedFixValidationError):
            needfix_store.preview_convert(init_store, r["id"], {"allowed_writes": "not-a-list"})

    def test_malformed_plan_not_a_mapping_fails_closed(self, init_store: Path):
        r = self._accepted_with_scope(init_store)
        with pytest.raises(needfix_store.NeedFixValidationError):
            needfix_store.preview_convert(init_store, r["id"], ["not", "a", "mapping"])

    def test_ambiguous_read_only_contradiction_fails_closed(self, init_store: Path):
        r = self._accepted_with_scope(init_store)
        with pytest.raises(needfix_store.NeedFixValidationError, match="contradictory"):
            needfix_store.normalize_task_plan(
                needfix_store.get_needfix(init_store, r["id"]),
                {"read_only": True, "allowed_writes": ["src/a.py"]},
            )

    def test_read_only_intent_overrides_scope_derived_writes(self, init_store: Path):
        r = self._accepted_with_scope(init_store, ["src/aiworkhub/foo.py"])
        card = needfix_store.normalize_task_plan(
            needfix_store.get_needfix(init_store, r["id"]),
            {
                "read_only": True,
                "runner": "deepseek_v4-pro",
                "topic": "needfix_readonly",
            },
        )
        assert card["read_only"] is True
        assert not card.get("allowed_writes")
        assert not card.get("required_outputs")
        assert not card.get("validation")

    def test_digest_drift_fails_closed_and_compensates(self, init_store: Path):
        """A commit bound to a digest that no longer matches the freshly
        normalized card (e.g. the NeedFix changed after preview) must fail
        closed instead of silently committing a different plan, restoring
        the NeedFix to its pre-claim status."""
        r = self._accepted_with_scope(init_store)
        readonly_plan = {
            "runner": "deepseek_v4-pro",
            "topic": "needfix_research",
        }
        pv = needfix_store.preview_convert(init_store, r["id"], readonly_plan)
        stale_digest = pv["plan_digest"]
        needfix_store.update_needfix(init_store, r["id"], title="Changed after preview")

        def _digest_bound_builder(snapshot):
            card = needfix_store.normalize_task_plan(snapshot, readonly_plan)
            actual = needfix_store.plan_digest(card)
            if actual != stale_digest:
                raise needfix_store.NeedFixConflictError("task_plan digest mismatch")
            return card

        with pytest.raises(needfix_store.NeedFixConflictError, match="digest mismatch"):
            needfix_store.convert_needfix(
                init_store, r["id"], self._create_task_capture, task_card_builder=_digest_bound_builder
            )
        nr = needfix_store.get_needfix(init_store, r["id"])
        assert nr["status"] == "accepted"
        assert nr["converted_task_id"] is None

    def test_plan_based_conversion_retry_is_idempotent_no_duplicate(self, init_store: Path):
        r = self._accepted_with_scope(init_store)
        plan = {
            "acceptance": ["Resolve it"],
            "runner": "deepseek_v4-pro",
            "topic": "needfix_research",
        }
        builder = lambda snapshot: needfix_store.normalize_task_plan(snapshot, plan)
        c1 = needfix_store.convert_needfix(
            init_store, r["id"], self._create_task_capture, task_card_builder=builder
        )
        c2 = needfix_store.convert_needfix(
            init_store, r["id"], self._create_task_capture, task_card_builder=builder
        )
        assert c1["already_converted"] is False
        assert c2["already_converted"] is True
        assert c2["converted_task_id"] == c1["converted_task_id"] == "task-plan-committed"

    def test_uncapped_unless_plan_explicitly_sets_max_live_tokens(self, init_store: Path):
        r = self._accepted_with_scope(init_store)
        default_card = needfix_store.normalize_task_plan(
            needfix_store.get_needfix(init_store, r["id"]),
            {"runner": "deepseek_v4-pro", "topic": "needfix_research"},
        )
        assert default_card.get("max_live_tokens") is None
        capped_card = needfix_store.normalize_task_plan(
            needfix_store.get_needfix(init_store, r["id"]),
            {
                "max_live_tokens": 5000,
                "runner": "deepseek_v4-pro",
                "topic": "needfix_research",
            },
        )
        assert capped_card["max_live_tokens"] == 5000

    def test_dashboard_preview_forwards_task_plan(self, monkeypatch):
        from aiworkhub import core, dashboard_mcp_app

        calls = []

        def fake(needfix_id, *, task_plan=None):
            calls.append((needfix_id, task_plan))
            return {"needfix_id": needfix_id, "task_plan": {"title": "x"}, "plan_digest": "abc"}

        monkeypatch.setattr(core, "needfix_preview_convert", fake)
        plan = {"acceptance": ["Custom"]}
        result = dashboard_mcp_app.needfix_convert_preview_view("NF-2026-00001", plan)
        assert calls == [("NF-2026-00001", plan)]
        assert result["ok"] is True
        assert result["plan_digest"] == "abc"

    def test_dashboard_commit_requires_plan_digest_when_plan_given(self):
        from aiworkhub import dashboard_mcp_app

        result = dashboard_mcp_app.needfix_convert_commit_view(
            "NF-2026-00001", confirm=True, task_plan={"acceptance": ["Custom"]}
        )
        assert result["ok"] is False
        assert result["error"] == "needfix_conversion_plan_digest_required"

    def test_dashboard_commit_forwards_task_plan_and_digest(self, monkeypatch):
        from aiworkhub import core, dashboard_mcp_app

        calls = []

        def fake(needfix_id, *, task_plan=None, plan_digest=None):
            calls.append((needfix_id, task_plan, plan_digest))
            return {"needfix_id": needfix_id, "converted_task_id": "task-x", "already_converted": False}

        monkeypatch.setattr(core, "needfix_convert", fake)
        plan = {"acceptance": ["Custom"]}
        result = dashboard_mcp_app.needfix_convert_commit_view(
            "NF-2026-00001", confirm=True, task_plan=plan, plan_digest="deadbeef"
        )
        assert calls == [("NF-2026-00001", plan, "deadbeef")]
        assert result["ok"] is True
        assert result["converted_task_id"] == "task-x"

    def test_dashboard_commit_still_requires_confirmation(self, monkeypatch):
        from aiworkhub import core, dashboard_mcp_app

        monkeypatch.setattr(
            core, "needfix_convert", lambda *a, **k: pytest.fail("must not be called")
        )
        result = dashboard_mcp_app.needfix_convert_commit_view("NF-2026-00001")
        assert result["ok"] is False
        assert result["error"] == "needfix_conversion_confirmation_required"

    def test_dashboard_commit_default_plan_digest_required(self):
        """Every confirmed non-idempotent conversion binds the commit to the
        ``plan_digest`` the matching preview returned. The default-card path
        (no explicit ``task_plan``) is held to that same digest discipline,
        not exempted from it -- a confirmed commit without ``plan_digest``
        fails closed instead of silently binding to an unconfirmed plan."""
        from aiworkhub import dashboard_mcp_app

        result = dashboard_mcp_app.needfix_convert_commit_view("NF-2026-00001", confirm=True)
        assert result["ok"] is False
        assert result["error"] == "needfix_conversion_plan_digest_required"

    def test_dashboard_commit_bound_default_plan_succeeds_with_correct_digest(self, monkeypatch):
        """A committed default card carries the matching ``plan_digest`` to
        core; core's own hash check then binds the commit to the scope the
        manager actually previewed."""
        from aiworkhub import core, dashboard_mcp_app

        calls = []

        def fake(needfix_id, *, task_plan=None, plan_digest=None):
            calls.append((needfix_id, task_plan, plan_digest))
            return {"needfix_id": needfix_id, "converted_task_id": "task-y", "already_converted": False}

        monkeypatch.setattr(core, "needfix_convert", fake)
        result = dashboard_mcp_app.needfix_convert_commit_view(
            "NF-2026-00001", confirm=True, plan_digest="deadbeef"
        )
        assert calls == [("NF-2026-00001", None, "deadbeef")]
        assert result["ok"] is True

    def test_dashboard_commit_needfix_drift_rejects_stale_digest(self, monkeypatch):
        """When the NeedFix changed between preview and commit, core's
        digest check raises a NeedFixConflictError; the dashboard surfaces
        the rejection as a non-mutating failed response instead of creating
        a task for a different plan."""
        from aiworkhub import core, dashboard_mcp_app, needfix_store

        def fake(needfix_id, *, task_plan=None, plan_digest=None):
            raise needfix_store.NeedFixConflictError("task_plan digest mismatch")

        monkeypatch.setattr(core, "needfix_convert", fake)
        result = dashboard_mcp_app.needfix_convert_commit_view(
            "NF-2026-00001", confirm=True, plan_digest="stale"
        )
        assert result["ok"] is False
        assert "digest mismatch" in result["error"]


class TestLinkExistingTask:
    """Explicit manager-only link of a NeedFix to an already-existing,
    same-repository, manager-accepted canonical task (in-flight or landed)."""

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

    def test_link_existing_task_processing_is_owned_not_closed(self, init_store: Path):
        """Moved to the derived-state contract (was ``unfinished_fails_closed``).

        BEFORE: linking to a ``processing`` (unfinished) task raised
        ``NeedFixConflictError`` and the NeedFix stayed ``accepted``. Refusing
        to link to anything unfinished was the only guard against a NeedFix
        that silently looked handled forever when its card never landed.

        NOW: linking to a ``processing`` card succeeds. The record's derived
        state is OWNED (hidden from the active list) and NOT CLOSED, and
        cancelling that card returns the record to the active list.

        PROTECTION NOW CARRIED BY:
        ``test_owned_status_is_hidden`` in tests/test_needfix_store.py (an
        in-flight card derives to OWNED, not CLOSED), and
        ``test_active_listing_excludes_owned_and_closed`` /
        ``test_active_state_is_derived_at_read_time_and_self_heals`` in
        tests/test_needfix_active_state_derived_from_card.py (owned records are
        hidden; a cancelled card re-enters the active list).
        """
        r = self._accepted_needfix(init_store)
        get_task_fn, status_fn = self._tasks(**{"task-1": {"status": "processing"}})
        result = needfix_store.link_existing_task(
            init_store, r["id"], "task-1", get_task_fn, status_fn
        )
        assert result["converted_task_id"] == "task-1"
        nr = needfix_store.get_needfix(init_store, r["id"])
        assert nr["status"] == "task_created"
        assert nr["converted_task_id"] == "task-1"

        derived = needfix_store.list_needfix(
            init_store, get_task_fn=get_task_fn, canonical_status_fn=status_fn
        )
        row = [d for d in derived if d["id"] == r["id"]][0]
        assert row["derived_state"] == "owned"
        assert row["derived_state"] != "closed"
        assert row["active"] is False

        # Cancelling the owning card returns the record to the active list.
        cancelled_get, cancelled_status = self._tasks(**{"task-1": {"status": "cancelled"}})
        active = needfix_store.list_active_needfix(
            init_store, get_task_fn=cancelled_get, canonical_status_fn=cancelled_status
        )
        assert r["id"] in {i["id"] for i in active["items"]}

    def test_link_existing_task_review_is_owned_not_closed(self, init_store: Path):
        """Moved to the derived-state contract (was ``unaccepted_review_fails_closed``).

        BEFORE: linking to a ``review`` (unaccepted) task raised
        ``NeedFixConflictError``. Same protection as the ``processing`` case:
        an unfinished card could never be linked, so a NeedFix could not sit
        silently handled forever.

        NOW: linking to a ``review`` card succeeds and derives OWNED (not
        CLOSED), so it is hidden from the active list only while the review
        card is still in flight.

        PROTECTION NOW CARRIED BY:
        ``test_owned_status_is_hidden`` in tests/test_needfix_store.py and
        ``test_active_listing_excludes_owned_and_closed`` in
        tests/test_needfix_active_state_derived_from_card.py.
        """
        r = self._accepted_needfix(init_store)
        get_task_fn, status_fn = self._tasks(**{"task-1": {"status": "review"}})
        result = needfix_store.link_existing_task(
            init_store, r["id"], "task-1", get_task_fn, status_fn
        )
        assert result["converted_task_id"] == "task-1"

        derived = needfix_store.list_needfix(
            init_store, get_task_fn=get_task_fn, canonical_status_fn=status_fn
        )
        row = [d for d in derived if d["id"] == r["id"]][0]
        assert row["derived_state"] == "owned"
        assert row["derived_state"] != "closed"
        assert row["active"] is False

    def test_link_existing_task_archived_is_closed(self, init_store: Path):
        """Moved to the derived-state contract (was ``archived_without_acceptance_fails_closed``).

        BEFORE: linking to an ``archived`` (never-accepted) card raised
        ``NeedFixConflictError``. The guard was that a card which never landed
        its fix could not look handled.

        NOW: linking to an ``archived`` card succeeds; the record's derived
        state is CLOSED (the fix is recorded as landed, so it stays hidden).

        PROTECTION NOW CARRIED BY:
        ``test_closed_status_is_hidden_as_fixed`` in tests/test_needfix_store.py
        (archived derives to CLOSED) and
        ``test_active_listing_excludes_owned_and_closed`` in
        tests/test_needfix_active_state_derived_from_card.py (closed records
        are excluded from the active list).
        """
        r = self._accepted_needfix(init_store)
        get_task_fn, status_fn = self._tasks(**{"task-1": {"status": "archived"}})
        result = needfix_store.link_existing_task(
            init_store, r["id"], "task-1", get_task_fn, status_fn
        )
        assert result["converted_task_id"] == "task-1"

        derived = needfix_store.list_needfix(
            init_store, get_task_fn=get_task_fn, canonical_status_fn=status_fn
        )
        row = [d for d in derived if d["id"] == r["id"]][0]
        assert row["derived_state"] == "closed"
        assert row["active"] is False

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

    def test_reopen_superseded_task_link_is_atomic_and_audited(self, init_store: Path):
        r = self._accepted_needfix(init_store)
        finished_get, status_fn = self._tasks(**{"task-1": {"status": "finished"}})
        needfix_store.link_existing_task(
            init_store, r["id"], "task-1", finished_get, status_fn
        )
        archived_get, archived_status = self._tasks(
            **{
                "task-1": {
                    "status": "archived",
                    "archive_operation": "superseded",
                }
            }
        )

        reopened = needfix_store.reopen_superseded_task_link(
            init_store,
            r["id"],
            get_task_fn=archived_get,
            canonical_status_fn=archived_status,
            reason="Replacement task will be planned from the rebased residual.",
        )

        assert reopened["status"] == "accepted"
        assert reopened["converted_task_id"] is None
        event = needfix_store.list_events(init_store, r["id"], limit=1)[0]
        assert event["event"] == "superseded_task_link_reopened"
        assert event["detail"]["prior_converted_task_id"] == "task-1"

    @pytest.mark.parametrize(
        ("status", "archive_operation"),
        [
            ("processing", ""),
            ("review", ""),
            ("finished", ""),
            ("archived", "quarantined"),
        ],
    )
    def test_reopen_superseded_task_link_rejects_non_superseded_authority(
        self, init_store: Path, status: str, archive_operation: str
    ):
        r = self._accepted_needfix(init_store)
        finished_get, status_fn = self._tasks(**{"task-1": {"status": "finished"}})
        needfix_store.link_existing_task(
            init_store, r["id"], "task-1", finished_get, status_fn
        )
        current_get, current_status = self._tasks(
            **{
                "task-1": {
                    "status": status,
                    "archive_operation": archive_operation,
                }
            }
        )

        with pytest.raises(needfix_store.NeedFixConflictError):
            needfix_store.reopen_superseded_task_link(
                init_store,
                r["id"],
                get_task_fn=current_get,
                canonical_status_fn=current_status,
                reason="Attempted reconciliation.",
            )

        unchanged = needfix_store.get_needfix(init_store, r["id"])
        assert unchanged["status"] == "task_created"
        assert unchanged["converted_task_id"] == "task-1"


    def test_reopen_ordinary_archived_unaccepted_task_link(self, init_store: Path):
        r = self._accepted_needfix(init_store)
        finished_get, status_fn = self._tasks(**{"task-1": {"status": "finished"}})
        needfix_store.link_existing_task(
            init_store, r["id"], "task-1", finished_get, status_fn
        )
        archived_get, archived_status = self._tasks(
            **{
                "task-1": {
                    "id": "task-1",
                    "status": "archived",
                    "archive_operation": "archived",
                }
            }
        )

        reopened = needfix_store.reopen_superseded_task_link(
            init_store,
            r["id"],
            get_task_fn=archived_get,
            canonical_status_fn=archived_status,
            actor="inventory-repair",
            reason="Canonical inventory confirms an ordinary archived task.",
        )

        assert reopened["status"] == "accepted"
        assert reopened["converted_task_id"] is None
        event = needfix_store.list_events(init_store, r["id"], limit=1)[0]
        assert event["event"] == "archived_task_link_reopened"
        assert event["detail"]["actor"] == "inventory-repair"
        assert event["detail"]["archive_operation"] == "archived"

    def test_reopen_archived_accepted_task_fails_closed(self, init_store: Path):
        r = self._accepted_needfix(init_store)
        finished_get, status_fn = self._tasks(**{"task-1": {"status": "finished"}})
        needfix_store.link_existing_task(
            init_store, r["id"], "task-1", finished_get, status_fn
        )
        archived_get, archived_status = self._tasks(
            **{
                "task-1": {
                    "id": "task-1",
                    "status": "archived",
                    "archive_operation": "archived",
                    "accepted_at": "2026-09-03T00:00:00Z",
                }
            }
        )

        with pytest.raises(needfix_store.NeedFixConflictError):
            needfix_store.reopen_superseded_task_link(
                init_store,
                r["id"],
                get_task_fn=archived_get,
                canonical_status_fn=archived_status,
                reason="Must not reopen accepted work.",
            )

        unchanged = needfix_store.get_needfix(init_store, r["id"])
        assert unchanged["status"] == "task_created"
        assert unchanged["converted_task_id"] == "task-1"

    def test_reopen_rolls_back_if_audit_event_fails(
        self, init_store: Path, monkeypatch: pytest.MonkeyPatch
    ):
        r = self._accepted_needfix(init_store)
        finished_get, status_fn = self._tasks(**{"task-1": {"status": "finished"}})
        needfix_store.link_existing_task(
            init_store, r["id"], "task-1", finished_get, status_fn
        )
        archived_get, archived_status = self._tasks(
            **{
                "task-1": {
                    "id": "task-1",
                    "status": "archived",
                    "archive_operation": "archived",
                }
            }
        )

        def fail_event(*_args, **_kwargs):
            raise RuntimeError("simulated audit write failure")

        monkeypatch.setattr(needfix_store, "_record_event", fail_event)
        with pytest.raises(RuntimeError, match="simulated audit write failure"):
            needfix_store.reopen_superseded_task_link(
                init_store,
                r["id"],
                get_task_fn=archived_get,
                canonical_status_fn=archived_status,
                reason="Prove row and event are one transaction.",
            )

        unchanged = needfix_store.get_needfix(init_store, r["id"])
        assert unchanged["status"] == "task_created"
        assert unchanged["converted_task_id"] == "task-1"
        assert unchanged["reopen_generation"] == 0


class TestReopenGenerationLifecycle:
    """Deterministic ``-rN`` successor task IDs driven by the exact durable
    reopen event count, plus the fail-closed ``reopen_generation`` migration."""

    @staticmethod
    def _create_task_fn(card):
        return {"ok": True, "task_id": card["task_id"]}

    @staticmethod
    def _archived_superseded_get(task_id):
        return {"id": task_id, "status": "archived", "archive_operation": "superseded"}

    @staticmethod
    def _canonical_status_fn(task):
        return task["status"]

    def _accepted(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        return needfix_store.accept_needfix(init_store, r["id"])

    def _convert(self, init_store: Path, nid: str):
        return needfix_store.convert_needfix(init_store, nid, self._create_task_fn)

    def _reopen(self, init_store: Path, nid: str):
        return needfix_store.reopen_superseded_task_link(
            init_store,
            nid,
            get_task_fn=self._archived_superseded_get,
            canonical_status_fn=self._canonical_status_fn,
            reason="Replacement task planned from the rebased residual.",
        )

    def _set_generation(self, init_store: Path, nid: str, value):
        conn = needfix_store._connect(init_store)
        try:
            conn.execute(
                "UPDATE needfix SET reopen_generation = ? WHERE id = ?", (value, nid)
            )
        finally:
            conn.close()

    def test_generation_zero_uses_canonical_task_id(self, init_store: Path):
        r = self._accepted(init_store)
        assert r["reopen_generation"] == 0
        conv = self._convert(init_store, r["id"])
        assert conv["converted_task_id"] == f"needfix-{r['id']}"

    def test_reopen_derives_deterministic_collision_free_successor_id(self, init_store: Path):
        r = self._accepted(init_store)
        self._convert(init_store, r["id"])
        first = needfix_store.get_needfix(init_store, r["id"])["converted_task_id"]

        reopened = self._reopen(init_store, r["id"])
        assert reopened["reopen_generation"] == 1
        assert reopened["converted_task_id"] is None

        conv = self._convert(init_store, r["id"])
        assert conv["converted_task_id"] == f"needfix-{r['id']}-r1"
        assert conv["converted_task_id"] != first

        self._reopen(init_store, r["id"])
        conv2 = self._convert(init_store, r["id"])
        assert conv2["converted_task_id"] == f"needfix-{r['id']}-r2"
        assert conv2["converted_task_id"] not in (first, conv["converted_task_id"])

    def test_repeated_preview_returns_identical_successor_and_digest(self, init_store: Path):
        r = self._accepted(init_store)
        self._convert(init_store, r["id"])
        self._reopen(init_store, r["id"])
        plan = {"runner": "x", "topic": "y", "required_outputs": ["f.py"]}

        p1 = needfix_store.preview_convert(init_store, r["id"], plan)
        p2 = needfix_store.preview_convert(init_store, r["id"], plan)

        assert p1["task_plan"]["task_id"] == f"needfix-{r['id']}-r1"
        assert p1["plan_digest"] == p2["plan_digest"]

    def test_convert_is_exactly_once_and_reconciles_lost_ack(self, init_store: Path):
        r = self._accepted(init_store)
        self._convert(init_store, r["id"])
        self._reopen(init_store, r["id"])

        conv = self._convert(init_store, r["id"])
        assert conv["converted_task_id"] == f"needfix-{r['id']}-r1"
        assert conv["already_converted"] is False

        retry = self._convert(init_store, r["id"])
        assert retry["already_converted"] is True
        assert retry["converted_task_id"] == f"needfix-{r['id']}-r1"

    def test_archived_predecessor_lineage_remains_auditable(self, init_store: Path):
        r = self._accepted(init_store)
        self._convert(init_store, r["id"])
        self._reopen(init_store, r["id"])
        self._convert(init_store, r["id"])
        self._reopen(init_store, r["id"])

        reopen_events = [
            e
            for e in needfix_store.list_events(init_store, r["id"], limit=100)
            if e["event"] == "superseded_task_link_reopened"
        ]
        assert len(reopen_events) == 2
        # list_events is newest-first.
        assert reopen_events[0]["detail"]["prior_converted_task_id"] == f"needfix-{r['id']}-r1"
        assert reopen_events[0]["detail"]["reopen_generation"] == 2
        assert reopen_events[1]["detail"]["prior_converted_task_id"] == f"needfix-{r['id']}"
        assert reopen_events[1]["detail"]["reopen_generation"] == 1

    def test_legacy_schema_backfills_generation_from_durable_events(self, repo_root: Path):
        hub = repo_root / ".aiworkhub" / "tasking"
        hub.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(hub / "needfix.sqlite"))
        conn.executescript(
            """
            CREATE TABLE needfix (
                id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL, title TEXT NOT NULL,
                description TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'other',
                severity TEXT NOT NULL DEFAULT 'medium', tags_json TEXT NOT NULL DEFAULT '[]',
                scope TEXT, scope_files_json TEXT DEFAULT '[]',
                scope_symbols_json TEXT DEFAULT '[]', provenance_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                readiness_score INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
                duplicate_parent_id TEXT, converted_task_id TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, task_planned_at TEXT,
                resolved_at TEXT, archived_at TEXT,
                UNIQUE(dedupe_key, status) ON CONFLICT IGNORE
            );
            CREATE TABLE needfix_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, needfix_id TEXT NOT NULL,
                event TEXT NOT NULL, detail_json TEXT, created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO needfix (id, dedupe_key, title, description, provenance_json, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "NF-2026-00001",
                "dk",
                "t",
                "d",
                "{}",
                "accepted",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        for _ in range(3):
            conn.execute(
                "INSERT INTO needfix_events (needfix_id, event, detail_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("NF-2026-00001", "superseded_task_link_reopened", "{}", "2026-01-01T00:00:00+00:00"),
            )
        conn.commit()
        conn.close()

        needfix_store.initialize_repository(repo_root)
        row = needfix_store.get_needfix(repo_root, "NF-2026-00001")
        assert row["reopen_generation"] == 3

    def test_migration_is_idempotent(self, init_store: Path):
        r = self._accepted(init_store)
        needfix_store.initialize_repository(init_store)
        assert needfix_store.get_needfix(init_store, r["id"])["reopen_generation"] == 0

    def test_migration_concurrent_winner_is_benign(self, monkeypatch):
        reads = {"n": 0}
        monkeypatch.setattr(
            needfix_store,
            "_column_exists",
            lambda conn, table, column: (reads.__setitem__("n", reads["n"] + 1) or reads["n"] > 1),
        )

        class FakeConn:
            def execute(self, sql, params=()):
                s = sql.lstrip().upper()
                if s.startswith("ALTER TABLE"):
                    raise sqlite3.OperationalError("duplicate column name: reopen_generation")
                if s.startswith("UPDATE"):
                    return None
                raise AssertionError(f"unexpected execute: {sql}")

        # Must not raise: a real concurrent winner is the only benign case.
        needfix_store._migrate_needfix_schema(FakeConn())

    def test_migration_reraise_unrelated_operational_error(self, monkeypatch):
        monkeypatch.setattr(needfix_store, "_column_exists", lambda conn, table, column: False)

        class FakeConn:
            def execute(self, sql, params=()):
                if sql.lstrip().upper().startswith("ALTER TABLE"):
                    raise sqlite3.OperationalError("database is locked")
                raise AssertionError(f"unexpected execute: {sql}")

        with pytest.raises(sqlite3.OperationalError):
            needfix_store._migrate_needfix_schema(FakeConn())

    def test_mismatch_stored_greater_than_events_fails_closed(self, init_store: Path):
        r = self._accepted(init_store)
        self._convert(init_store, r["id"])
        self._reopen(init_store, r["id"])  # generation 1, one durable event
        self._set_generation(init_store, r["id"], 5)
        with pytest.raises(needfix_store.NeedFixConflictError):
            needfix_store.get_needfix(init_store, r["id"])

    def test_mismatch_stored_less_than_events_fails_closed(self, init_store: Path):
        r = self._accepted(init_store)
        self._convert(init_store, r["id"])
        self._reopen(init_store, r["id"])
        self._convert(init_store, r["id"])
        self._reopen(init_store, r["id"])  # generation 2, two durable events
        self._set_generation(init_store, r["id"], 1)
        with pytest.raises(needfix_store.NeedFixConflictError):
            needfix_store.get_needfix(init_store, r["id"])

    @pytest.mark.parametrize("bad", ["abc", 2.5, -3])
    def test_invalid_generation_values_fail_closed(self, init_store: Path, bad):
        r = self._accepted(init_store)
        self._set_generation(init_store, r["id"], bad)
        with pytest.raises(needfix_store.NeedFixError):
            needfix_store.get_needfix(init_store, r["id"])

    def test_successor_task_id_rejects_bool_and_invalid_generations(self):
        assert needfix_store.successor_task_id("NF-2026-00001", 0) == "needfix-NF-2026-00001"
        assert needfix_store.successor_task_id("NF-2026-00001", 3) == "needfix-NF-2026-00001-r3"
        for bad in (True, False, -1, "2", 2.5):
            with pytest.raises(needfix_store.NeedFixError):
                needfix_store.successor_task_id("NF-2026-00001", bad)


class TestManagerVerifiedResolution:
    def _accepted(self, init_store: Path, *, readiness_score: int = 100, evidence=None, refs=None):
        row = needfix_store.capture_proposal(
            init_store,
            title="Canonical fix",
            description="Manager verified this fix directly on canonical HEAD.",
            evidence=evidence if evidence is not None else {"validation": "pytest passed"},
            evidence_refs=refs if refs is not None else ["commit:0123456789abcdef"],
        )
        needfix_store.triage_needfix(init_store, row["id"])
        return needfix_store.accept_needfix(
            init_store, row["id"], readiness_score=readiness_score
        )

    def test_resolve_verified_closes_accepted_evidenced_item(self, init_store: Path):
        row = self._accepted(init_store)
        resolved = needfix_store.resolve_verified_needfix(
            init_store, row["id"], resolution_note="Verified focused and full suites on HEAD."
        )
        assert resolved["status"] == "resolved"
        assert resolved["resolved_at"]
        assert resolved["converted_task_id"] is None
        events = needfix_store.list_events(init_store, row["id"])
        assert events[0]["event"] == "manager_verified_resolved"
        assert events[0]["detail"]["evidence_ref_count"] == 1

    @pytest.mark.parametrize(
        ("readiness_score", "evidence", "refs", "message"),
        [
            (99, {"validation": "passed"}, ["commit:abc1234"], "readiness_score=100"),
            (100, {}, ["commit:abc1234"], "durable evidence"),
            (100, {"validation": "passed"}, [], "evidence_refs"),
        ],
    )
    def test_resolve_verified_fails_closed_without_proof(
        self, init_store: Path, readiness_score, evidence, refs, message
    ):
        row = self._accepted(
            init_store,
            readiness_score=readiness_score,
            evidence=evidence,
            refs=refs,
        )
        with pytest.raises(needfix_store.NeedFixConflictError, match=message):
            needfix_store.resolve_verified_needfix(
                init_store, row["id"], resolution_note="Verified."
            )
        assert needfix_store.get_needfix(init_store, row["id"])["status"] == "accepted"

    def test_resolve_verified_requires_note(self, init_store: Path):
        row = self._accepted(init_store)
        with pytest.raises(needfix_store.NeedFixValidationError, match="resolution_note"):
            needfix_store.resolve_verified_needfix(init_store, row["id"], resolution_note="")

    def test_resolve_verified_is_idempotent_after_resolution(self, init_store: Path):
        row = self._accepted(init_store)
        first = needfix_store.resolve_verified_needfix(
            init_store, row["id"], resolution_note="Verified."
        )
        second = needfix_store.resolve_verified_needfix(
            init_store, row["id"], resolution_note="Verified again."
        )
        assert second == first

    def test_ordinary_resolve_still_rejects_accepted_item(self, init_store: Path):
        row = self._accepted(init_store)
        with pytest.raises(needfix_store.NeedFixConflictError):
            needfix_store.resolve_needfix(init_store, row["id"])

    def test_dashboard_delegates_verified_resolution(self, monkeypatch):
        from aiworkhub import core, dashboard_mcp_app

        calls = []

        def fake(needfix_id, *, resolution_note):
            calls.append((needfix_id, resolution_note))
            return {"id": needfix_id, "status": "resolved"}

        monkeypatch.setattr(core, "needfix_resolve_verified", fake)
        result = dashboard_mcp_app.needfix_transition_view(
            "NF-2026-00001",
            "resolve_verified",
            reason="Canonical tests passed.",
            confirm=True,
        )
        assert result["ok"] is True
        assert calls == [("NF-2026-00001", "Canonical tests passed.")]


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

    def test_server_reopen_superseded_delegates_to_core(self, monkeypatch):
        from aiworkhub import core, server

        calls = []

        def fake(needfix_id, reason):
            calls.append((needfix_id, reason))
            return {"status": "accepted"}

        monkeypatch.setattr(core, "needfix_reopen_superseded_task_link", fake)
        result = server.needfix_reopen_superseded_task_link(
            "NF-2026-00001", "Superseded task archived."
        )
        assert result["status"] == "accepted"
        assert calls == [("NF-2026-00001", "Superseded task archived.")]

    def test_dashboard_reopen_superseded_requires_confirmation(self):
        from aiworkhub import dashboard_mcp_app

        result = dashboard_mcp_app.needfix_reopen_superseded_task_link_view(
            "NF-2026-00001", "Superseded task archived."
        )
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
        "needfix_reopen_superseded_task_link",
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
        pv = needfix_store.preview_convert(
            init_store,
            r["id"],
            {"runner": "deepseek_v4-pro", "topic": "needfix_research"},
        )
        assert pv["claimable"] is False
        assert pv["unverified"] is True

    def test_preview_accepted_is_claimable(self, init_store: Path):
        r = needfix_store.capture_proposal(init_store, title="T", description="D")
        needfix_store.triage_needfix(init_store, r["id"])
        needfix_store.accept_needfix(init_store, r["id"])
        pv = needfix_store.preview_convert(
            init_store,
            r["id"],
            {"runner": "deepseek_v4-pro", "topic": "needfix_research"},
        )
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


class TestActiveListingProductionWiring:
    """End-to-end: the production entry points derive by DEFAULT.

    These tests go through ``needfix_ingest.list_active`` / ``count_active``
    exactly as an operator would -- supplying NO hooks -- against a real
    canonical task store. A test that called ``needfix_store.list_needfix``
    with hooks already passed on the predecessor and did not catch that the
    production caller supplied none; this drives the real entry point instead.
    """

    def _insert_task(self, repo: Path, task_id: str, status: str) -> None:
        db = task_store.canonical_db_path(repo)
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT INTO tasks (task_id, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, status, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            conn.commit()
        finally:
            conn.close()

    def _link(self, repo: Path, title: str, task_id: str) -> str:
        rec = needfix_store.capture_proposal(repo, title=title, description=title)
        conn = sqlite3.connect(str(needfix_store._db_path(repo)))
        try:
            conn.execute(
                "UPDATE needfix SET converted_task_id = ?, status = 'task_created' "
                "WHERE id = ?",
                (task_id, rec["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        return rec["id"]

    def test_list_active_derives_by_default_through_ingest_entry_point(
        self, tmp_path: Path
    ):
        task_store.initialize_repository(tmp_path)
        # A landed fix: the linked card is finished -> the record is CLOSED.
        self._insert_task(tmp_path, "T-fin", "finished")
        landed = self._link(tmp_path, "already-landed", "T-fin")
        # A real, current, unowned problem.
        live = needfix_store.capture_proposal(
            tmp_path, title="live-problem", description="still open"
        )["id"]

        report = needfix_ingest.list_active(tmp_path)

        # Derivation ran on the live path without anyone passing hooks.
        assert report["derived"] is True
        assert report["underived_reason"] is None
        ids = {item["id"] for item in report["items"]}
        # The landed record does NOT appear; the live one does.
        assert landed not in ids
        assert live in ids
        # count agrees with the listed set.
        assert report["count"] == len(report["items"]) == 1

        # count_active uses the same resolved hooks and agrees with the list.
        creport = needfix_ingest.count_active(tmp_path)
        assert creport["derived"] is True
        assert creport["count"] == report["count"] == 1

    def test_list_and_count_active_agree_under_include_archived_through_ingest(
        self, tmp_path: Path
    ):
        task_store.initialize_repository(tmp_path)
        self._insert_task(tmp_path, "T-fin", "finished")
        self._link(tmp_path, "landed-noise", "T-fin")
        needfix_store.capture_proposal(tmp_path, title="live-1", description="live-1")
        needfix_store.capture_proposal(tmp_path, title="live-2", description="live-2")
        needfix_store.add_needfix(
            tmp_path, title="archived-noise", description="archived", status="archived"
        )

        for include_archived in (False, True):
            report = needfix_ingest.list_active(
                tmp_path, include_archived=include_archived,
                limit=needfix_store.MAX_LIST_LIMIT,
            )
            creport = needfix_ingest.count_active(
                tmp_path, include_archived=include_archived
            )
            # Same set on every axis the listing honours, include_archived too.
            assert report["derived"] is creport["derived"] is True
            assert report["count"] == len(report["items"]) == 2, include_archived
            assert creport["count"] == report["count"], include_archived

    def test_active_listing_is_marked_underived_without_a_task_store(
        self, tmp_path: Path
    ):
        # NeedFix store only; no canonical task store to derive linked-card state.
        needfix_store.initialize_repository(tmp_path)
        needfix_store.capture_proposal(tmp_path, title="orphan", description="orphan")

        report = needfix_ingest.list_active(tmp_path)
        # Explicitly underived: not silently presented as an authoritative set.
        assert report["derived"] is False
        assert report["underived_reason"]
        assert report["count"] is None
        # The raw rows are still surfaced so the operator is not left blind.
        assert report["items"]

        creport = needfix_ingest.count_active(tmp_path)
        assert creport["derived"] is False
        assert creport["underived_reason"]
        assert creport["count"] is None

    def test_active_listing_uses_one_bounded_task_snapshot(
        self, tmp_path: Path, monkeypatch
    ):
        task_store.initialize_repository(tmp_path)
        for index in range(24):
            task_id = f"T-{index:02d}"
            self._insert_task(
                tmp_path, task_id, "finished" if index % 2 else "pending"
            )
            self._link(tmp_path, f"finding-{index:02d}", task_id)

        original_list = task_store.list_task_cards
        original_get = task_store.get_task
        calls = {"list": 0, "get": 0}

        def counted_list(*args, **kwargs):
            calls["list"] += 1
            return original_list(*args, **kwargs)

        def counted_get(*args, **kwargs):
            calls["get"] += 1
            return original_get(*args, **kwargs)

        monkeypatch.setattr(task_store, "list_task_cards", counted_list)
        monkeypatch.setattr(task_store, "get_task", counted_get)

        report = needfix_ingest.list_active(
            tmp_path, limit=needfix_store.MAX_LIST_LIMIT
        )

        # Every linked card either landed or is currently owned, so all 24
        # records are derived closed; the point lookup count must still be zero.
        assert report["count"] == len(report["items"]) == 0
        assert calls == {"list": 1, "get": 0}

    def test_active_listing_reuses_caller_task_snapshot(
        self, tmp_path: Path, monkeypatch
    ):
        task_store.initialize_repository(tmp_path)
        self._insert_task(tmp_path, "T-fin", "finished")
        self._link(tmp_path, "landed", "T-fin")
        task_cards = tuple(task_store.list_task_cards(tmp_path, limit=5000))

        def unexpected_list(*_args, **_kwargs):
            raise AssertionError("caller snapshot must avoid a second card query")

        def unexpected_get(*_args, **_kwargs):
            raise AssertionError("complete snapshot must prove task absence")

        monkeypatch.setattr(task_store, "list_task_cards", unexpected_list)
        monkeypatch.setattr(task_store, "get_task", unexpected_get)
        report = needfix_ingest.list_active(
            tmp_path,
            task_cards_snapshot=task_cards,
            task_cards_snapshot_complete=True,
        )

        assert report["derived"] is True
        assert report["count"] == 0

    def test_partial_caller_snapshot_keeps_point_lookup_fallback(
        self, tmp_path: Path, monkeypatch
    ):
        task_store.initialize_repository(tmp_path)
        self._insert_task(tmp_path, "T-fin", "finished")
        self._link(tmp_path, "landed", "T-fin")
        original_get = task_store.get_task
        calls = {"get": 0}

        def counted_get(*args, **kwargs):
            calls["get"] += 1
            return original_get(*args, **kwargs)

        monkeypatch.setattr(task_store, "get_task", counted_get)
        report = needfix_ingest.list_active(
            tmp_path,
            task_cards_snapshot=(),
            task_cards_snapshot_complete=False,
        )

        assert report["count"] == 0
        assert calls["get"] >= 1
