from __future__ import annotations

from aiworkhub import dashboard_mcp_app


def test_needfix_list_is_bounded_and_sanitized(monkeypatch):
    seen = {}

    def fake_list(**kwargs):
        seen.update(kwargs)
        return [{
            "id": "NF-2026-00001",
            "title": "x" * 400,
            "status": "captured",
            "kind": "bug",
            "severity": "high",
            "readiness_score": 20,
            "tags": ["tag"],
        }]

    monkeypatch.setattr(dashboard_mcp_app.core, "needfix_list", fake_list)
    result = dashboard_mcp_app.needfix_list_view(limit=9999, offset=-7)
    assert result["ok"] is True
    assert result["limit"] == 200
    assert result["offset"] == 0
    assert len(result["entries"][0]["title"]) == 240
    assert seen["limit"] == 200
    assert seen["offset"] == 0
    assert result["authority_flags"]["readonly"] is True


def test_needfix_detail_rejects_invalid_identity():
    result = dashboard_mcp_app.needfix_detail_view("not-an-id")
    assert result["ok"] is False
    assert result["error"] == "invalid_needfix_id"


def test_needfix_transition_requires_confirmation(monkeypatch):
    called = False

    def fake_accept(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(dashboard_mcp_app.core, "needfix_accept", fake_accept)
    result = dashboard_mcp_app.needfix_transition_view(
        "NF-2026-00001", "accept", confirm=False
    )
    assert result["ok"] is False
    assert result["error"] == "needfix_transition_confirmation_required"
    assert called is False


def test_needfix_transition_dispatches_explicit_action(monkeypatch):
    monkeypatch.setattr(
        dashboard_mcp_app.core,
        "needfix_accept",
        lambda needfix_id, readiness_score=None: {
            "id": needfix_id,
            "title": "Accepted",
            "status": "accepted",
            "readiness_score": readiness_score,
        },
    )
    result = dashboard_mcp_app.needfix_transition_view(
        "NF-2026-00001", "accept", readiness_score=80, confirm=True
    )
    assert result["ok"] is True
    assert result["item"]["status"] == "accepted"
    assert result["item"]["readiness_score"] == 80


def test_needfix_purge_and_convert_commit_require_separate_confirmation(monkeypatch):
    monkeypatch.setattr(
        dashboard_mcp_app.core,
        "needfix_purge",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not purge")),
    )
    monkeypatch.setattr(
        dashboard_mcp_app.core,
        "needfix_convert",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not convert")),
    )
    purge = dashboard_mcp_app.needfix_purge_view(
        "NF-2026-00001", "cleanup", confirm=False
    )
    convert = dashboard_mcp_app.needfix_convert_commit_view(
        "NF-2026-00001", confirm=False
    )
    assert purge["error"] == "needfix_purge_confirmation_required"
    assert convert["error"] == "needfix_conversion_confirmation_required"


def test_needfix_capture_is_always_a_proposal(monkeypatch):
    seen = {}

    def fake_capture(**kwargs):
        seen.update(kwargs)
        return {"id": "NF-2026-00001", "title": kwargs["title"], "status": "captured"}

    monkeypatch.setattr(dashboard_mcp_app.core, "needfix_capture", fake_capture)
    result = dashboard_mcp_app.needfix_capture_view("Title", "Description")
    assert result["ok"] is True
    assert result["item"]["status"] == "captured"
    assert seen["provenance"] == {"source": "dashboard_user"}


def test_register_exposes_all_needfix_dashboard_tools():
    registered = {}

    class FakeMcp:
        def tool(self, *, name):
            def decorator(fn):
                registered[name] = fn
                return fn

            return decorator

    names = dashboard_mcp_app.register(FakeMcp())
    expected = set(dashboard_mcp_app.NEEDFIX_READ_TOOLS) | set(
        dashboard_mcp_app.NEEDFIX_WRITE_TOOLS
    )
    assert expected.issubset(registered)
    assert expected.issubset(set(names))
