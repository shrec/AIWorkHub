"""NF-2026-00321: a FAILING quality-gate check must keep the TAIL of its
output, because test runners print the passing lines first and the one line
that says what broke last. A truncated field must also state that it was cut,
so a reader can tell short output from head-only truncation.
"""

from __future__ import annotations

from aiworkhub import quality_evidence as qe

CAP = qe.MAX_SUMMARY_CHARS
END_MARKER = "ZZZ_THE_ONE_LINE_THAT_SAYS_WHAT_BROKE_ZZZ"


def _failing_output_with_tail_marker() -> str:
    # Twenty passing lines, then the cause -- exactly the shape Node's test
    # runner emits: the failure lands AFTER the passing output.
    passing = "\n".join(f"ok {i} - passing assertion" for i in range(400))
    body = passing + "\n" + ("filler " * 400) + "\nFAILED: " + END_MARKER
    assert len(body) > CAP, "the fixture must exceed the cap to exercise truncation"
    return body


def test_failing_check_retains_the_tail_including_the_final_line():
    check = qe.EvidenceCheck(
        check_id="extension-regression",
        kind="test",
        status=qe.STATUS_FAILED,
        summary=_failing_output_with_tail_marker(),
    )
    summary = check.to_dict()["summary"]
    # The distinctive marker at the very end survives -- this is the assertion a
    # length-only check cannot make.
    assert END_MARKER in summary
    assert summary.rstrip().endswith(END_MARKER)
    # Still bounded.
    assert len(summary) <= CAP


def test_truncated_summary_states_that_it_was_truncated():
    check = qe.EvidenceCheck(
        check_id="extension-regression",
        kind="test",
        status=qe.STATUS_FAILED,
        summary=_failing_output_with_tail_marker(),
    )
    summary = check.to_dict()["summary"]
    assert qe.SUMMARY_TRUNCATION_MARKER in summary


def test_short_failing_summary_is_untouched_and_unmarked():
    short = "FAILED: " + END_MARKER
    check = qe.EvidenceCheck(
        check_id="c", kind="test", status=qe.STATUS_FAILED, summary=short
    )
    summary = check.to_dict()["summary"]
    assert summary == short
    # A reader can tell short output from cut output: no notice on short output.
    assert qe.SUMMARY_TRUNCATION_MARKER not in summary


def test_mapping_check_payload_also_keeps_the_failing_tail():
    payload = qe._check_payload(
        {
            "check_id": "extension-regression",
            "kind": "test",
            "status": qe.STATUS_FAILED,
            "summary": _failing_output_with_tail_marker(),
        }
    )
    summary = payload["summary"]
    assert END_MARKER in summary
    assert qe.SUMMARY_TRUNCATION_MARKER in summary
    assert len(summary) <= CAP


def test_passing_check_may_keep_the_head_but_must_mark_the_cut():
    body = ("head " * 500) + END_MARKER
    assert len(body) > CAP
    check = qe.EvidenceCheck(
        check_id="c", kind="test", status=qe.STATUS_PASSED, summary=body
    )
    summary = check.to_dict()["summary"]
    assert len(summary) <= CAP
    # Any truncated field states it was cut, even when the head is what's kept.
    assert qe.SUMMARY_TRUNCATION_MARKER in summary


def test_long_provenance_is_marked_and_identical_in_both_serializers():
    provenance = "source:" + ("bounded-evidence/" * 300)
    check = qe.EvidenceCheck(
        check_id="c",
        kind="test",
        status=qe.STATUS_PASSED,
        provenance=provenance,
    )

    dataclass_value = check.to_dict()["provenance"]
    mapping_value = qe._check_payload(
        {
            "check_id": "c",
            "kind": "test",
            "status": qe.STATUS_PASSED,
            "provenance": provenance,
        }
    )["provenance"]

    assert dataclass_value == mapping_value
    assert len(dataclass_value) <= CAP
    assert qe.SUMMARY_TRUNCATION_MARKER in dataclass_value


def test_short_provenance_is_byte_for_byte_unchanged():
    provenance = "source:exact-receipt:abc123"
    check = qe.EvidenceCheck(
        check_id="c",
        kind="test",
        status=qe.STATUS_PASSED,
        provenance=provenance,
    )

    assert check.to_dict()["provenance"] == provenance
    assert qe._check_payload(
        {
            "check_id": "c",
            "kind": "test",
            "status": qe.STATUS_PASSED,
            "provenance": provenance,
        }
    )["provenance"] == provenance
