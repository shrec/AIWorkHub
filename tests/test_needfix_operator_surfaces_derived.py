"""The three operator surfaces read the DERIVED NeedFix set through the real doors.

These tests drive the exact entry points production uses -- ``core.needfix_list``,
``core.needfix_count`` and ``dashboard._build_needfix_snapshot`` -- with NO hooks
passed by hand, against a real canonical task store. The pre-existing tests passed
the task-store hooks directly and so never caught that these three surfaces still
called the raw underived store functions; a rejected record resurfaced and a
landed one read as open. These go through the same door an operator does:
``core.needfix_list``/``count`` via the canonical ``AIWORKHUB_REPO_ROOT`` binding,
and the dashboard snapshot via its repository root.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from aiworkhub import core, dashboard, needfix_store, task_store


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> Path:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # Drive core.repo_root() through the canonical env binding exactly as the
        # MCP process does -- no monkeypatching of core internals or hook passing.
        monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
        monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
        yield root


def _insert_task(repo: Path, task_id: str, status: str, **card_fields) -> None:
    db = task_store.canonical_db_path(repo)
    worker_status = str(card_fields.get("worker_status") or "").strip() or "unclaimed"
    payload = {"task_id": task_id, "status": status, **card_fields}
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, status, worker_status, created_at, updated_at, card_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                task_id,
                status,
                worker_status,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                json.dumps(payload),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _link(repo: Path, title: str, task_id: str) -> str:
    """Capture a NeedFix and point its converted_task_id at ``task_id``."""
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


class TestOperatorSurfacesDeriveByDefault:
    """A landed (finished-card) record is hidden on every operator-facing path."""

    def test_core_needfix_list_hides_a_finished_linked_record(self, repo: Path):
        task_store.initialize_repository(repo)
        _insert_task(repo, "T-fin", "finished")
        landed = _link(repo, "already-landed", "T-fin")
        live = needfix_store.capture_proposal(
            repo, title="live-problem", description="still open"
        )["id"]

        rows = core.needfix_list()

        ids = {row["id"] for row in rows}
        assert landed not in ids  # the landed record does NOT resurface
        assert live in ids
        # Derivation ran through the real door without anyone passing hooks.
        assert rows.derived is True
        assert rows.underived_reason is None

    def test_core_needfix_count_agrees_with_the_list(self, repo: Path):
        task_store.initialize_repository(repo)
        _insert_task(repo, "T-fin", "finished")
        _link(repo, "already-landed", "T-fin")
        needfix_store.capture_proposal(repo, title="live-problem", description="open")

        rows = core.needfix_list()
        count = core.needfix_count()

        assert count.derived is True
        assert count.underived_reason is None
        # The count and the list describe the same derived active set.
        assert count == len(rows) == 1

    def test_dashboard_snapshot_hides_finished_and_states_derived(self, repo: Path):
        task_store.initialize_repository(repo)
        _insert_task(repo, "T-fin", "finished")
        landed = _link(repo, "already-landed", "T-fin")
        needfix_store.capture_proposal(repo, title="live-problem", description="open")

        snapshot = dashboard._build_needfix_snapshot(repo)

        assert snapshot["available"] is True
        assert snapshot["derived"] is True
        assert snapshot["underived_reason"] is None
        ids = {item["id"] for item in snapshot["items"]}
        assert landed not in ids
        assert snapshot["total"] == 1
        assert snapshot["open"] == 1


class TestOperatorSurfacesMarkUnderived:
    """No ready task store -> the caller is TOLD, not shown stale rows as truth."""

    def test_core_list_and_count_are_marked_underived_without_task_store(
        self, repo: Path
    ):
        # NeedFix store only; no canonical task store to derive linked-card state.
        needfix_store.initialize_repository(repo)
        needfix_store.capture_proposal(repo, title="orphan", description="orphan")

        rows = core.needfix_list()
        assert rows.derived is False
        assert rows.underived_reason  # bounded reason, not silence
        assert len(rows) == 1  # raw rows still surfaced, not swallowed

        count = core.needfix_count()
        assert count.derived is False
        assert count.underived_reason

    def test_dashboard_snapshot_is_marked_underived_without_task_store(
        self, repo: Path
    ):
        needfix_store.initialize_repository(repo)
        needfix_store.capture_proposal(repo, title="orphan", description="orphan")

        snapshot = dashboard._build_needfix_snapshot(repo)
        assert snapshot["available"] is True
        assert snapshot["derived"] is False
        assert snapshot["underived_reason"]
        # The raw rows are still shown so the operator is not left blind.
        assert snapshot["total"] == 1


class TestNeedFixTerminalArtifactProjection:
    def test_list_and_count_exclude_exact_terminal_accepted_finished_superseded(
        self, repo: Path
    ):
        from aiworkhub import needfix_ingest

        task_store.initialize_repository(repo)
        _insert_task(
            repo,
            "T-acc",
            "finished",
            worker_status="done",
            accepted_request_id="req-acc",
            accepted_at="2026-01-01T00:00:00+00:00",
            accepted_by="owner",
            accept_evidence={"acceptance_evidence_record": {"reference": "req-acc"}},
        )
        _insert_task(repo, "T-fin", "finished", worker_status="done")
        _insert_task(
            repo,
            "T-recorded-acc",
            "finished",
            worker_status="done",
            accepted_request_id="req-recorded-acc",
            accepted_at="2026-01-01T00:00:00+00:00",
            accepted_by="owner",
            accept_evidence={"acceptance_evidence_record": {"reference": "req-recorded-acc"}},
        )
        _insert_task(repo, "T-landed", "finished", worker_status="done")
        _insert_task(
            repo,
            "T-sup",
            "pending",
            archive_operation="superseded",
            superseded_by="T-landed",
        )
        _insert_task(
            repo,
            "QR-retry-acc",
            "review",
            quality_review={
                "target_task_id": "T-acc",
                "target_request_id": "req-acc",
            },
        )
        _insert_task(
            repo,
            "IMPL-retry-fin",
            "pending",
            implementation={
                "target_task_id": "T-fin",
                "target_request_id": "req-fin",
            },
        )
        _insert_task(
            repo,
            "QR-retry-recorded-acc",
            "review",
            quality_review={
                "target_task_id": "T-recorded-acc",
                "target_status": "accepted",
            },
        )
        hidden_acc = _link(repo, "accepted-link", "T-acc")
        hidden_fin = _link(repo, "finished-link", "T-fin")
        hidden_sup = _link(repo, "superseded-link", "T-sup")
        live = needfix_store.capture_proposal(
            repo, title="still-open", description="unresolved"
        )["id"]

        rows = core.needfix_list()
        count = core.needfix_count()
        report = needfix_ingest.list_active(repo)
        counted = needfix_ingest.count_active(repo)

        ids = {row["id"] for row in rows}
        assert hidden_acc not in ids
        assert hidden_fin not in ids
        assert hidden_sup not in ids
        assert live in ids
        assert count == len(rows) == 1
        assert report["count"] == counted["count"] == 1
        excluded = report["terminal_artifacts_excluded"]
        assert counted["terminal_artifacts_excluded"] == excluded
        assert report["terminal_artifacts_excluded_count"] == 3
        assert counted["terminal_artifacts_excluded_count"] == 3
        by_id = {row["task_id"]: row for row in excluded}
        assert by_id["QR-retry-acc"] == {
            "task_id": "QR-retry-acc",
            "target_task_id": "T-acc",
            "target_status": "accepted",
            "artifact_kind": "reviewer",
            "reason": "exact_target_terminal",
        }
        assert by_id["IMPL-retry-fin"] == {
            "task_id": "IMPL-retry-fin",
            "target_task_id": "T-fin",
            "target_status": "finished",
            "artifact_kind": "implementation",
            "reason": "exact_target_terminal",
        }
        assert by_id["QR-retry-recorded-acc"] == {
            "task_id": "QR-retry-recorded-acc",
            "target_task_id": "T-recorded-acc",
            "target_status": "accepted",
            "artifact_kind": "reviewer",
            "reason": "exact_target_terminal",
        }

    def test_list_and_count_retain_unresolved_and_rework(self, repo: Path):
        from aiworkhub import needfix_ingest

        task_store.initialize_repository(repo)
        _insert_task(repo, "T-rework", "pending", archive_operation="cancelled")
        _insert_task(repo, "T-reopen", "pending", archive_operation="superseded")
        _insert_task(
            repo,
            "QR-retry-rework",
            "review",
            quality_review={
                "target_task_id": "T-rework",
                "target_request_id": "req-rework",
            },
        )
        _insert_task(
            repo,
            "IMPL-retry-unresolved",
            "pending",
            implementation={
                "target_task_id": "T-missing",
                "target_request_id": "req-missing",
            },
        )
        rework_id = _link(repo, "rework-link", "T-rework")
        unresolved_id = _link(repo, "unresolved-link", "T-reopen")
        missing_id = _link(repo, "missing-link", "T-missing")

        rows = core.needfix_list()
        count = core.needfix_count()
        report = needfix_ingest.list_active(repo)
        counted = needfix_ingest.count_active(repo)

        ids = {row["id"] for row in rows}
        assert rework_id in ids
        assert unresolved_id in ids
        assert missing_id in ids
        assert count == len(rows) == 3
        assert report["count"] == counted["count"] == 3
        assert report["terminal_artifacts_excluded"] == []
        assert counted["terminal_artifacts_excluded"] == []
        assert report["terminal_artifacts_excluded_count"] == 0
        assert counted["terminal_artifacts_excluded_count"] == 0
        assert rows.derived is True

    def test_list_and_count_surface_list_task_cards_failure(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from aiworkhub import needfix_ingest

        task_store.initialize_repository(repo)
        needfix_store.capture_proposal(repo, title="live", description="open")

        def _boom(*_args, **_kwargs):
            raise RuntimeError("card_scan_failed")

        monkeypatch.setattr(task_store, "list_task_cards", _boom)
        report = needfix_ingest.list_active(repo)
        counted = needfix_ingest.count_active(repo)
        assert report["derived"] is False
        assert counted["derived"] is False
        assert report["count"] is None
        assert counted["count"] is None
        assert str(report["underived_reason"]).startswith("list_task_cards_failed:")
        assert counted["underived_reason"] == report["underived_reason"]
        assert report["terminal_artifacts_excluded"] == []
        assert counted["terminal_artifacts_excluded_count"] == 0

    def test_list_and_count_exclude_archive_reason_multi_hop_without_snapshot(
        self, repo: Path
    ):
        from aiworkhub import needfix_ingest

        task_store.initialize_repository(repo)
        _insert_task(repo, "T-landed", "finished", worker_status="done")
        _insert_task(
            repo,
            "T-hop",
            "pending",
            archive_operation="superseded",
            archive_reason="superseded_by:T-landed",
        )
        _insert_task(
            repo,
            "T-mid",
            "pending",
            archive_operation="superseded",
            archive_reason="superseded_by:T-hop",
        )
        hidden_mid = _link(repo, "mid-link", "T-mid")
        hidden_hop = _link(repo, "hop-link", "T-hop")
        live = needfix_store.capture_proposal(
            repo, title="still-open", description="unresolved"
        )["id"]

        rows = core.needfix_list()
        count = core.needfix_count()
        report = needfix_ingest.list_active(repo)
        counted = needfix_ingest.count_active(repo)

        ids = {row["id"] for row in rows}
        assert hidden_mid not in ids
        assert hidden_hop not in ids
        assert live in ids
        assert count == len(rows) == 1
        assert report["count"] == counted["count"] == 1
        assert report["derived"] is True
        assert counted["derived"] is True

    def test_shared_snapshot_list_and_count_agree(self, repo: Path):
        from aiworkhub import needfix_ingest

        task_store.initialize_repository(repo)
        _insert_task(repo, "T-landed", "finished", worker_status="done")
        _insert_task(
            repo,
            "T-sup",
            "pending",
            archive_operation="superseded",
            superseded_by="T-landed",
        )
        _insert_task(
            repo,
            "T-bare",
            "pending",
            archive_operation="superseded",
        )
        _insert_task(
            repo,
            "QR-retry-sup",
            "review",
            quality_review={"target_task_id": "T-sup"},
        )
        hidden = _link(repo, "superseded-link", "T-sup")
        live = _link(repo, "bare-link", "T-bare")
        snapshot = list(task_store.list_task_cards(repo, limit=5000))

        report = needfix_ingest.list_active(
            repo,
            task_cards_snapshot=snapshot,
            task_cards_snapshot_complete=True,
        )
        counted = needfix_ingest.count_active(
            repo,
            task_cards_snapshot=snapshot,
            task_cards_snapshot_complete=True,
        )
        ids = {row["id"] for row in report["items"]}
        assert hidden not in ids
        assert live in ids
        assert report["count"] == counted["count"] == 1
        assert report["terminal_artifacts_excluded"] == counted[
            "terminal_artifacts_excluded"
        ]
        assert report["terminal_artifacts_excluded_count"] == counted[
            "terminal_artifacts_excluded_count"
        ] == 1
        assert report["derived"] is True
        assert counted["derived"] is True

    def test_list_and_count_projection_exact_beyond_200_and_retains_live(self, repo: Path):
        from aiworkhub import needfix_ingest

        task_store.initialize_repository(repo)
        live = needfix_store.capture_proposal(
            repo, title="still-open", description="unresolved"
        )["id"]
        snapshot = [
            {
                "task_id": "T-done",
                "status": "finished",
                "worker_status": "done",
            },
            {
                "task_id": "T-rework",
                "status": "rework",
                "worker_status": "unclaimed",
            },
            {
                "task_id": "QR-rework",
                "status": "review",
                "quality_review": {
                    "target_task_id": "T-rework",
                    "target_status": "rework",
                },
            },
        ]
        snapshot.extend(
            {
                "task_id": f"QR-{idx:03d}",
                "status": "review",
                "quality_review": {
                    "target_task_id": "T-done",
                    "target_status": "finished",
                },
            }
            for idx in range(210)
        )
        report = needfix_ingest.list_active(
            repo,
            task_cards_snapshot=snapshot,
            task_cards_snapshot_complete=True,
        )
        counted = needfix_ingest.count_active(
            repo,
            task_cards_snapshot=snapshot,
            task_cards_snapshot_complete=True,
        )
        ids = {row["id"] for row in report["items"]}
        assert live in ids
        assert report["count"] == counted["count"] == 1
        assert len(report["terminal_artifacts_excluded"]) == 200
        assert report["terminal_artifacts_excluded_count"] == 210
        assert counted["terminal_artifacts_excluded_count"] == 210
        assert report["terminal_artifacts_excluded"] == counted[
            "terminal_artifacts_excluded"
        ]
        assert report["derived"] is True
        assert counted["derived"] is True
        assert all(
            row["task_id"] != "QR-rework"
            for row in report["terminal_artifacts_excluded"]
        )

    def test_partial_snapshot_point_lookup_archive_reason_successor(self, repo: Path):
        from aiworkhub import needfix_ingest

        task_store.initialize_repository(repo)
        _insert_task(repo, "T-landed", "finished", worker_status="done")
        _insert_task(
            repo,
            "T-hop",
            "pending",
            archive_operation="superseded",
            archive_reason="superseded_by:T-landed",
        )
        _insert_task(
            repo,
            "T-mid",
            "pending",
            archive_operation="superseded",
            archive_reason="superseded_by:T-hop",
        )
        _insert_task(
            repo,
            "QR-mid",
            "review",
            quality_review={"target_task_id": "T-mid"},
        )
        hidden_mid = _link(repo, "mid-link", "T-mid")
        live = needfix_store.capture_proposal(
            repo, title="still-open", description="unresolved"
        )["id"]
        partial = [
            {
                "task_id": "T-mid",
                "status": "pending",
                "archive_operation": "superseded",
                "archive_reason": "superseded_by:T-hop",
            },
            {
                "task_id": "QR-mid",
                "status": "review",
                "quality_review": {"target_task_id": "T-mid"},
            },
        ]

        closed = needfix_ingest.list_active(
            repo,
            task_cards_snapshot=partial,
            task_cards_snapshot_complete=True,
        )
        closed_count = needfix_ingest.count_active(
            repo,
            task_cards_snapshot=partial,
            task_cards_snapshot_complete=True,
        )
        closed_ids = {row["id"] for row in closed["items"]}
        assert hidden_mid in closed_ids
        assert live in closed_ids
        assert closed["count"] == closed_count["count"] == 2
        assert closed["terminal_artifacts_excluded_count"] == 0

        looked_up = needfix_ingest.list_active(
            repo,
            task_cards_snapshot=partial,
            task_cards_snapshot_complete=False,
        )
        looked_up_count = needfix_ingest.count_active(
            repo,
            task_cards_snapshot=partial,
            task_cards_snapshot_complete=False,
        )
        ids = {row["id"] for row in looked_up["items"]}
        assert hidden_mid not in ids
        assert live in ids
        assert looked_up["count"] == looked_up_count["count"] == 1
        assert looked_up["terminal_artifacts_excluded_count"] == 1
        assert looked_up["terminal_artifacts_excluded"][0]["target_task_id"] == "T-mid"
        assert looked_up["derived"] is True
        assert looked_up_count["derived"] is True

    def test_list_and_count_surface_raising_get_task(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from aiworkhub import needfix_ingest

        task_store.initialize_repository(repo)
        _insert_task(repo, "T-landed", "finished", worker_status="done")
        _insert_task(
            repo,
            "T-hop",
            "pending",
            archive_operation="superseded",
            archive_reason="superseded_by:T-landed",
        )
        _insert_task(
            repo,
            "QR-mid",
            "review",
            quality_review={"target_task_id": "T-hop"},
        )
        _link(repo, "retry-link", "QR-mid")
        needfix_store.capture_proposal(repo, title="still-open", description="unresolved")
        partial = [
            {
                "task_id": "QR-mid",
                "status": "review",
                "quality_review": {"target_task_id": "T-hop"},
            },
        ]

        def _boom(_task_id):
            raise RuntimeError("point_lookup_failed")

        monkeypatch.setattr(task_store, "get_task", _boom)
        report = needfix_ingest.list_active(
            repo,
            task_cards_snapshot=partial,
            task_cards_snapshot_complete=False,
        )
        counted = needfix_ingest.count_active(
            repo,
            task_cards_snapshot=partial,
            task_cards_snapshot_complete=False,
        )
        assert report["derived"] is False
        assert counted["derived"] is False
        assert report["count"] is None
        assert counted["count"] is None
        assert str(report["underived_reason"]).startswith("get_task_failed:")
        assert counted["underived_reason"] == report["underived_reason"]
        assert report["terminal_artifacts_excluded"] == []
        assert counted["terminal_artifacts_excluded_count"] == 0

    def test_list_count_dashboard_exclude_direct_terminal_retry_link(self, repo: Path):
        from aiworkhub import needfix_ingest

        task_store.initialize_repository(repo)
        _insert_task(
            repo,
            "T-acc",
            "finished",
            worker_status="done",
            accepted_request_id="req-acc",
            accepted_at="2026-01-01T00:00:00+00:00",
            accepted_by="owner",
            accept_evidence={"acceptance_evidence_record": {"reference": "req-acc"}},
        )
        _insert_task(repo, "T-rework", "pending", archive_operation="cancelled")
        _insert_task(
            repo,
            "IMPL-retry-acc",
            "pending",
            archive_operation="superseded",
            implementation={
                "target_task_id": "T-acc",
                "target_request_id": "req-acc",
            },
        )
        _insert_task(
            repo,
            "IMPL-retry-rework",
            "pending",
            archive_operation="cancelled",
            implementation={
                "target_task_id": "T-rework",
                "target_request_id": "req-rework",
            },
        )
        hidden = _link(repo, "direct-terminal-retry", "IMPL-retry-acc")
        live = _link(repo, "direct-rework-retry", "IMPL-retry-rework")
        open_id = needfix_store.capture_proposal(
            repo, title="still-open", description="unresolved"
        )["id"]

        rows = core.needfix_list()
        count = core.needfix_count()
        report = needfix_ingest.list_active(repo)
        counted = needfix_ingest.count_active(repo)
        snapshot = dashboard._build_needfix_snapshot(repo)

        ids = {row["id"] for row in rows}
        assert hidden not in ids
        assert live in ids
        assert open_id in ids
        assert count == len(rows) == 2
        assert report["count"] == counted["count"] == 2
        assert {item["id"] for item in snapshot["items"]} == ids
        assert snapshot["total"] == 2
        assert snapshot["derived"] is True
        excluded_ids = {row["task_id"] for row in report["terminal_artifacts_excluded"]}
        assert "IMPL-retry-acc" in excluded_ids
        assert "IMPL-retry-rework" not in excluded_ids
        assert report["terminal_artifacts_excluded_count"] == 1
        assert counted["terminal_artifacts_excluded_count"] == 1
        assert counted["terminal_artifacts_excluded"] == report[
            "terminal_artifacts_excluded"
        ]

    def test_snapshot_spoofed_terminal_identity_does_not_hide_live(
        self, repo: Path
    ):
        from aiworkhub import needfix_ingest

        task_store.initialize_repository(repo)
        _insert_task(repo, "T-live", "pending", archive_operation="cancelled")
        live = _link(repo, "live-link", "T-live")
        open_id = needfix_store.capture_proposal(
            repo, title="still-open", description="unresolved"
        )["id"]
        snapshot = [
            {
                "task_id": "T-spoof",
                "status": "finished",
                "worker_status": "done",
                "accepted_request_id": "req-spoof",
                "accepted_at": "2026-01-01T00:00:00+00:00",
                "accepted_by": "owner",
                "accept_evidence": {
                    "acceptance_evidence_record": {"reference": "req-spoof"}
                },
            },
            {
                "task_id": "QR-spoof",
                "status": "review",
                "quality_review": {
                    "target_task_id": "T-live",
                    "target_status": "accepted",
                },
            },
        ]
        report = needfix_ingest.list_active(
            repo,
            task_cards_snapshot=snapshot,
            task_cards_snapshot_complete=True,
        )
        counted = needfix_ingest.count_active(
            repo,
            task_cards_snapshot=snapshot,
            task_cards_snapshot_complete=True,
        )
        ids = {row["id"] for row in report["items"]}
        assert live in ids
        assert open_id in ids
        assert report["count"] == counted["count"] == 2
        assert report["derived"] is True
        assert counted["derived"] is True
        excluded_ids = {row["task_id"] for row in report["terminal_artifacts_excluded"]}
        assert "QR-spoof" not in excluded_ids

    def test_list_spoofed_terminal_identity_does_not_hide_live(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from aiworkhub import needfix_ingest

        task_store.initialize_repository(repo)
        _insert_task(repo, "T-live", "pending", archive_operation="cancelled")
        live = _link(repo, "live-link", "T-live")
        open_id = needfix_store.capture_proposal(
            repo, title="still-open", description="unresolved"
        )["id"]

        def _spoofed_list(*_args, **_kwargs):
            return [
                {
                    "task_id": "T-acc",
                    "status": "finished",
                    "worker_status": "done",
                    "accepted_request_id": "req-acc",
                    "accepted_at": "2026-01-01T00:00:00+00:00",
                    "accepted_by": "owner",
                    "accept_evidence": {
                        "acceptance_evidence_record": {"reference": "req-acc"}
                    },
                },
            ]

        monkeypatch.setattr(task_store, "list_task_cards", _spoofed_list)
        report = needfix_ingest.list_active(repo)
        counted = needfix_ingest.count_active(repo)
        ids = {row["id"] for row in report["items"]}
        assert live in ids
        assert open_id in ids
        assert report["count"] == counted["count"] == 2
        assert report["derived"] is True
        assert counted["derived"] is True
        assert report["terminal_artifacts_excluded_count"] == counted[
            "terminal_artifacts_excluded_count"
        ]

    def test_complete_snapshot_skips_store_point_reads(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from aiworkhub import needfix_ingest

        task_store.initialize_repository(repo)
        _insert_task(repo, "T-done", "finished", worker_status="done")
        hidden = _link(repo, "done-link", "T-done")
        live = needfix_store.capture_proposal(
            repo, title="still-open", description="unresolved"
        )["id"]
        snapshot = [
            {"task_id": "T-done", "status": "finished", "worker_status": "done"},
        ]
        calls = {"n": 0}

        def _counting_get(repo_arg, task_id):
            calls["n"] += 1
            raise AssertionError(f"unexpected store read for {task_id}")

        monkeypatch.setattr(task_store, "get_task", _counting_get)
        report = needfix_ingest.list_active(
            repo,
            task_cards_snapshot=snapshot,
            task_cards_snapshot_complete=True,
        )
        counted = needfix_ingest.count_active(
            repo,
            task_cards_snapshot=snapshot,
            task_cards_snapshot_complete=True,
        )
        ids = {row["id"] for row in report["items"]}
        assert hidden not in ids
        assert live in ids
        assert report["count"] == counted["count"] == 1
        assert calls["n"] == 0
        assert report["derived"] is True
        assert counted["derived"] is True

    def test_incomplete_snapshot_store_miss_fails_closed(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from aiworkhub import needfix_ingest

        task_store.initialize_repository(repo)
        _insert_task(repo, "T-landed", "finished", worker_status="done")
        _insert_task(
            repo,
            "T-hop",
            "pending",
            archive_operation="superseded",
            archive_reason="superseded_by:T-landed",
        )
        _insert_task(
            repo,
            "T-mid",
            "pending",
            archive_operation="superseded",
            archive_reason="superseded_by:T-hop",
        )
        hidden = _link(repo, "mid-link", "T-mid")
        live = needfix_store.capture_proposal(
            repo, title="still-open", description="unresolved"
        )["id"]
        partial = [
            {
                "task_id": "T-mid",
                "status": "pending",
                "archive_operation": "superseded",
                "archive_reason": "superseded_by:T-hop",
            },
        ]

        def _miss(_repo, _task_id):
            return None

        monkeypatch.setattr(task_store, "get_task", _miss)
        report = needfix_ingest.list_active(
            repo,
            task_cards_snapshot=partial,
            task_cards_snapshot_complete=False,
        )
        counted = needfix_ingest.count_active(
            repo,
            task_cards_snapshot=partial,
            task_cards_snapshot_complete=False,
        )
        ids = {row["id"] for row in report["items"]}
        assert hidden in ids
        assert live in ids
        assert report["count"] == counted["count"] == 2
        assert report["terminal_artifacts_excluded_count"] == 0
        assert report["derived"] is True

    def test_prefetch_over_limit_marks_underived_and_keeps_live(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from aiworkhub import needfix_ingest, task_plan

        task_store.initialize_repository(repo)
        live = needfix_store.capture_proposal(
            repo, title="still-open", description="unresolved"
        )["id"]
        hidden = _link(repo, "qr-link", "QR-000")
        overflow = task_plan.MAX_TERMINAL_ARTIFACT_ROWS + 1
        snapshot = [
            {
                "task_id": f"QR-{idx:03d}",
                "status": "review",
                "quality_review": {
                    "target_task_id": f"T-done-{idx:03d}",
                    "target_status": "finished",
                },
            }
            for idx in range(overflow)
        ]
        calls = {"n": 0}

        def _counting_get(_repo, _task_id):
            calls["n"] += 1
            return None

        monkeypatch.setattr(task_store, "get_task", _counting_get)
        report = needfix_ingest.list_active(
            repo,
            task_cards_snapshot=snapshot,
            task_cards_snapshot_complete=False,
        )
        assert calls["n"] == task_plan.MAX_TERMINAL_ARTIFACT_ROWS
        calls["n"] = 0
        counted = needfix_ingest.count_active(
            repo,
            task_cards_snapshot=snapshot,
            task_cards_snapshot_complete=False,
        )
        ids = {row["id"] for row in report["items"]}
        assert live in ids
        assert hidden in ids
        assert report["derived"] is False
        assert counted["derived"] is False
        assert report["underived_reason"] == "terminal_projection_incomplete"
        assert counted["underived_reason"] == "terminal_projection_incomplete"
        assert calls["n"] == task_plan.MAX_TERMINAL_ARTIFACT_ROWS
