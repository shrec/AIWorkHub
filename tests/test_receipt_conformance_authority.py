"""NF-2026-00247 / NF-2026-00219 / NF-2026-00225: honest gate + evidence.

- receipt_conformance is made honest: acceptance refuses on ``blocking: true``
  and names the exact blocker (chosen behaviour: acceptance consults the field).
- Authenticated audit-ledger numeric decoding fails closed with a named refusal
  and never raises out of the completion-gate boundary.
- Overlapping quality-evidence stdout windows are merged so a metric line is
  contributed exactly once (no false metric rejection).

The completion gate ``_worker_mcp_live_call_gate`` is a module-level function;
its authenticated ledger read is stubbed via monkeypatch, so no ProcessManager
is constructed and no process is launched.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from aiworkhub import process_launcher as pl
from aiworkhub import quality_evidence as qe


def _code_metadata() -> dict[str, Any]:
    return {
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


def _patch_verify(monkeypatch: pytest.MonkeyPatch, verification: dict[str, Any]) -> None:
    monkeypatch.setattr(
        pl.worker_ai_tools_mcp,
        "verify_audit_ledger",
        lambda *a, **k: verification,
    )


def test_acceptance_refuses_on_blocking_receipt_and_names_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_verify(monkeypatch, {
        "ok": True,
        "reason": "",
        "live_source_graph_calls": 1,
        "successful_call_count_by_tool": {
            "session_current_state": 1, "ai_memory": 1, "kb": 1,
        },
        "policy_violations": 0,
        "receipt_conformance": {
            "status": "fail",
            "blocking": True,
            "blockers": ["source_graph_mode_stage_sequence_mismatch"],
        },
    })
    gate = pl._worker_mcp_live_call_gate(_code_metadata(), "req-block")
    # Everything else is green, yet a self-declared-blocking receipt refuses.
    assert gate["satisfied"] is False
    assert gate.get("receipt_conformance_blocking") is True
    assert "source_graph_mode_stage_sequence_mismatch" in gate["reason"]


def test_non_blocking_receipt_does_not_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_verify(monkeypatch, {
        "ok": True,
        "reason": "",
        "live_source_graph_calls": 1,
        "successful_call_count_by_tool": {
            "session_current_state": 1, "ai_memory": 1, "kb": 1,
        },
        "policy_violations": 0,
        "receipt_conformance": {"status": "pass", "blocking": False, "blockers": []},
    })
    gate = pl._worker_mcp_live_call_gate(_code_metadata(), "req-ok")
    assert gate["satisfied"] is True


@pytest.mark.parametrize("verification", [
    {
        "ok": True, "reason": "", "live_source_graph_calls": 1,
        "successful_call_count_by_tool": {}, "policy_violations": "not-a-number",
        "receipt_conformance": {"blocking": False},
    },
    {
        "ok": True, "reason": "", "live_source_graph_calls": 1,
        "successful_call_count_by_tool": {"source_graph": "NaN"},
        "policy_violations": 0, "receipt_conformance": {"blocking": False},
    },
])
def test_audit_ledger_numeric_decode_fails_closed_without_raising(
    monkeypatch: pytest.MonkeyPatch, verification: dict[str, Any],
) -> None:
    _patch_verify(monkeypatch, verification)
    # A malformed number is a named refusal, never an exception at the boundary.
    gate = pl._worker_mcp_live_call_gate(_code_metadata(), "req-bad")
    assert gate["satisfied"] is False
    assert gate["reason"].startswith("audit_ledger_numeric_decode_failed:")


@pytest.mark.parametrize("raw", ["--5", "²", "١٢٣", "-", "5.0", "0x1f", " 12 x"])
def test_decode_ledger_int_refuses_isdigit_traps_without_raising(raw: str) -> None:
    # str.isdigit() is not the predicate "int() will succeed": '--5' and the
    # superscript '²' pass isdigit yet int() raises, and non-ASCII digits like
    # '١٢٣' are silently accepted from an authenticated ledger. int() is the
    # predicate and the field is restricted to ASCII digits with an optional
    # single leading '-', so each shape is a named refusal (None), never a raise.
    assert pl._decode_ledger_int(raw) is None


def test_decode_ledger_int_accepts_plain_ascii_integers() -> None:
    assert pl._decode_ledger_int(None) == 0
    assert pl._decode_ledger_int("123") == 123
    assert pl._decode_ledger_int("-5") == -5
    assert pl._decode_ledger_int(7) == 7
    assert pl._decode_ledger_int(True) is None


def test_overlapping_stdout_windows_yield_one_metric_not_a_false_rejection() -> None:
    prefix = qe._PERFORMANCE_METRIC_PREFIX
    metric_line = prefix + json.dumps({"metric": "latency", "unit": "ms", "value": 12.5})
    full = "start\n" + metric_line + "\nend"
    # head and tail overlap in the middle: both windows include the metric line.
    head = full[: len("start\n" + metric_line + "\ne")]
    tail = full[len("start"):]
    assert metric_line in head and metric_line in tail

    # Establish the truth: naive concatenation would duplicate the metric line.
    assert (head + "\n" + tail).count(metric_line) == 2

    receipt = {"stdout_truncated": False, "stdout_head": head, "stdout_tail": tail}
    metric = qe._performance_metric(receipt)
    assert metric["metric"] == "latency"
    assert metric["unit"] == "ms"
    assert metric["value"] == 12.5


def test_stdout_window_merge_dedupes_overlap_but_joins_disjoint() -> None:
    assert qe._merge_stdout_windows("abc", "abc") == "abc"
    assert qe._merge_stdout_windows("hello wor", "o world") == "hello world"
    assert qe._merge_stdout_windows("aaa", "bbb") == "aaa\nbbb"


def test_server_and_process_manager_share_one_fail_closed_provider_identity() -> None:
    """NF389: Server and ProcessManager accept the exact same bounded,
    authenticated provider_call_id + provenance validators as the worker
    ledger. Empty/oversized/malformed/spoofed values fail closed with named
    errors at both surfaces instead of reaching any audit row."""
    from aiworkhub import server  # deferred: server imports the MCP SDK lazily

    for surface in (pl, server):
        assert surface.validate_provider_call_id is pl.worker_ai_tools_mcp.validate_provider_call_id
        assert surface.validate_provenance is pl.worker_ai_tools_mcp.validate_provenance
        assert surface.validate_provider_call_id("pci_ok_1") == "pci_ok_1"
        assert surface.validate_provenance("live") == "live"
        for bad in (None, "", "x" * 33, "has space", "bad$char", "pci_ok_1\n", "pci_ok_1 "):
            with pytest.raises(pl.WorkerToolError) as exc:
                surface.validate_provider_call_id(bad)
            assert "worker_mcp_provider_call_id" in str(exc.value)
        for bad in (None, "", "spoofed", 123, True, 3.14, ["live"], {"p": "live"}):
            with pytest.raises(pl.WorkerToolError) as exc:
                surface.validate_provenance(bad)
            assert "worker_mcp_provenance" in str(exc.value)
