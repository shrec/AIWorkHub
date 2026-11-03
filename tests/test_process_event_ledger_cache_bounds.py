"""NF-2026-00679: the latest-event projection cache is bounded by bytes.

``latest_events`` used to bound its cache by entry count only, so up to 32
whole projections of a large ledger stayed resident, and every call copied the
whole projection. These tests pin the byte budget, the one-key / filtered
accessors that copy only what they return, and parity with ``latest_events``.
"""

from __future__ import annotations

import json
import sys
import tracemalloc
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_event_ledger as ledger  # noqa: E402

_PACKET = "p" * 4096
_ROWS = 60


@pytest.fixture(autouse=True)
def _clean_cache():
    ledger.reset_latest_event_cache()
    yield
    ledger.reset_latest_event_cache()


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _corpus(path: Path, *, rows: int = _ROWS, packet: str = _PACKET) -> None:
    batch = []
    for index in range(rows):
        batch.append(
            {
                "request_id": f"request-{index}",
                "task_id": f"task-{index % 7}",
                "state": "running" if index % 3 else "done",
                "packet": packet + str(index),
                "nested": {"items": [index, {"deep": index}]},
                "timestamp": f"2026-01-01T00:00:{index % 60:02d}Z",
            }
        )
        # A second lifecycle row per request so merge and replace differ.
        batch.append(
            {
                "request_id": f"request-{index}",
                "event_kind": "runtime_notice" if index % 5 == 0 else "lifecycle",
                "state": "finalizing",
                "extra": index,
            }
        )
    _write(path, batch)


def _single_projection_bytes(path: Path, **options) -> int:
    ledger.reset_latest_event_cache()
    ledger.latest_events(path, **options)
    return ledger.latest_event_cache_stats()["estimated_bytes"]


def test_estimated_bytes_stay_under_budget_across_many_projection_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    one = _single_projection_bytes(path)
    assert one > _ROWS * 4096

    budget = int(one * 2.5)
    monkeypatch.setattr(ledger, "_LATEST_EVENT_CACHE_MAX_BYTES", budget)
    ledger.reset_latest_event_cache()

    variants = 12
    for index in range(variants):
        ledger.latest_events(path, skip_event_kinds=(f"never-{index}",))
        stats = ledger.latest_event_cache_stats()
        assert stats["estimated_bytes"] <= budget
        assert stats["budget_bytes"] == budget

    stats = ledger.latest_event_cache_stats()
    assert 1 <= stats["entries"] < variants
    most_recent = (
        str(path.resolve(strict=False)),
        "request_id",
        (f"never-{variants - 1}",),
        False,
        (),
    )
    assert most_recent in ledger._LATEST_EVENT_CACHE


def test_single_projection_over_budget_is_returned_but_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    reference = ledger.latest_events(path)
    ledger.reset_latest_event_cache()

    monkeypatch.setattr(ledger, "_LATEST_EVENT_CACHE_MAX_BYTES", 1024)
    assert ledger.latest_events(path) == reference
    assert ledger.latest_event(path, "request-3") == reference["request-3"]
    stats = ledger.latest_event_cache_stats()
    assert stats["entries"] == 0
    assert stats["estimated_bytes"] == 0


def test_drop_fields_lowers_estimated_bytes(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    kept = _single_projection_bytes(path)
    dropped = _single_projection_bytes(path, drop_fields=("packet",))
    assert dropped < kept - _ROWS * 4000


OPTIONS = [
    {},
    {"replace": True},
    {"replace": True, "skip_event_kinds": ("runtime_notice",)},
    {"replace": True, "skip_event_kinds": ("runtime_notice",), "drop_fields": ("packet",)},
    {"key_field": "task_id"},
]


@pytest.mark.parametrize("options", OPTIONS)
def test_one_key_and_filtered_reads_match_latest_events(
    tmp_path: Path, options: dict
) -> None:
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    full = ledger.latest_events(path, **options)
    assert full

    for key in list(full)[:5]:
        assert ledger.latest_event(path, key, **options) == full[key]
    assert ledger.latest_event(path, "no-such-key", **options) is None

    def wanted(key: str, row) -> bool:
        return str(row.get("state") or "") == "running" or key.endswith("-1")

    expected = {k: v for k, v in full.items() if wanted(k, v)}
    assert ledger.latest_events_where(path, wanted, **options) == expected


def test_results_are_isolated_copies_including_nested_values(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    for fetch in (
        lambda: ledger.latest_event(path, "request-2"),
        lambda: ledger.latest_events(path)["request-2"],
        lambda: ledger.latest_events_where(path, lambda k, r: k == "request-2")[
            "request-2"
        ],
    ):
        row = fetch()
        row["nested"]["items"].append("tampered")
        row["state"] = "tampered"
        again = ledger.latest_event(path, "request-2")
        assert again["state"] != "tampered"
        assert again["nested"]["items"] == [2, {"deep": 2}]


def test_incremental_append_keeps_parity_and_exact_byte_estimate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    ledger.latest_events(path)

    _write(
        path,
        [
            {"request_id": "request-1", "state": "late", "blob": "b" * 9000},
            {"request_id": "request-new", "state": "fresh", "packet": "q" * 500},
            {"request_id": "request-1", "state": "later"},
        ],
    )
    incremental = ledger.latest_events(path)
    incremental_bytes = ledger.latest_event_cache_stats()["estimated_bytes"]
    assert incremental["request-1"]["state"] == "later"
    assert incremental["request-1"]["blob"] == "b" * 9000
    assert ledger.latest_event(path, "request-new")["state"] == "fresh"

    ledger.reset_latest_event_cache()
    cold = ledger.latest_events(path)
    assert cold == incremental
    assert ledger.latest_event_cache_stats()["estimated_bytes"] == incremental_bytes


def test_repeated_one_key_and_filtered_reads_retain_bounded_memory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "process_events.jsonl"
    _corpus(path, rows=300, packet="z" * 8192)

    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        # A cold latest_event scans one key and never builds the projection
        # (NF-2026-02013), so prime it through a copy-free filtered read.
        assert ledger.latest_events_where(path, lambda _key, _row: False) == {}
        projection_size = tracemalloc.get_traced_memory()[0] - base
        assert projection_size > 300 * 8000

        settled = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        calls = 200
        for index in range(calls):
            row = ledger.latest_event(path, f"request-{index % 300}")
            assert row is not None
            matched = ledger.latest_events_where(
                path, lambda key, _row, i=index: key == f"request-{(i + 1) % 300}"
            )
            assert len(matched) == 1
        del row, matched
        retained = tracemalloc.get_traced_memory()[0] - settled
        peak = tracemalloc.get_traced_memory()[1] - settled
    finally:
        tracemalloc.stop()

    assert retained < projection_size * 0.1
    assert peak < projection_size * 0.1
    assert ledger.latest_event_cache_stats()["entries"] == 1


def test_deeply_nested_row_is_estimated_and_copied_without_recursion() -> None:
    # Security review: a row nested deeper than the interpreter stack must not
    # turn into a RecursionError that breaks the whole ledger projection.
    depth = sys.getrecursionlimit() + 100
    leaf = {"leaf": "x"}
    row: dict = leaf
    for _ in range(depth):
        row = {"n": [row]}
    assert ledger._estimate_value_bytes(row) > depth
    copied = ledger._copy_row(row)
    source, target = row, copied
    for _ in range(depth):
        assert target is not source
        source, target = source["n"][0], target["n"][0]
    assert target == {"leaf": "x"} and target is not leaf
