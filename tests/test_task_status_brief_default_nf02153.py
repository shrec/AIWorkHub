"""NF-2026-02153: agent_task_status and task_show default to a brief lifecycle view.

The brief level is the default for these two tools only. summary, evidence and
full keep their output when passed explicitly; the other StatusDetail tools
keep refusing ``detail="brief"``.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import mcp_summary_folds, server  # noqa: E402

_BRIEF_MAX_BYTES = 2500


def _filler(n: int) -> str:
    return "x" * n


def _size(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _realistic_card() -> dict:
    return {
        "task_id": "TASK_BRIEF_FIXTURE",
        "title": "Brief fixture",
        "status": "finished",
        "substatus": "validation_failed",
        "runner": "claude_sonnet-5",
        "topic": "coding",
        "current_request_id": "req-0001",
        "objective": _filler(3500),
        "acceptance": ["fact " + str(i) for i in range(5)],
        "validation": ["python -m pytest -q"],
        "read_first": ["src/aiworkhub/server.py"],
        "allowed_writes": ["src/aiworkhub/server.py", "tests/test_x.py"],
        "project_context": {"bundle_bytes": 500, "bundle_sha256": "e" * 64, "filler": _filler(900)},
        "terminal_failure": {
            "substatus": "validation_failed",
            "reason": "pytest failed",
            "evidence": {"failure_class": "defect", "error": "AssertionError", "stderr_tail": _filler(7700)},
        },
        "template_provenance": {
            "schema_id": "aiworkhub.template_provenance.v1",
            "expanded_contract": {"allowed_writes": ["a.py"], "filler": _filler(1700)},
        },
        "terminal_review": {
            "verdict": "rejected",
            "blocker": "pytest failed in test_x",
            "evidence": {"acceptance_review": {"verdict": "rejected", "filler": _filler(4792)}},
        },
    }


def _status_result() -> dict:
    return {
        "ok": True,
        "request_id": "req-0001",
        "task_id": "TASK_BRIEF_FIXTURE",
        "state": "finished",
        "task_card": _realistic_card(),
        "latest_event": {
            "event": "finished",
            "state": "finished",
            "exit_code": 1,
            "started_at": "2026-11-07T10:00:00+00:00",
            "ended_at": "2026-11-07T10:20:00+00:00",
            "validation": [{"command": "pytest", "returncode": 1, "stderr_tail": _filler(500)}],
            "usage": {
                "total_tokens": 12345,
                "cost_usd": 0.42,
                "usage_samples": [
                    {"seq": i, "input_tokens": 100, "output_tokens": 50, "note": _filler(280)}
                    for i in range(60)
                ],
            },
            "semantic_edit_coverage": {"measured": True, "filler": _filler(1600)},
        },
    }


def _envelope(card: dict) -> dict:
    return {
        "ok": True,
        "returncode": 0,
        "command": ["show", card.get("task_id", "")],
        "stdout": json.dumps(card),
        "stderr": "",
    }


def _patch_status(monkeypatch, payload: dict) -> None:
    class FakeManager:
        def status(self, request_id: str) -> dict:
            assert request_id == "req-0001"
            return json.loads(json.dumps(payload))

    monkeypatch.setattr(server.process_launcher, "default_manager", lambda: FakeManager())


def _patch_show(monkeypatch, card: dict) -> None:
    monkeypatch.setattr(server.core, "show_task", lambda task_id, full=False: _envelope(card))


def test_default_detail_is_brief_on_the_two_card_tools_only() -> None:
    assert inspect.signature(server.aiworkhub_agent_task_status).parameters["detail"].default == "brief"
    assert inspect.signature(server.aiworkhub_task_show).parameters["detail"].default == "brief"
    assert inspect.signature(server.aiworkhub_source_graph_health).parameters["detail"].default == "summary"


def test_agent_task_status_default_brief_reply_is_small_and_lifecycle_only(monkeypatch) -> None:
    _patch_status(monkeypatch, _status_result())

    result = server.aiworkhub_agent_task_status("req-0001")

    assert result["detail"] == "brief"
    assert set(result["detail_request"]) == {"summary", "evidence", "full"}
    assert result["detail_request"]["summary"]["detail"] == "summary"
    assert _size(result) < _BRIEF_MAX_BYTES, _size(result)
    assert result["task_card"]["status"] == "finished"
    assert result["task_card"]["substatus"] == "validation_failed"
    assert result["card_sha256"]
    assert result["request_id"] == "req-0001"
    card = result["task_card"]
    assert card["current_request_id"] == "req-0001"
    assert card["runner"] == "claude_sonnet-5"
    assert card["terminal_review"] == {"verdict": "rejected", "blocker": "pytest failed in test_x"}
    for dropped in ("objective", "acceptance", "validation", "read_first", "allowed_writes",
                    "project_context", "terminal_failure", "template_provenance"):
        assert dropped not in card
    event = result["latest_event"]
    assert event["exit_code"] == 1
    assert event["started_at"] == "2026-11-07T10:00:00+00:00"
    assert event["usage"] == {"total_tokens": 12345, "cost_usd": 0.42}
    assert "validation" not in event
    assert "semantic_edit_coverage" not in event


def test_task_show_default_brief_card_is_small_and_lifecycle_only(monkeypatch) -> None:
    _patch_show(monkeypatch, _realistic_card())

    result = server.aiworkhub_task_show("TASK_BRIEF_FIXTURE")

    assert result["detail"] == "brief"
    assert set(result["detail_request"]) == {"summary", "evidence", "full"}
    assert _size(result) < _BRIEF_MAX_BYTES, _size(result)
    card = json.loads(result["stdout"])
    assert card == {
        "task_id": "TASK_BRIEF_FIXTURE",
        "title": "Brief fixture",
        "status": "finished",
        "substatus": "validation_failed",
        "runner": "claude_sonnet-5",
        "topic": "coding",
        "current_request_id": "req-0001",
        "terminal_review": {"verdict": "rejected", "blocker": "pytest failed in test_x"},
    }


def test_explicit_summary_evidence_full_keep_their_shape(monkeypatch) -> None:
    raw = _status_result()
    _patch_status(monkeypatch, raw)

    summary = server.aiworkhub_agent_task_status("req-0001", detail="summary")
    assert summary["detail"] == "summary"
    assert set(summary["detail_request"]) == {"evidence", "full"}
    assert summary["task_card"]["objective"]["summarized"] is True
    assert summary["task_card"]["terminal_failure"]["summarized"] is True
    assert summary["latest_event"]["usage"]["usage_samples"]["count"] == 60

    evidence = server.aiworkhub_agent_task_status("req-0001", detail="evidence")
    assert evidence["detail"] == "evidence"
    assert evidence["task_card"]["objective"] == raw["task_card"]["objective"]

    full = server.aiworkhub_agent_task_status("req-0001", detail="full")
    assert full == raw


def test_explicit_task_show_summary_evidence_full_keep_their_shape(monkeypatch) -> None:
    card = _realistic_card()
    _patch_show(monkeypatch, card)

    summary = server.aiworkhub_task_show("TASK_BRIEF_FIXTURE", detail="summary")
    assert summary["detail"] == "summary"
    assert set(summary["detail_request"]) == {"evidence", "full"}
    rendered = json.loads(summary["stdout"])
    assert rendered["objective"] == card["objective"]
    assert rendered["terminal_failure"]["summarized"] is True
    assert rendered["terminal_review"]["evidence"]["acceptance_review"]["summarized"] is True

    evidence = server.aiworkhub_task_show("TASK_BRIEF_FIXTURE", detail="evidence")
    assert mcp_summary_folds.expand_same_as(json.loads(evidence["stdout"])) == card

    full = server.aiworkhub_task_show("TASK_BRIEF_FIXTURE", detail="full")
    assert full == _envelope(card)


def test_bogus_detail_is_still_refused_on_both_tools(monkeypatch) -> None:
    _patch_status(monkeypatch, _status_result())
    _patch_show(monkeypatch, _realistic_card())

    for result in (
        server.aiworkhub_agent_task_status("req-0001", detail="bogus"),
        server.aiworkhub_task_show("TASK_BRIEF_FIXTURE", detail="bogus"),
    ):
        assert result["ok"] is False
        assert result["error"] == "invalid_detail"
        assert result["received"] == "bogus"
        assert "brief" in result["allowed"]


def test_source_graph_health_still_refuses_brief() -> None:
    result = server.aiworkhub_source_graph_health(detail="brief")

    assert result["ok"] is False
    assert result["error"] == "invalid_detail"
    assert "brief" not in result["allowed"]


def test_task_show_red_card_still_returns_the_validation_failed_packet_by_default(monkeypatch) -> None:
    _patch_show(monkeypatch, _realistic_card())
    packet = {"schema_id": "aiworkhub.validation_failed_packet.v1", "task_id": "TASK_BRIEF_FIXTURE"}
    monkeypatch.setattr(server.validation_failed_packet, "build_packet", lambda card, repo_root=None: packet)

    result = server.aiworkhub_task_show("TASK_BRIEF_FIXTURE")

    assert result["detail"] == "validation_failed_packet"
    assert json.loads(result["stdout"]) == packet


def test_brief_status_projection_keeps_identity_fields_and_drops_bodies() -> None:
    reply = {
        "ok": True,
        "request_id": "req-9",
        "request_id_source": "task",
        "task_id": "T-9",
        "state": "finished",
        "liveness": {"alive": False},
        "card_sha256": "a" * 64,
        "card_bytes": 10,
        "review_findings_count": 2,
        "history": [{"filler": _filler(100)}],
        "task_card": {"task_id": "T-9", "objective": {"summarized": True}, "status": "finished"},
        "latest_event": {"event": "finished", "validation": [{"filler": _filler(100)}]},
    }

    projected = mcp_summary_folds.brief_status_reply(reply)

    assert projected["request_id_source"] == "task"
    assert projected["liveness"] == {"alive": False}
    assert projected["review_findings_count"] == 2
    assert "history" not in projected
    assert projected["task_card"] == {"task_id": "T-9", "status": "finished"}
    assert projected["latest_event"] == {"event": "finished"}
