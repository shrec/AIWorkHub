"""NF-2026-02308: a reviewer that never read the candidate is blind, whatever it filed.

Field report (NF-2026-00310, card S9): the candidate overlay was unreachable
for the reviewer in both review rounds, yet the hub recorded the lens verified,
because blindness was decided only from the reviewer's own ``process_limit``
findings or zero usage.  The hub now MEASURES it, from the reviewer request's
own HMAC-authenticated MCP audit ledger, and feeds the measured set to the one
blindness rule every caller shares.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import process_launcher_accept_review as par
from aiworkhub import quality_evidence as qe
from aiworkhub import review_orchestrator
from aiworkhub import reviewer_hub_inspection as rhi
from aiworkhub import worker_ai_tools_mcp as mcp

TASK_ID = "REVIEWER_TASK"
RUNNER = "reviewer-runner"
TOPIC = "quality_review"
REQUEST_ID = "rq-overlay"
UNAVAILABLE = "quality_review_candidate_source_graph_unavailable"
KEY = b"k" * 32


# ---------------------------------------------------------------------------
# A real authenticated ledger, written the way ``_append_audit`` writes it.
# ---------------------------------------------------------------------------
def _entry(
    *,
    ok: bool,
    violation: str = "",
    mode: str = "body",
    authority_source: str = "candidate_overlay",
    authority_state: str = "quality_review_readonly",
    request_id: str = REQUEST_ID,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "schema_id": mcp.AUDIT_ENTRY_SCHEMA_ID,
        "timestamp": "2026-11-17T00:00:00+00:00",
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": TOPIC,
        "request_id": request_id,
        "tool": "source_graph",
        "ok": ok,
        "cache_hit": False,
        "hit_count": 1 if ok else 0,
        "bytes_returned": 10,
        "violation": violation,
        "authority_source": authority_source if ok else "",
        "authority_state": authority_state if ok else "",
        "authority_repo": "/repo",
    }
    if ok:
        entry["payload"] = {"mode": mode}
    return entry


def _ledger(tmp_path: Path, rows: list[dict[str, Any]], *, forge: bool = False) -> dict:
    """Write the ledger + key and return the request metadata that names them."""
    key_path = tmp_path / "audit_hmac.key"
    ledger_path = tmp_path / "audit_ledger.jsonl"
    key_path.write_bytes(KEY)
    lines = []
    for row in rows:
        digest = mcp._hmac_entry(row, b"x" * 32 if forge else KEY)
        lines.append(json.dumps({**row, "hmac_sha256": digest}, sort_keys=True))
    ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": TOPIC,
        "request_id": REQUEST_ID,
        "worker_mcp": {
            "audit_ledger_path": str(ledger_path),
            "audit_hmac_key_path": str(key_path),
        },
    }


def _metadata_file(tmp_path: Path, metadata: dict) -> Path:
    path = tmp_path / f"{REQUEST_ID}.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    return path


def _failed() -> dict[str, Any]:
    return _entry(ok=False, violation=UNAVAILABLE)


def _candidate_read(mode: str = "body") -> dict[str, Any]:
    return _entry(ok=True, mode=mode)


def _canonical_read() -> dict[str, Any]:
    return _entry(
        ok=True, authority_source="canonical", authority_state="canonical_active"
    )


# ---------------------------------------------------------------------------
# The hub-side measurement: ONE public function, ``overlay_unreachable``.
# ---------------------------------------------------------------------------
def _measure(tmp_path: Path, metadata: dict, request_id: str = REQUEST_ID) -> bool:
    path = _metadata_file(tmp_path, metadata)
    return rhi.overlay_unreachable([{"metadata_path": str(path)}], request_id)


def test_there_is_exactly_one_public_measurement() -> None:
    public = {
        name
        for name in vars(rhi)
        if name.startswith("overlay_unreachable") and callable(getattr(rhi, name))
    }
    assert public == {"overlay_unreachable", "overlay_unreachable_from_verification"}


def test_only_failed_overlay_calls_is_unreachable(tmp_path: Path) -> None:
    assert _measure(tmp_path, _ledger(tmp_path, [_failed(), _failed()])) is True


def test_canonical_reads_do_not_count_as_reading_the_candidate(tmp_path: Path) -> None:
    # The reviewer read docs/baseline from the canonical index only.
    metadata = _ledger(tmp_path, [_failed(), _canonical_read(), _canonical_read()])
    assert _measure(tmp_path, metadata) is True


@pytest.mark.parametrize("mode", ["body", "file", "diff", "bodygrep"])
def test_failed_call_then_successful_candidate_read_is_sighted(
    tmp_path: Path, mode: str
) -> None:
    metadata = _ledger(tmp_path, [_failed(), _candidate_read(mode)])
    assert _measure(tmp_path, metadata) is False


def test_no_failed_call_is_not_unreachable(tmp_path: Path) -> None:
    assert _measure(tmp_path, _ledger(tmp_path, [])) is False
    assert _measure(tmp_path, _ledger(tmp_path, [_canonical_read()])) is False


def test_missing_or_unreadable_ledger_is_unknown_not_blind(tmp_path: Path) -> None:
    assert _measure(tmp_path, {}) is False
    assert _measure(tmp_path, {"worker_mcp": {}}) is False
    metadata = _ledger(tmp_path, [_failed()])
    Path(metadata["worker_mcp"]["audit_ledger_path"]).unlink()
    assert _measure(tmp_path, metadata) is False
    assert rhi.overlay_unreachable([{"metadata_path": str(tmp_path / "nope.json")}]) is False
    assert rhi.overlay_unreachable([{"metadata_path": None}]) is False
    assert rhi.overlay_unreachable([]) is False
    assert rhi.overlay_unreachable({}) is False
    assert rhi.overlay_unreachable(None) is False


def test_forged_ledger_lines_are_not_trusted(tmp_path: Path) -> None:
    # A line with the wrong HMAC is dropped by the same verifier the gate uses.
    assert _measure(tmp_path, _ledger(tmp_path, [_failed()], forge=True)) is False


def test_other_requests_ledger_rows_are_ignored(tmp_path: Path) -> None:
    metadata = _ledger(tmp_path, [_entry(ok=False, violation=UNAVAILABLE, request_id="x")])
    assert _measure(tmp_path, metadata) is False


# ---------------------------------------------------------------------------
# accept, accept_preview and the reuse check read the SAME fact.
# ---------------------------------------------------------------------------
def _history(path: Path | None) -> list[dict[str, Any]]:
    """A reviewer request's event history; the newest event is bookkeeping only."""
    return [
        {"request_id": REQUEST_ID, "state": "running"},
        {"request_id": REQUEST_ID, "state": "review_ready", "metadata_path": str(path)},
        {"request_id": REQUEST_ID, "state": "accepted"},
    ]


def _three_ways(events: list[dict[str, Any]]) -> tuple[bool, bool, bool]:
    # accept: the full event list; preview: the same list via its own lookup;
    # reuse: one event.  All three go through the one function.
    store = {REQUEST_ID: events}
    return (
        rhi.overlay_unreachable(store[REQUEST_ID], REQUEST_ID),
        rhi.overlay_unreachable(lambda rid=REQUEST_ID: store[rid], REQUEST_ID),
        rhi.overlay_unreachable(events[1], REQUEST_ID),
    )


@pytest.mark.parametrize("case", ["unreachable", "reachable", "unknown"])
def test_accept_and_preview_agree_on_one_event_history(
    tmp_path: Path, case: str
) -> None:
    if case == "unknown":
        events = _history(tmp_path / "missing.json")
    else:
        rows = [_failed()] if case == "unreachable" else [_failed(), _candidate_read()]
        events = _history(_metadata_file(tmp_path, _ledger(tmp_path, rows)))
    expected = case == "unreachable"
    assert _three_ways(events) == (expected, expected, expected)


def test_a_raising_events_lookup_is_unknown_not_an_exception() -> None:
    def boom() -> list[dict[str, Any]]:
        raise RuntimeError("metadata extraction failed")

    assert rhi.overlay_unreachable(boom, REQUEST_ID) is False


def test_a_hostile_events_value_is_unknown_not_an_exception() -> None:
    assert rhi.overlay_unreachable(object(), REQUEST_ID) is False
    assert rhi.overlay_unreachable([object(), 7, None], REQUEST_ID) is False


# ---------------------------------------------------------------------------
# The ledger row is built by the REAL audit writer, not by hand.
# ---------------------------------------------------------------------------
def _writer_ctx(tmp_path: Path) -> mcp.WorkerToolContext:
    key_path = tmp_path / "audit_hmac.key"
    key_path.write_bytes(KEY)
    return mcp.WorkerToolContext(
        task_id=TASK_ID,
        runner=RUNNER,
        topic=TOPIC,
        request_id=REQUEST_ID,
        repo=tmp_path,
        authority_repo=tmp_path,
        source_graph_targets=(),
        session_topic="",
        audit_ledger_path=tmp_path / "audit_ledger.jsonl",
        audit_hmac_key_path=key_path,
    )


def _verify(ctx: mcp.WorkerToolContext) -> dict[str, Any]:
    return mcp.verify_audit_ledger(
        ctx.audit_ledger_path,
        ctx.audit_hmac_key_path,
        task_id=TASK_ID,
        runner=RUNNER,
        topic=TOPIC,
        request_id=REQUEST_ID,
    )


def test_real_audit_writer_row_for_a_refused_overlay_call_is_counted(
    tmp_path: Path,
) -> None:
    ctx = _writer_ctx(tmp_path)
    # ``source_graph_query`` turns the WorkerToolError the candidate binding
    # raises into ``_violation(ctx, tool, str(exc))`` -- the real writer path.
    mcp._violation(ctx, "source_graph", UNAVAILABLE)

    verification = _verify(ctx)
    assert verification["candidate_overlay_unavailable_calls"] == 1
    assert verification["candidate_overlay_read_successes"] == 0
    assert rhi.overlay_unreachable_from_verification(verification) is True

    # A candidate read written by the same writer clears it.
    mcp._append_audit(
        ctx,
        tool="source_graph",
        ok=True,
        cache_hit=False,
        hit_count=1,
        bytes_returned=10,
        authority_source="candidate_overlay",
        authority_state="quality_review_readonly",
        payload={"mode": "body"},
    )
    verification = _verify(ctx)
    assert verification["candidate_overlay_unavailable_calls"] == 1
    assert verification["candidate_overlay_read_successes"] == 1
    assert rhi.overlay_unreachable_from_verification(verification) is False


def test_an_unrelated_refusal_is_not_an_overlay_refusal(tmp_path: Path) -> None:
    ctx = _writer_ctx(tmp_path)
    mcp._violation(ctx, "source_graph", "invalid_query")
    assert _verify(ctx)["candidate_overlay_unavailable_calls"] == 0


def test_measurement_from_event_history_and_real_ledger(tmp_path: Path) -> None:
    path = _metadata_file(tmp_path, _ledger(tmp_path, [_failed()]))
    assert rhi.overlay_unreachable(_history(path), REQUEST_ID) is True
    assert rhi.overlay_unreachable({"metadata_path": str(path)}, REQUEST_ID) is True
    assert (
        rhi.overlay_unreachable([{"metadata_path": str(path)}], REQUEST_ID)
        is True
    )


# ---------------------------------------------------------------------------
# quality_evidence: the shared rule, I/O-free, byte-identical when omitted.
# ---------------------------------------------------------------------------
def _finding(severity: str = qe.SEVERITY_LOW) -> dict[str, str]:
    return {
        "id": "f-1",
        "severity": severity,
        "disposition": "observation",
        "summary": "an ordinary observation, written from the docs",
        "evidence": "docs/spec.md",
    }


def _report(lens: str = qe.LENS_CORRECTNESS, **extra: object) -> dict[str, object]:
    return {
        "lens": lens,
        "provider": "reviewer-b",
        "read_only": True,
        "can_mutate_repo": False,
        "findings": [_finding()],
        **extra,
    }


def _verdict(reports: list[dict[str, object]], **kwargs: object) -> dict[str, Any]:
    return qe.fold_quality_verdict(
        [],
        risk_profile=qe.resolve_risk_profile(qe.RISK_MEDIUM),
        reviewer_reports=reports,
        combined_tree_checks=[
            qe.EvidenceCheck(
                check_id="union-tests", kind="test", status=qe.STATUS_PASSED, summary=""
            )
        ],
        **kwargs,
    )


def test_ordinary_findings_from_unreachable_reviewer_make_the_lens_blind() -> None:
    stamped = rhi.stamp_report(_report(), REQUEST_ID, True)

    assert qe.blind_reviewer_lenses([stamped]) == set()  # keyword omitted
    assert qe.blind_reviewer_lenses(
        [stamped], overlay_unreachable_reviewers=frozenset({REQUEST_ID})
    ) == {qe.LENS_CORRECTNESS}
    assert qe.reviewer_report_could_not_inspect(stamped) is False
    assert qe.reviewer_report_could_not_inspect(
        stamped, overlay_unreachable_reviewers=frozenset({REQUEST_ID})
    ) is True


def test_accept_fold_blocks_an_unreachable_reviewers_lens() -> None:
    baseline = _verdict([_report()])
    assert baseline["passed"] is True

    stamped = rhi.stamp_report(_report(), REQUEST_ID, True)
    verdict = _verdict([stamped], overlay_unreachable_reviewers=frozenset({REQUEST_ID}))

    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]
    row = next(r for r in verdict["lenses"] if r["lens"] == qe.LENS_CORRECTNESS)
    assert row["status"] == qe.STATUS_REVIEWER_COULD_NOT_INSPECT


def test_omitting_the_keyword_is_byte_identical() -> None:
    reports = [_report(), _report(qe.LENS_SECURITY)]
    assert _verdict(reports) == _verdict(
        reports, overlay_unreachable_reviewers=frozenset()
    )


def test_a_sighted_reviewer_for_the_same_lens_still_answers_a_blind_one() -> None:
    blind = rhi.stamp_report(_report(), "rq-blind", True)
    sighted = rhi.stamp_report(_report(), "rq-sighted", False)
    assert qe.blind_reviewer_lenses(
        [blind, sighted], overlay_unreachable_reviewers=frozenset({"rq-blind"})
    ) == set()


def test_a_model_cannot_set_or_clear_the_fact_through_its_report() -> None:
    # Setting: a report naming an id never makes itself blind, nor anyone else's.
    forged = _report(reviewer_request_id="rq-other")
    assert qe.blind_reviewer_lenses([forged]) == set()
    # Clearing: the hub overwrites whatever id the model wrote.
    cleared = rhi.stamp_report(
        _report(reviewer_request_id="not-my-id"), REQUEST_ID, True
    )
    assert cleared["reviewer_request_id"] == REQUEST_ID
    assert qe.blind_reviewer_lenses(
        [cleared], overlay_unreachable_reviewers=frozenset({REQUEST_ID})
    ) == {qe.LENS_CORRECTNESS}
    # A report that was NOT measured loses a model-written id, so it cannot
    # collide with another reviewer's measured id.
    plain = rhi.stamp_report(_report(reviewer_request_id=REQUEST_ID), "rq-b", False)
    assert "reviewer_request_id" not in plain
    assert qe.blind_reviewer_lenses(
        [plain], overlay_unreachable_reviewers=frozenset({REQUEST_ID})
    ) == set()
    # And with nothing measured the stamped report equals the plain copy.
    assert rhi.stamp_report(_report(), REQUEST_ID, False) == _report()


# ---------------------------------------------------------------------------
# accept_preview predicts the same refusal the accept fold makes.
# ---------------------------------------------------------------------------
def _row(
    request_id: str = REQUEST_ID, *, unreachable: bool, lens: str = qe.LENS_CORRECTNESS
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "task_id": f"T-{request_id}",
        "lens": lens,
        "packet_sha256": "c" * 64,
        "terminal_substatus": "review_ready",
        "receipt": {"report": _report(lens)},
        "request_id": request_id,
        "state": "review_ready",
        "usable": True,
        "attempt_count": 1,
        "running": False,
        "running_request_id": "",
        "state_unknown": False,
        "state_unknown_cause": "",
    }
    if unreachable:
        row["overlay_unreachable"] = True
    return row


def test_accept_preview_predicts_the_same_blind_lens_as_the_fold() -> None:
    rows = [_row(unreachable=True)]
    preview = par._blind_reviewer_lenses(rows)
    reports, unreachable = rhi.blind_inputs(rows)
    fold = _verdict(reports, overlay_unreachable_reviewers=unreachable)

    assert preview == {qe.LENS_CORRECTNESS}
    assert "reviewer_could_not_inspect:correctness" in fold["blocking_evidence"]


def test_accept_preview_unchanged_without_a_measurement() -> None:
    assert par._blind_reviewer_lenses([_row(unreachable=False)]) == set()


def test_preview_fold_blocker_is_reviewer_could_not_inspect() -> None:
    profile = qe.resolve_risk_profile(qe.RISK_MEDIUM)
    fold = par.fold_accept_blockers(
        reviewers=[_row(unreachable=True)],
        reviewer_request_ids=None,
        risk_profile=profile,
        terminal_substatus="review_ready",
    )
    kinds = {(b["kind"], b.get("lens")) for b in fold["blockers"]}
    assert ("reviewer_could_not_inspect", qe.LENS_CORRECTNESS) in kinds

    clear = par.fold_accept_blockers(
        reviewers=[_row(unreachable=False)],
        reviewer_request_ids=None,
        risk_profile=profile,
        terminal_substatus="review_ready",
    )
    assert "reviewer_could_not_inspect" not in {b["kind"] for b in clear["blockers"]}


# ---------------------------------------------------------------------------
# existing_lens_reviewer: a measured-blind reviewer is not reused.
# ---------------------------------------------------------------------------
_ATTEMPT = {
    "target_request_id": "target-req",
    "target_task_id": "TARGET_TASK",
    "lens": "correctness",
    "target_claim_epoch": "2",
}


def _sealed_card(task_id: str, request_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "terminal_review": {
            "evidence": {
                "quality_review": {
                    "lens": "correctness",
                    "packet_sha256": "c" * 64,
                    "target_request_id": "target-req",
                    "target_task_id": "TARGET_TASK",
                    "target_claim_epoch": "2",
                },
                "quality_review_receipt": {
                    "packet_sha256": "c" * 64,
                    "target": {
                        "request_id": "target-req",
                        "task_id": "TARGET_TASK",
                        "claim_epoch": "2",
                    },
                    "reviewer": {"request_id": request_id, "task_id": task_id},
                    "report": _report(),
                    "authority": {
                        "process_identity_verified": True,
                        "audit_verified": True,
                        "terminal_state": "review_ready",
                    },
                    "submission_id": "d" * 64,
                    "physical_submission_count": 1,
                    "logical_submission_count": 1,
                },
            },
        },
    }


def _reuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, metadata_path: Path | None
) -> dict[str, str] | None:
    card = _sealed_card("QR_OVERLAY", REQUEST_ID)
    monkeypatch.setattr(
        review_orchestrator.task_store, "get_task", lambda repo, task_id: card
    )
    event: dict[str, Any] = {
        "topic": "quality_review",
        "task_id": "QR_OVERLAY",
        "state": "review_ready",
        "runner": "r",
        "adapter_id": "a",
        "quality_review_attempt": dict(_ATTEMPT),
    }
    if metadata_path is not None:
        event["metadata_path"] = str(metadata_path)
    return review_orchestrator.existing_lens_reviewer(
        tmp_path,
        target_task_id="TARGET_TASK",
        target_request_id="target-req",
        claim_epoch="2",
        lens="correctness",
        attempts=[(REQUEST_ID, event, False)],
    )


def test_a_measured_blind_reviewer_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _metadata_file(tmp_path, _ledger(tmp_path, [_failed()]))
    assert _reuse(monkeypatch, tmp_path, path) is None


def test_a_sighted_or_unmeasured_reviewer_is_still_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sighted = _metadata_file(tmp_path, _ledger(tmp_path, [_failed(), _candidate_read()]))
    found = _reuse(monkeypatch, tmp_path, sighted)
    assert found is not None and found["request_id"] == REQUEST_ID
    # No metadata / no ledger at all: unknown, so behavior is unchanged.
    found = _reuse(monkeypatch, tmp_path, None)
    assert found is not None and found["state"] == "sealed"
