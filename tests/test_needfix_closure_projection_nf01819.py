"""NF-2026-01819: the reconciler's idle pass reads an append-aware projection."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import needfix_store, process_event_ledger, process_launcher  # noqa: E402

_SMALL = 1024
_LARGE = 1 << 20
_METRIC = "closure_reconcile_ledger_rows_parsed"


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    process_event_ledger.reset_state_event_cache()
    yield
    process_event_ledger.reset_state_event_cache()


def _row(index: int, state: str) -> dict[str, Any]:
    return {
        "request_id": f"req-{index}",
        "task_id": f"task-{index}",
        "state": state,
        "timestamp": f"2026-01-01T00:00:{index:02d}Z",
        "nested": {"index": index},
        "pad": "x" * 200,
    }


def _append(path: Path, index: int, state: str, bound: int = _SMALL) -> None:
    process_event_ledger.append_event(path, _row(index, state), max_active_bytes=bound)


def _expected(path: Path) -> list[dict[str, Any]]:
    return [
        row
        for row in process_event_ledger.iter_events(path)
        if row.get("state") in process_event_ledger.CLOSURE_EVENT_STATES
    ]


def _seed(path: Path, count: int = 12) -> None:
    states = ("running", "needfix_closure_pending", "needfix_closure_reconciled")
    for index in range(count):
        _append(path, index, states[index % len(states)])


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    return tmp_path / "processes.jsonl"


@pytest.fixture
def counters(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    counts = SimpleNamespace(parsed=0, replays=0)

    def counting_loads(*args: Any, **kwargs: Any) -> Any:
        counts.parsed += 1
        return json.loads(*args, **kwargs)

    monkeypatch.setattr(
        process_event_ledger,
        "json",
        SimpleNamespace(
            loads=counting_loads,
            dumps=json.dumps,
            JSONDecodeError=json.JSONDecodeError,
        ),
    )
    real_iter = process_event_ledger.iter_events

    def counting_iter(path: Path):
        counts.replays += 1
        return real_iter(path)

    monkeypatch.setattr(process_event_ledger, "iter_events", counting_iter)
    return counts


def test_parity_on_multi_segment_ledger_and_after_rotation(ledger: Path) -> None:
    _seed(ledger)
    assert len(process_event_ledger.ledger_paths(ledger)) > 1
    assert process_event_ledger.closure_events(ledger) == _expected(ledger)
    assert process_event_ledger.closure_events(ledger) == _expected(ledger)

    before = len(process_event_ledger.ledger_paths(ledger))
    for index in range(12, 20):
        _append(ledger, index, "needfix_closure_pending")
    assert len(process_event_ledger.ledger_paths(ledger)) > before
    assert process_event_ledger.closure_events(ledger) == _expected(ledger)


def test_parity_with_custom_states(ledger: Path) -> None:
    _seed(ledger)
    got = process_event_ledger.closure_events(ledger, ("running",))
    assert got == [
        row for row in process_event_ledger.iter_events(ledger) if row["state"] == "running"
    ]
    assert process_event_ledger.closure_events(ledger) == _expected(ledger)


def test_parity_after_truncation(ledger: Path) -> None:
    _seed(ledger, 6)
    assert process_event_ledger.closure_events(ledger) == _expected(ledger)
    first_line = ledger.read_bytes().split(b"\n")[0] + b"\n"
    ledger.write_bytes(first_line)
    assert process_event_ledger.closure_events(ledger) == _expected(ledger)
    ledger.write_bytes(b"")
    assert process_event_ledger.closure_events(ledger) == _expected(ledger)


def test_partial_tail_row_is_counted_exactly_once(ledger: Path) -> None:
    _append(ledger, 0, "needfix_closure_pending", _LARGE)
    payload = (json.dumps(_row(1, "needfix_closure_pending")) + "\n").encode()
    split = len(payload) // 2

    with ledger.open("ab") as handle:
        handle.write(payload[:split])
    assert process_event_ledger.closure_events(ledger) == _expected(ledger)
    assert len(process_event_ledger.closure_events(ledger)) == 1

    with ledger.open("ab") as handle:
        handle.write(payload[split:])
    assert process_event_ledger.closure_events(ledger) == _expected(ledger)
    assert len(process_event_ledger.closure_events(ledger)) == 2
    assert len(process_event_ledger.closure_events(ledger)) == 2


def test_partial_tail_after_warm_cache_is_parsed_once(
    ledger: Path, counters: SimpleNamespace
) -> None:
    _append(ledger, 0, "needfix_closure_pending", _LARGE)
    assert len(process_event_ledger.closure_events(ledger)) == 1
    payload = (json.dumps(_row(1, "needfix_closure_pending")) + "\n").encode()
    split = len(payload) // 2

    counters.parsed = 0
    with ledger.open("ab") as handle:
        handle.write(payload[:split])
    assert len(process_event_ledger.closure_events(ledger)) == 1
    assert counters.parsed == 0

    with ledger.open("ab") as handle:
        handle.write(payload[split:])
    assert len(process_event_ledger.closure_events(ledger)) == 2
    assert counters.parsed == 1
    assert len(process_event_ledger.closure_events(ledger)) == 2
    assert counters.parsed == 1


def test_returned_rows_cannot_mutate_the_cache(ledger: Path) -> None:
    _seed(ledger, 6)
    first = process_event_ledger.closure_events(ledger)
    assert first
    first[0]["nested"]["index"] = -1
    first[0]["state"] = "tampered"
    first.pop()
    assert process_event_ledger.closure_events(ledger) == _expected(ledger)


def _reconciler(
    ledger: Path, monkeypatch: pytest.MonkeyPatch, *, finished: bool
) -> tuple[Any, list[str]]:
    closed: list[str] = []

    def close(_repo: Any, task_id: str, *, accepted_request_id: str) -> dict[str, Any]:
        closed.append(task_id)
        return {"closure_id": f"closure-{task_id}"}

    monkeypatch.setattr(needfix_store, "close_for_accepted_task", close)
    monkeypatch.setattr(process_launcher, "_parse_card", lambda card, _task_id: card)
    monkeypatch.setattr(
        process_launcher, "_canonical_task_status", lambda card: card["status"]
    )
    manager = SimpleNamespace(
        process_log_path=ledger,
        repo=Path("."),
        _show_task=lambda task_id: {
            "status": "finished" if finished else "open",
            "accepted_request_id": f"req-{task_id.split('-')[1]}",
        },
        _append_event=lambda event: process_event_ledger.append_event(
            ledger, event, max_active_bytes=_LARGE
        ),
    )
    return manager, closed


def _reconcile(manager: Any) -> None:
    process_launcher.ProcessManager._reconcile_pending_needfix_closures(manager)


def test_appended_pending_row_parses_only_new_bytes_and_reconciles_once(
    ledger: Path, monkeypatch: pytest.MonkeyPatch, counters: SimpleNamespace
) -> None:
    _seed(ledger, 6)
    manager, closed = _reconciler(ledger, monkeypatch, finished=True)
    _reconcile(manager)
    closed.clear()

    _reconcile(manager)
    assert closed == []
    assert not [p for p in ledger.parent.iterdir() if p.name.startswith(f"{ledger.stem}.spill.")]  # _SMALL rotates; spills only come from a lock timeout

    _append(ledger, 40, "needfix_closure_pending", _LARGE)
    counters.parsed = 0
    counters.replays = 0
    _reconcile(manager)
    assert closed == ["task-40"]
    assert counters.replays == 0
    assert counters.parsed == 1

    counters.parsed = 0
    _reconcile(manager)
    assert closed == ["task-40"]
    assert counters.replays == 0
    assert counters.parsed == 1

    counters.parsed = 0
    _reconcile(manager)
    assert closed == ["task-40"]
    assert counters.parsed == 0


def _pending_ledger(ledger: Path) -> None:
    for index in range(20):
        _append(
            ledger,
            index,
            "needfix_closure_pending" if index % 2 else "running",
        )


def _emit(payload: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    with capsys.disabled():
        print("\nAIWORKHUB_METRIC:" + json.dumps(payload))


def test_baseline_cold_reconcile_rows_parsed(
    ledger: Path,
    monkeypatch: pytest.MonkeyPatch,
    counters: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pending_ledger(ledger)
    manager, closed = _reconciler(ledger, monkeypatch, finished=False)
    _reconcile(manager)

    process_event_ledger.reset_state_event_cache()
    counters.parsed = 0
    counters.replays = 0
    _reconcile(manager)

    assert closed == []
    assert counters.replays == 1
    assert counters.parsed > 0
    _emit({"metric": _METRIC, "value": counters.parsed, "unit": "rows"}, capsys)


def test_delta_warm_reconcile_rows_parsed(
    ledger: Path,
    monkeypatch: pytest.MonkeyPatch,
    counters: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pending_ledger(ledger)
    manager, closed = _reconciler(ledger, monkeypatch, finished=False)
    _reconcile(manager)

    counters.parsed = 0
    counters.replays = 0
    _reconcile(manager)

    assert closed == []
    assert counters.replays == 0
    assert counters.parsed == 0
    _emit(
        {
            "metric": _METRIC,
            "value": counters.parsed,
            "unit": "rows",
            "direction": "lower",
            "max_regression_percent": 0,
        },
        capsys,
    )
