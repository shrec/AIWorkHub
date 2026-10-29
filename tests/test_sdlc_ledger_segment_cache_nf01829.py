from __future__ import annotations

import json

import pytest

from aiworkhub import sdlc_outcome_metrics as metrics


def _row(request_id: str, pad: int = 200) -> str:
    return json.dumps({
        "request_id": request_id,
        "task_id": "T-" + request_id,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "reasoning_context_attempt": {"id": request_id},
        "noise": "z" * pad,
    }) + "\n"


def _noise() -> str:
    return json.dumps({"request_id": "R-noise", "state": "running"}) + "\n"


def _put(path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


class _Ledger:
    def __init__(self, root):
        self.dir = root / "runtime" / "process_logs"
        self.dir.mkdir(parents=True)
        self.live = self.dir / "process_events.jsonl"
        self.cache = root / "runtime" / "cache" / "sdlc_ledger_segments_v1.json"

    def archive(self, stamp: int, text: str):
        path = self.dir / f"process_events.20260101T00000{stamp}.000000Z.1.aaaaaaaa.jsonl"
        _put(path, text)
        return path

    def append(self, text: str) -> None:
        with self.live.open("a", encoding="utf-8", newline="") as handle:
            handle.write(text)


@pytest.fixture
def ledger(tmp_path):
    fixture = _Ledger(tmp_path)
    fixture.archive(1, _row("R-a1") + _noise() + _row("R-a2"))
    fixture.archive(2, _row("R-b1") + _row("R-b2") + _row("R-b3"))
    _put(fixture.live, _row("R-l1") + _row("R-l2") + '{"request_id": "R-cut", "reas')
    return fixture


@pytest.fixture
def reads(monkeypatch):
    calls: list[tuple[str, int, int]] = []
    real = metrics._scan_range

    def counting(path, start, size, rows):
        calls.append((path.name, start, size))
        return real(path, start, size, rows)

    monkeypatch.setattr(metrics, "_scan_range", counting)
    return calls


def _cold(ledger_fixture, monkeypatch, **kwargs):
    with monkeypatch.context() as ctx:
        ctx.setattr(metrics, "_segment_cache_path", lambda _log: None)
        return metrics.read_attempt_evidence(ledger_fixture.live, **kwargs)


def _same_result(warm, cold):
    assert warm.rows == cold.rows
    for name in ("available", "truncated", "segments_total", "segments_scanned",
                 "oversized_rows", "truncation_causes"):
        assert getattr(warm, name) == getattr(cold, name), name


def test_warm_call_reads_no_sealed_bytes_and_matches_a_cold_scan(ledger, reads, monkeypatch):
    first = metrics.read_attempt_evidence(ledger.live)
    assert len(reads) == 3 and ledger.cache.is_file()
    assert first.bytes_scanned == sum(
        path.stat().st_size for path in ledger.dir.iterdir()
    )
    reads.clear()

    warm = metrics.read_attempt_evidence(ledger.live)

    # The live segment is unchanged, so nothing at all is read.
    assert reads == [] and warm.bytes_scanned == 0
    _same_result(warm, _cold(ledger, monkeypatch))
    assert [row["request_id"] for row in warm.rows] == [
        "R-l1", "R-l2", "R-b1", "R-b2", "R-b3", "R-a1", "R-a2"
    ]
    assert warm.truncation_causes == ("segment_incomplete",)


def test_live_segment_is_read_only_from_the_cached_complete_line(ledger, reads, monkeypatch):
    metrics.read_attempt_evidence(ledger.live)
    committed = len((_row("R-l1") + _row("R-l2")).encode())
    ledger.append('oning_context_attempt": {"id": "x"}}\n' + _row("R-l3") + '{"request_id": "R-cut2", "r')
    size = ledger.live.stat().st_size
    reads.clear()

    warm = metrics.read_attempt_evidence(ledger.live)

    assert reads == [(ledger.live.name, committed, size)]
    assert warm.bytes_scanned == size - committed
    _same_result(warm, _cold(ledger, monkeypatch))
    assert [row["request_id"] for row in warm.rows][:3] == ["R-l1", "R-l2", "R-cut"]

    ledger.append("\n" + _row("R-l4"))
    reads.clear()
    again = metrics.read_attempt_evidence(ledger.live)
    assert reads and reads[0][0] == ledger.live.name and reads[0][1] > 0
    _same_result(again, _cold(ledger, monkeypatch))


def test_oversized_rows_are_counted_once_across_incremental_scans(ledger, monkeypatch):
    monkeypatch.setattr(metrics, "MAX_LEDGER_ROW_BYTES", 1024)
    first = metrics.read_attempt_evidence(ledger.live)
    assert first.oversized_rows == 0
    ledger.append("y" * 3000)
    mid = metrics.read_attempt_evidence(ledger.live)
    _same_result(mid, _cold(ledger, monkeypatch))
    ledger.append("y" * 100 + "\n" + _row("R-after", pad=1))
    last = metrics.read_attempt_evidence(ledger.live)
    _same_result(last, _cold(ledger, monkeypatch))
    assert last.oversized_rows == mid.oversized_rows == 1


def _rewrite_cache(ledger_fixture, mutate):
    payload = json.loads(ledger_fixture.cache.read_text(encoding="utf-8"))
    mutate(payload["segments"])
    ledger_fixture.cache.write_text(json.dumps(payload), encoding="utf-8")


def test_a_shrunk_sealed_segment_is_rescanned(ledger, reads, monkeypatch):
    metrics.read_attempt_evidence(ledger.live)
    archive = ledger.archive(2, _row("R-b9"))
    reads.clear()

    warm = metrics.read_attempt_evidence(ledger.live)

    assert [call[0] for call in reads] == [archive.name]
    assert reads[0][1] == 0
    _same_result(warm, _cold(ledger, monkeypatch))
    assert "R-b9" in [row["request_id"] for row in warm.rows]
    assert "R-b1" not in [row["request_id"] for row in warm.rows]


@pytest.mark.parametrize("field", ["mtime_ns", "ino"])
def test_a_changed_mtime_or_inode_is_rescanned_never_served_stale(
    ledger, reads, monkeypatch, field
):
    metrics.read_attempt_evidence(ledger.live)
    sealed = ledger.dir / "process_events.20260101T000002.000000Z.1.aaaaaaaa.jsonl"
    same_size = _row("R-b1") + _row("R-b2") + _row("R-bX")
    assert len(same_size) == sealed.stat().st_size
    _put(sealed, same_size)

    def bump(segments):
        for key, entry in segments.items():
            if key.endswith(sealed.name):
                entry[field] += 1

    _rewrite_cache(ledger, bump)
    reads.clear()

    warm = metrics.read_attempt_evidence(ledger.live)

    assert [call[0] for call in reads] == [sealed.name]
    assert "R-bX" in [row["request_id"] for row in warm.rows]
    _same_result(warm, _cold(ledger, monkeypatch))


def test_a_live_segment_that_shrank_is_rescanned_from_zero(ledger, reads, monkeypatch):
    metrics.read_attempt_evidence(ledger.live)
    _put(ledger.live, _row("R-n1"))
    reads.clear()

    warm = metrics.read_attempt_evidence(ledger.live)

    assert reads == [(ledger.live.name, 0, ledger.live.stat().st_size)]
    _same_result(warm, _cold(ledger, monkeypatch))


@pytest.mark.parametrize(
    "damage",
    [
        "not json at all {",
        json.dumps({"version": 999, "segments": {}}),
        json.dumps({"version": 1, "segments": {"k": {"size": "x"}}}),
        json.dumps({"version": 1, "segments": []}),
        json.dumps([1, 2, 3]),
    ],
)
def test_a_corrupt_or_mismatched_cache_is_ignored_and_rebuilt(ledger, reads, monkeypatch, damage):
    metrics.read_attempt_evidence(ledger.live)
    ledger.cache.write_text(damage, encoding="utf-8")
    reads.clear()

    rebuilt = metrics.read_attempt_evidence(ledger.live)

    assert len(reads) == 3
    _same_result(rebuilt, _cold(ledger, monkeypatch))
    payload = json.loads(ledger.cache.read_text(encoding="utf-8"))
    assert payload["version"] == metrics._SEGMENT_CACHE_VERSION and len(payload["segments"]) == 3
    reads.clear()
    metrics.read_attempt_evidence(ledger.live)
    assert reads == []


def test_an_unwritable_cache_location_never_raises(ledger, monkeypatch):
    ledger.cache.parent.parent.joinpath("cache").write_text("a file", encoding="utf-8")
    evidence = metrics.read_attempt_evidence(ledger.live)
    _same_result(evidence, _cold(ledger, monkeypatch))


def test_cached_sealed_segments_are_not_charged_to_the_byte_bound(ledger, monkeypatch):
    metrics.read_attempt_evidence(ledger.live)
    live_size = ledger.live.stat().st_size
    total = sum(path.stat().st_size for path in ledger.dir.iterdir())
    assert total > live_size

    cold = _cold(ledger, monkeypatch, byte_bound=live_size)
    assert "byte_bound" in cold.truncation_causes and cold.segments_scanned == 1

    warm = metrics.read_attempt_evidence(ledger.live, byte_bound=live_size)

    assert "byte_bound" not in warm.truncation_causes
    assert (warm.segments_scanned, warm.segments_total) == (3, 3)
    assert warm.bytes_scanned == 0
    assert len(warm.rows) == 7


def test_a_missing_ledger_creates_no_cache(tmp_path):
    log = tmp_path / "runtime" / "process_logs" / "process_events.jsonl"
    evidence = metrics.read_attempt_evidence(log)
    assert (evidence.available, evidence.rows) == (False, ())
    assert not (tmp_path / "runtime").exists()
