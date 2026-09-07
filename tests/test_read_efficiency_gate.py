"""Read-efficiency gate: the measurement must gate something, and honestly.

Audit 2026-09-07 s5.3 found a complete read-efficiency subsystem wired only to
a dashboard tile.  These tests pin the three things that make it safe to
consume: the Source Graph parser really recognizes Source Graph calls (so the
gate is not built on a broken measurement), an unmeasured run never reads as a
measured zero, and the gate cannot block while its blocking switch is off.
"""

from __future__ import annotations

import json

import pytest

try:
    from aiworkhub import process_launcher_read_efficiency as parser
    from aiworkhub import quality_evidence
except ImportError:  # pragma: no cover - exercised only outside an installed tree
    from src.aiworkhub import process_launcher_read_efficiency as parser
    from src.aiworkhub import quality_evidence


def record(total_reads: int, *, unbounded: int = 0, unknown: int = 0, **extra):
    payload = {
        "schema_id": "aiworkhub.provider_read_efficiency.v2",
        "evidence_observed": True,
        "total_reads": total_reads,
        "unbounded_reads": unbounded,
        "unknown_repetitions": unknown,
    }
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# The measurement the gate is built on.
# ---------------------------------------------------------------------------


def test_source_graph_calls_are_recognized_for_both_provider_shapes(tmp_path):
    """A zero must mean "the worker did not call Source Graph", not "unparsed".

    Both live provider shapes are covered: the Claude stream-json ``tool_use``
    node whose MCP name is fully qualified, and the Codex ``mcp_tool_call``
    item whose tool name lives under ``tool``.
    """

    log = tmp_path / "stdout.log"
    log.write_text(
        "\n".join(
            [
                json.dumps({
                    "type": "assistant",
                    "message": {"content": [{
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": (
                            "mcp__aiworkhub_worker_ai_tools__"
                            "aiworkhub_worker_source_graph_query"
                        ),
                        "input": {"mode": "focus", "query": "x"},
                    }]},
                }),
                json.dumps({
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "id": "mcp_1",
                        "server": "aiworkhub_worker_ai_tools",
                        "tool": "aiworkhub_worker_source_graph_query",
                        "status": "completed",
                        "arguments": {"mode": "slice"},
                    },
                }),
            ]
        ),
        encoding="utf-8",
    )

    result = parser._provider_read_efficiency_from_output(log)

    assert result["recognized_source_graph_events"] == 2
    assert result["evidence_observed"] is True


def test_codex_in_progress_item_does_not_double_count_a_source_graph_call(tmp_path):
    """``item.started`` and ``item.completed`` describe one call, not two."""

    call = {
        "type": "mcp_tool_call",
        "id": "mcp_1",
        "server": "aiworkhub_worker_ai_tools",
        "tool": "aiworkhub_worker_source_graph_query",
        "arguments": {"mode": "focus"},
    }
    log = tmp_path / "stdout.log"
    log.write_text(
        "\n".join([
            json.dumps({"type": "item.started", "item": {**call, "status": "in_progress"}}),
            json.dumps({"type": "item.completed", "item": {**call, "status": "completed"}}),
        ]),
        encoding="utf-8",
    )

    assert parser._provider_read_efficiency_from_output(log)[
        "recognized_source_graph_events"
    ] == 1


def test_prose_mentioning_the_tool_name_is_not_counted_as_a_call(tmp_path):
    """The contract banner names the tool in every prompt; that is not evidence."""

    log = tmp_path / "stdout.log"
    log.write_text(
        json.dumps({
            "type": "user",
            "message": {"content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_9",
                "content": "call aiworkhub_worker_source_graph_query before reading",
            }]},
        }),
        encoding="utf-8",
    )

    assert parser._provider_read_efficiency_from_output(log)[
        "recognized_source_graph_events"
    ] == 0


# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------


def test_thresholds_match_the_sample_they_were_derived_from():
    """Silent threshold drift would decouple the gate from its evidence."""

    assert quality_evidence.READ_EFFICIENCY_UNBOUNDED_READ_RATE_THRESHOLD == 60.0
    assert quality_evidence.READ_EFFICIENCY_UNKNOWN_REPETITION_RATE_THRESHOLD == 80.0
    assert quality_evidence.READ_EFFICIENCY_MIN_MEASURED_READS == 5
    assert quality_evidence.READ_EFFICIENCY_BLOCKING_DEFAULT is False


def test_gate_fires_above_the_unbounded_read_threshold():
    gate = quality_evidence.evaluate_read_efficiency_gate(
        record(20, unbounded=13)
    )

    assert gate["measured"] is True
    assert gate["observed_reads"] == 20
    assert gate["unbounded_read_rate"] == 65.0
    assert gate["signals"] == [
        quality_evidence.READ_EFFICIENCY_SIGNAL_UNBOUNDED
    ]
    assert gate["verdict"] == "above_threshold"


def test_gate_fires_above_the_unknown_repetition_threshold():
    gate = quality_evidence.evaluate_read_efficiency_gate(
        record(20, unknown=17)
    )

    assert gate["unknown_repetition_rate"] == 85.0
    assert gate["signals"] == [
        quality_evidence.READ_EFFICIENCY_SIGNAL_UNKNOWN_REPETITION
    ]


def test_gate_stays_silent_below_both_thresholds():
    gate = quality_evidence.evaluate_read_efficiency_gate(
        record(20, unbounded=4, unknown=12)
    )

    assert gate["measured"] is True
    assert gate["unbounded_read_rate"] == 20.0
    assert gate["unknown_repetition_rate"] == 60.0
    assert gate["signals"] == []
    assert gate["verdict"] == "within_threshold"
    assert gate["status"] == quality_evidence.STATUS_PASSED


def test_threshold_is_inclusive_at_the_boundary():
    gate = quality_evidence.evaluate_read_efficiency_gate(
        record(10, unbounded=6)
    )

    assert gate["unbounded_read_rate"] == 60.0
    assert quality_evidence.READ_EFFICIENCY_SIGNAL_UNBOUNDED in gate["signals"]


# ---------------------------------------------------------------------------
# Honesty: unmeasured must never read as measured.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"evidence_observed": False, "total_reads": 0},
        {"evidence_observed": True, "total_reads": 0},
    ],
)
def test_unobserved_evidence_reports_none_rates_and_not_available(payload):
    gate = quality_evidence.evaluate_read_efficiency_gate(payload)

    assert gate["measured"] is False
    assert gate["measurement_state"] == quality_evidence.READ_EFFICIENCY_STATE_UNOBSERVED
    # The whole point: absent evidence is None, never a measured 0.0.
    assert gate["unbounded_read_rate"] is None
    assert gate["unknown_repetition_rate"] is None
    assert gate["signals"] == []
    assert gate["verdict"] == "unmeasured"
    assert gate["status"] == quality_evidence.STATUS_NOT_AVAILABLE


def test_under_sampled_run_is_unmeasured_even_when_every_read_is_unbounded():
    """Four unbounded reads out of four is 100% and still means nothing."""

    gate = quality_evidence.evaluate_read_efficiency_gate(record(4, unbounded=4))

    assert gate["measured"] is False
    assert (
        gate["measurement_state"]
        == quality_evidence.READ_EFFICIENCY_STATE_BELOW_MINIMUM_SAMPLE
    )
    assert gate["unbounded_read_rate"] is None
    assert gate["signals"] == []
    assert gate["status"] == quality_evidence.STATUS_NOT_AVAILABLE
    # The denominator travels with the verdict so a reader can see why.
    assert gate["observed_reads"] == 4
    assert gate["minimum_measured_reads"] == 5


def test_non_integer_counts_are_unknown_not_zero():
    gate = quality_evidence.evaluate_read_efficiency_gate(
        record(20, unbounded=0) | {"unbounded_reads": "many", "unknown_repetitions": None}
    )

    assert gate["unbounded_read_rate"] is None
    assert gate["unknown_repetition_rate"] is None
    assert gate["signals"] == []


# ---------------------------------------------------------------------------
# Blocking is a separate, explicitly-off switch.
# ---------------------------------------------------------------------------


def test_gate_never_blocks_while_the_blocking_switch_is_off():
    gate = quality_evidence.evaluate_read_efficiency_gate(
        record(40, unbounded=40, unknown=39)
    )

    assert gate["signals"]  # it did fire
    assert gate["blocking_enabled"] is False
    assert gate["blocked"] is False
    assert gate["status"] == quality_evidence.STATUS_PASSED
    assert gate["disposition"] == quality_evidence.FINDING_DISPOSITION_OBSERVATION


def test_gate_blocks_only_when_blocking_is_explicitly_enabled():
    gate = quality_evidence.evaluate_read_efficiency_gate(
        record(40, unbounded=40), blocking=True
    )

    assert gate["blocking_enabled"] is True
    assert gate["blocked"] is True
    assert gate["status"] == quality_evidence.STATUS_FAILED
    assert gate["disposition"] == quality_evidence.FINDING_DISPOSITION_DEFECT


def test_blocking_switch_cannot_block_an_unmeasured_run():
    """Fail-closed means unknown never blocks, even with blocking demanded."""

    gate = quality_evidence.evaluate_read_efficiency_gate(None, blocking=True)

    assert gate["blocked"] is False
    assert gate["status"] == quality_evidence.STATUS_NOT_AVAILABLE


def test_evidence_check_row_is_canonical_and_non_blocking_by_default():
    check = quality_evidence.read_efficiency_evidence_check(
        record(40, unbounded=40)
    )

    assert check.check_id == "read_efficiency"
    assert check.status == quality_evidence.STATUS_PASSED
    assert check.to_dict()["schema_id"] == quality_evidence.SCHEMA_ID
    assert "40" in check.summary

    blocking_check = quality_evidence.read_efficiency_evidence_check(
        record(40, unbounded=40), blocking=True
    )
    assert blocking_check.status == quality_evidence.STATUS_FAILED


def test_a_fired_observation_cannot_escalate_the_monotonic_risk_floors():
    """While the switch is off the observation must not raise the risk tier.

    ``derive_risk_signals`` promotes any failed check it is handed to
    ``destructive_change``.  The default read-efficiency row is status passed
    precisely so a fired observation cannot reach that escalation; only the
    explicitly-enabled blocking row can, and that is the switch's whole job.
    """

    worst = record(40, unbounded=40, unknown=39)
    card = {"task_type": "code", "validation": ["pytest"]}
    paths = ["src/aiworkhub/read_efficiency.py"]

    observation = quality_evidence.read_efficiency_evidence_check(worst)
    assert quality_evidence.evaluate_read_efficiency_gate(worst)["signals"]
    assert "destructive_change" not in quality_evidence.derive_risk_signals(
        card, paths, destructive_checks=[observation]
    )

    blocking = quality_evidence.read_efficiency_evidence_check(worst, blocking=True)
    assert "destructive_change" in quality_evidence.derive_risk_signals(
        card, paths, destructive_checks=[blocking]
    )


# ---------------------------------------------------------------------------
# Persistence: the record must outlive the live process report.
# ---------------------------------------------------------------------------


def test_persisted_record_round_trips_with_task_attribution(tmp_path):
    receipt = parser.persist_provider_read_efficiency(
        record(20, unbounded=13),
        destination_dir=tmp_path,
        task_id="T-1",
        request_id="R-1",
        runner="claude",
        adapter_id="claude_cli",
    )
    assert receipt["persisted"] is True

    loaded = parser.load_provider_read_efficiency_records(tmp_path)
    assert len(loaded) == 1
    assert loaded[0]["task_id"] == "T-1"
    assert loaded[0]["runner"] == "claude"
    assert loaded[0]["read_efficiency"]["total_reads"] == 20

    # The persisted record feeds the gate directly.
    gate = quality_evidence.evaluate_read_efficiency_gate(
        loaded[0]["read_efficiency"]
    )
    assert gate["unbounded_read_rate"] == 65.0


def test_persistence_refuses_a_record_with_no_task_identity(tmp_path):
    receipt = parser.persist_provider_read_efficiency(
        record(20), destination_dir=tmp_path, task_id="  ",
    )

    assert receipt == {"persisted": False, "reason": "missing_task_id"}
    assert parser.load_provider_read_efficiency_records(tmp_path) == []


def test_persistence_strips_per_event_rows_so_no_path_is_written(tmp_path):
    parser.persist_provider_read_efficiency(
        record(20) | {"events": [{"path": "src/secret.py"}]},
        destination_dir=tmp_path,
        task_id="T-2",
    )

    raw = (tmp_path / parser.READ_EFFICIENCY_RECORD_FILENAME).read_text(
        encoding="utf-8"
    )
    assert "src/secret.py" not in raw
    assert "events" not in json.loads(raw)["read_efficiency"]


def test_persistence_never_raises_when_the_sink_is_unwritable(tmp_path):
    blocker = tmp_path / "sink"
    blocker.write_text("not a directory", encoding="utf-8")

    receipt = parser.persist_provider_read_efficiency(
        record(20), destination_dir=blocker, task_id="T-3",
    )

    assert receipt["persisted"] is False
    assert receipt["reason"] == "write_failed"


def test_loader_skips_a_truncated_tail_without_losing_earlier_records(tmp_path):
    parser.persist_provider_read_efficiency(
        record(20), destination_dir=tmp_path, task_id="T-4",
    )
    with open(
        tmp_path / parser.READ_EFFICIENCY_RECORD_FILENAME, "a", encoding="utf-8"
    ) as handle:
        handle.write('{"schema_id": "aiworkhub.provider_read_effic')

    loaded = parser.load_provider_read_efficiency_records(tmp_path)
    assert [entry["task_id"] for entry in loaded] == ["T-4"]


def test_loader_returns_empty_for_a_missing_sink(tmp_path):
    assert parser.load_provider_read_efficiency_records(tmp_path / "absent") == []
