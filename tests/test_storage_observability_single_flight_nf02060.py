"""NF-2026-02060: one storage measurement per TTL per repository, not per process.

Every MCP server process bound to a repository used to re-run the full
``_measure`` walk on its own 300 s timer.  A second process is simulated here by
resetting the in-process cache between calls; the persisted cache and the
in-repo measure lock are the only state the two "processes" share.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from aiworkhub import storage_observability as so

# Never a real pid on any supported platform; liveness is stubbed for it.
OTHER_PID = 2_000_000_123
_ORIGINAL_REFRESH = so._refresh


def _canonical_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".aiworkhub").mkdir(parents=True)
    # ``_persist`` and the measure lock only write in a canonical repository.
    (repo / ".aiworkhub" / "project.json").write_text("{}", encoding="utf-8")
    return repo.resolve()


def _lock_path(repo: Path) -> Path:
    return repo / so.MEASURE_LOCK_RELATIVE_PATH


def _measured(total: int) -> dict:
    return {
        "scan_status": "ready",
        "scanned_at": f"2026-11-04T00:00:{total % 60:02d}+00:00",
        "managed_total_bytes": total,
        "components": [{"id": "state.db", "bytes": total, "files": 1}],
        "terminal_log_retention": {},
        "errors": [],
    }


def _snapshot_after_refresh(monkeypatch, repo: Path) -> dict:
    """Trigger one background refresh and return the snapshot it published."""
    done = threading.Event()

    def refresh(key, root):
        try:
            return _ORIGINAL_REFRESH(key, root)
        finally:
            done.set()

    monkeypatch.setattr(so, "_refresh", refresh)
    so.snapshot(repo)
    # Deadlock guard only, not a performance bound.
    assert done.wait(30.0), "storage refresh did not complete"
    return so.snapshot(repo)


def _as_second_process(repo: Path) -> None:
    """Fresh in-process state whose first-snapshot load already happened."""
    so._reset_cache_for_tests()
    with so._lock:
        so._loaded.add(str(repo))


def _write_lock(repo: Path, *, pid: int, age: float) -> bytes:
    raw = json.dumps({
        "pid": pid,
        "acquired_at_epoch": time.time() - age,
        "nonce": "another-process",
    }).encode("ascii")
    path = _lock_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def _stub_other_pid_liveness(monkeypatch, alive: bool) -> None:
    real = so.platform_io.process_is_alive
    monkeypatch.setattr(
        so.platform_io,
        "process_is_alive",
        lambda pid: alive if pid == OTHER_PID else real(pid),
    )


def test_second_owner_serves_persisted_result_without_measuring(tmp_path, monkeypatch):
    repo = _canonical_repo(tmp_path)
    calls: list[Path] = []
    lock_held_by_us: list[bool] = []

    def measure(root: Path) -> dict:
        calls.append(root)
        lock = json.loads(_lock_path(repo).read_bytes())
        lock_held_by_us.append(lock["pid"] == os.getpid())
        return _measured(4242)

    monkeypatch.setattr(so, "_measure", measure)
    so._reset_cache_for_tests()

    first = _snapshot_after_refresh(monkeypatch, repo)
    assert first["scan_status"] == "ready"
    assert calls == [repo]
    assert lock_held_by_us == [True]
    assert not _lock_path(repo).exists()

    _as_second_process(repo)
    second = _snapshot_after_refresh(monkeypatch, repo)

    assert calls == [repo], "second owner re-measured inside the TTL"
    assert second["scan_status"] == "ready"
    for key in ("managed_total_bytes", "scanned_at", "components", "errors"):
        assert second[key] == first[key], key
    assert not _lock_path(repo).exists()
    # Nothing is written outside the repository.
    assert sorted(item.name for item in tmp_path.iterdir()) == ["repo"]


def test_invalidated_owner_measures_even_with_a_fresh_persisted_result(tmp_path, monkeypatch):
    repo = _canonical_repo(tmp_path)
    so._persist(repo, _measured(1))
    calls: list[Path] = []
    monkeypatch.setattr(so, "_measure", lambda root: calls.append(root) or _measured(2))
    so._reset_cache_for_tests()
    assert so.snapshot(repo)["managed_total_bytes"] == 1

    so.invalidate(repo)
    result = _snapshot_after_refresh(monkeypatch, repo)

    assert calls == [repo]
    assert result["managed_total_bytes"] == 2


def test_lock_held_by_live_pid_makes_second_owner_skip_measuring(tmp_path, monkeypatch):
    repo = _canonical_repo(tmp_path)
    raw = _write_lock(repo, pid=OTHER_PID, age=0.0)
    _stub_other_pid_liveness(monkeypatch, alive=True)
    calls: list[Path] = []
    monkeypatch.setattr(so, "_measure", lambda root: calls.append(root) or _measured(9))
    so._reset_cache_for_tests()

    result = _snapshot_after_refresh(monkeypatch, repo)

    assert calls == []
    assert result["scan_status"] == "unavailable"
    assert _lock_path(repo).read_bytes() == raw, "another owner's lock was touched"
    # The loser backs off instead of re-polling the lock on every request.
    retried: list[str] = []
    monkeypatch.setattr(so, "_refresh", lambda key, _root: retried.append(key))
    so.snapshot(repo)
    assert retried == []


def test_lock_loser_adopts_the_holders_persisted_result(tmp_path, monkeypatch):
    repo = _canonical_repo(tmp_path)
    so._persist(repo, _measured(31))
    _write_lock(repo, pid=OTHER_PID, age=0.0)
    _stub_other_pid_liveness(monkeypatch, alive=True)
    calls: list[Path] = []
    monkeypatch.setattr(so, "_measure", lambda root: calls.append(root) or _measured(9))
    _as_second_process(repo)

    result = _snapshot_after_refresh(monkeypatch, repo)

    assert calls == []
    assert result["scan_status"] == "ready"
    assert result["managed_total_bytes"] == 31


def _assert_lock_taken_over(monkeypatch, tmp_path: Path, repo: Path) -> None:
    holders: list[int] = []

    def measure(_root: Path) -> dict:
        holders.append(json.loads(_lock_path(repo).read_bytes())["pid"])
        return _measured(7)

    monkeypatch.setattr(so, "_measure", measure)
    so._reset_cache_for_tests()

    result = _snapshot_after_refresh(monkeypatch, repo)

    assert holders == [os.getpid()]
    assert result["scan_status"] == "ready"
    assert result["managed_total_bytes"] == 7
    assert not _lock_path(repo).exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == ["repo"]


@pytest.mark.parametrize(
    "alive",
    [False, True],
    ids=["dead_pid", "live_pid"],
)
def test_lock_older_than_bound_is_taken_over_regardless_of_liveness(
    tmp_path, monkeypatch, alive
):
    repo = _canonical_repo(tmp_path)
    _write_lock(repo, pid=OTHER_PID, age=so.MEASURE_LOCK_STALE_SECONDS + 60.0)
    _stub_other_pid_liveness(monkeypatch, alive=alive)
    _assert_lock_taken_over(monkeypatch, tmp_path, repo)


def test_fresh_lock_from_dead_pid_is_taken_over_immediately(tmp_path, monkeypatch):
    repo = _canonical_repo(tmp_path)
    _write_lock(repo, pid=OTHER_PID, age=0.0)
    _stub_other_pid_liveness(monkeypatch, alive=False)
    _assert_lock_taken_over(monkeypatch, tmp_path, repo)


def test_lock_stamped_in_the_future_is_taken_over(tmp_path, monkeypatch):
    repo = _canonical_repo(tmp_path)
    _write_lock(repo, pid=OTHER_PID, age=-3600.0)
    _stub_other_pid_liveness(monkeypatch, alive=True)
    _assert_lock_taken_over(monkeypatch, tmp_path, repo)


def test_overflowing_lock_stamp_does_not_raise_or_strand_refresh(tmp_path, monkeypatch):
    repo = _canonical_repo(tmp_path)
    path = _lock_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"pid": 1, "acquired_at_epoch": 1' + "0" * 400 + "}", encoding="ascii"
    )
    monkeypatch.setattr(so, "_measure", lambda _root: _measured(5))
    so._reset_cache_for_tests()
    key = str(repo)
    with so._lock:
        so._running.add(key)

    so._refresh(key, repo)

    with so._lock:
        assert key not in so._running


def test_lock_acquire_failure_does_not_strand_refresh(tmp_path, monkeypatch):
    repo = _canonical_repo(tmp_path)

    def broken_acquire(_root: Path):
        raise RuntimeError("lock subsystem failed")

    monkeypatch.setattr(so, "_acquire_measure_lock", broken_acquire)
    monkeypatch.setattr(so, "_measure", lambda _root: _measured(5))
    so._reset_cache_for_tests()
    key = str(repo)
    with so._lock:
        so._running.add(key)

    so._refresh(key, repo)

    with so._lock:
        assert key not in so._running
        published = so._cache[key]
    assert published["scan_status"] == "error"
    assert published["errors"] == ["storage_scan_failed:RuntimeError"]


def test_lock_is_removed_after_a_failed_measure(tmp_path, monkeypatch):
    repo = _canonical_repo(tmp_path)
    lock_seen: list[bool] = []

    def failing_measure(_root: Path) -> dict:
        lock_seen.append(_lock_path(repo).is_file())
        raise RuntimeError("walk failed")

    monkeypatch.setattr(so, "_measure", failing_measure)
    so._reset_cache_for_tests()

    result = _snapshot_after_refresh(monkeypatch, repo)

    assert lock_seen == [True]
    assert result["scan_status"] == "error"
    assert result["errors"] == ["storage_scan_failed:RuntimeError"]
    assert not _lock_path(repo).exists()
    assert not (repo / so.PERSISTED_CACHE_RELATIVE_PATH).exists()
