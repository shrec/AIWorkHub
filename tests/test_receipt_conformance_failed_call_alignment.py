"""A failed Source Graph call must not make a card unacceptable.

``receipt_conformance_report`` refuses on ``source_graph_mode_stage_sequence_mismatch``
when the mode and stage sequences differ in length. Both describe the SAME calls,
so a difference can only mean one recorder skipped a call the other counted.

That is exactly what happened: the stage recorder appended for every source_graph
entry while the mode recorder appended only when the mode was recognised, and a
FAILED call carries no usable payload mode. Measured on three cards refused on
2026-08-19, the difference equalled the failed-call count every time::

    modes=21 stages=26  delta=5  failed_calls=5
    modes=11 stages=12  delta=1  failed_calls=1
    modes=14 stages=20  delta=6  failed_calls=6

One failed Source Graph call was therefore enough to refuse a card whose code was
green, under a blocker whose name reads as a worker labelling fault.
"""

from __future__ import annotations

from aiworkhub.worker_ai_tools_mcp import receipt_conformance_report


def _verification(*, modes: list[str], stages: list[str]) -> dict[str, object]:
    return {
        "call_count_by_tool": {"source_graph": len(stages)},
        "successful_call_count_by_tool": {"source_graph": len(modes)},
        "compact_replay": {"provider_token_savings_measured": False},
        "source_graph_mode_sequence": modes,
        "source_graph_stage_sequence": stages,
        "fresh_source_graph_calls": 0,
        "entries_tampered": 0,
    }


def test_equal_length_sequences_do_not_block() -> None:
    report = receipt_conformance_report(
        _verification(
            modes=["focus", "body", "unspecified"],
            stages=["orientation", "implementation", "unspecified"],
        )
    )
    assert "source_graph_mode_stage_sequence_mismatch" not in report["blockers"]


def test_a_failed_call_still_occupies_a_slot_in_both_sequences() -> None:
    """The regression: three calls, one of them failed and mode-less."""
    modes = ["focus", "body", "unspecified"]
    stages = ["orientation", "implementation", "unspecified"]
    assert len(modes) == len(stages)
    report = receipt_conformance_report(_verification(modes=modes, stages=stages))
    assert report["blockers"] == []


def test_a_genuine_length_difference_still_blocks() -> None:
    """The check is not weakened: a real accounting gap must still refuse."""
    report = receipt_conformance_report(
        _verification(
            modes=["focus", "body"],
            stages=["orientation", "implementation", "validation"],
        )
    )
    assert "source_graph_mode_stage_sequence_mismatch" in report["blockers"]
