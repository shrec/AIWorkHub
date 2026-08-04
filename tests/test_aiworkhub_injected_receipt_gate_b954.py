"""Project-context injection and continuous worker tool-use gate coverage.

The measured defect: correctly-finished work went to validation_failed with
``required_aiworkhub_mcp_calls_missing:session_current_state,ai_memory`` even
though the launcher had injected those sections with a verified hash receipt
(executed=true), because the gate only ever credited LIVE worker calls. This
locks the acceptance criteria: an executed, non-degraded section whose bundle
receipt is acknowledged satisfies optional context tools (zero hit_count is
still valid), while Source Graph additionally requires a fresh live call;
tampered / repo-mismatched / unacknowledged receipts and degraded sections stay
fail-closed; the terminal evidence names the satisfaction source per tool.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher as pl  # noqa: E402
from aiworkhub import project_context  # noqa: E402


def _sha(text: str = "bundle-b954") -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_receipt(tmp_path: Path, bundle_sha: str, *, section_count: int = 2, acknowledged: bool = True) -> Path:
    stdout = tmp_path / "worker.stdout.log"
    receipt = {
        "schema_id": project_context.RECEIPT_SCHEMA_ID,
        "acknowledged": acknowledged,
        "bundle_sha256": bundle_sha,
        "prompt_sha256": "",
        "section_count": section_count,
    }
    stdout.write_text(
        "worker preamble\nPROJECT_CONTEXT_RECEIPT: " + json.dumps(receipt) + "\ntool output line\n",
        encoding="utf-8",
    )
    return stdout


def _metadata(
    tmp_path: Path,
    *,
    bundle_sha: str,
    sections: list[dict],
    stdout: Path,
    runtime: bool = True,
    task_type: str = "code",
) -> dict:
    return {
        "task_id": "TASK_B954", "runner": "codex", "topic": "representation",
        "stdout_path": str(stdout),
        "worker_mcp": (
            {"audit_ledger_path": str(tmp_path / "ledger.jsonl"), "audit_hmac_key_path": str(tmp_path / "key.bin")}
            if runtime else {}
        ),
        "project_context": {
            "task_context_policy": {"task_type": task_type},
            "bundle_sha256": bundle_sha,
            "sections": sections,
        },
    }


def _patch_verify(monkeypatch, *, live_source_graph: int = 0, successful: dict | None = None, policy_violations: int = 0) -> None:
    payload = dict(successful or {})

    def fake_verify(*_a, **_k):
        return {
            "ok": True,
            "live_source_graph_calls": live_source_graph,
            "successful_call_count_by_tool": dict(payload),
            "policy_violations": policy_violations,
            "reason": "",
        }

    monkeypatch.setattr(pl.worker_ai_tools_mcp, "verify_audit_ledger", fake_verify)


def _sections(*specs) -> list[dict]:
    out = []
    for name, executed, hit_count, degraded in specs:
        out.append({
            "name": name, "requested": True, "executed": executed,
            "hit_count": hit_count, "degraded_reason": degraded,
        })
    return out


# --- PASS: injected+verified sections, no live call ------------------------

def test_injected_session_and_source_graph_no_live_call_fails_continuous_use_gate(tmp_path, monkeypatch):
    _patch_verify(monkeypatch)  # empty ledger, zero live calls
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=2)
    sections = _sections(
        ("source_graph", True, 5, ""),
        ("session_current_state", True, 8, ""),
    )
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req")
    assert gate["satisfied"] is False
    assert gate["missing_tools"] == ["source_graph_live_call"]
    assert gate["injected_context_acknowledged"] is True
    assert gate["satisfaction_by_tool"]["source_graph"] == "injected_only_not_sufficient"
    assert gate["satisfaction_by_tool"]["session_current_state"] == "injected_receipt"


def test_injected_ai_memory_zero_hit_is_valid(tmp_path, monkeypatch):
    _patch_verify(monkeypatch)
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=2)
    sections = _sections(
        ("source_graph", True, 3, ""),
        ("ai_memory", True, 0, ""),   # zero hits, but executed + acknowledged
    )
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req")
    assert gate["satisfied"] is False
    assert gate["missing_tools"] == ["source_graph_live_call"]
    assert gate["satisfaction_by_tool"]["ai_memory"] == "injected_receipt"


def test_research_task_reports_acknowledged_injected_context_without_becoming_gated(
    tmp_path,
    monkeypatch,
):
    _patch_verify(
        monkeypatch,
        live_source_graph=1,
        successful={"source_graph": 1},
    )
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=1)
    sections = _sections(("source_graph", True, 3, ""))

    gate = pl._worker_mcp_live_call_gate(
        _metadata(
            tmp_path,
            bundle_sha=bundle,
            sections=sections,
            stdout=stdout,
            task_type="research",
        ),
        "req",
    )

    assert gate["gated"] is False
    assert gate["satisfied"] is True
    assert gate["injected_context_acknowledged"] is True
    assert gate["observation_only"] is True
    assert gate["telemetry_observed"] is True
    assert gate["verification"]["live_source_graph_calls"] == 1


# --- FAIL: not injected / degraded, no live call ---------------------------

def test_section_not_executed_and_no_live_call_fails(tmp_path, monkeypatch):
    _patch_verify(monkeypatch)
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=1)
    sections = _sections(
        ("source_graph", True, 3, ""),
        ("ai_memory", False, 0, ""),   # requested but not executed (not injected)
    )
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req")
    assert gate["satisfied"] is False
    assert gate["missing_tools"] == ["source_graph_live_call", "ai_memory"]
    assert gate["reason"] == "required_aiworkhub_mcp_calls_missing:source_graph_live_call,ai_memory"


def test_degraded_injected_section_requires_live_recovery(tmp_path, monkeypatch):
    _patch_verify(monkeypatch)
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=2)
    sections = _sections(
        ("source_graph", True, 3, ""),
        ("session_current_state", True, 0, "session_store_unavailable"),  # degraded
    )
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req")
    assert gate["satisfied"] is False
    assert gate["missing_tools"] == ["source_graph_live_call", "session_current_state"]


# --- FAIL: tampered / repo-mismatched receipt stays fail-closed ------------

def test_receipt_sha_mismatch_fails_closed(tmp_path, monkeypatch):
    _patch_verify(monkeypatch)
    stored = _sha("real-bundle")
    # The worker echoes a DIFFERENT bundle_sha256 (tampered / another repo's bundle).
    stdout = _write_receipt(tmp_path, _sha("forged-bundle"), section_count=2)
    sections = _sections(
        ("source_graph", True, 3, ""),
        ("session_current_state", True, 8, ""),
    )
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=stored, sections=sections, stdout=stdout), "req")
    assert gate["injected_context_acknowledged"] is False
    assert gate["satisfied"] is False
    assert set(gate["missing_tools"]) == {"source_graph", "session_current_state"}


def test_unacknowledged_receipt_fails_closed(tmp_path, monkeypatch):
    _patch_verify(monkeypatch)
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=2, acknowledged=False)
    sections = _sections(("source_graph", True, 3, ""), ("ai_memory", True, 0, ""))
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req")
    assert gate["injected_context_acknowledged"] is False
    assert gate["satisfied"] is False


# --- PASS: no injection, verified live calls -------------------------------

def test_no_injection_but_live_calls_pass(tmp_path, monkeypatch):
    _patch_verify(monkeypatch, live_source_graph=1, successful={"session_current_state": 1})
    bundle = _sha()
    stdout = tmp_path / "no_receipt.log"
    stdout.write_text("worker output without any receipt\n", encoding="utf-8")
    sections = _sections(
        ("source_graph", False, 0, ""),
        ("session_current_state", False, 0, ""),
    )
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req")
    assert gate["satisfied"] is True
    assert gate["satisfaction_by_tool"]["source_graph"] == "live_worker_call"
    assert gate["satisfaction_by_tool"]["session_current_state"] == "live_worker_call"


def test_live_calls_with_denied_request_recover_as_policy_warning(tmp_path, monkeypatch):
    _patch_verify(monkeypatch, live_source_graph=1, policy_violations=1)
    stdout = tmp_path / "worker.log"
    stdout.write_text("live worker\n", encoding="utf-8")
    gate = pl._worker_mcp_live_call_gate(
        _metadata(tmp_path, bundle_sha=_sha(), sections=[], stdout=stdout), "req"
    )
    assert gate["satisfied"] is True
    assert gate["reason"] == ""
    assert gate["policy_warning"] is True
    assert gate["policy_warning_count"] == 1
    assert gate["warnings"] == ["denied_aiworkhub_tool_requests_recovered:1"]


def test_policy_warning_never_overrides_a_missing_required_source_graph_call(tmp_path, monkeypatch):
    _patch_verify(monkeypatch, policy_violations=1)
    stdout = tmp_path / "worker.log"
    stdout.write_text("worker with denied request but no valid source graph call\n", encoding="utf-8")
    gate = pl._worker_mcp_live_call_gate(
        _metadata(tmp_path, bundle_sha=_sha(), sections=[], stdout=stdout), "req"
    )
    assert gate["satisfied"] is False
    assert gate["missing_tools"] == ["source_graph"]
    assert gate["reason"] == "required_aiworkhub_mcp_calls_missing:source_graph"
    assert gate["policy_warning"] is True
    assert gate["policy_warning_count"] == 1


# --- evidence surfaces the satisfaction source per tool --------------------

def test_satisfaction_source_distinguishes_injected_vs_live(tmp_path, monkeypatch):
    # source_graph via a live call, session_current_state via injection.
    _patch_verify(monkeypatch, live_source_graph=1)
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=1)
    sections = _sections(("session_current_state", True, 8, ""))
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req")
    assert gate["satisfied"] is True
    assert gate["satisfaction_by_tool"]["source_graph"] == "live_worker_call"
    assert gate["satisfaction_by_tool"]["session_current_state"] == "injected_receipt"


def test_gate_result_never_leaks_paths(tmp_path, monkeypatch):
    _patch_verify(monkeypatch)
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=2)
    sections = _sections(("source_graph", True, 3, ""), ("session_current_state", True, 8, ""))
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req")
    blob = json.dumps(gate, default=str)
    assert str(stdout) not in blob
    assert str(tmp_path) not in blob
