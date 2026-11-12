"""Dashboard NeedFix list carries work_state per row and by_work_state per reply."""

from __future__ import annotations

from typing import Any

import pytest

from aiworkhub import dashboard_mcp_app, needfix_store

BREAKDOWN = {
    "count": 3,
    "by_work_state": {
        "untriaged": 2,
        "decided_no_card": 0,
        "in_work": 1,
        "stalled": 0,
        "reopened": 0,
        "deferred": 0,
        "closed": 4,
    },
    "by_status": {"captured": 2, "resolved": 4, "task_created": 1},
    "derived": True,
    "underived_reason": None,
}


def _row(needfix_id: str, status: str, work_state: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": needfix_id,
        "title": f"title {needfix_id}",
        "status": status,
        "kind": "bug",
        "severity": "medium",
        "tags": [],
        "converted_task_id": None,
    }
    if work_state is not None:
        row["work_state"] = work_state
    return row


@pytest.fixture
def hooked(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the core door, the count breakdown, hooks and the finished listing."""
    calls: dict[str, Any] = {}
    hooks = {"get_task_fn": lambda _task_id: None, "canonical_status_fn": lambda _task: "pending"}

    def fake_list(**kwargs: Any) -> list[dict[str, Any]]:
        calls["list"] = kwargs
        return [_row("NF-2026-00001", "captured", "untriaged"), _row("NF-2026-00002", "task_created", "in_work")]

    def fake_count(**kwargs: Any) -> dict[str, Any]:
        calls["count"] = kwargs
        return dict(BREAKDOWN)

    def fake_finished(_root: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls["finished"] = kwargs
        return [_row("NF-2026-00009", "resolved", "closed")]

    monkeypatch.setattr(dashboard_mcp_app.core, "needfix_list", fake_list)
    monkeypatch.setattr(dashboard_mcp_app.core, "needfix_count", fake_count)
    monkeypatch.setattr(dashboard_mcp_app.core, "repo_root", lambda: ".")
    monkeypatch.setattr(dashboard_mcp_app, "_needfix_derivation_hooks", lambda _include_archived: hooks)
    monkeypatch.setattr(dashboard_mcp_app.needfix_store, "list_needfix", fake_finished)
    calls["hooks"] = hooks
    return calls


def test_list_rows_carry_work_state_and_reply_carries_breakdown(hooked: dict[str, Any]) -> None:
    result = dashboard_mcp_app.needfix_list_view()

    assert result["ok"] is True
    assert [row["work_state"] for row in result["entries"]] == ["untriaged", "in_work"]
    assert all(row["work_state"] in needfix_store.WORK_STATES for row in result["entries"])
    assert result["by_work_state"] == BREAKDOWN["by_work_state"]
    assert set(result["by_work_state"]) == set(needfix_store.WORK_STATES)
    assert hooked["count"]["breakdown"] is True


def test_finished_rows_are_kept_off_the_main_list(hooked: dict[str, Any]) -> None:
    result = dashboard_mcp_app.needfix_list_view()

    assert [row["id"] for row in result["finished_entries"]] == ["NF-2026-00009"]
    assert result["finished_entries"][0]["work_state"] == "closed"
    assert all(row["work_state"] != "closed" for row in result["entries"])
    assert hooked["finished"]["work_state"] == "closed"


def test_explicit_filter_decorates_raw_rows_and_skips_finished_listing(
    monkeypatch: pytest.MonkeyPatch, hooked: dict[str, Any]
) -> None:
    raw = _row("NF-2026-00003", "captured", None)
    monkeypatch.setattr(dashboard_mcp_app.core, "needfix_list", lambda **_kwargs: [raw])

    result = dashboard_mcp_app.needfix_list_view(status="captured")

    assert result["entries"][0]["work_state"] == "untriaged"
    assert result["finished_entries"] == []
    assert "finished" not in hooked
    assert hooked["count"]["status"] == "captured"


def test_underived_reply_names_why_by_work_state_is_missing(
    monkeypatch: pytest.MonkeyPatch, hooked: dict[str, Any]
) -> None:
    monkeypatch.setattr(
        dashboard_mcp_app.core,
        "needfix_count",
        lambda **_kwargs: {"count": 5, "by_work_state": None, "underived_reason": "no ready task store"},
    )
    monkeypatch.setattr(dashboard_mcp_app, "_needfix_derivation_hooks", lambda _include_archived: None)

    result = dashboard_mcp_app.needfix_list_view()

    assert result["ok"] is True
    assert result["by_work_state"] is None
    assert result["by_work_state_error"] == "no ready task store"
    assert result["finished_entries"] == []
