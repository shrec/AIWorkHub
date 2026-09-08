import json

import pytest

from aiworkhub import quality_review_ingest as ingest


def _report() -> str:
    return json.dumps({"lens": "correctness", "findings": []})


@pytest.mark.parametrize(
    "event",
    [
        {"type": "item.completed", "item": {"type": "agent_message", "text": _report()}},
        {"type": "result", "result": _report()},
        {"type": "assistant.message", "data": {"content": _report()}},
        {"type": "assistant_message", "data": {"content": _report()}},
        {"type": "text", "part": {"type": "text", "text": _report()}},
    ],
)
def test_real_provider_final_shapes(event):
    result = ingest.extract_structured_final([json.dumps(event)], expected_lens="correctness")
    assert result.status == "structured_final"
    assert result.report == {"lens": "correctness", "findings": []}


def test_tool_chatter_and_progress_are_ignored():
    events = [
        json.dumps({"type": "item.completed", "item": {"type": "command_execution", "text": _report()}}),
        json.dumps({"type": "progress", "data": {"content": _report()}}),
    ]
    assert ingest.extract_structured_final(events, expected_lens="correctness").status == "missing_final"


def test_explicit_only_dedup_and_conflict():
    report = {"lens": "correctness", "findings": []}
    legacy = ingest.ingest_structured_final([], expected_lens="correctness", explicit_report=report)
    assert legacy.status == "explicit_only"
    same = ingest.ingest_structured_final(
        [json.dumps({"type": "result", "result": json.dumps(report)})],
        expected_lens="correctness", explicit_report=report,
    )
    assert same.deduplicated is True
    with pytest.raises(ingest.ReviewProtocolError, match="explicit_submission_conflict"):
        ingest.ingest_structured_final(
            [json.dumps({"type": "result", "result": json.dumps({**report, "findings": [{"x": 1}]})})],
            expected_lens="correctness", explicit_report=report,
        )


def test_multiple_malformed_missing_and_lens_fail_closed():
    event = json.dumps({"type": "result", "result": _report()})
    with pytest.raises(ingest.ReviewProtocolError, match="multiple_structured_finals"):
        ingest.extract_structured_final([event, event], expected_lens="correctness")
    with pytest.raises(ingest.ReviewProtocolError, match="malformed_structured_output"):
        ingest.extract_structured_final(
            [json.dumps({"type": "result", "result": '{"lens":'})],
            expected_lens="correctness",
        )
    assert ingest.extract_structured_final([], expected_lens="correctness").status == "missing_final"
    with pytest.raises(ingest.ReviewProtocolError, match="lens_mismatch"):
        ingest.extract_structured_final([event], expected_lens="security")


def test_supervisor_submit_called_once():
    calls = []
    result = ingest.ingest_structured_final(
        [json.dumps({"type": "result", "result": _report()})],
        expected_lens="correctness", submit=calls.append,
    )
    assert result.submitted is True
    assert calls == [{"lens": "correctness", "findings": []}]


def test_retry_with_normalized_explicit_report_is_logical_dedup():
    raw = {"lens": "correctness", "findings": [{"severity": "low"}]}
    explicit = {
        "lens": "correctness",
        "findings": [{"severity": "low", "disposition": "observation"}],
    }

    def normalize(report):
        findings = [dict(finding) for finding in report["findings"]]
        for finding in findings:
            finding.setdefault("disposition", "observation")
        return {"lens": report["lens"], "findings": findings}

    result = ingest.ingest_structured_final(
        [json.dumps({"type": "result", "result": json.dumps(raw)})],
        expected_lens="correctness",
        explicit_report=explicit,
        normalize=normalize,
    )
    assert result.status == "deduplicated"
    assert result.deduplicated is True


def _strip_to_severity(report):
    return {
        "lens": report["lens"],
        "findings": [{"severity": finding["severity"]} for finding in report["findings"]],
    }


def test_an_explicit_receipt_is_renormalized_before_it_is_called_a_conflict():
    """The ledger receipt and the provider final are two shapes of one judgment.

    The explicit copy is the receipt producer's shape and the final is the
    supervisor's; they differ in which optional keys they carry even when the
    findings are identical.  Comparing them raw refused the pair as
    ``explicit_submission_conflict`` -- an unrepairable category that threw the
    whole review away.  A genuinely different judgment must still conflict.
    """
    final = {"lens": "correctness", "findings": [{"severity": "low"}]}
    explicit = {"lens": "correctness", "findings": [{"severity": "low", "actionable": False}]}
    events = [json.dumps({"type": "result", "result": json.dumps(final)})]

    result = ingest.ingest_structured_final(
        list(events), expected_lens="correctness",
        explicit_report=explicit, normalize=_strip_to_severity,
    )

    assert result.status == "deduplicated"
    assert result.report == _strip_to_severity(final)
    with pytest.raises(ingest.ReviewProtocolError, match="explicit_submission_conflict"):
        ingest.ingest_structured_final(
            list(events), expected_lens="correctness",
            explicit_report={"lens": "correctness", "findings": [{"severity": "high"}]},
            normalize=_strip_to_severity,
        )


def test_the_explicit_copy_is_compared_through_its_own_normalizer():
    """``normalize_explicit`` keeps the audit naming only the FINAL's repairs.

    The supervisor's ``normalize`` records every per-finding coercion on the
    audit receipt; running the explicit receipt through it would write the
    explicit copy's repairs onto the provider final's record.
    """
    final = {"lens": "correctness", "findings": [{"severity": "low"}]}
    explicit = {"lens": "correctness", "findings": [{"severity": "low", "actionable": False}]}
    order: list[str] = []

    def normalize(report):
        order.append("final")
        return _strip_to_severity(report)

    def normalize_explicit(report):
        order.append("explicit")
        return _strip_to_severity(report)

    result = ingest.ingest_structured_final(
        [json.dumps({"type": "result", "result": json.dumps(final)})],
        expected_lens="correctness", explicit_report=explicit,
        normalize=normalize, normalize_explicit=normalize_explicit,
    )

    assert result.status == "deduplicated"
    assert order == ["final", "explicit"]


def _host_tool_use_event(findings, *, name: str = "ReportFindings") -> str:
    """One claude stream-json assistant turn that filed findings through a host tool."""
    return json.dumps({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Filing the findings."},
                {"type": "tool_use", "name": name, "input": {"findings": findings}},
            ]
        },
    })


def test_the_host_report_findings_call_is_read_when_nothing_was_typed():
    """9 of 242 claude reviewer runs filed ONLY through the host's own
    ``ReportFindings`` tool and were recorded as ``missing_final`` -- complete
    reviews thrown away over a transport the supervisor never read."""
    findings = [{"severity": "medium", "summary": "s", "evidence": "src/m.py:3"}]

    result = ingest.extract_structured_final(
        [_host_tool_use_event(findings)], expected_lens="security",
    )

    assert result.status == "structured_tool_use"
    assert result.report == {"lens": "security", "findings": findings}


def test_the_host_call_report_goes_through_the_normal_normalize_and_submit_path():
    findings = [{"severity": "medium", "extra": "dropped"}]
    submitted: list[dict] = []

    result = ingest.ingest_structured_final(
        [_host_tool_use_event(findings)], expected_lens="security",
        submit=submitted.append, normalize=_strip_to_severity,
    )

    assert result.submitted is True
    assert submitted == [{"lens": "security", "findings": [{"severity": "medium"}]}]


def test_a_typed_final_wins_over_the_host_call_and_is_not_a_second_report():
    """66 of the 73 runs that used the host tool ALSO typed the final JSON.
    The typed final is authoritative and the pair is never two reports."""
    typed = {"lens": "correctness", "findings": []}
    events = [
        _host_tool_use_event([{"severity": "low", "summary": "filed"}]),
        json.dumps({"type": "result", "result": json.dumps(typed)}),
    ]

    result = ingest.extract_structured_final(events, expected_lens="correctness")

    assert result.status == "structured_final"
    assert result.report == typed


def test_only_the_last_allowlisted_host_call_carrying_findings_counts():
    last = [{"severity": "high", "summary": "last"}]
    events = [
        _host_tool_use_event([{"severity": "low", "summary": "first"}]),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "ReportFindings", "input": {"note": "no findings"}},
        ]}}),
        _host_tool_use_event(last),
    ]

    result = ingest.extract_structured_final(events, expected_lens="code_quality")

    assert result.report == {"lens": "code_quality", "findings": last}
    # An ordinary tool call is not a report channel, whatever its input holds.
    assert ingest.extract_structured_final(
        [_host_tool_use_event(last, name="Read")], expected_lens="code_quality",
    ).status == "missing_final"


def test_review_finding_aliases_are_copied_and_evidence_is_preserved():
    original = {
        "lens": "correctness",
        "findings": [{
            "severity": "high",
            "summary": "missing validation",
            "evidence": "validation is skipped",
            "actionable": True,
            "evidence_reference": {
                "path": "src/aiworkhub/quality_review_ingest.py",
                "line_start": 173,
                "line_end": 180,
            },
        }],
    }

    normalized = ingest._normalize_review_finding_aliases(original)

    assert original["findings"][0]["actionable"] is True
    assert "evidence_reference" in original["findings"][0]
    finding = normalized["findings"][0]
    assert "actionable" not in finding
    assert "evidence_reference" not in finding
    assert finding["evidence"] == "validation is skipped"
    assert finding["path"] == "src/aiworkhub/quality_review_ingest.py"
    assert finding["line_start"] == 173
    assert finding["line_end"] == 180


@pytest.mark.parametrize(
    "finding",
    [
        {"actionable": 1},
        {"evidence_reference": "src/aiworkhub/quality_review_ingest.py:173"},
        {"evidence_reference": {"path": "src/aiworkhub/quality_review_ingest.py", "line_start": True}},
        {"evidence_reference": {"path": "src/aiworkhub/quality_review_ingest.py", "unknown": 1}},
    ],
)
def test_review_finding_aliases_reject_malformed_values(finding):
    with pytest.raises(ingest.ReviewProtocolError, match="structured_report_invalid"):
        ingest._normalize_review_finding_aliases({"lens": "correctness", "findings": [finding]})


def test_alias_layer_passes_unknown_keys_on_to_the_ingest_stripper():
    # The alias layer only translates; normalize_review_findings is the layer
    # that filters a finding against the canonical ingress allowlist.
    raw = {"actionable": False, "unexpected": "stripped by the ingest driver"}

    aliased = ingest._normalize_review_finding_aliases(
        {"lens": "correctness", "findings": [dict(raw)]}
    )
    stripped, record, kept = ingest.normalize_review_findings(
        {"lens": "correctness", "findings": [dict(raw)]}
    )

    assert aliased["findings"] == [{"unexpected": "stripped by the ingest driver"}]
    assert stripped["findings"] == [{}]
    assert kept == [0]
    assert record == [{"index": 0, "coerced": ["actionable"], "stripped": ["unexpected"]}]


def test_provider_compatible_finding_aliases_are_normalized_to_canonical_fields():
    normalized = ingest._normalize_review_finding_aliases({
        "lens": "correctness",
        "findings": [{
            "severity": "high",
            "file": "src/aiworkhub/quality_review_ingest.py",
            "line": 173,
            "failure_scenario": "call with malformed input raises",
            "short_summary": "missing validation",
        }],
    })

    finding = normalized["findings"][0]
    assert finding["path"] == "src/aiworkhub/quality_review_ingest.py"
    assert finding["line_start"] == 173
    assert finding["line_end"] == 173
    assert finding["reproduction"] == "call with malformed input raises"
    assert finding["summary"] == "missing validation"
    assert "file" not in finding
    assert "line" not in finding
    assert "failure_scenario" not in finding
    assert "short_summary" not in finding


def test_matching_alias_and_canonical_values_are_accepted_as_idempotent():
    normalized = ingest._normalize_review_finding_aliases({
        "lens": "correctness",
        "findings": [{
            "file": "src/module.py",
            "path": "src/module.py",
            "line": 12,
            "line_start": 12,
            "line_end": 12,
        }],
    })

    finding = normalized["findings"][0]
    assert finding["path"] == "src/module.py"
    assert finding["line_start"] == 12
    assert finding["line_end"] == 12


@pytest.mark.parametrize(
    "finding",
    [
        {"file": "src/a.py", "path": "src/b.py"},
        {"line": 12, "line_start": 13},
        {"line": 12, "line_end": 13},
        {"failure_scenario": "a", "reproduction": "b"},
        {"short_summary": "a", "summary": "b"},
    ],
)
def test_conflicting_alias_and_canonical_values_fail_closed(finding):
    with pytest.raises(ingest.ReviewProtocolError, match="structured_report_invalid"):
        ingest._normalize_review_finding_aliases({"lens": "correctness", "findings": [finding]})


@pytest.mark.parametrize(
    "finding",
    [
        {"file": ""},
        {"file": 1},
        {"line": 0},
        {"line": 1_000_001},
        {"line": "12"},
        {"line": True},
        {"failure_scenario": ""},
        {"failure_scenario": 1},
        {"short_summary": ""},
        {"short_summary": 1},
    ],
)
def test_malformed_alias_values_fail_closed(finding):
    with pytest.raises(ingest.ReviewProtocolError, match="structured_report_invalid"):
        ingest._normalize_review_finding_aliases({"lens": "correctness", "findings": [finding]})


def test_provider_verdict_is_stripped_and_cannot_gain_authority():
    normalized = ingest._normalize_review_finding_aliases({
        "lens": "correctness",
        "findings": [{
            "severity": "low",
            "disposition": "observation",
            "verdict": "accept",
        }],
    })

    finding = normalized["findings"][0]
    assert "verdict" not in finding
    assert finding["disposition"] == "observation"
    assert finding["severity"] == "low"


def test_multiple_findings_each_normalized_independently():
    normalized = ingest._normalize_review_finding_aliases({
        "lens": "correctness",
        "findings": [
            {"file": "src/a.py", "line": 1},
            {"failure_scenario": "boom", "verdict": "reject"},
            {"short_summary": "s2"},
        ],
    })

    findings = normalized["findings"]
    assert findings[0]["path"] == "src/a.py"
    assert findings[0]["line_start"] == 1
    assert findings[0]["line_end"] == 1
    assert findings[1]["reproduction"] == "boom"
    assert "verdict" not in findings[1]
    assert findings[2]["summary"] == "s2"


class _Workspace:
    """Minimal stand-in for the reviewer workspace ``supervisor_ingest`` reads."""

    def __init__(self, repo: str) -> None:
        self.repo = repo


def _accept_all_findings(packet, *, lens, findings):
    """Stand in for the canonical validator when every finding is usable."""
    return [dict(finding) for finding in findings]


def _refusing_validator(*refused_summaries):
    """Refuse the named findings the way ``normalize_packet_findings`` does."""
    from aiworkhub import quality_reviewer

    def normalize_packet_findings(packet, *, lens, findings):
        for index, finding in enumerate(findings):
            if finding.get("summary") in refused_summaries:
                raise quality_reviewer.ReviewerEvidenceError(
                    f"review_finding_{index}_exact_evidence_required"
                )
        return [dict(finding) for finding in findings]

    return normalize_packet_findings


def _run_supervisor_ingest(tmp_path, monkeypatch, *, findings, validator):
    """Drive the real supervisor ingest over one provider final."""
    from aiworkhub import quality_reviewer, worker_ai_tools_mcp

    stdout = tmp_path / "req-1.stdout.log"
    stdout.write_text(
        json.dumps({
            "type": "result",
            "result": json.dumps({"lens": "correctness", "findings": findings}),
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        worker_ai_tools_mcp,
        "verify_audit_ledger",
        lambda *args, **kwargs: {"ok": True, "entries_tampered": 0, "verified_payloads": []},
    )
    monkeypatch.setattr(quality_reviewer, "normalize_packet_findings", validator)
    monkeypatch.setattr(worker_ai_tools_mcp, "WorkerToolContext", lambda **kwargs: object())
    submitted = []

    def quality_review_submit(ctx, **kwargs):
        submitted.append(dict(kwargs))
        return {"ok": True}

    monkeypatch.setattr(worker_ai_tools_mcp, "quality_review_submit", quality_review_submit)
    verification, _payloads = ingest.supervisor_ingest(
        metadata={
            "worker_mcp": {
                "audit_ledger_path": str(tmp_path / "ledger.jsonl"),
                "audit_hmac_key_path": str(tmp_path / "ledger.key"),
            },
            "stdout_path": str(stdout),
            "task_id": "task-1",
            "runner": "claude",
            "topic": "quality_review",
        },
        workspace=_Workspace(str(tmp_path)),
        packet={"packet_sha256": "packet-sha"},
        packet_path=tmp_path / "packet.json",
        request_id="req-1",
        expected_lens="correctness",
    )
    return verification, submitted


@pytest.mark.parametrize(
    "alias, value, canonical",
    [
        ("file", "src/a.py", {"path": "src/a.py"}),
        ("line", 7, {"line_start": 7, "line_end": 7}),
        ("failure_scenario", "boom", {"reproduction": "boom"}),
        ("short_summary", "s", {"summary": "s"}),
    ],
)
def test_each_provider_alias_key_is_coerced_and_recorded(alias, value, canonical):
    normalized, record, kept = ingest.normalize_review_findings(
        {"lens": "correctness", "findings": [{alias: value}]}
    )

    finding = normalized["findings"][0]
    assert kept == [0]
    assert alias not in finding
    for key, expected in canonical.items():
        assert finding[key] == expected
    assert record == [{"index": 0, "coerced": [alias]}]


def test_provider_verdict_is_recorded_as_coerced_and_never_reaches_the_finding():
    normalized, record, _kept = ingest.normalize_review_findings({
        "lens": "correctness",
        "findings": [{"severity": "low", "verdict": "accept"}],
    })

    assert normalized["findings"] == [{"severity": "low"}]
    assert record == [{"index": 0, "coerced": ["verdict"]}]


def test_unknown_keys_are_stripped_and_recorded_instead_of_failing_the_report():
    normalized, record, kept = ingest.normalize_review_findings({
        "lens": "correctness",
        "findings": [{
            "severity": "high",
            "summary": "missing validation",
            "evidence": "validation is skipped",
            "failure_scenario": "call with malformed input",
            "unexpected": "provider tool field",
            "also_unexpected": 1,
        }],
    })

    finding = normalized["findings"][0]
    assert kept == [0]
    assert "unexpected" not in finding
    assert "also_unexpected" not in finding
    assert finding["reproduction"] == "call with malformed input"
    assert record == [{
        "index": 0,
        "coerced": ["failure_scenario"],
        "stripped": ["also_unexpected", "unexpected"],
    }]


def test_stripped_keys_are_filtered_against_the_canonical_ingress_allowlist():
    from aiworkhub import quality_reviewer

    normalized, _record, _kept = ingest.normalize_review_findings({
        "lens": "correctness",
        "findings": [{
            "severity": "low",
            "summary": "s",
            "evidence": "e",
            "confidence": "high",
            "not_a_schema_key": "x",
        }],
    })

    finding = normalized["findings"][0]
    assert set(finding) <= quality_reviewer.QUALITY_REVIEW_FINDING_INGRESS_KEYS
    assert finding["confidence"] == "high"


def test_a_stripped_key_never_reaches_the_canonical_validator(tmp_path, monkeypatch):
    seen = []

    def validator(packet, *, lens, findings):
        seen.extend(dict(finding) for finding in findings)
        return [dict(finding) for finding in findings]

    _verification, submitted = _run_supervisor_ingest(
        tmp_path,
        monkeypatch,
        findings=[{
            "severity": "critical",
            "summary": "s",
            "evidence": "e",
            "verdict": "reject",
            "vendor_only": "must not influence the verdict",
        }],
        validator=validator,
    )

    assert seen == [{"severity": "critical", "summary": "s", "evidence": "e"}]
    assert submitted[0]["findings"] == seen


def test_malformed_finding_at_index_zero_does_not_discard_the_valid_findings():
    normalized, record, kept = ingest.normalize_review_findings({
        "lens": "correctness",
        "findings": [
            {"file": ""},
            {"severity": "high", "summary": "kept", "evidence": "e", "file": "src/a.py"},
            {"line": 0},
            {"severity": "low", "summary": "also kept", "evidence": "e"},
        ],
    })

    findings = normalized["findings"]
    assert kept == [1, 3]
    assert [finding["summary"] for finding in findings] == ["kept", "also kept"]
    assert findings[0]["path"] == "src/a.py"
    dropped = [entry for entry in record if "dropped" in entry]
    assert [entry["index"] for entry in dropped] == [0, 2]
    assert dropped[0]["dropped"].endswith("review_finding_0_file_invalid")
    assert dropped[1]["dropped"].endswith("review_finding_2_line_invalid")
    assert {"index": 1, "coerced": ["file"]} in record


def test_non_object_findings_are_dropped_with_a_reason_not_passed_downstream():
    normalized, record, kept = ingest.normalize_review_findings({
        "lens": "correctness",
        "findings": ["prose instead of a finding", {"severity": "low"}],
    })

    assert normalized["findings"] == [{"severity": "low"}]
    assert kept == [1]
    assert record == [{"index": 0, "dropped": "review_finding_0_not_object"}]


@pytest.mark.parametrize(
    "reason, expected",
    [
        ("review_finding_0_unknown_key:file,line", 0),
        ("review_finding_12_exact_evidence_required", 12),
        ("review_findings_overflow", None),
        ("review_finding_x_invalid", None),
        ("review_finding_3", None),
        ("quality_review_finding_0_keys_invalid", None),
    ],
)
def test_review_finding_error_index_is_recovered_only_from_canonical_reasons(
    reason, expected
):
    assert ingest._review_finding_error_index(reason) == expected


def test_canonical_validation_continues_past_a_refused_finding(tmp_path, monkeypatch):
    verification, submitted = _run_supervisor_ingest(
        tmp_path,
        monkeypatch,
        findings=[
            {"severity": "high", "summary": "refused", "evidence": "e"},
            {"severity": "low", "summary": "kept", "evidence": "e"},
        ],
        validator=_refusing_validator("refused"),
    )

    assert [finding["summary"] for finding in submitted[0]["findings"]] == ["kept"]
    assert verification["review_finding_normalization"] == [
        {"index": 0, "dropped": "review_finding_0_exact_evidence_required"}
    ]


def test_normalization_record_is_attached_to_the_returned_audit_receipt(
    tmp_path, monkeypatch
):
    verification, _submitted = _run_supervisor_ingest(
        tmp_path,
        monkeypatch,
        findings=[
            {"severity": "low", "summary": "s", "evidence": "e", "file": "src/a.py",
             "line": 4, "unexpected": 1},
            {"file": ""},
        ],
        validator=_accept_all_findings,
    )

    assert verification["review_finding_normalization"] == [
        {"index": 0, "coerced": ["file", "line"], "stripped": ["unexpected"]},
        {"index": 1, "dropped": "structured_report_invalid:review_finding_1_file_invalid"},
    ]


def test_a_clean_report_leaves_no_normalization_record_on_the_receipt(
    tmp_path, monkeypatch
):
    verification, _submitted = _run_supervisor_ingest(
        tmp_path,
        monkeypatch,
        findings=[{"severity": "low", "summary": "s", "evidence": "e"}],
        validator=_accept_all_findings,
    )

    assert "review_finding_normalization" not in verification


def test_a_report_whose_every_finding_is_refused_still_fails_closed(
    tmp_path, monkeypatch
):
    with pytest.raises(
        ingest.ReviewProtocolError, match="review_findings_all_invalid"
    ) as excinfo:
        _run_supervisor_ingest(
            tmp_path,
            monkeypatch,
            findings=[{"severity": "high", "summary": "refused", "evidence": "e"}],
            validator=_refusing_validator("refused"),
        )

    assert "0:review_finding_0_exact_evidence_required" in excinfo.value.category


def test_a_reviewer_error_without_a_finding_index_is_never_swallowed(
    tmp_path, monkeypatch
):
    from aiworkhub import quality_reviewer

    def validator(packet, *, lens, findings):
        raise quality_reviewer.ReviewerEvidenceError("review_findings_overflow")

    with pytest.raises(ingest.ReviewProtocolError, match="review_findings_overflow"):
        _run_supervisor_ingest(
            tmp_path,
            monkeypatch,
            findings=[{"severity": "low", "summary": "s", "evidence": "e"}],
            validator=validator,
        )


def test_an_empty_findings_list_is_still_a_clean_report():
    normalized, record, kept = ingest.normalize_review_findings(
        {"lens": "correctness", "findings": []}
    )

    assert normalized == {"lens": "correctness", "findings": []}
    assert record == []
    assert kept == []


def test_dropped_reasons_are_bounded_and_never_empty():
    record = [{"index": index, "dropped": f"r{index}"} for index in range(50)]

    reasons = ingest._dropped_reasons(record)

    assert reasons.count(",") == ingest.MAX_RECORDED_DROP_REASONS - 1
    assert reasons.startswith("0:r0,")
    assert ingest._dropped_reasons([]) == "no_findings_retained"


# --- NF-2026-00667: the bounded correction ask ----------------------------
#
# EVERY reason below was cut verbatim from ``card_json.blocker_reason`` on a
# live blocked card in this repository's canonical store
# (``.aiworkhub/tasking/task_queue.sqlite``), read 2026-09-07. They are the
# bytes a producer actually emitted, not a shape invented to match the parser:
# 45 blocked ``review_protocol:*`` reviewer cards existed at that read, and
# these are one of each distinct family and detail shape among them.
_STORED_REJECTIONS = {
    # CODEX54_QR_PONYTAIL_RULECOPY_V1_E34_CODE_QUALITY_V13
    "exact_evidence": (
        "review_protocol:structured_report_invalid:"
        "review_finding_0_exact_evidence_required"
    ),
    # CLAUDESONNET5_QR_PONYTAIL_MINIMALITY_V1_E38_SECURITY_V7 -- the key list
    # is ONE comma-separated tail; splitting it dropped every key but the first.
    "unknown_keys": (
        "review_protocol:structured_report_invalid:"
        "review_finding_0_unknown_key:category,file,line"
    ),
    # CLAUDESONNET5_QR_NF622_V7R3_SECURITY_V1 -- a non-zero finding index.
    "later_finding": (
        "review_protocol:structured_report_invalid:"
        "review_finding_1_overbuild_replacement_required"
    ),
    # CLAUDESONNET5_QR_RELEASE_01098_V2_CORRECTNESS_V1
    "multiple_finals": "review_protocol:multiple_structured_finals",
}

# CLAUDESONNET5_QR_NF658_V6_SECURITY_V1 -- the tail here is 500 characters of
# the reviewer's OWN prose, which must never be echoed back into a prompt.
_STORED_PROSE_REJECTION = (
    "review_protocol:no_report_in_final:Reported one critical finding: the "
    "per-request audit-ledger HMAC key (`worker_ai_tools_mcp.py`'s "
    "`task_mcp_worker_runtime/audit_hmac.key`) lives inside the sandboxed "
    "worker's own $HOME"
)

# DEEPSEEKV4P_QR_NF625_M2_SECURITY_V1 -- the provider stream, not the reviewer.
_STORED_INFRASTRUCTURE_REJECTION = "review_protocol:provider_events_oversized"


@pytest.mark.parametrize("stored", sorted(_STORED_REJECTIONS.values()))
def test_stored_rejections_each_yield_one_bounded_ask(stored: str) -> None:
    request = ingest.schema_repair_request(stored)

    assert request is not None, stored
    assert request["schema_id"] == ingest.SCHEMA_REPAIR_SCHEMA_ID
    assert request["category"] in stored
    assert request["instruction"]
    assert len(request["detail"]) <= ingest.MAX_REPAIR_DETAIL_CHARS


def test_an_unknown_key_reason_keeps_every_key_it_named() -> None:
    request = ingest.schema_repair_request(_STORED_REJECTIONS["unknown_keys"])

    assert request is not None
    assert request["detail"] == "review_finding_0_unknown_key:category,file,line"


def test_a_prose_tail_is_never_echoed_back_into_the_ask() -> None:
    request = ingest.schema_repair_request(_STORED_PROSE_REJECTION)

    assert request is not None
    assert request["category"] == "no_report_in_final"
    assert request["detail"] == ""
    assert "HMAC" not in json.dumps(request)


def test_an_infrastructure_failure_is_never_asked_of_the_reviewer() -> None:
    assert ingest.schema_repair_request(_STORED_INFRASTRUCTURE_REJECTION) is None


@pytest.mark.parametrize("reason", ["", None, "worker_failed:exit=1", "  "])
def test_an_unrelated_reason_asks_for_nothing(reason: str | None) -> None:
    assert ingest.schema_repair_request(reason) is None


def test_a_shapeless_structured_report_invalid_asks_for_nothing() -> None:
    """Without a finding this module named, the ask degenerates to "try again"."""

    assert ingest.schema_repair_request("review_protocol:structured_report_invalid") is None
    assert (
        ingest.schema_repair_request(
            "review_protocol:structured_report_invalid:Something Went Wrong"
        )
        is None
    )


def test_a_bare_category_is_accepted_as_well_as_the_durable_reason() -> None:
    """``exc.category`` and ``card_json.blocker_reason`` are the same fact."""

    assert (
        ingest.schema_repair_request("multiple_structured_finals")
        == ingest.schema_repair_request("review_protocol:multiple_structured_finals")
    )


def test_every_protocol_category_is_repairable_or_disclaimed() -> None:
    """Derived from the source, so a new refusal cannot arrive unaskable.

    The same positive control ``terminal_failure_classification`` keeps for its
    own vocabulary: a category added to this module lands in neither set and
    fails here, rather than silently becoming a refusal no reviewer is ever
    asked to fix.
    """

    import ast
    import pathlib

    source = pathlib.Path(ingest.__file__).read_text(encoding="utf-8")
    raised: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "ReviewProtocolError" or not node.args:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            raised.add(argument.value)
        elif isinstance(argument, ast.JoinedStr):
            for part in argument.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    raised.add(part.value)
                    break
        elif isinstance(argument, ast.IfExp):
            for branch in (argument.body, argument.orelse):
                if isinstance(branch, ast.Constant) and isinstance(branch.value, str):
                    raised.add(branch.value)

    assert raised, "no ReviewProtocolError categories were recovered from the source"
    assert ingest.unclassified_protocol_categories(raised) == ()
    # Positive control: a category nobody placed must be reported, or the gate
    # above would pass in a vacuum.
    assert ingest.unclassified_protocol_categories(
        {"a_refusal_nobody_reasoned_about"}
    ) == ("a_refusal_nobody_reasoned_about",)
