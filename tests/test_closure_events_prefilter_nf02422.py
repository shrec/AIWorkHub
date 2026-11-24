"""NF-2026-02422: the closure_events cold path prefilters raw ledger lines."""

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

from aiworkhub import process_event_ledger  # noqa: E402

_SMALL = 1024
_WANTED = ("needfix_closure_pending", "needfix_closure_reconciled")


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    process_event_ledger.reset_state_event_cache()
    yield
    process_event_ledger.reset_state_event_cache()


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    return tmp_path / "processes.jsonl"


def _row(index: int, state: Any, **extra: Any) -> dict[str, Any]:
    return {
        "request_id": f"req-{index}",
        "task_id": f"task-{index}",
        "state": state,
        "timestamp": f"2026-01-01T00:00:{index:02d}Z",
        "pad": "x" * 200,
        **extra,
    }


def _append(path: Path, row: dict[str, Any]) -> None:
    process_event_ledger.append_event(path, row, max_active_bytes=_SMALL)


def _append_raw(path: Path, line: str) -> None:
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")


def _build_mixed_ledger(path: Path) -> None:
    """A rotated multi-segment ledger with wanted rows, decoys and junk."""

    index = 0
    for round_index in range(4):
        for state in (
            "running",
            "needfix_closure_pending",
            "finished",
            "needfix_closure_reconciled",
            5,
            ["needfix_closure_pending"],
            None,
        ):
            _append(path, _row(index, state))
            index += 1
        # Decoys: a wanted state text in another field, state itself unwanted.
        _append(path, _row(index, "running", note="needfix_closure_pending"))
        index += 1
        _append(path, _row(index, "finished", detail={"state": "needfix_closure_reconciled"}))
        index += 1
        # Malformed JSON that still mentions a wanted state.
        _append_raw(path, '{"state": "needfix_closure_pending", broken')
        _append_raw(path, "not json at all needfix_closure_reconciled")
        # A non-object JSON value mentioning a wanted state.
        _append_raw(path, '["needfix_closure_pending"]')


def _expected(path: Path, states: tuple[str, ...]) -> list[dict[str, Any]]:
    wanted = frozenset(states)
    return [
        row
        for row in process_event_ledger.iter_events(path)
        if isinstance(row.get("state"), str) and row["state"] in wanted
    ]


def test_cold_path_matches_full_replay_on_rotated_ledger(ledger: Path) -> None:
    _build_mixed_ledger(ledger)
    assert len(process_event_ledger.ledger_paths(ledger)) > 1

    expected = _expected(ledger, _WANTED)
    assert expected  # the fixture really contains wanted rows
    assert process_event_ledger.closure_events(ledger, _WANTED) == expected

    process_event_ledger.reset_state_event_cache()
    only_pending = ("needfix_closure_pending",)
    assert process_event_ledger.closure_events(ledger, only_pending) == _expected(
        ledger, only_pending
    )

    process_event_ledger.reset_state_event_cache()
    assert process_event_ledger.closure_events(ledger) == _expected(
        ledger, tuple(process_event_ledger.CLOSURE_EVENT_STATES)
    )


def test_cold_path_skips_json_loads_for_lines_without_a_quoted_state(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_mixed_ledger(ledger)
    tokens = [f'"{state}"'.encode() for state in _WANTED]
    lines: list[bytes] = []
    for segment in process_event_ledger.ledger_paths(ledger):
        lines.extend(segment.read_bytes().splitlines())
    candidate_lines = [line for line in lines if any(token in line for token in tokens)]
    assert 0 < len(candidate_lines) < len(lines)

    parsed: list[str] = []

    def counting_loads(text: Any, *args: Any, **kwargs: Any) -> Any:
        parsed.append(text)
        return json.loads(text, *args, **kwargs)

    monkeypatch.setattr(
        process_event_ledger,
        "json",
        SimpleNamespace(
            loads=counting_loads,
            dumps=json.dumps,
            JSONDecodeError=json.JSONDecodeError,
        ),
    )
    rows = process_event_ledger.closure_events(ledger, _WANTED)

    assert rows
    assert len(parsed) == len(candidate_lines)
    assert len(parsed) < len(lines)
    assert all(any(token.decode() in text for token in tokens) for text in parsed)


def test_unquotable_state_falls_back_to_full_replay(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    escaped = "needfix_closure_café"
    for index, state in enumerate(("running", escaped, "finished", escaped)):
        _append(ledger, _row(index, state))

    replays: list[Path] = []
    real_iter = process_event_ledger.iter_events

    def counting_iter(path: Path):
        replays.append(path)
        return real_iter(path)

    monkeypatch.setattr(process_event_ledger, "iter_events", counting_iter)
    rows = process_event_ledger.closure_events(ledger, (escaped,))

    assert len(replays) == 1
    assert [row["request_id"] for row in rows] == ["req-1", "req-3"]


def test_prefilter_path_does_not_use_full_replay(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_mixed_ledger(ledger)
    expected = _expected(ledger, _WANTED)

    def forbidden_iter(_path: Path):
        raise AssertionError("cold path must stream through the prefilter")

    monkeypatch.setattr(process_event_ledger, "iter_events", forbidden_iter)
    assert process_event_ledger.closure_events(ledger, _WANTED) == expected


def test_spill_ledger_uses_full_replay(ledger: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _build_mixed_ledger(ledger)
    expected = _expected(ledger, _WANTED)
    monkeypatch.setattr(process_event_ledger, "_is_spill", lambda _path, _ledger: True)

    replays: list[Path] = []
    real_iter = process_event_ledger.iter_events

    def counting_iter(path: Path):
        replays.append(path)
        return real_iter(path)

    monkeypatch.setattr(process_event_ledger, "iter_events", counting_iter)
    assert process_event_ledger.closure_events(ledger, _WANTED) == expected
    assert len(replays) == 1
