from __future__ import annotations

from aiworkhub import dashboard_mcp_app


def test_dashboard_needfix_closed_gate_refuses_before_storage(monkeypatch):
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "0")

    def forbidden(*args, **kwargs):
        raise AssertionError("closed gate touched normalization, repository or storage")

    monkeypatch.setattr(dashboard_mcp_app.needfix_store, "normalize_kind", forbidden)
    monkeypatch.setattr(dashboard_mcp_app.core, "repo_root", forbidden)
    monkeypatch.setattr(dashboard_mcp_app.core, "needfix_capture", forbidden)
    monkeypatch.setattr(dashboard_mcp_app.core, "needfix_update", forbidden)
    for name, result in (
        ("capture", dashboard_mcp_app.needfix_capture_view("closed", "closed", kind="observability")),
        ("update", dashboard_mcp_app.needfix_update_view("missing", kind="observability")),
    ):
        assert result == {
            "ok": False, "error": "write_gate_closed",
            "server_tool": "aiworkhub_dashboard_needfix_" + name, "authority": "storage_write",
        }


def test_dashboard_needfix_registered_closed_and_open_gate_contracts(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = """
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from aiworkhub import needfix_store, task_store
root = Path(sys.argv[1])
assert task_store.initialize_repository(root)["ok"]
seed = needfix_store.add_needfix(root, title="Fixture seed", description="Existing isolated row")
from aiworkhub import server
assert server._MCP_SDK_AVAILABLE is (os.environ['AIWORKHUB_MCP_STDIO_BACKEND'] == 'sdk')
assert os.environ["AIWORKHUB_ALLOW_WRITES"] == "0"
assert not server.core.writes_allowed()
# Existing manager-identity fixture stub: no live authentication claim.
server.core._claude_manager_identity = lambda: None
server.core._codex_manager_identity = lambda: {
    "provider": "codex", "session_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
    "thread_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
}
def dispatch(name, arguments):
    if server._MCP_SDK_AVAILABLE:
        return asyncio.run(server.mcp._tool_manager.get_tool(name).run(arguments))
    assert importlib.util.find_spec("mcp") is None
    assert importlib.util.find_spec("pydantic") is None
    response = server._stdio_tools_call(server.mcp._tools, {"name": name, "arguments": arguments})
    return json.loads(response["content"][0]["text"])
def check_closed(identity):
    before_count = server.core.needfix_count()
    before_row = needfix_store.get_needfix(root, identity)
    originals = (
        (server.core, "repo_root", server.core.repo_root),
        (server.core, "needfix_capture", server.core.needfix_capture),
        (server.core, "needfix_update", server.core.needfix_update),
        (needfix_store, "normalize_kind", needfix_store.normalize_kind),
    )
    def forbidden(*args, **kwargs):
        raise AssertionError("closed dispatch reached normalization, repository or storage")
    for module, name, original in originals:
        setattr(module, name, forbidden)
    for name, arguments in (
        ("capture", {"title": "Closed fixture", "description": "Must not persist"}),
        ("update", {"needfix_id": identity, "title": "Must not change"}),
    ):
        result = dispatch("aiworkhub_dashboard_needfix_" + name, arguments)
        assert result == {
            "ok": False, "error": "write_gate_closed",
            "server_tool": "aiworkhub_dashboard_needfix_" + name, "authority": "storage_write",
        }, result
    for module, name, original in originals:
        setattr(module, name, original)
    assert server.core.needfix_count() == before_count
    assert needfix_store.get_needfix(root, identity) == before_row
check_closed(seed["id"])
os.environ["AIWORKHUB_ALLOW_WRITES"] = "1"
created = dispatch("aiworkhub_dashboard_needfix_capture", {
    "title": "Open fixture", "description": "Open gate", "kind": " Defect ",
})
assert created["ok"] and created["kind"] == "bug"
assert created["kind_normalized"] == {"from": "Defect", "to": "bug"}
changed = dispatch("aiworkhub_dashboard_needfix_update", {
    "needfix_id": created["id"], "kind": " GAP ", "title": "x" * 300,
})
assert changed["ok"] and changed["kind_normalized"] == {"from": "GAP", "to": "improvement"}
assert len(needfix_store.get_needfix(root, created["id"])["title"]) == 240
before_count = server.core.needfix_count()
before_row = needfix_store.get_needfix(root, created["id"])
assert not dispatch("aiworkhub_dashboard_needfix_capture", {
    "title": "Refused fixture", "description": "Invalid input", "kind": "observability",
})["ok"]
assert not dispatch("aiworkhub_dashboard_needfix_update", {
    "needfix_id": created["id"], "severity": "urgent",
})["ok"]
assert server.core.needfix_count() == before_count
assert needfix_store.get_needfix(root, created["id"]) == before_row
os.environ["AIWORKHUB_ALLOW_WRITES"] = "0"
check_closed(created["id"])
print("registered gate contracts passed")
"""
    for backend in ("sdk", "stdlib"):
        root = tmp_path / backend
        root.mkdir()
        home = root / "home"
        temporary = root / "tmp"
        home.mkdir()
        temporary.mkdir()
        env = dict(
            os.environ, AIWORKHUB_ALLOW_WRITES="0", AIWORKHUB_REPO=str(root),
            AIWORKHUB_MCP_STDIO_BACKEND=backend,
            PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
            HOME=str(home), USERPROFILE=str(home), APPDATA=str(home), LOCALAPPDATA=str(home),
            TEMP=str(temporary), TMP=str(temporary),
        )
        argv = [sys.executable] + (["-S"] if backend == "stdlib" else [])
        result = subprocess.run(
            argv + ["-c", script, str(root)], env=env, capture_output=True,
            text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "registered gate contracts passed"


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
    # Deliberate contract change (mcp-output-4/9): the seven constant
    # authority booleans became one string; the authority is a function of
    # the tool, not of the call.
    assert result["authority"] == "readonly"
    assert "authority_flags" not in result


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
    # Deliberate contract change (mcp-output-4): a transition answers with a
    # delta receipt (status_after, readiness_score, event ids) plus the compact
    # list projection of the item; description/evidence only on include_item.
    assert result["ok"] is True
    assert result["schema_id"] == "aiworkhub.needfix_transition_receipt.v1"
    assert result["action"] == "accept"
    assert result["status_after"] == "accepted"
    assert result["readiness_score"] == 80
    assert result["item"]["status"] == "accepted"
    assert result["item"]["id"] == "NF-2026-00001"
    assert "description" not in result["item"]
    assert result["authority"] == "storage_write"


def test_needfix_transition_receipt_carries_store_transition_and_detail_item(monkeypatch):
    monkeypatch.setattr(
        dashboard_mcp_app.core,
        "needfix_accept",
        lambda needfix_id, readiness_score=None: {
            "id": needfix_id,
            "title": "Accepted",
            "description": "long body the manager wrote",
            "status": "accepted",
            "readiness_score": 70,
            "updated_at": "2026-09-08T10:00:00+00:00",
            "evidence": {"measured": 1},
            "transition": {
                "event": "accepted",
                "event_id": 41,
                "status_before": "triaged",
                "status_after": "accepted",
            },
        },
    )
    result = dashboard_mcp_app.needfix_transition_view(
        "NF-2026-00001", "accept", confirm=True, include_item=True
    )
    assert result["status_before"] == "triaged"
    assert result["event_id"] == 41
    assert result["events"] == [
        {"event": "accepted", "event_id": 41, "status_before": "triaged", "status_after": "accepted"}
    ]
    assert result["updated_at"] == "2026-09-08T10:00:00+00:00"
    assert result["item"]["description"] == "long body the manager wrote"
    assert result["item"]["evidence"] == {"measured": 1}


def test_needfix_transition_promote_to_accepted_records_both_steps(monkeypatch):
    calls: list[tuple[str, object]] = []

    def fake_triage(needfix_id, readiness_score=None, triage_note=None):
        calls.append(("triage", triage_note))
        return {
            "id": needfix_id, "status": "triaged", "readiness_score": readiness_score,
            "transition": {"event": "triaged", "event_id": 7, "status_before": "captured", "status_after": "triaged"},
        }

    def fake_accept(needfix_id, readiness_score=None):
        calls.append(("accept", readiness_score))
        return {
            "id": needfix_id, "status": "accepted", "readiness_score": readiness_score,
            "transition": {"event": "accepted", "event_id": 8, "status_before": "triaged", "status_after": "accepted"},
        }

    monkeypatch.setattr(dashboard_mcp_app.core, "needfix_triage", fake_triage)
    monkeypatch.setattr(dashboard_mcp_app.core, "needfix_accept", fake_accept)
    result = dashboard_mcp_app.needfix_transition_view(
        "NF-2026-00001", "triage", reason="worth doing", readiness_score=60,
        confirm=True, promote_to="accepted",
    )
    assert calls == [("triage", "worth doing"), ("accept", 60)]
    assert result["ok"] is True
    assert result["promote_to"] == "accepted"
    assert result["status_before"] == "captured"
    assert result["status_after"] == "accepted"
    assert [event["event_id"] for event in result["events"]] == [7, 8]
    assert result["event_id"] == 8


def test_needfix_transition_promote_to_is_gated(monkeypatch):
    monkeypatch.setattr(
        dashboard_mcp_app.core, "needfix_accept",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not transition")),
    )
    wrong_value = dashboard_mcp_app.needfix_transition_view(
        "NF-2026-00001", "triage", confirm=True, promote_to="resolved"
    )
    assert wrong_value["ok"] is False
    assert wrong_value["error"] == "invalid_promote_to"
    wrong_action = dashboard_mcp_app.needfix_transition_view(
        "NF-2026-00001", "accept", confirm=True, promote_to="accepted"
    )
    assert wrong_action["ok"] is False
    assert wrong_action["error"] == "promote_to_requires_triage_action"


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
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    seen = {}

    def fake_capture(**kwargs):
        seen.update(kwargs)
        return {"id": "NF-2026-00001", "title": kwargs["title"], "status": "captured"}

    monkeypatch.setattr(dashboard_mcp_app.core, "needfix_capture", fake_capture)
    result = dashboard_mcp_app.needfix_capture_view("Title", "Description")
    # Deliberate contract change (mcp-output-2): capture answers with a receipt
    # (id/status/dedupe outcome) plus the compact item, not the description
    # and evidence the caller just supplied.
    assert result["ok"] is True
    assert result["schema_id"] == "aiworkhub.needfix_capture_receipt.v1"
    assert result["id"] == "NF-2026-00001"
    assert result["status"] == "captured"
    assert result["deduped"] is False
    assert result["existing_id"] is None
    assert result["kind_normalized"] is None
    assert result["item"]["status"] == "captured"
    assert "description" not in result["item"]
    assert seen["provenance"] == {"source": "dashboard_user"}
    assert seen["kind"] == "other"


def test_needfix_capture_normalises_kind_synonyms_and_reports_dedupe(monkeypatch):
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    seen = {}

    def fake_capture(**kwargs):
        seen.update(kwargs)
        return {
            "id": "NF-2026-00001",
            "title": kwargs["title"],
            "status": "captured",
            "kind": kwargs["kind"],
            "created_at": "2020-01-01T00:00:00+00:00",
            "provenance": {"origin": "worker_proposal"},
        }

    monkeypatch.setattr(dashboard_mcp_app.core, "needfix_capture", fake_capture)
    result = dashboard_mcp_app.needfix_capture_view("Title", "Description", kind="defect")
    assert seen["kind"] == "bug"
    assert result["kind"] == "bug"
    assert result["kind_normalized"] == {"from": "defect", "to": "bug"}
    # A row whose created_at precedes the call is the dedupe-hit row.
    assert result["deduped"] is True
    assert result["existing_id"] == "NF-2026-00001"
    assert result["provenance"] == {"origin": "worker_proposal"}


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
