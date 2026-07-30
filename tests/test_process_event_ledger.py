from __future__ import annotations

from pathlib import Path

from aiworkhub import process_event_ledger


def test_rotates_before_active_bound_and_streams_all_rows(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    rows = [
        {"request_id": f"request-{index}", "state": "running", "payload": "x" * 90}
        for index in range(12)
    ]
    for row in rows:
        process_event_ledger.append_event(path, row, max_active_bytes=1024)

    ledgers = process_event_ledger.ledger_paths(path)
    assert len(ledgers) > 1
    assert ledgers[-1] == path
    assert path.stat().st_size <= 1024
    assert list(process_event_ledger.iter_events(path)) == rows


def test_stream_reader_skips_malformed_rows(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    path.write_text('{"request_id":"good"}\nnot-json\n', encoding="utf-8")
    assert list(process_event_ledger.iter_events(path)) == [{"request_id": "good"}]
