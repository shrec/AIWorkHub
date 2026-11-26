"""NF-2026-00966: a per-key ledger lookup costs the key's rows, not history.

``latest_event``'s exact single-key scan (NF-2026-02013) read every byte of
every rotated archive for each key, and retention asks once per card, so the
dashboard server streamed the whole 2.4 GB history over and over. Rotated
archives are immutable: their key index is built once and persisted, and a
lookup then reads only the key's own lines. These tests pin that N lookups
read O(1) archive bytes, that the indexed answer equals the full fold and the
raw scan, and that a corrupt, stale or forged index falls back exactly.
"""

from __future__ import annotations

import json
import sys
import threading
from array import array
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_event_ledger as ledger  # noqa: E402

ARCHIVES = 10
KEYS_PER_ARCHIVE = 24
PAD = "x" * 512
NOTE_ONLY = "n" * 32
ABSENT = "f" * 32
NUMERIC = "123"

_OPTIONS: list[dict[str, Any]] = [
    {},
    {"replace": True},
    {"skip_event_kinds": ("observation",)},
    {"drop_fields": ("packet",)},
    {"keep_fields": ("request_id", "state", "task_id")},
    {"replace": True, "skip_event_kinds": ("observation",), "drop_fields": ("packet",)},
]


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch: pytest.MonkeyPatch):
    from aiworkhub import storage_retention

    monkeypatch.setattr(
        storage_retention, "schedule_repository_cleanup", lambda *_a, **_k: False
    )
    for thread in threading.enumerate():
        if thread.name == "aiworkhub-storage-retention":
            thread.join(timeout=60)
    ledger.reset_latest_event_cache()
    ledger.reset_request_event_cache()
    yield
    ledger.reset_latest_event_cache()
    ledger.reset_request_event_cache()


def _key(archive: int, slot: int) -> str:
    return f"req-{archive:02d}-{slot:02d}-{'0' * 16}"


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _archive(path: Path, index: int) -> Path:
    return path.with_name(f"{path.stem}.2026{index:02d}01T000000Z{path.suffix}")


def _ledger(tmp_path: Path) -> Path:
    """Ten rotated archives plus the active file: per key a lifecycle row, an
    observation and a later overwrite; keys mentioned in other rows' values; a
    key whose rows span two archives; a numeric key and a note-only key."""

    path = tmp_path / "process_events.jsonl"
    for index in range(ARCHIVES):
        rows: list[dict[str, Any]] = []
        for slot in range(KEYS_PER_ARCHIVE):
            key = _key(index, slot)
            rows.append(
                {
                    "request_id": key,
                    "task_id": f"T{index}-{slot}",
                    "state": "starting",
                    "packet": PAD,
                    "timestamp": f"2026-01-{index + 1:02d}T00:00:{slot:02d}+00:00",
                }
            )
            rows.append({"request_id": key, "event_kind": "observation", "state": "observed"})
            # Other requests' bulk: what a raw scan pays and a lookup must not.
            rows.append(
                {"request_id": f"other-{index}-{slot}", "note": f"see {key}", "filler": PAD * 8}
            )
            rows.append({"request_id": key, "state": "running", "pid": slot})
        if index:
            rows.append({"request_id": _key(index - 1, 0), "state": "succeeded", "exit_code": 0})
        if index == 2:
            rows.append({"request_id": 123, "state": "numeric"})
            rows.append({"request_id": NUMERIC, "extra": "string-keyed"})
        if index == 3:
            rows.append({"request_id": "z" * 32, "note": f"names {NOTE_ONLY}"})
        _write(_archive(path, index), rows)
    _write(
        path,
        [
            {"request_id": _key(ARCHIVES - 1, 1), "state": "launch_failed", "error": "boom"},
            {"request_id": _key(0, 2), "state": "replaced"},
            {"request_id": _key(5, 7), "event_kind": "observation", "state": "seen"},
        ],
    )
    return path


class _CountingHandle:
    def __init__(self, handle: Any, sink: dict[str, int], name: str) -> None:
        self._handle = handle
        self._sink = sink
        self._name = name

    def _add(self, data: Any) -> Any:
        self._sink[self._name] = self._sink.get(self._name, 0) + len(data)
        return data

    def read(self, *args: Any) -> Any:
        return self._add(self._handle.read(*args))

    def readline(self, *args: Any) -> Any:
        return self._add(self._handle.readline(*args))

    def __iter__(self):
        for line in self._handle:
            yield self._add(line)

    def __enter__(self) -> "_CountingHandle":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self._handle.close()
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)


def _count_ledger_reads(
    monkeypatch: pytest.MonkeyPatch, path: Path
) -> tuple[dict[str, int], list[str]]:
    """Bytes read from, and names opened of, every ledger segment."""

    sink: dict[str, int] = {}
    opened: list[str] = []
    original = Path.open

    def counting_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any):
        handle = original(self, mode, *args, **kwargs)
        if self.suffix == path.suffix and self.name.startswith(f"{path.stem}."):
            opened.append(self.name)
            return _CountingHandle(handle, sink, self.name)
        return handle

    monkeypatch.setattr(Path, "open", counting_open)
    return sink, opened


def _own_line_bytes(path: Path, keys: list[str]) -> int:
    """Bytes of the archive lines whose row is keyed by one of ``keys``."""

    wanted = set(keys)
    total = 0
    for index in range(ARCHIVES):
        for line in _archive(path, index).read_bytes().splitlines(keepends=True):
            if json.loads(line).get("request_id") in wanted:
                total += len(line)
    return total


def _archive_bytes(sink: dict[str, int], path: Path) -> int:
    return sum(count for name, count in sink.items() if name != path.name)


def _full_fold(path: Path, key: str, options: dict[str, Any]) -> dict[str, Any] | None:
    ledger.reset_latest_event_cache()
    try:
        return ledger.latest_events(path, **options).get(key)
    finally:
        ledger.reset_latest_event_cache()


def _raw_scan(
    path: Path, key: str, options: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any] | None:
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_indexed_key_rows", lambda *_a, **_k: None)
        return ledger.latest_event(path, key, **options)


@pytest.mark.parametrize("options", _OPTIONS)
def test_indexed_answer_equals_full_fold_and_raw_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, options: dict[str, Any]
) -> None:
    path = _ledger(tmp_path)
    keys = [
        _key(0, 0),  # rows in two archives, merged
        _key(0, 2),  # overwritten from the active file
        _key(ARCHIVES - 1, 1),
        _key(5, 7),
        _key(7, KEYS_PER_ARCHIVE - 1),
        NOTE_ONLY,
        ABSENT,
        NUMERIC,
    ]
    for key in keys:
        indexed = ledger.latest_event(path, key, **options)
        assert indexed == _full_fold(path, key, options)
        assert indexed == _raw_scan(path, key, options, monkeypatch)
        # Warm (in-memory index) answers the same.
        assert ledger.latest_event(path, key, **options) == indexed
    assert ledger.latest_event(path, NOTE_ONLY, **options) is None
    assert ledger.latest_event(path, ABSENT, **options) is None
    merged = ledger.latest_event(path, _key(0, 0))
    assert merged is not None and merged["state"] == "succeeded"
    assert merged["packet"] == PAD and merged["exit_code"] == 0


def test_n_lookups_read_o1_archive_bytes_once_the_index_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _ledger(tmp_path)
    history = sum(_archive(path, index).stat().st_size for index in range(ARCHIVES))
    largest = max(_archive(path, index).stat().st_size for index in range(ARCHIVES))
    before = ledger.latest_event_key_index_stats()
    assert ledger.latest_event(path, _key(0, 0)) is not None  # builds every index
    built = ledger.latest_event_key_index_stats()
    assert built["built"] - before["built"] >= ARCHIVES
    assert built["written"] - before["written"] == ARCHIVES
    assert len(list(ledger.checkpoint_dir(path).glob("*.keyidx"))) == ARCHIVES

    keys = [_key(index, slot) for index in range(ARCHIVES) for slot in (3, 4, 5)]
    own = _own_line_bytes(path, keys)
    for phase in ("memory", "persisted"):
        if phase == "persisted":
            ledger.reset_key_index_cache()
            start = ledger.latest_event_key_index_stats()
        with monkeypatch.context() as patch:
            sink, opened = _count_ledger_reads(patch, path)
            for key in keys:
                opened.clear()
                assert ledger.latest_event(path, key)["state"] == "running"
                # Only the key's own archive (plus the active file) is opened.
                assert len([name for name in opened if name != path.name]) == 1
        read = _archive_bytes(sink, path)
        assert read <= own, phase
        # 30 lookups read less than ONE archive; the raw scan read them all.
        assert read < largest < history, phase
        if phase == "memory":
            assert sink.get(path.name, 0) == 0  # active file unchanged: no tail
        else:
            stats = ledger.latest_event_key_index_stats()
            assert stats["loaded"] - start["loaded"] == ARCHIVES
            assert stats["rejected"] == start["rejected"]
            assert stats["fallback"] == start["fallback"]


def test_active_file_growth_reads_only_appended_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _ledger(tmp_path)
    key = _key(4, 4)
    assert ledger.latest_event(path, key)["state"] == "running"
    size_before = path.stat().st_size
    _write(path, [{"request_id": key, "state": "succeeded"}])
    appended = path.stat().st_size - size_before  # on-disk bytes (CRLF on Windows)
    with monkeypatch.context() as patch:
        sink, _opened = _count_ledger_reads(patch, path)
        row = ledger.latest_event(path, key)
    assert row is not None and row["state"] == "succeeded" and row["pid"] == 4
    assert row == _full_fold(path, key, {})
    # The appended row is indexed once and read once; the head check is fixed.
    read_active = sink.get(path.name, 0)
    assert read_active <= ledger._KEY_INDEX_HEAD_BYTES + 2 * appended
    assert _archive_bytes(sink, path) <= _own_line_bytes(path, [key])


@pytest.mark.parametrize("damage", ["truncated", "tampered", "foreign_key_field"])
def test_corrupt_index_is_rebuilt_with_the_same_answer(
    tmp_path: Path, damage: str
) -> None:
    path = _ledger(tmp_path)
    key = _key(3, 3)
    expected = _full_fold(path, key, {})
    assert ledger.latest_event(path, key) == expected
    target = ledger._key_index_path(path, _archive(path, 3).name, "request_id")
    data = target.read_bytes()
    lines = data.split(b"\n")
    if damage == "truncated":
        target.write_bytes(data[: len(data) // 2])
    elif damage == "tampered":
        # Still valid JSON, but the payload digest no longer matches.
        lines[1] = lines[1].replace(b"]]", b",1]]", 1)
        target.write_bytes(b"\n".join(lines))
    else:
        lines[0] = lines[0].replace(b'"request_id"', b'"task_id"', 1)
        target.write_bytes(b"\n".join(lines))
    ledger.reset_key_index_cache()
    before = ledger.latest_event_key_index_stats()
    assert ledger.latest_event(path, key) == expected
    after = ledger.latest_event_key_index_stats()
    assert after["rejected"] - before["rejected"] == 1
    assert after["built"] - before["built"] >= 1
    assert after["written"] - before["written"] == 1


def test_stale_index_for_a_changed_archive_is_rebuilt(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    key = _key(3, 3)
    assert ledger.latest_event(path, key)["state"] == "running"
    # An archive is immutable by contract; a rewritten one changes its
    # (size, mtime_ns), so its persisted index no longer describes it.
    _write(_archive(path, 3), [{"request_id": key, "state": "rewritten"}])
    ledger.reset_key_index_cache()
    before = ledger.latest_event_key_index_stats()
    row = ledger.latest_event(path, key)
    assert row is not None and row["state"] == "rewritten"
    assert row == _full_fold(path, key, {})
    after = ledger.latest_event_key_index_stats()
    assert after["rejected"] - before["rejected"] == 1


def test_forged_index_fails_verification_and_falls_back_to_the_scan(
    tmp_path: Path,
) -> None:
    path = _ledger(tmp_path)
    key = _key(3, 3)
    expected = _full_fold(path, key, {})
    assert ledger.latest_event(path, key) == expected
    archive = _archive(path, 3).resolve(strict=False)
    signature = next(
        item for item in ledger._ledger_signatures(path) if item[0] == str(archive)
    )
    # A well-formed, digest-valid index whose offset names another key's line.
    forged = ledger._KeyIndex(
        signature=signature,
        complete_offset=signature[3],
        offsets={key: array("Q", [0]).tobytes()},
    )
    ledger._write_key_index(path, forged, "request_id")
    target = ledger._key_index_path(path, archive.name, "request_id")
    assert target.exists()
    ledger.reset_key_index_cache()
    before = ledger.latest_event_key_index_stats()
    assert ledger.latest_event(path, key) == expected
    after = ledger.latest_event_key_index_stats()
    assert after["verify_failed"] - before["verify_failed"] == 1
    assert after["fallback"] - before["fallback"] == 1
    assert not target.exists()  # discarded, so the next lookup rebuilds it
    assert ledger.latest_event(path, key) == expected
    assert target.exists()


def test_spill_keeps_the_full_projection_answer(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    key = _key(6, 6)
    spill = path.with_name(f"{path.stem}.spill.20260201T000000.000000Z.1.abc{path.suffix}")
    _write(spill, [{"request_id": key, "state": "spilled", "timestamp": "2026-02-01T00:00:00+00:00"}])
    for options in _OPTIONS:
        assert ledger.latest_event(path, key, **options) == _full_fold(path, key, options)


def test_retired_archive_indexes_are_pruned(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    assert ledger.latest_event(path, _key(1, 1)) is not None
    retired = ledger._key_index_path(path, _archive(path, 0).name, "request_id")
    assert retired.exists()
    _archive(path, 0).unlink()
    _write(_archive(path, ARCHIVES), [{"request_id": _key(1, 1), "state": "late"}])
    assert ledger.latest_event(path, _key(1, 1))["state"] == "late"
    assert not retired.exists()
    assert len(list(ledger.checkpoint_dir(path).glob("*.keyidx"))) == ARCHIVES


def test_retention_asks_only_for_the_fields_it_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiworkhub import task_retention

    seen: list[dict[str, Any]] = []

    def spy(_path: Path, _key: str, **kwargs: Any) -> None:
        seen.append(kwargs)
        return None

    monkeypatch.setattr(ledger, "latest_event", spy)
    assert task_retention._latest_process_row(tmp_path, _key(1, 1)) is None
    assert seen == [
        {"key_field": "request_id", "keep_fields": task_retention._PROCESS_ROW_FIELDS}
    ]
    for field in ("request_id", "task_id", "state", "terminal_state", "timestamp"):
        assert field in task_retention._PROCESS_ROW_FIELDS
