from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from aiworkhub import core, dashboard_mcp_app, needfix_store, roadmap_store, server


def _add(repo: Path, title: str = "Outcome", **kwargs):
    return roadmap_store.add_item(
        repo,
        title=title,
        outcome=f"Deliver {title}",
        acceptance=[f"{title} is verified"],
        evidence_refs=["docs/PRODUCT_ROADMAP.md"],
        **kwargs,
    )


def test_store_is_repo_local_and_records_bounded_events(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    initialized = roadmap_store.initialize_repository(repo)
    item = _add(repo, needfix_ids=["NF-2026-00001"])

    assert initialized["db_path"] == str(
        repo / ".aiworkhub" / "tasking" / "roadmap.sqlite"
    )
    assert item["id"] == "RM-2026-00001"
    assert item["status"] == "proposed"
    assert roadmap_store.list_items(repo) == [item]
    assert roadmap_store.list_events(repo, item["id"])[0]["event"] == "created"


def test_create_rolls_back_item_when_audit_event_cannot_be_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"

    def fail_event(*_args, **_kwargs) -> None:
        raise RuntimeError("event write failed")

    monkeypatch.setattr(roadmap_store, "_event", fail_event)
    with pytest.raises(RuntimeError, match="event write failed"):
        _add(repo)

    assert roadmap_store.list_items(repo) == []


def test_dependency_gate_blocks_promotion_until_predecessor_completed(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    parent = _add(repo, "Parent")
    child = _add(repo, "Child", depends_on=[parent["id"]])

    with pytest.raises(roadmap_store.RoadmapConflictError, match="incomplete"):
        roadmap_store.transition_item(
            repo, child["id"], "approved", reason="manager approved"
        )

    roadmap_store.transition_item(
        repo, parent["id"], "approved", reason="manager approved"
    )
    roadmap_store.transition_item(
        repo, parent["id"], "in_progress", reason="execution started"
    )
    roadmap_store.transition_item(
        repo, parent["id"], "completed", reason="evidence verified"
    )
    approved = roadmap_store.transition_item(
        repo, child["id"], "approved", reason="dependency complete"
    )
    assert approved["status"] == "approved"


def test_missing_dependency_and_malformed_id_fail_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    with pytest.raises(roadmap_store.RoadmapNotFoundError):
        _add(repo, depends_on=["RM-2026-00099"])
    with pytest.raises(roadmap_store.RoadmapValidationError):
        _add(repo, needfix_ids=["not-a-needfix"])
    with pytest.raises(roadmap_store.RoadmapValidationError):
        roadmap_store.get_item(repo, "../../escape")


def test_link_task_is_idempotent_and_audited(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    item = _add(repo)
    first = roadmap_store.link_task(repo, item["id"], "task-one")
    second = roadmap_store.link_task(repo, item["id"], "task-one")

    assert first["task_ids"] == ["task-one"]
    assert second["task_ids"] == ["task-one"]
    assert [event["event"] for event in roadmap_store.list_events(repo, item["id"])].count(
        "task_linked"
    ) == 1


def test_core_requires_manager_accepted_needfix_before_roadmap_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    captured = needfix_store.add_needfix(
        repo,
        title="Captured",
        description="Not yet accepted",
        status="captured",
    )

    with pytest.raises(roadmap_store.RoadmapConflictError, match="accepted NeedFix"):
        core.roadmap_add(
            "Roadmap outcome", "Deliver it", needfix_ids=[captured["id"]]
        )

    needfix_store.triage_needfix(repo, captured["id"])
    needfix_store.accept_needfix(repo, captured["id"])
    item = core.roadmap_add(
        "Roadmap outcome",
        "Deliver it",
        acceptance=["Verified"],
        needfix_ids=[captured["id"]],
    )
    assert item["needfix_ids"] == [captured["id"]]
    assert item["provenance"]["verified"] is True


def test_core_completion_uses_canonical_task_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    item = _add(repo)
    item = roadmap_store.link_task(repo, item["id"], "task-one")
    roadmap_store.transition_item(repo, item["id"], "approved", reason="approved")
    roadmap_store.transition_item(
        repo, item["id"], "in_progress", reason="started"
    )
    monkeypatch.setattr(core.task_store, "get_task", lambda *_args: {"status": "processing"})
    monkeypatch.setattr(
        core.task_store, "canonical_status", lambda card: str(card["status"])
    )
    with pytest.raises(roadmap_store.RoadmapConflictError, match="unfinished"):
        core.roadmap_transition(item["id"], "completed", reason="done")

    monkeypatch.setattr(core.task_store, "get_task", lambda *_args: {"status": "finished"})
    completed = core.roadmap_transition(item["id"], "completed", reason="done")
    assert completed["status"] == "completed"


def test_snapshot_joins_tasks_and_dependency_blockers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    parent = _add(repo, "Parent")
    child = _add(repo, "Child", depends_on=[parent["id"]])
    roadmap_store.link_task(repo, child["id"], "task-child")
    monkeypatch.setattr(core.task_store, "get_task", lambda *_args: {"status": "pending"})
    monkeypatch.setattr(
        core.task_store, "canonical_status", lambda card: str(card["status"])
    )

    snapshot = core.roadmap_snapshot()
    child_row = next(row for row in snapshot["items"] if row["id"] == child["id"])
    assert snapshot["active"] == 2
    assert child_row["dependency_blockers"] == [parent["id"]]
    assert child_row["tasks"] == [{"task_id": "task-child", "status": "pending"}]


def test_snapshot_aggregates_are_not_limited_to_visible_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    _add(repo, "First")
    _add(repo, "Second")

    snapshot = core.roadmap_snapshot(limit=1)

    assert len(snapshot["items"]) == 1
    assert snapshot["total"] == 2
    assert snapshot["active"] == 2
    assert snapshot["status_counts"]["proposed"] == 2
    assert snapshot["truncated"] is True


def test_dashboard_roadmap_views_are_bounded_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    item = _add(repo)

    listing = dashboard_mcp_app.roadmap_list_view(limit=10)
    detail = dashboard_mcp_app.roadmap_detail_view(item["id"])

    assert listing["ok"] is True
    assert listing["entries"][0]["id"] == item["id"]
    assert listing["authority_flags"]["readonly"] is True
    assert detail["ok"] is True
    assert detail["item"]["outcome"] == item["outcome"]


def test_public_mcp_surface_exposes_roadmap_contract() -> None:
    for name in (
        "roadmap_list",
        "roadmap_show",
        "roadmap_events",
        "roadmap_snapshot",
        "roadmap_add",
        "roadmap_transition",
        "roadmap_link_task",
    ):
        assert hasattr(server, name)
    assert "needfix_ids" in inspect.signature(server.roadmap_add).parameters
    assert set(dashboard_mcp_app.ROADMAP_READ_TOOLS) == {
        "aiworkhub_dashboard_roadmap_list",
        "aiworkhub_dashboard_roadmap_detail",
    }
