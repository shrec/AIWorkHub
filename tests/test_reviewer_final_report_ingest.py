"""Reviewer final-report ingestion: prose-wrapped, bare, fenced, and absent JSON.

Regression coverage for the self-hosting blocker where a capable reviewer ran to
completion with a correct review but ended with a markdown prose summary instead
of a bare JSON object.  ``ingest_structured_final`` found no report, nothing
reached the durable ledger, and the finalizer refused with a bare
``quality_review_submission_count:0``.  The tolerant final-text parser must now
accept the last balanced JSON object -- bare, prose-wrapped, or fenced -- while a
genuinely JSON-free final still fails closed with a bounded excerpt of the
reviewer's actual output rather than a bare count.
"""

from __future__ import annotations

import json

import pytest

from aiworkhub.quality_review_ingest import (
    ReviewProtocolError,
    extract_structured_final,
    ingest_structured_final,
)

LENS = "correctness"


def _report() -> dict:
    return {"lens": LENS, "findings": []}


def _result_event(text: str) -> str:
    return json.dumps({"type": "result", "result": text})


def _ingest(text: str):
    submitted: list[dict] = []
    result = ingest_structured_final(
        [_result_event(text)],
        expected_lens=LENS,
        submit=submitted.append,
    )
    return result, submitted


def test_markdown_prose_wrapping_json_ingests_exactly_one_report() -> None:
    # Reproduction: fails on current code (zero reports ingested) and passes
    # after the tolerant parser lands.
    final = (
        "I've verified the candidate against every correctness invariant.\n"
        "- inputs are bounded\n"
        "- the ledger append is fail-closed\n\n"
        "Here is the structured report:\n"
        f"{json.dumps(_report())}\n"
    )
    result, submitted = _ingest(final)
    assert submitted == [_report()]
    assert result.submitted is True
    assert result.report == _report()


def test_bare_json_final_ingests_exactly_one_report() -> None:
    result, submitted = _ingest(json.dumps(_report()))
    assert submitted == [_report()]
    assert result.submitted is True
    assert result.report == _report()


def test_fenced_json_final_ingests_exactly_one_report() -> None:
    final = (
        "Summary: no blocking defects found.\n\n"
        "```json\n"
        f"{json.dumps(_report(), indent=2)}\n"
        "```\n"
    )
    result, submitted = _ingest(final)
    assert submitted == [_report()]
    assert result.submitted is True
    assert result.report == _report()


def test_final_without_any_json_fails_with_bounded_excerpt() -> None:
    final = (
        "I've verified the candidate against every correctness invariant and "
        "found no defects worth reporting."
    )
    submitted: list[dict] = []
    with pytest.raises(ReviewProtocolError) as excinfo:
        ingest_structured_final(
            [_result_event(final)],
            expected_lens=LENS,
            submit=submitted.append,
        )
    assert submitted == []
    reason = str(excinfo.value)
    # Carries a real excerpt of the reviewer output, not a bare count.
    assert "submission_count" not in reason
    assert "correctness invariant" in reason
    assert excinfo.value.category.startswith("no_report_in_final:")


def test_extract_reports_unstructured_final_with_bounded_excerpt() -> None:
    final = "purely prose, no json at all"
    result = extract_structured_final([_result_event(final)], expected_lens=LENS)
    assert result.report is None
    assert result.status == "unstructured_final"
    assert final in result.final_excerpt


def test_prose_with_a_lone_quote_still_finds_the_report() -> None:
    # A stray quote in prose must not swallow the trailing report object.
    final = (
        'The reviewer noted the string "candidate" is bounded; report follows.\n'
        f"{json.dumps(_report())}"
    )
    result, submitted = _ingest(final)
    assert submitted == [_report()]
    assert result.submitted is True
