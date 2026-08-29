from __future__ import annotations

import json
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


def test_required_research_context_is_a_blocking_authenticated_tool_gate(
    monkeypatch,
) -> None:
    metadata = _metadata()
    metadata["project_context"]["required"] = True
    metadata["project_context"]["task_context_policy"]["task_type"] = "research"
    monkeypatch.setattr(
        process_launcher.worker_ai_tools_mcp,
        "verify_audit_ledger",
        lambda *args, **kwargs: {
            "ok": True,
            "policy_violations": 0,
            "live_source_graph_calls": 1,
            "successful_call_count_by_tool": {"source_graph": 1},
        },
    )

    result = process_launcher._worker_mcp_live_call_gate(metadata, "request-1")

    assert result["gated"] is True
    assert result["required_tools"] == [
        "source_graph",
        "session_current_state",
        "ai_memory",
        "kb",
    ]
    assert result["satisfied"] is False
    assert result["missing_tools"] == ["session_current_state", "ai_memory", "kb"]
    assert result["reason"] == (
        "worker_mcp_required_tools_missing:session_current_state,ai_memory,kb"
    )


def test_required_context_accepts_acknowledged_supervisor_receipt(
    monkeypatch, tmp_path
) -> None:
    metadata = _metadata()
    bundle_sha256 = "a" * 64
    stdout_path = tmp_path / "worker.jsonl"
    stdout_path.write_text(
        "PROJECT_CONTEXT_RECEIPT: "
        + json.dumps(
            {
                "acknowledged": True,
                "bundle_sha256": bundle_sha256,
                "prompt_sha256": "",
                "schema_id": process_launcher.project_context.RECEIPT_SCHEMA_ID,
                "section_count": 4,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    metadata["stdout_path"] = str(stdout_path)
    metadata["project_context"]["required"] = True
    metadata["project_context"]["bundle_sha256"] = bundle_sha256
    for section in metadata["project_context"]["sections"]:
        section.update(executed=True, degraded_reason="")
    monkeypatch.setattr(
        process_launcher.worker_ai_tools_mcp,
        "verify_audit_ledger",
        lambda *args, **kwargs: {
            "ok": True,
            "policy_violations": 0,
            "live_source_graph_calls": 1,
            "successful_call_count_by_tool": {"source_graph": 1},
        },
    )

    result = process_launcher._worker_mcp_live_call_gate(metadata, "request-1")

    assert result["satisfied"] is True
    assert result["missing_tools"] == []
    assert result["injected_context_acknowledged"] is True
    assert result["satisfaction_by_tool"] == {
        "source_graph": "live_worker_call",
        "session_current_state": "injected_receipt",
        "ai_memory": "injected_receipt",
        "kb": "injected_receipt",
    }


def test_completion_gate_rejects_cache_only_source_graph_for_code_tasks(monkeypatch) -> None:
    """A successful ledger entry that is not a fresh live invocation remains
    fail-closed and is reported consistently as stale/cached, not missing."""
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
    assert "trusted injected bundle are already canonical queries" in prompt
    assert "Never repeat an unchanged zero-hit query as ceremony" in prompt
    assert "Call aiworkhub_worker_session_current_state for continuity" not in prompt


def test_context_gate_honors_repo_policy_toggle(monkeypatch, tmp_path) -> None:
    policy = json.loads(json.dumps(process_launcher.repo_policy.DEFAULT_POLICY))
    policy["tools"]["session_memory_kb_required_for_nontrivial"] = False
    path = tmp_path / ".aiworkhub" / "config" / "policy.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(policy), encoding="utf-8")
    metadata = _metadata()
    metadata["worker_mcp"]["authority_repo"] = str(tmp_path)
    monkeypatch.setattr(
        process_launcher.worker_ai_tools_mcp,
        "verify_audit_ledger",
        lambda *args, **kwargs: {
            "ok": True,
            "live_source_graph_calls": 1,
            "successful_call_count_by_tool": {"source_graph": 1},
        },
    )

    result = process_launcher._worker_mcp_live_call_gate(metadata, "request-1")

    assert result["satisfied"] is True
    assert result["required_tools"] == ["source_graph"]
    assert result["tools_policy"] == {
        "source_graph_required_for_code": True,
        "session_memory_kb_required_for_nontrivial": False,
    }


def test_context_gate_fails_closed_on_malformed_repo_policy(tmp_path) -> None:
    path = tmp_path / ".aiworkhub" / "config" / "policy.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    metadata = _metadata()
    metadata["worker_mcp"]["authority_repo"] = str(tmp_path)

    result = process_launcher._worker_mcp_live_call_gate(metadata, "request-1")

    assert result["gated"] is True
    assert result["satisfied"] is False
    assert result["reason"].startswith("repo_policy_invalid:")
