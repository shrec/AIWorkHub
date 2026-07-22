from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher  # noqa: E402


def _metadata() -> dict:
    return {
        "task_id": "T1",
        "runner": "claude_worker",
        "topic": "coding",
        "project_context": {
            "task_context_policy": {"task_type": "code"},
            "sections": [
                {"name": "source_graph", "requested": True},
                {"name": "session_current_state", "requested": True},
                {"name": "ai_memory", "requested": True},
                {"name": "kb", "requested": True},
            ],
        },
        "worker_mcp": {
            "audit_ledger_path": "/bounded/audit.jsonl",
            "audit_hmac_key_path": "/bounded/audit.key",
        },
    }


def test_completion_gate_requires_every_requested_aiworkhub_surface(monkeypatch) -> None:
    monkeypatch.setattr(
        process_launcher.worker_ai_tools_mcp,
        "verify_audit_ledger",
        lambda *args, **kwargs: {
            "ok": True,
            "live_source_graph_calls": 1,
            "successful_call_count_by_tool": {
                "source_graph": 1,
                "session_current_state": 1,
                "ai_memory": 1,
            },
        },
    )

    result = process_launcher._worker_mcp_live_call_gate(_metadata(), "request-1")

    assert result["satisfied"] is False
    assert result["missing_tools"] == ["kb"]
    assert result["reason"] == "required_aiworkhub_mcp_calls_missing:kb"


def test_completion_gate_accepts_zero_hit_but_successful_optional_calls(monkeypatch) -> None:
    monkeypatch.setattr(
        process_launcher.worker_ai_tools_mcp,
        "verify_audit_ledger",
        lambda *args, **kwargs: {
            "ok": True,
            "live_source_graph_calls": 1,
            "successful_call_count_by_tool": {
                "source_graph": 1,
                "session_current_state": 1,
                "ai_memory": 1,
                "kb": 1,
            },
        },
    )

    result = process_launcher._worker_mcp_live_call_gate(_metadata(), "request-1")

    assert result["satisfied"] is True
    assert result["missing_tools"] == []


def test_worker_prompt_explains_runtime_enforcement() -> None:
    prompt = process_launcher.build_worker_prompt(
        task_id="T1", runner="claude_worker", topic="coding", card={}
    )
    assert "MANDATORY_AIWORKHUB_TOOLS" in prompt
    assert "provider-blocked" in prompt
    assert "HMAC-authenticated MCP audit ledger" in prompt
    assert "new coordinator-authorized fallback card" in prompt
