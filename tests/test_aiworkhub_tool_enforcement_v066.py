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


def test_completion_gate_rejects_zero_hit_source_graph_for_code_tasks(monkeypatch) -> None:
    """B834 + B950: a source_graph call that was successful (canonical) but
    cached / zero-hit (``live_source_graph_calls == 0``) is still fail-closed
    (B834 -- ``satisfied is False``), but it must NOT be reported as a bare
    ``missing:source_graph`` while the same evidence's
    ``successful_call_count_by_tool.source_graph`` reads > 0 (the B950
    self-contradiction). It is reported as ``stale_or_cached`` instead."""
    monkeypatch.setattr(
        process_launcher.worker_ai_tools_mcp,
        "verify_audit_ledger",
        lambda *args, **kwargs: {
            "ok": True,
            "live_source_graph_calls": 0,
            "successful_call_count_by_tool": {
                "source_graph": 1,
                "session_current_state": 1,
                "ai_memory": 1,
                "kb": 1,
            },
        },
    )

    result = process_launcher._worker_mcp_live_call_gate(_metadata(), "request-1")

    # B834 preserved: a cached/zero-hit source_graph does not satisfy the gate.
    assert result["satisfied"] is False
    # B950 fix: it is NOT "missing" (it was called successfully) -- no
    # contradiction with the evidence's own successful_call_count.
    assert result["missing_tools"] == []
    assert result["stale_tools"] == ["source_graph"]
    assert result["satisfaction_by_tool"]["source_graph"] == "stale_or_cached"
    assert result["reason"] == "source_graph_stale_or_cached:source_graph"
    # The report is internally consistent: the same result carries the
    # successful source_graph count that would have contradicted a bare "missing".
    assert result["verification"]["successful_call_count_by_tool"]["source_graph"] == 1


def test_worker_prompt_explains_runtime_enforcement() -> None:
    prompt = process_launcher.build_worker_prompt(
        task_id="T1", runner="claude_worker", topic="coding", card={}
    )
    assert "MANDATORY_AIWORKHUB_TOOLS" in prompt
    assert "provider-blocked" in prompt
    assert "HMAC-authenticated MCP audit ledger" in prompt
    assert "new coordinator-authorized fallback card" in prompt
