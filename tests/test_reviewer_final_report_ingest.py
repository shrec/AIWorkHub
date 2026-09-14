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


# --- NF-2026-00163: the same one report, out of an amplified stream --------
#
# The sibling blocker to the prose final above, and the same loss: a complete
# review was thrown away over the transport rather than its content.  Here the
# reviewer typed a perfectly good JSON final, but its turn had also emitted
# more than ``MAX_EVENTS`` live-progress records, so the retained stream was
# refused as ``provider_events_oversized`` before the final was ever read.
# Those known replayable events are compacted out of the retained stream;
# submission must still happen exactly once, from the same report.

_PROGRESS_TYPES = ("assistant.message_delta", "session.background_tasks_changed")


def _progress_event(index: int) -> str:
    return json.dumps({
        "type": _PROGRESS_TYPES[index % len(_PROGRESS_TYPES)],
        "data": {"delta": f"chunk {index}", "tasks": [{"id": f"bg-{index}"}]},
    })


def _amplified(final: str, *, progress: int) -> list[str]:
    """One real final buried in ``progress`` known replayable events."""
    half = progress // 2
    return [
        *(_progress_event(index) for index in range(half)),
        _result_event(final),
        *(_progress_event(index) for index in range(half, progress)),
    ]


def test_an_amplified_stream_still_submits_exactly_one_report() -> None:
    from aiworkhub.quality_review_ingest import MAX_EVENTS

    events = _amplified(json.dumps(_report()), progress=MAX_EVENTS + 500)
    submitted: list[dict] = []

    result = ingest_structured_final(
        events, expected_lens=LENS, submit=submitted.append
    )

    assert len(events) > MAX_EVENTS
    # Byte-for-byte the outcome of the unamplified stream.
    assert submitted == [_report()]
    assert result.submitted is True
    assert result.report == _report()


def test_the_amplified_and_unamplified_streams_agree_on_the_report() -> None:
    from aiworkhub.quality_review_ingest import MAX_EVENTS

    final = json.dumps(_report())
    plain, _ = _ingest(final)
    amplified = ingest_structured_final(
        _amplified(final, progress=MAX_EVENTS + 500),
        expected_lens=LENS,
        submit=lambda report: None,
    )

    assert amplified.report == plain.report
    assert amplified.status == plain.status == "submitted"


def test_the_submitted_result_measures_only_persisted_savings() -> None:
    from aiworkhub.quality_review_ingest import MAX_EVENTS

    events = _amplified(json.dumps(_report()), progress=MAX_EVENTS + 500)

    result = ingest_structured_final(
        events, expected_lens=LENS, submit=lambda report: None
    )
    record = result.event_compaction

    assert record["retained_events"] == 1
    assert record["persisted_events_dropped"] == MAX_EVENTS + 500
    assert record["saving_scope"] == "persisted_bytes_and_records_only"
    # The provider generated and billed every dropped event before this reader
    # saw it, so nothing here may be dressed up as a token saving.
    assert "token" not in json.dumps(record).lower()


def test_an_amplified_stream_without_a_report_still_fails_closed() -> None:
    """Compaction removes chatter, never the reason a review was refused."""
    from aiworkhub.quality_review_ingest import MAX_EVENTS

    final = "I reviewed everything and found no defects worth reporting."
    events = _amplified(final, progress=MAX_EVENTS + 500)
    submitted: list[dict] = []

    with pytest.raises(ReviewProtocolError) as excinfo:
        ingest_structured_final(events, expected_lens=LENS, submit=submitted.append)

    assert submitted == []
    assert excinfo.value.category.startswith("no_report_in_final:")
    # Still the reviewer's own words, not a bare count and not the chatter.
    assert "no defects worth reporting" in str(excinfo.value)
    assert "chunk" not in str(excinfo.value)


def test_a_second_final_hidden_in_the_chatter_is_still_detected() -> None:
    from aiworkhub.quality_review_ingest import MAX_EVENTS

    final = json.dumps(_report())
    events = [
        *_amplified(final, progress=MAX_EVENTS + 500),
        _result_event(final),
    ]
    submitted: list[dict] = []

    with pytest.raises(ReviewProtocolError, match="multiple_structured_finals"):
        ingest_structured_final(events, expected_lens=LENS, submit=submitted.append)

    assert submitted == []


def test_extract_reports_the_unamplified_status_across_the_chatter() -> None:
    from aiworkhub.quality_review_ingest import MAX_EVENTS

    events = _amplified("purely prose, no json at all", progress=MAX_EVENTS + 500)

    result = extract_structured_final(events, expected_lens=LENS)

    assert result.report is None
    assert result.status == "unstructured_final"
    assert "purely prose, no json at all" in result.final_excerpt
