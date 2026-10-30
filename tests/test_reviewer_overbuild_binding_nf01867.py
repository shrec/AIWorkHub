"""Overbuild removable_surface binding and final-attempt drop (NF-2026-01867)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aiworkhub import quality_reviewer, worker_ai_tools_mcp as worker_tools
from aiworkhub.quality_reviewer import ReviewerEvidenceError

PATH = "src/module.py"
SOURCE = "changed code"
DIGEST = hashlib.sha256(SOURCE.encode("utf-8")).hexdigest()
SPANS = {PATH: [(12, 14), (40, 42)]}


def _bind(value: str):
    return quality_reviewer._changed_line_reference(
        value, changed_paths={PATH}, changed_source_lines=SPANS
    )


def _segment(start: int, end: int) -> dict:
    return {
        "kind": "replace",
        "candidate_start_line": start,
        "candidate_end_line": end,
        "changed_start_line": start,
        "changed_end_line": end,
        "baseline_start_line": start,
        "baseline_end_line": end,
        "excerpt_bytes": len(SOURCE),
        "truncated": False,
    }


def _scoped_audit(lens: str) -> dict:
    inner = {
        "task_id": "task1",
        "review_lens": {"lens_kind": lens},
        "changed_paths": [{"path": PATH}],
        "known_unknowns": [f"{lens} graph boundary"],
    }
    digest = hashlib.sha256(
        json.dumps(inner, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return {
        "schema_id": "aiworkhub.scoped_audit.v1",
        "fingerprint": digest,
        "known_unknowns": inner["known_unknowns"],
        "packet": inner,
    }


def _packet() -> dict:
    return quality_reviewer.build_review_packet(
        request_id="req1",
        task_id="task1",
        claim_epoch=1,
        worker_provider="adapter-a",
        changed_path_hashes={PATH: DIGEST},
        objective="review the changed delta",
        acceptance=["acceptance contract only"],
        required_outputs=[PATH],
        source_evidence={
            PATH: {
                "candidate_sha256": DIGEST,
                "excerpt": SOURCE,
                "excerpt_bytes": len(SOURCE),
                "source_bytes": len(SOURCE),
                "truncated": False,
                "segments": [_segment(12, 14), _segment(40, 42)],
            }
        },
        scoped_audits={"code_quality": _scoped_audit("code_quality")},
    )


def _overbuild(surface: str) -> dict:
    return {
        "severity": "low",
        "category": "excess_scope",
        "summary": "Wrapper adds scope nobody uses",
        "evidence": f"Source Graph and diff show {PATH}:12",
        "removable_surface": surface,
    }


def _high() -> dict:
    return {
        "id": "H1",
        "severity": "high",
        "summary": "Changed branch drops the error",
        "evidence": f"{PATH}:13",
    }


def _normalize(findings: list[dict]) -> list[dict]:
    return quality_reviewer.normalize_packet_findings(
        _packet(), lens="code_quality", findings=findings
    )


def _ctx(tmp_path: Path, packet: dict) -> worker_tools.WorkerToolContext:
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ledger = tmp_path / "audit.jsonl"
    ledger.write_text("", encoding="utf-8")
    key = tmp_path / "audit.key"
    key.write_bytes(b"k" * 32)
    return worker_tools.WorkerToolContext(
        task_id="REVIEW_TASK_1",
        runner="claude_sonnet5",
        topic="quality_review",
        request_id="a" * 32,
        repo=tmp_path,
        authority_repo=tmp_path,
        source_graph_targets=(),
        session_topic="quality_review",
        audit_ledger_path=ledger,
        audit_hmac_key_path=key,
        quality_review_packet_path=packet_path,
    )


def _submit(ctx, packet: dict, findings: list[dict]) -> dict:
    return worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="code_quality",
        findings=findings,
    )


def test_trailing_parenthetical_after_changed_line_binds():
    assert _bind("src/module.py:12 (_snapshot)") == {
        "kind": "source", "path": PATH, "line_start": 12, "line_end": 12,
    }
    assert _bind("src/module.py:12-13 - the snapshot helper")["line_end"] == 13
    assert _bind("src/module.py:12 -- the snapshot helper")["line_start"] == 12
    result = _normalize([_overbuild("src/module.py:12 (_snapshot)")])
    assert result[0]["removable_surface"] == "src/module.py:12 (_snapshot)"


def test_range_overlapping_a_span_edge_binds_clamped():
    assert _bind("src/module.py:10-13") == {
        "kind": "source", "path": PATH, "line_start": 12, "line_end": 13,
    }
    assert _bind("src/module.py:14-20 (tail)")["line_start"] == 14
    assert _bind("src/module.py:41-50")["line_end"] == 42
    assert _normalize([_overbuild("src/module.py:10-13")])[0]["category"] == "excess_scope"


@pytest.mark.parametrize("surface", ["ConfigWrapper", "src/module.py:20-30", "src/module.py:9"])
def test_bare_symbol_and_non_overlapping_range_stay_refused_with_detail(surface):
    assert _bind(surface) is None
    with pytest.raises(ReviewerEvidenceError) as caught:
        _normalize([_overbuild(surface)])
    reason = str(caught.value)
    assert reason.startswith("review_finding_0_overbuild_removable_surface_unbound")
    assert "'<path>:N' or '<path>:N-M'" in reason
    assert "src/module.py:12-14" in reason
    assert "src/module.py:40-42" in reason


def test_required_refusal_keeps_prefix_and_names_spans_capped():
    with pytest.raises(ReviewerEvidenceError) as caught:
        _normalize([{**_overbuild(""), "removable_surface": ""}])
    assert str(caught.value).startswith(
        "review_finding_0_overbuild_removable_surface_required"
    )
    detail = quality_reviewer._removable_surface_refusal_detail(
        "", {PATH}, {PATH: [(n, n) for n in range(1, 13)]}
    )
    assert detail.count("src/module.py:") == 8
    assert "+4 more" in detail


def test_final_attempt_drops_unbindable_overbuild_and_keeps_high(tmp_path):
    packet = _packet()
    ctx = _ctx(tmp_path, packet)
    findings = [_high(), _overbuild("ConfigWrapper")]

    first = _submit(ctx, packet, findings)
    assert first["ok"] is False
    assert first["reason"].startswith("review_finding_1_overbuild_removable_surface_unbound")
    assert first["attempt"] == 1
    assert first["terminal"] is False

    second = _submit(ctx, packet, findings)
    assert second["ok"] is True, second
    assert second["finding_count"] == 1
    assert second["dropped_unbound_overbuild"] == [
        {
            "index": 1,
            "dropped": first["reason"],
            "note": "dropped_unbound_overbuild",
        }
    ]
    payloads = worker_tools.verify_audit_ledger(
        ctx.audit_ledger_path,
        ctx.audit_hmac_key_path,
        task_id=ctx.task_id,
        runner=ctx.runner,
        topic=ctx.topic,
        request_id=ctx.request_id,
    )["verified_payloads"]
    assert [row["id"] for row in payloads[0]["report"]["findings"]] == ["H1"]


def test_final_attempt_with_only_unbindable_overbuild_is_refused_after_rejected_intent(tmp_path):
    packet = _packet()
    ctx = _ctx(tmp_path, packet)
    findings = [_overbuild("ConfigWrapper")]
    assert _submit(ctx, packet, findings)["ok"] is False
    second = _submit(ctx, packet, findings)
    assert second["ok"] is False, second
    assert second["reason"].startswith("quality_review_empty_after_rejected_intent")


def test_replacement_with_removable_surface_is_refused_naming_replacement():
    with pytest.raises(ReviewerEvidenceError) as caught:
        _normalize([{**_overbuild("src/module.py:12"), "replacement": "x"}])
    reason = str(caught.value)
    assert reason.startswith("review_finding_0_overbuild_removable_surface_required")
    assert "replacement must be omitted" in reason


def test_final_attempt_still_refuses_non_overbuild_invalid_finding(tmp_path):
    packet = _packet()
    ctx = _ctx(tmp_path, packet)
    bad = {"severity": "high", "summary": "No location", "evidence": "somewhere"}
    findings = [_overbuild("ConfigWrapper"), bad]

    first = _submit(ctx, packet, findings)
    assert first["reason"].startswith("review_finding_0_overbuild_removable_surface_unbound")
    second = _submit(ctx, packet, findings)
    assert second["ok"] is False
    assert "_overbuild_" not in second["reason"]
    assert second["terminal"] is True
    assert "dropped_unbound_overbuild" not in second
