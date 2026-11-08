"""NF-2026-02174: needfix_show and the NeedFix transition tool take a bounded id list.

Triage cost one model turn per row (needfix_show 1,495 calls, the transition
tool 816 in the audit window). Both tools accept ``needfix_ids`` (at most 25),
refuse a malformed list before any row runs, and answer one entry per id; one
refused row never fails or rolls back the others.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import dashboard_mcp_app, mcp_tool_metadata, needfix_store, server  # noqa: E402

_MAX = mcp_tool_metadata.NEEDFIX_BULK_MAX_IDS


def _row(needfix_id: str, *, description: str = "short") -> dict:
    return {
        "id": needfix_id,
        "title": f"title {needfix_id}",
        "status": "accepted",
        "kind": "bug",
        "severity": "high",
        "readiness_score": 80,
        "updated_at": "2026-11-08T00:00:00+00:00",
        "description": description,
    }


def _install_show(monkeypatch, *, unknown=(), big=()):
    calls: list[str] = []

    def fake_show(needfix_id):
        calls.append(needfix_id)
        if needfix_id in unknown:
            raise needfix_store.NeedFixError(f"needfix not found: {needfix_id}")
        return _row(needfix_id, description="x" * 9000 if needfix_id in big else "short")

    monkeypatch.setattr(server.core, "needfix_show", fake_show)
    return calls


# --- needfix_show ----------------------------------------------------------


def test_show_ids_returns_one_folded_row_per_id_in_input_order(monkeypatch):
    _install_show(monkeypatch, big={"NF-2026-00002"})
    ids = ["NF-2026-00003", "NF-2026-00001", "NF-2026-00002"]

    result = server.needfix_show(needfix_ids=ids)

    assert result["count"] == 3
    assert [item["id"] for item in result["items"]] == ids
    folded = result["items"][2]["description"]
    assert folded["summarized"] is True and folded["length"] == 9000
    assert result["items"][0]["description"] == "short"


def test_show_unknown_id_is_a_per_row_failure(monkeypatch):
    _install_show(monkeypatch, unknown={"NF-2026-00009"})

    result = server.needfix_show(
        needfix_ids=["NF-2026-00001", "NF-2026-00009", "NF-2026-00002"]
    )

    assert result["count"] == 3
    assert result["items"][0]["id"] == "NF-2026-00001"
    assert result["items"][1]["ok"] is False
    assert result["items"][1]["needfix_id"] == "NF-2026-00009"
    assert "not found" in result["items"][1]["error"]
    assert result["items"][2]["id"] == "NF-2026-00002"


def test_show_single_id_reply_is_unchanged(monkeypatch):
    _install_show(monkeypatch)

    assert server.needfix_show("NF-2026-00001") == server.core.needfix_bodies_bounded(
        _row("NF-2026-00001")
    )
    assert "items" not in server.needfix_show("NF-2026-00001", full=True)


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({}, "needfix_id_or_needfix_ids_required"),
        ({"needfix_id": "NF-2026-00001", "needfix_ids": ["NF-2026-00002"]},
         "needfix_id_and_needfix_ids_are_exclusive"),
        ({"needfix_ids": []}, "needfix_ids_empty"),
        ({"needfix_ids": [f"NF-2026-{i:05d}" for i in range(1, _MAX + 2)]},
         "needfix_ids_over_limit"),
        ({"needfix_ids": ["NF-2026-00001", "NF-2026-00001"]}, "needfix_ids_duplicate"),
        ({"needfix_ids": ["NF-2026-00001", " "]}, "needfix_ids_contains_blank_id"),
        ({"needfix_ids": ["NF-2026-00001"], "full": True}, "full_not_allowed_with_needfix_ids"),
    ],
)
def test_show_refuses_malformed_selection_before_any_row_runs(monkeypatch, kwargs, error):
    calls = _install_show(monkeypatch)

    result = server.needfix_show(**kwargs)

    assert result["ok"] is False and result["error"] == error
    assert calls == []


def test_show_accepts_exactly_the_limit(monkeypatch):
    _install_show(monkeypatch)
    ids = [f"NF-2026-{i:05d}" for i in range(1, _MAX + 1)]

    result = server.needfix_show(needfix_ids=ids)

    assert result["count"] == _MAX
    assert len(json.dumps(result)) < 64 * 1024


# --- transition ------------------------------------------------------------


def _install_accept(monkeypatch, *, refuse=()):
    applied: list[str] = []

    def fake_accept(needfix_id, readiness_score=None):
        if needfix_id in refuse:
            raise needfix_store.NeedFixConflictError(f"cannot accept {needfix_id}")
        applied.append(needfix_id)
        return {
            "id": needfix_id,
            "status": "accepted",
            "readiness_score": 70,
            "updated_at": "2026-11-08T00:00:00+00:00",
            "transition": {
                "event": "accepted",
                "event_id": len(applied),
                "status_before": "triaged",
                "status_after": "accepted",
            },
        }

    monkeypatch.setattr(dashboard_mcp_app.core, "needfix_accept", fake_accept)
    return applied


def test_transition_ids_applies_each_and_a_refused_row_leaves_the_rest(monkeypatch):
    applied = _install_accept(monkeypatch, refuse={"NF-2026-00002"})
    ids = ["NF-2026-00001", "NF-2026-00002", "NF-2026-00003"]

    result = dashboard_mcp_app.needfix_transition_view(
        action="accept", confirm=True, needfix_ids=ids
    )

    assert applied == ["NF-2026-00001", "NF-2026-00003"]
    assert result["succeeded"] == 2 and result["failed"] == 1
    assert [row["needfix_id"] for row in result["results"]] == ids
    assert [row["ok"] for row in result["results"]] == [True, False, True]
    assert result["results"][0]["event_id"] == 1
    assert result["results"][0]["status_after"] == "accepted"
    assert "cannot accept" in result["results"][1]["error"]
    assert result["authority"] == "storage_write"


def test_transition_ids_rows_match_the_single_id_receipt(monkeypatch):
    _install_accept(monkeypatch)
    single = dashboard_mcp_app.needfix_transition_view(
        "NF-2026-00001", "accept", confirm=True
    )
    _install_accept(monkeypatch)

    bulk = dashboard_mcp_app.needfix_transition_view(
        action="accept", confirm=True, needfix_ids=["NF-2026-00001"]
    )

    row = dict(bulk["results"][0])
    assert row.pop("needfix_id") == "NF-2026-00001"
    assert row == {k: v for k, v in single.items() if k not in {"server_tool", "authority"}}


def test_transition_ids_keeps_the_confirm_rule_per_row(monkeypatch):
    applied = _install_accept(monkeypatch)

    result = dashboard_mcp_app.needfix_transition_view(
        action="accept", needfix_ids=["NF-2026-00001", "NF-2026-00002"]
    )

    assert applied == []
    assert result["succeeded"] == 0 and result["failed"] == 2
    assert {row["error"] for row in result["results"]} == {
        "needfix_transition_confirmation_required"
    }


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({}, "needfix_id_or_needfix_ids_required"),
        ({"needfix_id": "NF-2026-00001", "needfix_ids": ["NF-2026-00002"]},
         "needfix_id_and_needfix_ids_are_exclusive"),
        ({"needfix_ids": []}, "needfix_ids_empty"),
        ({"needfix_ids": [f"NF-2026-{i:05d}" for i in range(1, _MAX + 2)]},
         "needfix_ids_over_limit"),
        ({"needfix_ids": ["NF-2026-00001", "NF-2026-00001"]}, "needfix_ids_duplicate"),
    ],
)
def test_transition_refuses_malformed_selection_before_any_row_runs(
    monkeypatch, kwargs, error
):
    applied = _install_accept(monkeypatch)

    result = dashboard_mcp_app.needfix_transition_view(
        action="accept", confirm=True, **kwargs
    )

    assert result["ok"] is False and result["error"] == error
    assert applied == []


def test_transition_without_ids_keeps_the_single_id_errors(monkeypatch):
    _install_accept(monkeypatch)

    unconfirmed = dashboard_mcp_app.needfix_transition_view("NF-2026-00001", "accept")
    assert unconfirmed["error"] == "needfix_transition_confirmation_required"
    invalid = dashboard_mcp_app.needfix_transition_view(
        "NF-2026-00001", "nonsense", confirm=True
    )
    assert invalid["error"] == "invalid_needfix_transition"
    assert "results" not in invalid
