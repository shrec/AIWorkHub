"""A reviewer that writes the review-submit call as plain text (NF-2026-02100).

Measured on opencode + deepseek-v4-pro (needfix-NF-2026-02089, correctness
lens): the final message carried the submit tool call as TEXT, the reviewer
ended ``review_protocol:no_report_in_final`` with findings ``[]``, and the
required lens stayed missing.  A readable text call must now be salvaged like
a real report; an unreadable one must be a named provider protocol failure.
"""

from __future__ import annotations

import json

import pytest

from aiworkhub.quality_review_ingest import (
    ReviewProtocolError,
    _looks_like_text_submit_call,
    _salvage_text_submit,
    ingest_structured_final,
    schema_repair_request,
    unclassified_protocol_categories,
)

LENS = "correctness"
TOOL = "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_quality_review_submit"
FINDING = {
    "severity": "high",
    "summary": "ledger append is not fail-closed",
    "evidence": {"path": "src/aiworkhub/x.py", "line_start": 3, "line_end": 4},
}


def _event(text: str) -> str:
    return json.dumps({"type": "result", "result": text})


def _ingest(text: str, **kwargs):
    submitted: list[dict] = []
    result = ingest_structured_final(
        [_event(text)], expected_lens=LENS, submit=submitted.append, **kwargs
    )
    return result, submitted


def _args(**overrides) -> dict:
    args = {"packet_sha256": "ab" * 32, "lens": LENS, "findings": [FINDING]}
    args.update(overrides)
    return args


def _expected(findings=None) -> dict:
    return {"lens": LENS, "findings": [FINDING] if findings is None else findings}


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            f"Submitting now.\n{TOOL}({json.dumps(_args())})", id="function_call"
        ),
        pytest.param(
            json.dumps({"name": TOOL, "arguments": _args()}), id="json_envelope"
        ),
        pytest.param(
            json.dumps({"tool": TOOL, "input": _args()}), id="input_envelope"
        ),
        pytest.param(
            json.dumps({"name": TOOL, "arguments": json.dumps(_args())}),
            id="arguments_as_json_string",
        ),
        pytest.param(
            "```json\n"
            + json.dumps({"tool_calls": [{"function": {"name": TOOL, "arguments": _args()}}]})
            + "\n```",
            id="tool_calls_list_fenced",
        ),
        pytest.param(
            f'<tool_call name="{TOOL}">\n'
            + f'<parameter name="lens">{LENS}</parameter>\n'
            + f'<parameter name="findings">{json.dumps([FINDING])}</parameter>\n'
            + "</tool_call>",
            id="xml_parameters",
        ),
        pytest.param(
            json.dumps({"name": TOOL, "arguments": {"findings": [FINDING]}}),
            id="lens_omitted_borrows_expected",
        ),
    ],
)
def test_text_submit_call_is_salvaged_into_one_normal_report(text: str) -> None:
    result, submitted = _ingest(text)
    assert submitted == [_expected()]
    assert result.submitted is True
    assert result.report == _expected()


def test_salvaged_empty_findings_is_a_real_empty_report() -> None:
    text = json.dumps({"name": TOOL, "arguments": _args(findings=[])})
    result, submitted = _ingest(text)
    assert submitted == [_expected([])]
    assert result.report == _expected([])


def test_salvaged_report_drops_non_report_keys() -> None:
    result, _ = _ingest(json.dumps({"name": TOOL, "arguments": _args()}))
    assert set(result.report) == {"lens", "findings"}


def test_salvaged_wrong_lens_still_fails_as_lens_mismatch() -> None:
    text = json.dumps({"name": TOOL, "arguments": _args(lens="security")})
    with pytest.raises(ReviewProtocolError, match="lens_mismatch"):
        _ingest(text)


def test_last_text_call_wins_within_one_final() -> None:
    first = json.dumps({"name": TOOL, "arguments": _args(findings=[])})
    second = json.dumps({"name": TOOL, "arguments": _args()})
    result, _ = _ingest(f"retrying\n{first}\n{second}")
    assert result.report == _expected()


def test_salvaged_text_call_matches_an_explicit_receipt_as_one_report() -> None:
    text = json.dumps({"name": TOOL, "arguments": _args()})
    result, submitted = _ingest(text, explicit_report=_expected())
    assert submitted == []
    assert result.status == "deduplicated"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(f"{TOOL}(this is not json)", id="garbled_arguments"),
        pytest.param(f"I will call {TOOL}({{ but then ran out", id="truncated_call"),
        pytest.param(
            json.dumps({"name": TOOL, "arguments": {"summary": "no findings key"}}),
            id="envelope_without_findings",
        ),
        pytest.param(
            json.dumps({"name": TOOL, "arguments": {"findings": "not-a-list"}}),
            id="findings_not_a_list",
        ),
    ],
)
def test_unreadable_text_call_is_a_named_protocol_failure(text: str) -> None:
    with pytest.raises(ReviewProtocolError) as caught:
        _ingest(text)
    assert caught.value.category.startswith("text_submit_unparseable:")
    assert str(caught.value).startswith("review_protocol:text_submit_unparseable:")


def test_text_submit_failure_is_not_a_silent_empty_findings_list() -> None:
    submitted: list[dict] = []
    with pytest.raises(ReviewProtocolError):
        ingest_structured_final(
            [_event(f"{TOOL}(oops)")], expected_lens=LENS, submit=submitted.append
        )
    assert submitted == []


def test_unreadable_text_call_defers_to_an_explicit_receipt() -> None:
    result, submitted = _ingest(f"done, called {TOOL}(...)", explicit_report=_expected())
    assert submitted == []
    assert result.status == "explicit_only"
    assert result.report == _expected()


def test_prose_without_a_call_keeps_the_old_classification() -> None:
    with pytest.raises(ReviewProtocolError, match="no_report_in_final"):
        _ingest("The change looks correct and I found nothing to report.")


def test_prose_naming_the_tool_without_calling_it_is_not_a_text_submit() -> None:
    with pytest.raises(ReviewProtocolError, match="no_report_in_final"):
        _ingest("I submitted my findings through the quality_review_submit tool.")


def test_malformed_json_without_a_submit_call_keeps_its_diagnostic() -> None:
    with pytest.raises(ReviewProtocolError, match="malformed_structured_output"):
        _ingest('{"lens": "correctness", "findings": [')


def test_bare_report_json_is_unchanged() -> None:
    result, submitted = _ingest(json.dumps(_expected()))
    assert submitted == [_expected()]
    assert result.status == "submitted"


def test_new_category_is_classified_and_repairable() -> None:
    assert unclassified_protocol_categories(["text_submit_unparseable"]) == ()
    repair = schema_repair_request("review_protocol:text_submit_unparseable:abc")
    assert repair is not None
    assert repair["category"] == "text_submit_unparseable"


def test_broken_final_call_never_falls_back_to_an_earlier_quoted_example() -> None:
    text = (
        'example: quality_review_submit({"lens":"correctness","findings":[]}) '
        'and my final: quality_review_submit({"lens":"correctness","findings":[{"broken'
    )
    assert _salvage_text_submit(text, LENS) is None
    submitted: list[dict] = []
    with pytest.raises(ReviewProtocolError):
        ingest_structured_final([_event(text)], expected_lens=LENS, submit=submitted.append)
    assert submitted == []


def test_prose_mentioning_the_tool_with_a_space_is_not_a_text_submit_call() -> None:
    assert not _looks_like_text_submit_call(
        "I will call it via quality_review_submit (the MCP tool) later."
    )
