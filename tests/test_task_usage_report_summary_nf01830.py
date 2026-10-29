"""Bounded default summary for ``aiworkhub_task_usage_report`` (NF-2026-01830)."""

from __future__ import annotations

from typing import Any

from aiworkhub import core, server


def _record(index: int, runner: str, observed: bool = True) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "task_id": f"task-{index:04d}",
        "runner": runner,
        "topic": "release_0_12_77",
        "status": "accepted",
        "records": 1,
        "created_at": "2026-10-28T00:00:00Z",
        "telemetry_reason": None,
    }
    if observed:
        rec.update(
            total_tokens=1000 + (index % 97),
            input_tokens=600,
            output_tokens=400 + (index % 97),
            cost_usd=0.0025,
            usage_observed=True,
            cost_observed=True,
        )
    else:
        rec.update(usage_observed=False, cost_observed=False)
    return rec


def _record_line(rec: dict[str, Any]) -> str:
    usage_is_observed = bool(rec.get("usage_observed"))
    cost_is_observed = bool(rec.get("cost_observed"))
    tokens = str(int(rec.get("total_tokens") or 0)) if usage_is_observed else "unknown"
    input_tokens = str(int(rec.get("input_tokens") or 0)) if usage_is_observed else "unknown"
    output_tokens = str(int(rec.get("output_tokens") or 0)) if usage_is_observed else "unknown"
    cost = f"${float(rec.get('cost_usd') or 0.0):.4f}" if cost_is_observed else "unknown"
    return (
        f"{rec.get('task_id')} | runner={rec.get('runner', '?')} | topic={rec.get('topic', '?')} | "
        f"records={int(rec.get('records') or 1)} | tokens={tokens} | "
        f"in={input_tokens} out={output_tokens} | cost={cost} | "
        f"usage_observed={str(usage_is_observed).lower()} "
        f"cost_observed={str(cost_is_observed).lower()} "
        f"telemetry_reason={rec.get('telemetry_reason') or '-'}"
    )


def _core_result(records: list[dict[str, Any]]) -> dict[str, Any]:
    stdout = "\n".join(["=== Task Usage Report ===", *(_record_line(rec) for rec in records)])
    return {
        "ok": True,
        "returncode": 0,
        "stdout": stdout,
        "command": ["usage-report"],
        "schema_id": "aiworkhub.usage_report.v2",
        "record_count": len(records),
        "usage_observed_records": sum(bool(rec.get("usage_observed")) for rec in records),
        "usage_unknown_records": sum(not bool(rec.get("usage_observed")) for rec in records),
        "cost_observed_records": sum(bool(rec.get("cost_observed")) for rec in records),
        "cost_unknown_records": sum(not bool(rec.get("cost_observed")) for rec in records),
    }


class _FakeUsageReport:
    """Stands in for ``core.usage_report`` and honours ``records_out``."""

    def __init__(self, records: list[dict[str, Any]], result: dict[str, Any]) -> None:
        self.records = records
        self.result = result

    def __call__(
        self,
        runner: str | None = None,
        topic: str | None = None,
        status: str | None = None,
        records_out: list | None = None,
    ) -> dict[str, Any]:
        if records_out is not None:
            records_out.extend(self.records)
        return self.result


def _fixture_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index in range(2000):
        runner = "glm_5.3" if index % 3 else "claude_4_5"
        observed = index % 10 != 0
        records.append(_record(index, runner, observed))
    return records


def _observed_tokens(rec: dict[str, Any]) -> int:
    return int(rec.get("total_tokens") or 0) if rec.get("usage_observed") else -1


def test_default_reply_is_bounded_summary_on_2000_events_nf01830(monkeypatch) -> None:
    records = _fixture_records()
    core_result = _core_result(records)
    monkeypatch.setattr(core, "usage_report", _FakeUsageReport(records, core_result))

    reply = server.aiworkhub_task_usage_report()

    assert isinstance(reply["stdout"], str)
    assert len(reply["stdout"].encode("utf-8")) < 15 * 1024
    lines = reply["stdout"].split("\n")
    assert lines[0] == "=== Task Usage Report ==="
    record_lines = [line for line in lines if "usage_observed=" in line]
    assert len(record_lines) <= 20
    for runner_name in ("glm_5.3", "claude_4_5"):
        subset = [rec for rec in records if rec["runner"] == runner_name]
        expected = (
            f"runner={runner_name} | records={len(subset)} | "
            f"tokens={sum(int(rec.get('total_tokens') or 0) for rec in subset if rec.get('usage_observed'))} | "
            f"cost=${sum(float(rec.get('cost_usd') or 0.0) for rec in subset if rec.get('cost_observed')):.4f} | "
            f"usage_unknown={sum(1 for rec in subset if not rec.get('usage_observed'))}"
        )
        assert expected in lines
    expected_top = [
        _record_line(rec) for rec in sorted(records, key=_observed_tokens, reverse=True)[:20]
    ]
    assert record_lines == expected_top
    for key in (
        "schema_id",
        "record_count",
        "usage_observed_records",
        "usage_unknown_records",
        "cost_observed_records",
        "cost_unknown_records",
    ):
        assert reply[key] == core_result[key]
    assert reply["detail_omitted"] == server._serialized_fingerprint(core_result["stdout"])
    assert reply["detail_request"] == {"tool": "aiworkhub_task_usage_report", "full": True}


def test_full_true_returns_core_stdout_byte_identical_nf01830(monkeypatch) -> None:
    records = _fixture_records()
    core_result = _core_result(records)
    monkeypatch.setattr(core, "usage_report", _FakeUsageReport(records, core_result))

    reply = server.aiworkhub_task_usage_report(full=True)

    assert reply == core_result
    assert reply["stdout"].encode("utf-8") == core_result["stdout"].encode("utf-8")
    assert "detail_omitted" not in reply


def test_no_records_result_passes_through_untouched_nf01830(monkeypatch) -> None:
    core_result = {
        "ok": True,
        "returncode": 0,
        "stdout": "No usage records.",
        "command": ["usage-report"],
    }
    monkeypatch.setattr(core, "usage_report", _FakeUsageReport([], core_result))

    reply = server.aiworkhub_task_usage_report()

    assert reply == core_result
    assert "detail_omitted" not in reply
    assert "detail_request" not in reply


def test_error_result_passes_through_untouched_nf01830(monkeypatch) -> None:
    core_result = {
        "ok": False,
        "returncode": 1,
        "stderr": "task store unavailable",
        "command": ["usage-report"],
    }
    records = _fixture_records()[:5]
    monkeypatch.setattr(core, "usage_report", _FakeUsageReport(records, core_result))

    reply = server.aiworkhub_task_usage_report()

    assert reply == core_result
    assert "detail_omitted" not in reply
    assert "detail_request" not in reply
