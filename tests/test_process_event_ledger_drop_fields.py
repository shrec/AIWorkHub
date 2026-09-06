"""NF-2026-00637 M1: drop the unused ``packet`` field before retention.

``process_event_ledger.latest_events`` cached every field of every row
forever, including a ``packet`` field that ``task_retention`` and
``terminal_log_retention`` never read. ``drop_fields`` lets a caller name
fields that must be stripped before a row enters the retained projection --
not merely from the dict handed back to the caller -- and joins the cache
identity so two different discard sets can never share a cached answer.

The baseline/delta tests below print a standalone ``AIWORKHUB_METRIC:`` JSON
receipt measuring the retained projection's serialized byte size: byte truth
for what the cache actually keeps, with no timing assertion.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_event_ledger as ledger  # noqa: E402

_LARGE_PACKET = "p" * 4096
_ROW_COUNT = 20


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _corpus(path: Path) -> None:
    _write(
        path,
        [
            {
                "request_id": f"request-{index}",
                "state": "running",
                "packet": _LARGE_PACKET,
            }
            for index in range(_ROW_COUNT)
        ],
    )


def _retained_bytes(cache_key: tuple) -> int:
    """Byte truth for what the cache actually retains, not a returned copy."""
    cached = ledger._LATEST_EVENT_CACHE[cache_key]
    return len(
        json.dumps(cached.latest, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )


def _emit_metric(role: str, value: int) -> None:
    payload = {
        "metric": "process_event_ledger_retained_projection_bytes",
        "unit": "bytes",
        "value": float(value),
        "direction": "lower",
        "max_regression_percent": 0,
        "role": role,
    }
    print(f"AIWORKHUB_METRIC:{json.dumps(payload, sort_keys=True)}")


def test_baseline_default_projection_retains_the_packet_field(
    tmp_path: Path, capsys
) -> None:
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    ledger._LATEST_EVENT_CACHE.clear()

    projection = ledger.latest_events(path, key_field="request_id")
    cache_key = (str(path.resolve(strict=False)), "request_id", (), False, ())

    assert len(projection) == _ROW_COUNT
    assert all("packet" in row for row in projection.values())
    assert all("packet" in row for row in ledger._LATEST_EVENT_CACHE[cache_key].latest.values())

    with capsys.disabled():
        _emit_metric("baseline", _retained_bytes(cache_key))


def test_delta_drop_fields_removes_packet_before_retention(
    tmp_path: Path, capsys
) -> None:
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    ledger._LATEST_EVENT_CACHE.clear()

    baseline_key = (str(path.resolve(strict=False)), "request_id", (), False, ())
    ledger.latest_events(path, key_field="request_id")
    baseline_bytes = _retained_bytes(baseline_key)

    projection = ledger.latest_events(
        path, key_field="request_id", drop_fields=("packet",)
    )
    delta_key = (
        str(path.resolve(strict=False)),
        "request_id",
        (),
        False,
        ("packet",),
    )
    delta_bytes = _retained_bytes(delta_key)

    assert len(projection) == _ROW_COUNT
    assert all("packet" not in row for row in projection.values())
    assert all(
        "packet" not in row
        for row in ledger._LATEST_EVENT_CACHE[delta_key].latest.values()
    )
    assert delta_bytes < baseline_bytes

    with capsys.disabled():
        _emit_metric("delta", delta_bytes)


def test_drop_fields_is_removed_on_cold_rebuild(tmp_path: Path) -> None:
    """The field must never reach the retained dict, not just the return copy."""
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    ledger._LATEST_EVENT_CACHE.clear()

    ledger.latest_events(path, key_field="request_id", drop_fields=("packet",))
    cache_key = (
        str(path.resolve(strict=False)),
        "request_id",
        (),
        False,
        ("packet",),
    )
    cached = ledger._LATEST_EVENT_CACHE[cache_key]
    assert all("packet" not in row for row in cached.latest.values())


def test_drop_fields_is_removed_on_incremental_append_replay(tmp_path: Path) -> None:
    """The append-only fast path must also strip the field before retention."""
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    ledger._LATEST_EVENT_CACHE.clear()

    first = ledger.latest_events(
        path, key_field="request_id", drop_fields=("packet",)
    )
    assert "packet" not in first["request-0"]

    _write(
        path,
        [{"request_id": "request-new", "state": "running", "packet": _LARGE_PACKET}],
    )

    second = ledger.latest_events(
        path, key_field="request_id", drop_fields=("packet",)
    )
    cache_key = (
        str(path.resolve(strict=False)),
        "request_id",
        (),
        False,
        ("packet",),
    )
    cached = ledger._LATEST_EVENT_CACHE[cache_key]

    assert "packet" not in second["request-new"]
    assert all("packet" not in row for row in cached.latest.values())


def test_different_drop_fields_identities_never_share_a_cached_projection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    ledger._LATEST_EVENT_CACHE.clear()

    with_packet = ledger.latest_events(path, key_field="request_id")
    without_packet = ledger.latest_events(
        path, key_field="request_id", drop_fields=("packet",)
    )

    assert with_packet != without_packet
    assert all("packet" in row for row in with_packet.values())
    assert all("packet" not in row for row in without_packet.values())

    # Asking again in the other order must still give each its own answer --
    # one view must never be served the other view's cached answer.
    assert (
        ledger.latest_events(path, key_field="request_id", drop_fields=("packet",))
        == without_packet
    )
    assert ledger.latest_events(path, key_field="request_id") == with_packet

    with_key = (str(path.resolve(strict=False)), "request_id", (), False, ())
    without_key = (
        str(path.resolve(strict=False)),
        "request_id",
        (),
        False,
        ("packet",),
    )
    assert with_key in ledger._LATEST_EVENT_CACHE
    assert without_key in ledger._LATEST_EVENT_CACHE


def test_drop_fields_order_does_not_split_the_cache(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    ledger._LATEST_EVENT_CACHE.clear()

    ledger.latest_events(
        path, key_field="request_id", drop_fields=("packet", "other")
    )
    before = len(ledger._LATEST_EVENT_CACHE)
    ledger.latest_events(
        path, key_field="request_id", drop_fields=("other", "packet")
    )
    assert len(ledger._LATEST_EVENT_CACHE) == before


def test_default_projection_is_byte_compatible_without_drop_fields(
    tmp_path: Path,
) -> None:
    """Omitting ``drop_fields`` must reproduce the exact historical projection."""
    path = tmp_path / "process_events.jsonl"
    _write(
        path,
        [
            {"request_id": "a", "state": "starting", "pid": 1},
            {"request_id": "a", "state": "running"},
            {"request_id": "b", "state": "review_ready", "exit_code": 0},
        ],
    )
    ledger._LATEST_EVENT_CACHE.clear()

    result = ledger.latest_events(path)

    assert result == {
        "a": {"request_id": "a", "state": "running", "pid": 1},
        "b": {"request_id": "b", "state": "review_ready", "exit_code": 0},
    }
