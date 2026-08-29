"""NF-2026-00138 / NF-2026-00246: rework predecessor retention + gate.

- A timed-out worker's delta is retained as a rework predecessor (the same
  pinning a validation failure receives), so the successor starts from the work
  instead of nothing.
- A rework attempt no longer discards fully-green work over context-tool
  receipts the rework (validation-only replay) path structurally never makes.

Exercised through the module-level seams; no ProcessManager is constructed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aiworkhub import process_launcher as pl


class _FakeWorkspace:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.repo = path.parent
        self.allowed_writes = ("delta.py",)
        self.base_oid = "base-oid"
        self.parent_baseline: dict[str, str] = {}

    def as_metadata(self) -> dict[str, Any]:
        return {
            "repo": str(self.repo),
            "path": str(self.path),
            "home": str(self.path.parent / "home"),
            "request_id": "req-rework",
            "allowed_writes": list(self.allowed_writes),
            "parent_baseline": dict(self.parent_baseline),
            "base_oid": self.base_oid,
        }


def _seed(tmp_path: Path) -> _FakeWorkspace:
    (tmp_path / "delta.py").write_text("PARTIAL = 1\n", encoding="utf-8")
    return _FakeWorkspace(tmp_path)


def test_timed_out_delta_is_retained_as_rework_predecessor(tmp_path: Path) -> None:
    workspace = _seed(tmp_path)
    metadata = {"task_id": "T", "runner": "r", "topic": "t"}
    evidence = pl.retained_rework_candidate_evidence(
        "timed_out", workspace, metadata, "req-rework", ["delta.py"], "claimed",
    )
    # The exact bytes a successor can resume from are pinned.
    assert "changed_path_hashes" in evidence
    assert evidence["changed_path_hashes"]["delta.py"]
    assert "workspace" in evidence
    assert evidence["request_identity"]["request_id"] == "req-rework"
    workspace_metadata = evidence["workspace"]
    request_identity = evidence["request_identity"]
    assert workspace_metadata["allowed_writes"] == list(workspace.allowed_writes)
    assert workspace_metadata["allowed_writes"] == request_identity["allowed_writes"]
    assert workspace_metadata["base_oid"] == workspace.base_oid
    assert workspace_metadata["base_oid"] == request_identity["base_oid"]
    assert workspace_metadata["parent_baseline"] == workspace.parent_baseline
    assert workspace_metadata["parent_baseline"] == request_identity["parent_baseline"]
    candidate_authority = evidence["python_candidate_authority"]
    assert workspace_metadata["python_candidate_authority"] == candidate_authority
    assert candidate_authority["sources"] == [
        {
            "path": "delta.py",
            "state": "added",
            "bytes_sha256": evidence["changed_path_hashes"]["delta.py"],
        },
    ]
    assert "timed_out" in pl.DELTA_RETAINING_TERMINAL_STATES


def test_states_without_a_usable_delta_retain_nothing(tmp_path: Path) -> None:
    workspace = _seed(tmp_path)
    metadata = {"task_id": "T", "runner": "r", "topic": "t"}
    # A clean exit or an empty change set retains nothing.
    assert pl.retained_rework_candidate_evidence(
        "exited", workspace, metadata, "req", ["delta.py"], "claimed",
    ) == {}
    assert pl.retained_rework_candidate_evidence(
        "timed_out", workspace, metadata, "req", [], "claimed",
    ) == {}


def _rework_metadata(rework: bool) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "task_id": "T",
        "runner": "r",
        "topic": "t",
        "worker_mcp": {
            "audit_ledger_path": "/runtime/ledger.jsonl",
            "audit_hmac_key_path": "/runtime/key.bin",
        },
        "project_context": {
            "task_context_policy": {"task_type": "code"},
            "sections": [
                {"name": "session_current_state", "requested": True},
                {"name": "ai_memory", "requested": True},
                {"name": "kb", "requested": True},
            ],
        },
    }
    if rework:
        metadata["rework_predecessor"] = {"request_id": "pred-1"}
    return metadata


def test_is_rework_attempt_detection() -> None:
    assert pl._is_rework_attempt({"rework_predecessor": {"request_id": "x"}}) is True
    assert pl._is_rework_attempt({"rework_predecessor": {}}) is False
    assert pl._is_rework_attempt({}) is False


def test_rework_does_not_discard_green_work_over_missing_context_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Source Graph is freshly called this attempt, but the rework path did not
    # re-issue the session/memory/kb context calls the predecessor made.
    verification = {
        "ok": True,
        "reason": "",
        "live_source_graph_calls": 1,
        "successful_call_count_by_tool": {},
        "policy_violations": 0,
        "receipt_conformance": {"status": "pass", "blocking": False, "blockers": []},
    }
    monkeypatch.setattr(
        pl.worker_ai_tools_mcp, "verify_audit_ledger", lambda *a, **k: verification,
    )

    normal = pl._worker_mcp_live_call_gate(_rework_metadata(rework=False), "req-a")
    rework = pl._worker_mcp_live_call_gate(_rework_metadata(rework=True), "req-a")

    # Without the rework marker the missing context calls fail the gate.
    assert normal["satisfied"] is False
    assert set(normal["missing_tools"]) == {"session_current_state", "ai_memory", "kb"}
    # A rework honors the predecessor's receipts instead of discarding the work.
    assert rework["satisfied"] is True
    assert rework["missing_tools"] == []
