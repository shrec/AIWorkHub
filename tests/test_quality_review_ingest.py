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
