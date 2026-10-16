"""NF-2026-01566: bounded default projections for two card-echoing manager tools.

``aiworkhub_manager_workforce_catalog`` returned 282,987 characters (83 workers,
every field plus outcome history) and ``aiworkhub_task_recover_blocked_rework``
returned 127,300 characters because its stdout carried the whole recovered card
(``terminal_failure`` and ``rework_predecessor`` included).  These tests pin the
compact default projection, the exact ``full=True`` passthrough and the rule
that error text is never hidden.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    core,
    manager_ai_tools,
    server,
)

_COMPACT_WORKER_KEYS = {
    "worker_id",
    "adapter_id",
    "model",
    "enabled",
    "launch_eligible",
    "sample_count",
    "accepted_rate",
}


def _full_worker(index: int) -> dict:
    """One full build_routing_catalog row: every field plus outcome history."""
    return {
        "worker_id": f"worker-{index:02d}",
        "adapter_id": "anthropic-messages",
        "model": "claude-opus-4-5",
        "provider": "anthropic",
        "enabled": True,
        "supports": ["implementation", "review"],
        "tools": ["bash", "read"],
        "max_context_tokens": 200000,
        "max_risk": "high",
        "quality_ceiling": 1.0,
        "manager_score_adjustment": 0.0,
        "manager": False,
        "implementation_worker": True,
        "reviewer": True,
        "policy_enabled": True,
        "policy_provider": "anthropic",
        "policy_adapter": "anthropic-messages",
        "vendor_policy_enabled": True,
        "effective_adapter_id": "anthropic-messages",
        "adapter_fallback_used": False,
        "launch_eligible": bool(index % 3),
        "available": bool(index % 3),
        "route_question": "ROUTE_QUESTION_ROUND_TRIP_OBSERVED",
        "availability_predicate": ["launchable", "failure_circuit_closed"],
        "route_family": "anthropic",
        "availability_reason": "",
        "route_observation": {
            "state": "capability_supported",
            "reason": "",
            "evidence_sources": {},
        },
        "round_trip_observed": "yes",
        "historical_route_observation": {
            "recorded": True,
            "count": 7,
            "in_current_window": False,
            "reason": "",
        },
        "reviewer_submit": {"capability": "supported", "evidence": "probe"},
        "readiness_status": "observed",
        "route_health": {"state": "closed", "failure_kind": ""},
        "sandbox_start_failure_streak": {
            "tripped": False,
            "launch_count": 3,
            "success_count": 3,
            "sandbox_failure_count": 0,
        },
        "quota_observed": False,
        "quota_state": "unavailable_from_provider_api",
        "outcomes": {
            "sample_count": 12,
            "accepted_rate": 0.75,
            "review_ready_rate": 0.83,
            "evidence_source": "observed",
        },
        "cost_per_accepted_outcome": {
            "anthropic": {"accepted_outcomes": 9, "matched_decided_tasks": 11},
        },
        "observed_score": 61.5,
        "effective_score": 61.5,
    }


def _catalog_payload() -> dict:
    return {
        "ok": True,
        "schema_id": "aiworkhub.workforce_catalog.v1",
        "revision": 7,
        "configured": True,
        "workers": [_full_worker(index) for index in range(1, 81)],
        "summary": {
            "workers": 80,
            "enabled": 80,
            "available": 53,
            "observed": 80,
            "startable": 53,
        },
        "truth_contract": {
            "provider_quota_fabricated": False,
            "missing_outcomes_use_labeled_prior": True,
            "manager_adjustment_range": [-20.0, 20.0],
            "economic_routing_is_advisory_only": True,
        },
        "manager": {"provider": "claude", "session_id": "manager-session", "repo": "canonical"},
        "surface": "manager_mcp",
    }


def test_workforce_catalog_default_is_bounded_compact_summary_nf01566(monkeypatch):
    payload = _catalog_payload()
    monkeypatch.setattr(manager_ai_tools, "workforce_catalog_read", lambda: payload)

    result = server.aiworkhub_manager_workforce_catalog()

    assert [row["worker_id"] for row in result["workers"]] == [
        f"worker-{index:02d}" for index in range(1, 81)
    ]
    for row in result["workers"]:
        assert set(row) == _COMPACT_WORKER_KEYS
    assert result["workers"][0] == {
        "worker_id": "worker-01",
        "adapter_id": "anthropic-messages",
        "model": "claude-opus-4-5",
        "enabled": True,
        "launch_eligible": True,
        "sample_count": 12,
        "accepted_rate": 0.75,
    }
    for key in (
        "ok",
        "schema_id",
        "revision",
        "configured",
        "summary",
        "truth_contract",
        "manager",
        "surface",
    ):
        assert result[key] == payload[key]
    canonical = json.dumps(
        payload["workers"], sort_keys=True, separators=(",", ":"), default=str
    )
    assert result["detail_omitted"] == {
        "bytes": len(canonical.encode("utf-8")),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    assert result["detail_request"] == {
        "tool": "aiworkhub_manager_workforce_catalog",
        "full": True,
    }
    assert len(json.dumps(result).encode("utf-8")) < 16 * 1024
    assert len(json.dumps(payload).encode("utf-8")) > 16 * 1024


def test_workforce_catalog_full_true_returns_exact_previous_payload_nf01566(monkeypatch):
    payload = _catalog_payload()
    monkeypatch.setattr(manager_ai_tools, "workforce_catalog_read", lambda: payload)

    result = server.aiworkhub_manager_workforce_catalog(full=True)

    assert result is payload


def test_workforce_catalog_error_result_passes_through_untouched_nf01566(monkeypatch):
    error = {"ok": False, "error": "verified_manager_identity_required"}
    monkeypatch.setattr(manager_ai_tools, "workforce_catalog_read", lambda: error)

    assert server.aiworkhub_manager_workforce_catalog() is error


def test_workforce_catalog_rows_omit_counts_the_row_lacks_nf01566(monkeypatch):
    payload = _catalog_payload()
    payload["workers"] = [
        {
            "worker_id": "worker-01",
            "adapter_id": "a",
            "model": "m",
            "enabled": True,
            "launch_eligible": False,
            "outcomes": {"sample_count": 3},
        },
        {
            "worker_id": "worker-02",
            "adapter_id": "a",
            "model": "m",
            "enabled": True,
            "launch_eligible": False,
            "sample_count": 9,
            "accepted_rate": 0.5,
        },
    ]
    monkeypatch.setattr(manager_ai_tools, "workforce_catalog_read", lambda: payload)

    result = server.aiworkhub_manager_workforce_catalog()

    assert result["workers"] == [
        {
            "worker_id": "worker-01",
            "adapter_id": "a",
            "model": "m",
            "enabled": True,
            "launch_eligible": False,
            "sample_count": 3,
        },
        {
            "worker_id": "worker-02",
            "adapter_id": "a",
            "model": "m",
            "enabled": True,
            "launch_eligible": False,
        },
    ]


def test_recover_blocked_rework_receipt_omits_card_interiors_nf01566(monkeypatch):
    card = {
        "task_id": "T_RECOVERED",
        "topic": "aiworkhub",
        "status": "pending",
        "worker_status": "unclaimed",
        "runner": "codex",
        "recovery_epoch": 4,
        "recovery_predecessor": "T_PREDECESSOR",
        "terminal_failure": {
            "narrative": "x" * 4000,
            "evidence": ["y" * 200 for _ in range(40)],
        },
        "rework_predecessor": {"objective": "z" * 3000, "reason": "w" * 1200},
    }
    stdout = json.dumps(card, ensure_ascii=False, default=str)
    canonical = json.dumps(card, sort_keys=True, separators=(",", ":"), default=str)
    calls = []

    def recover(task_id, **kwargs):
        calls.append((task_id, kwargs))
        return {
            "ok": True,
            "returncode": 0,
            "command": ["recover-blocked-rework", task_id],
            "stdout": stdout,
            "stderr": "",
            "workspace_retention": {"policy": "keep", "root": "worktree"},
        }

    monkeypatch.setattr(core, "recover_blocked_rework", recover)

    result = server.aiworkhub_task_recover_blocked_rework("T_RECOVERED", "focused repair")

    assert "stdout" not in result
    assert "card" not in result
    assert "command" not in result
    assert "terminal_failure" not in result
    assert "rework_predecessor" not in result
    assert result["schema_id"] == "aiworkhub.recover_blocked_rework_receipt.v1"
    assert result["ok"] is True
    assert result["task_id"] == "T_RECOVERED"
    assert result["status"] == "pending"
    assert result["worker_status"] == "unclaimed"
    assert result["runner"] == "codex"
    assert result["recovery_epoch"] == 4
    assert result["recovery_predecessor"] == "T_PREDECESSOR"
    assert result["workspace_retention"] == {"policy": "keep", "root": "worktree"}
    payload = canonical.encode("utf-8")
    assert result["card_sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["card_bytes"] == len(payload)
    assert result["detail_request"] == {
        "tool": "aiworkhub_task_show",
        "task_id": "T_RECOVERED",
        "detail": "full",
    }
    assert calls == [
        (
            "T_RECOVERED",
            {
                "feedback_reason": "focused repair",
                "validation_only_replay": False,
                "clean_root_if_predecessor_missing": False,
            },
        )
    ]
    assert len(json.dumps(result).encode("utf-8")) < 4096


def test_recover_blocked_rework_non_json_stdout_passes_through_unchanged_nf01566(
    monkeypatch,
):
    raw = {
        "ok": False,
        "returncode": 1,
        "command": ["recover-blocked-rework", "T_X"],
        "stdout": "recover_blocked_rework_failed:write_gate_closed",
        "stderr": "recover_blocked_rework_failed:write_gate_closed",
        "reason": "write_gate_closed",
    }
    monkeypatch.setattr(core, "recover_blocked_rework", lambda task_id, **kwargs: raw)

    result = server.aiworkhub_task_recover_blocked_rework("T_X")

    assert result == {key: value for key, value in raw.items() if key != "command"}
    assert result["stdout"] == "recover_blocked_rework_failed:write_gate_closed"


def test_recover_blocked_rework_non_object_json_stdout_passes_through_unchanged_nf01566(
    monkeypatch,
):
    raw = {
        "ok": True,
        "returncode": 0,
        "command": ["recover-blocked-rework", "T_X"],
        "stdout": "[1, 2]",
        "stderr": "",
    }
    monkeypatch.setattr(core, "recover_blocked_rework", lambda task_id, **kwargs: raw)

    result = server.aiworkhub_task_recover_blocked_rework("T_X")

    assert result == {key: value for key, value in raw.items() if key != "command"}
    assert result["stdout"] == "[1, 2]"


def test_recover_blocked_rework_failed_object_json_keeps_error_text_nf01566(
    monkeypatch,
):
    error_card = {"ok": False, "task_id": "T_X", "error": "write_gate_closed"}
    raw = {
        "ok": False,
        "returncode": 3,
        "command": ["recover-blocked-rework", "T_X", "--feedback-reason", "r" * 64],
        "stdout": json.dumps(error_card, sort_keys=True),
        "stderr": "recover_blocked_rework_failed:write_gate_closed",
    }
    monkeypatch.setattr(core, "recover_blocked_rework", lambda task_id, **kwargs: raw)

    result = server.aiworkhub_task_recover_blocked_rework("T_X", "r" * 64)

    assert result == {key: value for key, value in raw.items() if key != "command"}
    assert "command" not in result
    assert json.loads(result["stdout"]) == error_card
    assert json.loads(result["stdout"])["error"] == "write_gate_closed"
    assert "write_gate_closed" in result["stderr"]
