from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _ensure_deepseek_credentials_stub() -> None:
    import importlib
    import types

    try:
        importlib.import_module("aiworkhub.deepseek_credentials")
        return
    except ImportError:
        pass

    stub = types.ModuleType("aiworkhub.deepseek_credentials")

    class CredentialError(Exception):
        def __init__(self, reason: str = "deepseek_credential_stub_environment") -> None:
            super().__init__(reason)
            self.reason = reason

    def load_credential(repo=None):  # noqa: ANN001, ARG001
        raise CredentialError("deepseek_credential_stub_environment")

    def adapter_readiness(repo=None):  # noqa: ANN001, ARG001
        return {"ok": True, "readonly": True, "adapters": []}

    stub.CredentialError = CredentialError
    stub.load_credential = load_credential
    stub.adapter_readiness = adapter_readiness
    sys.modules["aiworkhub.deepseek_credentials"] = stub


_ensure_deepseek_credentials_stub()

from aiworkhub import dashboard  # noqa: E402


TASK_ID = "TASK_EFFORT_NF01698"
EFFORT_KEYS = ("effort", "effort_profile", "effort_status")

RECEIPT_MAX = {
    "applied": True,
    "flag_emitted": True,
    "status": "applied",
    "profile": "maximum",
    "applied_key": "max",
    "provider_family": "anthropic",
    "difficulty": "hard",
    "context_capacity": 200000,
}
RECEIPT_CEILING = {
    **RECEIPT_MAX,
    "applied": False,
    "flag_emitted": False,
    "status": "capability_ceiling",
    "applied_key": None,
}


def _launch_event(**extra: object) -> dict[str, object]:
    return {
        "request_id": "req-1",
        "task_id": TASK_ID,
        "state": "running",
        "timestamp": "2026-07-21T00:00:00+00:00",
        **extra,
    }


def _process_report(tmp_path: Path, events: list[dict[str, object]]) -> dict[str, object]:
    log = tmp_path / "process_events.jsonl"
    log.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return dashboard.read_process_runs(process_log_path=log)


def _process_row(tmp_path: Path, events: list[dict[str, object]]) -> dict[str, object]:
    report = _process_report(tmp_path, events)
    return next(row for row in report["processes"] if row["request_id"] == "req-1")


def _merged_task_row(process_rows: list[dict[str, object]]) -> dict[str, object]:
    groups = {"processing": [{"task_id": TASK_ID, "status": "processing"}]}
    dashboard._merge_process_liveness_into_tasks(groups, {"processes": process_rows})
    return groups["processing"][0]


def _effort_triplet(row: dict[str, object]) -> tuple[object, ...]:
    return tuple(row.get(key) for key in EFFORT_KEYS)


def test_applied_receipt_surfaces_key_profile_and_status(tmp_path: Path) -> None:
    row = _process_row(tmp_path, [_launch_event(reasoning_effort=dict(RECEIPT_MAX))])

    assert _effort_triplet(row) == ("max", "maximum", "applied")
    assert "reasoning_effort" not in row


def test_unapplied_receipt_reports_default_with_the_real_profile_and_status(tmp_path: Path) -> None:
    row = _process_row(tmp_path, [_launch_event(reasoning_effort=dict(RECEIPT_CEILING))])

    assert _effort_triplet(row) == ("default", "maximum", "capability_ceiling")


def test_unapplied_receipt_never_reports_a_leftover_applied_key(tmp_path: Path) -> None:
    receipt = {**RECEIPT_MAX, "applied": False}
    row = _process_row(tmp_path, [_launch_event(reasoning_effort=receipt)])

    assert row["effort"] == "default"


def test_receipt_without_a_route_status_omits_effort_status(tmp_path: Path) -> None:
    receipt = {**RECEIPT_CEILING, "status": None}
    row = _process_row(tmp_path, [_launch_event(reasoning_effort=receipt)])

    assert row["effort"] == "default"
    assert row["effort_profile"] == "maximum"
    assert "effort_status" not in row


def test_event_without_a_receipt_surfaces_no_effort_keys(tmp_path: Path) -> None:
    row = _process_row(tmp_path, [_launch_event()])

    assert [key for key in EFFORT_KEYS if key in row] == []


@pytest.mark.parametrize(
    "receipt",
    [
        pytest.param(None, id="none"),
        pytest.param("max", id="string"),
        pytest.param(["max"], id="list"),
        pytest.param(7, id="number"),
        pytest.param({}, id="empty-object"),
        pytest.param(
            {"applied_key": "max", "profile": "maximum", "status": "applied"},
            id="missing-applied",
        ),
        pytest.param({"applied": "true", "applied_key": "max"}, id="string-applied"),
        pytest.param({"applied": 1, "applied_key": "max"}, id="integer-applied"),
        pytest.param({"applied": True}, id="applied-without-key"),
        pytest.param({"applied": True, "applied_key": None}, id="applied-null-key"),
        pytest.param({"applied": True, "applied_key": 3}, id="applied-numeric-key"),
        pytest.param({"applied": True, "applied_key": "   "}, id="applied-blank-key"),
    ],
)
def test_malformed_receipt_surfaces_no_effort_keys(tmp_path: Path, receipt: object) -> None:
    row = _process_row(tmp_path, [_launch_event(reasoning_effort=receipt)])

    assert [key for key in EFFORT_KEYS if key in row] == []


def test_effort_scalars_are_bounded_and_whitespace_normalised(tmp_path: Path) -> None:
    receipt = {
        **RECEIPT_MAX,
        "applied_key": "x" * 500,
        "profile": "  very\n  high\t ",
        "status": "y" * 500,
    }
    row = _process_row(tmp_path, [_launch_event(reasoning_effort=receipt)])

    assert row["effort"] == "x" * 64
    assert row["effort_profile"] == "very high"
    assert row["effort_status"] == "y" * 64


def test_non_text_profile_and_status_are_omitted_not_stringified(tmp_path: Path) -> None:
    receipt = {**RECEIPT_MAX, "profile": 5, "status": ["applied"]}
    row = _process_row(tmp_path, [_launch_event(reasoning_effort=receipt)])

    assert row["effort"] == "max"
    assert "effort_profile" not in row
    assert "effort_status" not in row


def test_launch_receipt_survives_newer_events_that_carry_none(tmp_path: Path) -> None:
    events = [
        _launch_event(reasoning_effort=dict(RECEIPT_MAX)),
        _launch_event(state="review_ready", timestamp="2026-07-21T00:05:00+00:00", exit_code=0),
    ]
    row = _process_row(tmp_path, events)

    assert row["state"] == "review_ready"
    assert _effort_triplet(row) == ("max", "maximum", "applied")


def test_merge_copies_effort_scalars_onto_the_task_row() -> None:
    row = _merged_task_row(
        [
            {
                "request_id": "req-1",
                "task_id": TASK_ID,
                "state": "running",
                "effort": "max",
                "effort_profile": "maximum",
                "effort_status": "applied",
            }
        ]
    )

    assert _effort_triplet(row) == ("max", "maximum", "applied")


def test_merge_adds_no_effort_keys_when_the_process_row_has_none() -> None:
    row = _merged_task_row([{"request_id": "req-1", "task_id": TASK_ID, "state": "running"}])

    assert [key for key in EFFORT_KEYS if key in row] == []


def test_merge_takes_effort_only_from_the_newest_attempt() -> None:
    row = _merged_task_row(
        [
            {"request_id": "new", "task_id": TASK_ID, "state": "running"},
            {
                "request_id": "old",
                "task_id": TASK_ID,
                "state": "launch_failed",
                "effort": "max",
                "effort_profile": "maximum",
                "effort_status": "applied",
            },
        ]
    )

    assert [key for key in EFFORT_KEYS if key in row] == []


@pytest.mark.parametrize(
    ("receipt", "expected"),
    [
        pytest.param(dict(RECEIPT_MAX), ("max", "maximum", "applied"), id="applied"),
        pytest.param(
            dict(RECEIPT_CEILING), ("default", "maximum", "capability_ceiling"), id="not-applied"
        ),
        pytest.param(None, (None, None, None), id="none"),
        pytest.param("max", (None, None, None), id="malformed"),
    ],
)
def test_launch_receipt_reaches_the_merged_task_row(
    tmp_path: Path, receipt: object, expected: tuple[object, ...]
) -> None:
    report = _process_report(tmp_path, [_launch_event(reasoning_effort=receipt)])
    groups = {"processing": [{"task_id": TASK_ID, "status": "processing"}]}

    dashboard._merge_process_liveness_into_tasks(groups, report)

    assert _effort_triplet(groups["processing"][0]) == expected
