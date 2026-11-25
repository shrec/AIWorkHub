"""NF-2026-02443: identical-snapshot appends, cold request scans, retention.

Field evidence: a dashboard reconciler reached VmHWM 18.6 GB and rchar 4.1 TB
because (1) unchanged status snapshots were re-appended on every pass, (2) a
64-key request projection turned every status call into a cold full-history
scan, and (3) rotated archives were never pruned.
"""

from __future__ import annotations

import json
import os
import pathlib
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import process_event_ledger as ledger


@pytest.fixture(autouse=True)
def _clean_caches():
    ledger.reset_append_digest_cache()
    ledger.reset_request_event_cache()
    ledger.reset_latest_event_cache()
    yield
    ledger.reset_append_digest_cache()
    ledger.reset_request_event_cache()
    ledger.reset_latest_event_cache()


def _rows(path: Path) -> list[dict[str, Any]]:
    return list(ledger.iter_events(path))


def _stamp(counter: int) -> str:
    return f"2026-11-26T00:00:00.{counter:06d}+00:00"


# --- (1) identical-snapshot append guard ------------------------------------


def test_unchanged_snapshot_minus_timestamp_is_not_appended(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    base = {"request_id": "37b85eb2", "state": "review_ready", "exit_code": 0}

    ledger.append_event(path, {**base, "timestamp": _stamp(1)}, skip_unchanged=True)
    for counter in range(2, 40):
        ledger.append_event(
            path, {**base, "timestamp": _stamp(counter)}, skip_unchanged=True
        )
    assert len(_rows(path)) == 1

    # A real change is always written, and so is a return to an older snapshot
    # (it differs from the request's LATEST row).
    ledger.append_event(
        path, {**base, "state": "accepted", "timestamp": _stamp(50)}, skip_unchanged=True
    )
    ledger.append_event(path, {**base, "timestamp": _stamp(51)}, skip_unchanged=True)
    assert [row["state"] for row in _rows(path)] == ["review_ready", "accepted", "review_ready"]

    # Another request's identical snapshot is its own history.
    ledger.append_event(
        path, {**base, "request_id": "other", "timestamp": _stamp(52)}, skip_unchanged=True
    )
    assert len(_rows(path)) == 4


def test_guard_never_scans_the_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "process_events.jsonl"
    for index in range(40):
        ledger.append_event(
            path,
            {"request_id": f"r-{index}", "state": "running", "payload": "x" * 200},
            max_active_bytes=2048,
        )
    assert len(ledger.ledger_paths(path)) > 2

    def forbidden(*_args, **_kwargs):
        pytest.fail("the append guard must never replay ledger history")

    monkeypatch.setattr(ledger, "iter_events", forbidden)
    monkeypatch.setattr(ledger, "_iter_ledger_file", forbidden)
    monkeypatch.setattr(ledger, "_scan_request_events", forbidden)
    row = {"request_id": "steady", "state": "review_ready"}
    for counter in range(10):
        ledger.append_event(
            path, {**row, "timestamp": _stamp(counter)}, skip_unchanged=True,
            max_active_bytes=2048,
        )
    monkeypatch.undo()
    assert sum(1 for item in _rows(path) if item["request_id"] == "steady") == 1


def test_guard_follows_foreign_appends_and_foreign_rotation(tmp_path: Path) -> None:
    """Rows written by another process (and a rotation it made) are folded from
    the appended bytes only, so the latest digest stays exact."""

    path = tmp_path / "process_events.jsonl"
    mine = {"request_id": "req", "state": "review_ready"}
    theirs = {"request_id": "req", "state": "finalize_failed", "error": "e"}
    ledger.append_event(path, {**mine, "timestamp": _stamp(1)}, skip_unchanged=True)

    # Another process appends a different latest row, then rotates.
    with path.open("ab") as handle:
        handle.write((json.dumps({**theirs, "timestamp": _stamp(2)}) + "\n").encode())
    os.replace(path, path.with_name("process_events.29990101T000000.000000Z.1.deadbeef.jsonl"))

    # ``mine`` is no longer the request's latest row: it must be written.
    ledger.append_event(path, {**mine, "timestamp": _stamp(3)}, skip_unchanged=True)
    assert [row["state"] for row in _rows(path)] == [
        "review_ready", "finalize_failed", "review_ready",
    ]
    # And now it is the latest row, so a repeat is dropped.
    ledger.append_event(path, {**mine, "timestamp": _stamp(4)}, skip_unchanged=True)
    assert len(_rows(path)) == 3


def test_guard_appends_while_a_spill_exists(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    row = {"request_id": "req", "state": "review_ready"}
    ledger.append_event(path, {**row, "timestamp": _stamp(1)}, skip_unchanged=True)
    ledger._write_immutable_spill(
        path,
        (json.dumps({"request_id": "req", "state": "cancelled", "timestamp": _stamp(2)}) + "\n").encode(),
    )
    ledger.append_event(path, {**row, "timestamp": _stamp(3)}, skip_unchanged=True)
    assert [item["state"] for item in _rows(path)] == ["review_ready", "cancelled", "review_ready"]


def _manager(tmp_path: Path, log: Path):
    from aiworkhub.process_launcher import ProcessManager

    def show_task(task_id: str) -> dict[str, Any]:
        raise RuntimeError(f"no task store in this test: {task_id}")

    return ProcessManager(
        repo=tmp_path,
        process_log_path=log,
        process_dir=tmp_path / f"{log.stem}-processes",
        show_task=show_task,
        isolation_enabled=False,
    )


def test_rerunning_status_and_reconcile_of_unchanged_request_appends_zero_rows(
    tmp_path: Path,
) -> None:
    log = tmp_path / "state" / "process_events.jsonl"
    manager = _manager(tmp_path, log)
    snapshot = {
        "request_id": "37b85eb2" * 4,
        "task_id": "NF-1",
        "runner": "codex",
        "topic": "t",
        "adapter_id": "claude_cli",
        "state": "review_ready",
        "exit_code": 0,
    }
    manager._append_event(snapshot)
    before = log.read_bytes()
    first = manager.status(snapshot["request_id"])
    for _ in range(25):
        # The reconciler's re-record of the same snapshot, plus status reads.
        manager._append_event(dict(snapshot))
        assert manager.status(snapshot["request_id"]) == first
    assert log.read_bytes() == before
    assert len(_rows(log)) == 1


# --- (2) cold misses do not re-read history per status call ------------------


def test_second_reconcile_scan_over_unchanged_ledger_reads_zero_archive_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "process_events.jsonl"
    request_ids = [f"request-{index:03d}" for index in range(100)]
    assert len(request_ids) > 64
    for round_index in range(3):
        for request_id in request_ids:
            ledger.append_event(
                path,
                {"request_id": request_id, "state": f"s{round_index}", "pad": "p" * 40},
                max_active_bytes=4096,
            )
    archives = {str(item) for item in ledger.ledger_paths(path) if item != path}
    assert len(archives) > 3
    expected = {
        request_id: [row for row in _rows(path) if row["request_id"] == request_id]
        for request_id in request_ids
    }

    def scan() -> dict[str, list[dict[str, Any]]]:
        # One status lookup per request, as the reconciler issues them.
        return {
            request_id: ledger.events_for_requests(path, [request_id])[request_id]
            for request_id in request_ids
        }

    assert scan() == expected

    opened: list[str] = []
    original_open = pathlib.Path.open

    def counting_open(self, *args, **kwargs):
        opened.append(str(self))
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", counting_open)
    assert scan() == expected
    monkeypatch.undo()
    assert [name for name in opened if name in archives] == []
    assert opened == []


def test_request_projection_stays_within_its_byte_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "process_events.jsonl"
    for index in range(20):
        ledger.append_event(path, {"request_id": f"r-{index}", "blob": "b" * 2000})
    monkeypatch.setattr(ledger, "_REQUEST_EVENT_CACHE_MAX_BYTES", 12_000)
    for index in range(20):
        rows = ledger.events_for_requests(path, [f"r-{index}"])[f"r-{index}"]
        assert len(rows) == 1
        with ledger._REQUEST_EVENT_CACHE_LOCK:
            for projection in ledger._REQUEST_EVENT_CACHE.values():
                assert projection.estimated_bytes <= 12_000
                # The key this call asked for is always retained first.
                assert f"r-{index}" in projection.events


def test_many_token_filter_is_exact() -> None:
    keys = [f"k{index}" for index in range(40)]
    tokens = [ledger._json_token(key) for key in keys]
    lines = [
        # An escaped backslash right before the key literal.
        b'{"note": "\\\\", "request_id": "k7"}\n',
        b'{"note": "say \\"k8\\" here", "request_id": "zz"}\n',
        b'{"request_id": "k39", "x": 1}\n',
        b'{"request_id": "k400"}\n',
        b'not json "k1"\n',
    ]
    many = [row["request_id"] for row in ledger._rows_containing(lines, tokens)]
    few = [
        row["request_id"]
        for token in tokens
        for row in ledger._rows_containing(lines, [token])
    ]
    assert many == ["k7", "k39"]
    assert sorted(set(few)) == sorted({"k7", "k39"})


# --- (3) bounded archive retention -------------------------------------------


def _history() -> list[dict[str, Any]]:
    """A legacy ledger: old requests buried under repeated snapshots, one
    request spanning old and new history, then distinct newer traffic."""

    events: list[dict[str, Any]] = []
    counter = 0

    def add(row: dict[str, Any]) -> None:
        nonlocal counter
        counter += 1
        events.append({**row, "timestamp": _stamp(counter)})

    for index in range(6):
        request_id = f"old-{index:02d}"
        identity = {
            "request_id": request_id,
            "task_id": f"NF-OLD-{index}",
            "runner": "codex",
            "topic": "t",
            "adapter_id": "claude_cli",
        }
        add({**identity, "state": "running", "pid": 100 + index,
             "metadata_path": f"/m/{request_id}.json", "pad": "o" * 60})
        add({**identity, "state": "review_ready", "exit_code": 0})
        for _ in range(30):
            add({**identity, "state": "review_ready", "exit_code": 0})
        # A sparse error overlay repeated: the lineage fold records the repeat
        # as retention_error, which compaction must preserve.
        for _ in range(4):
            add({"request_id": request_id, "error": "gc_failed"})
        add({"request_id": request_id, "event_kind": "runtime_notice", "notice": "n"})
    add({"request_id": "span", "task_id": "NF-SPAN", "state": "running", "pid": 7})
    for _ in range(20):
        add({"request_id": "span", "task_id": "NF-SPAN", "state": "running", "pid": 7})
    for index in range(120):
        add({"request_id": f"new-{index % 12:02d}", "task_id": f"NF-NEW-{index % 12}",
             "state": f"step-{index}", "pad": "n" * 60})
    add({"request_id": "span", "task_id": "NF-SPAN", "state": "review_ready", "exit_code": 0})
    return events


_FOLDS: list[dict[str, Any]] = [
    {},
    {"replace": True},
    {"replace": True, "skip_event_kinds": ("runtime_notice",)},
    {"keep_fields": ("request_id", "state", "error")},
    {"drop_fields": ("pad",)},
    {"key_field": "task_id"},
]


def _build(path: Path, *, max_archive_bytes: int) -> None:
    for row in _history():
        ledger.append_event(
            path, row, max_active_bytes=2048, max_archive_bytes=max_archive_bytes
        )


def test_rotation_beyond_budget_removes_oldest_archives_preserving_status(
    tmp_path: Path,
) -> None:
    unbounded = tmp_path / "unbounded" / "process_events.jsonl"
    bounded = tmp_path / "bounded" / "process_events.jsonl"
    budget = 4 * 2048
    stats_before = ledger.ledger_retention_stats()
    _build(unbounded, max_archive_bytes=10**12)
    _build(bounded, max_archive_bytes=budget)
    stats = ledger.ledger_retention_stats()

    original = [item.name for item in ledger.ledger_paths(unbounded) if item != unbounded]
    retained = [item for item in ledger.ledger_paths(bounded) if item != bounded]
    assert len(retained) < len(original)
    assert stats["merged"] > stats_before["merged"]
    assert stats["archives_removed"] > stats_before["archives_removed"]
    assert stats["rows_dropped"] > stats_before["rows_dropped"]
    retained_bytes = sum(item.stat().st_size for item in retained)
    assert retained_bytes < sum(
        item.stat().st_size for item in ledger.ledger_paths(unbounded) if item != unbounded
    )
    # Distinct newer rows beyond the budget are history, never removed: the
    # bound is met only as far as pure duplicates allow, and says so.
    assert stats["over_budget"] > stats_before["over_budget"]

    # The old requests now exist only in compacted archives.
    newest_kept = {str(item) for item in retained[-3:]}
    old_requests = [f"old-{index:02d}" for index in range(6)]
    assert all(
        not any(
            key in item.read_text(encoding="utf-8") for item in map(Path, newest_kept)
        )
        for key in old_requests
    )

    for options in _FOLDS:
        assert ledger.latest_events(bounded, **options) == ledger.latest_events(
            unbounded, **options
        ), options

    keys = sorted({row["request_id"] for row in _history()})
    full = ledger.events_for_requests(unbounded, keys)
    kept = ledger.events_for_requests(bounded, keys)
    manager_full = _manager(tmp_path / "unbounded", unbounded)
    manager_kept = _manager(tmp_path / "bounded", bounded)
    for key in keys:
        # Compaction removes rows only; what remains keeps ledger order, and a
        # survivor differs from its original row only by ``collapsed_count``.
        remaining = iter(full[key])
        assert all(
            any(_without_collapsed(row) == other for other in remaining)
            for row in kept[key]
        ), key
        assert ledger.persisted_row_count(kept[key]) == len(full[key]), key
        status_full = manager_full.status(key)
        status_kept = manager_kept.status(key)
        assert status_full["ok"] is True
        # Including event_count: the collapsed rows are counted back.
        assert status_kept == status_full, key
    assert any(
        ledger.COLLAPSED_COUNT_FIELD in row for rows in kept.values() for row in rows
    )
    assert manager_kept.status("old-00")["latest_event"]["retention_error"] == "gc_failed"


def _without_collapsed(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != ledger.COLLAPSED_COUNT_FIELD}


def _drain(path: Path, *, max_archive_bytes: int, merge_bytes: int = 2048) -> int:
    calls = 0
    while ledger._apply_archive_retention(
        path, max_archive_bytes=max_archive_bytes, merge_bytes=merge_bytes
    ):
        calls += 1
        assert calls < 1000
    return calls


def test_retention_is_idempotent_and_skips_compacted_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "process_events.jsonl"
    budget = 4 * 2048
    _build(path, max_archive_bytes=budget)
    # Each rotation runs a bounded number of steps; finish whatever is due.
    _drain(path, max_archive_bytes=budget)
    marks = [
        (item.name, item.stat().st_size, item.stat().st_mtime_ns)
        for item in ledger.ledger_paths(path)
    ]
    latest = ledger.latest_events(path)

    reads: list[str] = []
    original = ledger._duplicate_lines

    def spy(sources):
        reads.extend(item.name for item in sources)
        return original(sources)

    monkeypatch.setattr(ledger, "_duplicate_lines", spy)
    ledger._apply_archive_retention(path, max_archive_bytes=budget, merge_bytes=2048)
    assert reads == []
    assert [
        (item.name, item.stat().st_size, item.stat().st_mtime_ns)
        for item in ledger.ledger_paths(path)
    ] == marks
    ledger.reset_latest_event_cache()
    assert ledger.latest_events(path) == latest


def test_interrupted_merge_is_finished_without_losing_rows(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    for index in range(30):
        ledger.append_event(
            path, {"request_id": f"r-{index}", "state": "s", "pad": "x" * 300},
            max_active_bytes=2048, max_archive_bytes=10**12,
        )
    archives = [item for item in ledger.ledger_paths(path) if item != path]
    older, newer = archives[0], archives[1]
    expected = ledger.latest_events(path)

    # Simulate a crash after publishing a merge of (older, newer) into newer.
    temporary = ledger._write_compacted(path, [older, newer], set())
    manifest = ledger._load_retention_manifest(path)
    manifest["pending"] = {
        "target": newer.name,
        "mark": ledger._archive_mark(temporary),
        "members": [older.name],
    }
    ledger._save_retention_manifest(path, manifest)
    ledger._publish_compacted(temporary, newer)

    ledger._apply_archive_retention(path, max_archive_bytes=10**12, merge_bytes=2048)
    assert not older.exists()
    ledger.reset_latest_event_cache()
    assert ledger.latest_events(path) == expected
    assert ledger._load_retention_manifest(path)["pending"] is None


# --- review rework: error rows, symlinks, pending members, spills, bounds ----


_ERROR_IDENTITY = {
    "request_id": "e" * 32,
    "task_id": "NF-ERR",
    "runner": "codex",
    "topic": "t",
    "adapter_id": "claude_cli",
    "state": "review_ready",
    "exit_code": 0,
}


def test_repeated_sparse_error_row_is_never_skipped(tmp_path: Path) -> None:
    """The lineage fold reads the second identical sparse error row as
    retention_error, so the guard must write it: status() is identical with
    the guard on and off."""

    request_id = _ERROR_IDENTITY["request_id"]
    statuses = []
    for skip in (True, False):
        root = tmp_path / f"skip-{skip}"
        log = root / "process_events.jsonl"
        ledger.reset_append_digest_cache()
        ledger.append_event(log, {**_ERROR_IDENTITY, "timestamp": _stamp(1)}, skip_unchanged=skip)
        for counter in (2, 3):
            ledger.append_event(
                log,
                {"request_id": request_id, "error": "gc_failed", "timestamp": _stamp(counter)},
                skip_unchanged=skip,
            )
        assert len(_rows(log)) == 3
        statuses.append(_manager(root, log).status(request_id))
    guarded, unguarded = statuses
    assert guarded == unguarded
    assert guarded["latest_event"]["retention_error"] == "gc_failed"
    assert guarded["event_count"] == 3


def test_failure_terminal_repeat_is_never_skipped(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    row = {"request_id": "req", "state": "cancelled", "error": ""}
    for counter in range(3):
        ledger.append_event(path, {**row, "timestamp": _stamp(counter)}, skip_unchanged=True)
    assert len(_rows(path)) == 3
    # A plain snapshot after it is still skippable.
    plain = {"request_id": "req", "state": "review_ready"}
    for counter in range(3, 6):
        ledger.append_event(path, {**plain, "timestamp": _stamp(counter)}, skip_unchanged=True)
    assert len(_rows(path)) == 4


def test_retention_never_opens_or_truncates_a_non_regular_archive_name(
    tmp_path: Path,
) -> None:
    """Platform-independent half of the symlink contract: a directory named
    like an archive is never opened, emptied or listed."""

    path = tmp_path / "ledger" / "process_events.jsonl"
    for index in range(12):
        ledger.append_event(
            path, {"request_id": f"r-{index % 2}", "state": "same", "pad": "x" * 300},
            max_active_bytes=2048, max_archive_bytes=10**12,
        )
    impostor = path.with_name("process_events.20000101T000000.000000Z.1.deadbeef.jsonl")
    impostor.mkdir()
    kept = impostor / "kept.txt"
    kept.write_bytes(b"keep me")
    expected = ledger.latest_events(path)

    assert impostor not in ledger.ledger_paths(path)
    with pytest.raises(OSError):
        ledger._open_archive(impostor)
    ledger._retire_member(impostor)
    assert kept.read_bytes() == b"keep me"

    newest = [item for item in ledger.ledger_paths(path) if item != path][-1]
    manifest = ledger._load_retention_manifest(path)
    manifest["pending"] = {
        "target": newest.name,
        "mark": ledger._archive_mark(newest),
        "members": [impostor.name],
    }
    ledger._save_retention_manifest(path, manifest)
    ledger._apply_archive_retention(path, max_archive_bytes=2048, merge_bytes=10**6)
    assert kept.read_bytes() == b"keep me"
    # It holds no rows readers list, so it never stalls the pending merge.
    assert ledger._load_retention_manifest(path)["pending"] is None
    ledger.reset_latest_event_cache()
    assert ledger.latest_events(path) == expected


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are unavailable on this host: {exc}")


def test_retention_never_follows_an_archive_named_symlink(tmp_path: Path) -> None:
    path = tmp_path / "ledger" / "process_events.jsonl"
    for index in range(30):
        ledger.append_event(
            path,
            {"request_id": f"r-{index % 3}", "state": "same", "pad": "x" * 300},
            max_active_bytes=2048,
            max_archive_bytes=10**12,
        )
    outside = tmp_path / "outside.txt"
    secret = b'{"request_id": "r-0", "state": "same"}\n' * 50
    outside.write_bytes(secret)
    link = path.with_name("process_events.20000101T000000.000000Z.1.deadbeef.jsonl")
    _symlink_or_skip(link, outside)
    retired_link = path.with_name("process_events.20000101T000000.000001Z.1.deadbeef.jsonl")
    _symlink_or_skip(retired_link, outside)
    expected = ledger.latest_events(path)

    # Compaction never opens it and readers never list it.
    assert link not in ledger.ledger_paths(path)
    with pytest.raises(OSError):
        ledger._open_archive(link)
    with pytest.raises(OSError):
        ledger._duplicate_lines([link])

    # Retiring it only removes the name.
    assert ledger._retire_member(retired_link) is True
    assert not os.path.lexists(retired_link)
    assert outside.read_bytes() == secret

    # A pending merge that lists it as a member: the finish unlinks the link.
    newest = [item for item in ledger.ledger_paths(path) if item != path][-1]
    manifest = ledger._load_retention_manifest(path)
    manifest["pending"] = {
        "target": newest.name,
        "mark": ledger._archive_mark(newest),
        "members": [link.name],
    }
    ledger._save_retention_manifest(path, manifest)
    _drain(path, max_archive_bytes=2048, merge_bytes=10**6)
    assert not os.path.lexists(link)
    assert outside.read_bytes() == secret
    assert ledger._load_retention_manifest(path)["pending"] is None
    ledger.reset_latest_event_cache()
    assert ledger.latest_events(path) == expected


def test_failed_retire_after_published_merge_never_shows_rows_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state" / "process_events.jsonl"
    keys = [f"{index:032x}" for index in range(6)]
    for index in range(36):
        ledger.append_event(
            path,
            {
                "request_id": keys[index % 6],
                "task_id": f"NF-{index % 6}",
                "state": f"s-{index}",
                "pad": "x" * 200,
                "timestamp": _stamp(index),
            },
            max_active_bytes=2048,
            max_archive_bytes=10**12,
        )
    manager = _manager(tmp_path, path)
    expected_events = ledger.events_for_requests(path, keys)
    expected_status = {key: manager.status(key) for key in keys}
    expected_latest = ledger.latest_events(path)

    monkeypatch.setattr(ledger, "_retire_member", lambda _member: False)
    for _ in range(20):
        ledger._apply_archive_retention(path, max_archive_bytes=2048, merge_bytes=10**6)
        if ledger._load_retention_manifest(path)["pending"] is not None:
            break
    pending = ledger._load_retention_manifest(path)["pending"]
    assert pending is not None
    members = [path.with_name(name) for name in pending["members"]]
    assert members and all(item.exists() for item in members)
    assert not set(members) & set(ledger.ledger_paths(path))

    for cold in (False, True):
        if cold:
            ledger.reset_request_event_cache()
            ledger.reset_latest_event_cache()
        assert ledger.events_for_requests(path, keys) == expected_events
        assert {key: manager.status(key) for key in keys} == expected_status
        assert ledger.latest_events(path) == expected_latest

    # Once a retire succeeds the merge finishes, still without a change.
    monkeypatch.undo()
    _drain(path, max_archive_bytes=2048, merge_bytes=10**6)
    assert ledger._load_retention_manifest(path)["pending"] is None
    assert not any(item.exists() for item in members)
    ledger.reset_request_event_cache()
    ledger.reset_latest_event_cache()
    assert ledger.events_for_requests(path, keys) == expected_events
    assert {key: manager.status(key) for key in keys} == expected_status


def test_spill_does_not_disable_retention(tmp_path: Path) -> None:
    unbounded = tmp_path / "unbounded" / "process_events.jsonl"
    bounded = tmp_path / "bounded" / "process_events.jsonl"
    for target in (unbounded, bounded):
        for row in _history():
            ledger.append_event(target, row, max_active_bytes=2048, max_archive_bytes=10**12)
        ledger._write_immutable_spill(
            target,
            (json.dumps({"request_id": "old-00", "state": "cancelled",
                         "timestamp": _stamp(5000)}) + "\n").encode(),
        )
    before = ledger.ledger_retention_stats()
    _drain(bounded, max_archive_bytes=4 * 2048)
    after = ledger.ledger_retention_stats()
    assert after["merged"] > before["merged"]
    assert after["rows_dropped"] > before["rows_dropped"]
    assert len(ledger.ledger_paths(bounded)) < len(ledger.ledger_paths(unbounded))
    for options in _FOLDS:
        assert ledger.latest_events(bounded, **options) == ledger.latest_events(
            unbounded, **options
        ), options
    keys = sorted({row["request_id"] for row in _history()})
    manager_full = _manager(tmp_path / "unbounded", unbounded)
    manager_kept = _manager(tmp_path / "bounded", bounded)
    for key in keys:
        assert manager_kept.status(key) == manager_full.status(key), key


def test_out_of_order_archive_is_not_rewritten_beside_a_spill(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    # Timestamps out of order inside the oldest archive (a writer that took
    # its timestamp before the lock), with pure duplicates to drop.
    for counter in (9, 3, 4, 5, 6, 7, 8, 1, 2, 3):
        ledger.append_event(
            path,
            {"request_id": "req", "state": "same", "pad": "x" * 300,
             "timestamp": _stamp(counter)},
            max_active_bytes=2048, max_archive_bytes=10**12,
        )
    ledger._write_immutable_spill(
        path,
        (json.dumps({"request_id": "req", "state": "spilled",
                     "timestamp": _stamp(4)}) + "\n").encode(),
    )
    expected = [row for row in _rows(path) if row["request_id"] == "req"]
    oldest = [item for item in ledger.ledger_paths(path) if item != path][0]
    original = oldest.read_bytes()
    _drain(path, max_archive_bytes=0, merge_bytes=10**6)
    assert oldest.read_bytes() == original
    assert [row for row in _rows(path) if row["request_id"] == "req"] == expected


def test_archive_deferred_beside_a_spill_is_compacted_once_the_spill_is_gone(
    tmp_path: Path,
) -> None:
    path = tmp_path / "process_events.jsonl"
    for counter in (9, 3, 4, 5, 6, 7, 8, 1, 2, 3):
        ledger.append_event(
            path,
            {"request_id": "req", "state": "same", "pad": "x" * 300,
             "timestamp": _stamp(counter)},
            max_active_bytes=2048, max_archive_bytes=10**12,
        )
    ledger._write_immutable_spill(
        path,
        (json.dumps({"request_id": "req", "state": "spilled",
                     "timestamp": _stamp(4)}) + "\n").encode(),
    )
    oldest = [item for item in ledger.ledger_paths(path) if item != path][0]
    original = oldest.read_bytes()
    _drain(path, max_archive_bytes=0, merge_bytes=10**6)
    assert oldest.read_bytes() == original
    manifest = ledger._load_retention_manifest(path)
    assert oldest.name in manifest["deferred"]
    assert oldest.name not in manifest["compacted"]

    spills = [item for item in ledger.ledger_paths(path) if ledger._is_spill(path, item)]
    assert spills
    for spill in spills:
        spill.unlink()
    before = [row for row in _rows(path) if row["request_id"] == "req"]
    status_before = _manager(tmp_path / "before", path).status("req")
    latest_before = ledger.latest_events(path)
    assert status_before["ok"] is True

    _drain(path, max_archive_bytes=0, merge_bytes=10**6)  # terminates (capped)

    after = [row for row in _rows(path) if row["request_id"] == "req"]
    assert len(after) < len(before)  # the pure duplicates were dropped
    kept = ledger.events_for_requests(path, ["req"])["req"]
    assert ledger.persisted_row_count(kept) == len(before)
    assert _manager(tmp_path / "after", path).status("req") == status_before
    assert ledger.latest_events(path) == latest_before
    manifest = ledger._load_retention_manifest(path)
    assert manifest["deferred"] == {}
    # A second drain over the compacted history has nothing left to do.
    assert _drain(path, max_archive_bytes=0, merge_bytes=10**6) == 0


def test_one_rotation_runs_a_bounded_number_of_retention_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "process_events.jsonl"
    for row in _history():
        ledger.append_event(path, row, max_active_bytes=2048, max_archive_bytes=10**12)
    archives = len(ledger.ledger_paths(path)) - 1
    assert archives > 4 * ledger._RETENTION_STEPS_PER_CALL
    expected = ledger.latest_events(path)

    read: list[str] = []
    original = ledger._duplicate_lines

    def spy(sources):
        read.extend(item.name for item in sources)
        return original(sources)

    monkeypatch.setattr(ledger, "_duplicate_lines", spy)
    assert ledger._apply_archive_retention(path, max_archive_bytes=2048, merge_bytes=2048)
    # A step reads its archive and at most one older neighbour, twice.
    assert len(read) <= 3 * ledger._RETENTION_STEPS_PER_CALL
    first_call = len(read)
    monkeypatch.undo()
    calls = _drain(path, max_archive_bytes=2048)
    assert calls > 1
    assert first_call < archives
    ledger.reset_latest_event_cache()
    assert ledger.latest_events(path) == expected
