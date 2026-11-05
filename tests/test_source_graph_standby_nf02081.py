"""NF-2026-02081: Source Graph standby truth.

Field report (awh NF-2026-00020 / NF-2026-00003 refresh part, 0.12.87/0.12.88):

  1. A writer in STANDBY (another process holds the writer lease) answered
     refresh_now with ``succeeded`` because ANY readable prior generation
     existed -- on awh that generation was 12.8 h old.
  2. On a never-built repository refresh_now failed with
     ``refresh_terminal_status:standby`` while doctor reported ok.
  3. New/edited files stayed ``declared_input_unindexed`` /
     ``stale_served_from_disk`` for minutes until the periodic tick.

The contract pinned here:

  (a) STANDBY hands the request to the lease holder (durable handoff record)
      and waits, bounded, for a generation that finished after
      ``requested_at``; when the lease is free (holder finished, died, or its
      record is stale) the caller takes it over and builds; otherwise it
      fails as ``standby_holder_did_not_refresh`` naming holder pid/liveness.
  (b) a repository with no generation gets its first one from refresh_now,
      or a named reason why not.
  (c) doctor flags a never-built or stale-beyond-threshold index as a problem.
  (d) a read that sees a file ahead of the index nudges a coalesced refresh.

Every test uses its own ``tmp_path`` repository and in-process (thread) builds.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import doctor, source_graph, source_graph_daemon, task_store  # noqa: E402

# Hang-guard deadline only, never a timing assertion.
_HANG_GUARD_SECONDS = 120


@pytest.fixture(autouse=True)
def thread_builds(monkeypatch):
    monkeypatch.setenv(
        source_graph_daemon.BUILD_EXECUTION_ENV,
        source_graph_daemon.BUILD_EXECUTION_THREAD,
    )


@pytest.fixture
def cleanup_daemons():
    roots: list[Path] = []
    yield roots
    for root in roots:
        source_graph_daemon.stop_daemon(root)


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    result = task_store.initialize_repository(root)
    assert result["ok"], result
    return root


def _hold_lease_until(root: Path, held: threading.Event, release: threading.Event) -> None:
    with source_graph.index_write_lease(root) as acquired:
        assert acquired is True
        held.set()
        release.wait(timeout=_HANG_GUARD_SECONDS)


# ---------------------------------------------------------------------------
# (a) STANDBY never reports succeeded on a generation older than the request
# ---------------------------------------------------------------------------


def test_standby_with_live_holder_fails_by_name_with_holder_pid_and_liveness(
    tmp_path, cleanup_daemons,
):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    (root / "old.py").write_text("def old():\n    return 1\n", encoding="utf-8")
    source_graph.build_index(root, incremental=False)  # readable, but older
    daemon = source_graph_daemon.SourceGraphDaemon(root, standby_wait_seconds=0.5)

    with source_graph.index_write_lease(root) as acquired:
        assert acquired is True
        result = daemon.refresh_now()

    job = result["refresh_job"]
    assert result["status"] == source_graph_daemon.STATUS_STANDBY
    assert job["state"] == "failed"
    assert job["error"].startswith(
        f"{source_graph_daemon.STANDBY_HOLDER_DID_NOT_REFRESH}:holder_pid={os.getpid()}"
        ":holder_liveness=alive"
    )
    holder = job["standby"]["holder"]
    assert holder["held"] is True
    assert holder["pid"] == os.getpid()
    assert holder["liveness"] == "alive"
    # The request was durably handed to the holder.
    handoff = source_graph_daemon._read_refresh_handoff(root)
    assert handoff["requested_at"] == job["requested_at"]
    assert handoff["requester_pid"] == os.getpid()
    assert handoff["job_id"] == job["job_id"]


def test_standby_waits_for_holder_generation_newer_than_request(
    tmp_path, monkeypatch, cleanup_daemons,
):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    (root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    real_build = source_graph.build_index
    real_build(root, incremental=False)

    def lease_elsewhere(repo_root, *, incremental=True, db_path=None):
        raise source_graph.SourceGraphBuildInProgressError("locked_by_other_process")

    monkeypatch.setattr(source_graph, "build_index", lease_elsewhere)
    # The holder is alive and keeps the lease: this caller must not take over.
    monkeypatch.setattr(
        source_graph_daemon,
        "_standby_lease_holder",
        lambda _root: {"held": True, "pid": 4242, "liveness": "alive", "acquired_at": ""},
    )
    daemon = source_graph_daemon.SourceGraphDaemon(
        root, standby_wait_seconds=_HANG_GUARD_SECONDS
    )

    def holder_builds_after_handoff() -> None:
        deadline = time.monotonic() + _HANG_GUARD_SECONDS
        while not source_graph_daemon._read_refresh_handoff(root):
            if time.monotonic() > deadline:
                return
            time.sleep(0.02)
        (root / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
        real_build(root, incremental=True)

    holder = threading.Thread(target=holder_builds_after_handoff)
    holder.start()
    try:
        result = daemon.refresh_now()
    finally:
        holder.join(timeout=_HANG_GUARD_SECONDS)

    job = result["refresh_job"]
    assert job["state"] == "succeeded", job
    assert job["standby"]["resolved_by"] == "lease_holder_generation"
    finished = datetime.fromisoformat(job["standby"]["generation_finished_at"])
    assert finished > datetime.fromisoformat(job["requested_at"])


def test_standby_takes_over_lease_when_holder_releases_it(tmp_path, cleanup_daemons):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    (root / "base.py").write_text("def base():\n    return 1\n", encoding="utf-8")
    source_graph.build_index(root, incremental=False)
    (root / "added.py").write_text("def added_after_request():\n    return 2\n", encoding="utf-8")
    daemon = source_graph_daemon.SourceGraphDaemon(
        root, standby_wait_seconds=_HANG_GUARD_SECONDS
    )
    held = threading.Event()

    def holder_releases_after_handoff() -> None:
        with source_graph.index_write_lease(root) as acquired:
            assert acquired is True
            held.set()
            deadline = time.monotonic() + _HANG_GUARD_SECONDS
            while not source_graph_daemon._read_refresh_handoff(root):
                if time.monotonic() > deadline:
                    return
                time.sleep(0.02)

    holder = threading.Thread(target=holder_releases_after_handoff)
    holder.start()
    assert held.wait(timeout=_HANG_GUARD_SECONDS)
    try:
        result = daemon.refresh_now()
    finally:
        holder.join(timeout=_HANG_GUARD_SECONDS)

    job = result["refresh_job"]
    assert job["state"] == "succeeded", job
    assert job["standby"]["resolved_by"] == "lease_takeover_build"
    assert job["standby"]["takeover_attempts"] >= 1
    found = source_graph.file_query(root, "added.py")
    assert found["matches"], found


def test_stale_holder_record_of_dead_process_does_not_block_takeover(
    tmp_path, monkeypatch, cleanup_daemons,
):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    (root / "x.py").write_text("def x():\n    return 1\n", encoding="utf-8")
    real_build = source_graph.build_index
    real_build(root, incremental=False)
    # A crashed holder left its record behind; the OS released its lease.
    lock_path = source_graph.resolve_db_path(root).with_name("index.lock")
    source_graph._lease_holder_path(lock_path).write_text(
        json.dumps({"pid": 999_999_999, "token": "dead", "acquired_at": ""}),
        encoding="utf-8",
    )
    calls = {"n": 0}

    def first_call_standby(repo_root, *, incremental=True, db_path=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise source_graph.SourceGraphBuildInProgressError("locked_by_other_process")
        return real_build(repo_root, incremental=incremental, db_path=db_path)

    monkeypatch.setattr(source_graph, "build_index", first_call_standby)
    daemon = source_graph_daemon.SourceGraphDaemon(
        root, standby_wait_seconds=_HANG_GUARD_SECONDS
    )
    result = daemon.refresh_now()

    job = result["refresh_job"]
    assert job["state"] == "succeeded", job
    assert job["standby"]["resolved_by"] == "lease_takeover_build"
    assert result["status"] == source_graph_daemon.STATUS_READY


def test_loop_consumed_refresh_job_in_standby_fails_by_name(tmp_path, cleanup_daemons):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    (root / "loop.py").write_text("def loop():\n    return 1\n", encoding="utf-8")
    daemon = source_graph_daemon.SourceGraphDaemon(
        root,
        refresh_interval_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS,
        standby_wait_seconds=0.5,
    )
    daemon.start()
    try:
        assert daemon.wait_for_first_build(timeout=_HANG_GUARD_SECONDS)
        held = threading.Event()
        release = threading.Event()
        holder = threading.Thread(target=_hold_lease_until, args=(root, held, release))
        holder.start()
        try:
            assert held.wait(timeout=_HANG_GUARD_SECONDS)
            reply = daemon.request_refresh()
            job_id = reply["job_id"]
            deadline = time.monotonic() + _HANG_GUARD_SECONDS
            while True:
                with daemon._state_lock:
                    job = dict(daemon._last_refresh_job or {})
                if job.get("job_id") == job_id and job.get("state") in {"succeeded", "failed"}:
                    break
                assert time.monotonic() < deadline, job
                time.sleep(0.02)
        finally:
            release.set()
            holder.join(timeout=_HANG_GUARD_SECONDS)
    finally:
        daemon.stop()

    assert job["state"] == "failed"
    assert job["error"].startswith(source_graph_daemon.STANDBY_HOLDER_DID_NOT_REFRESH)
    assert job["standby"]["holder"]["pid"] == os.getpid()


def test_refresh_terminal_state_never_maps_standby_to_succeeded(tmp_path):
    root = _init_repo(tmp_path)
    (root / "g.py").write_text("def g():\n    return 1\n", encoding="utf-8")
    source_graph.build_index(root, incremental=False)
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    assert source_graph_daemon._repo_has_readable_generation(root) is True
    state, error = daemon._refresh_terminal_state(source_graph_daemon.STATUS_STANDBY, "")
    assert state == "failed"
    assert error == source_graph_daemon.STANDBY_HOLDER_DID_NOT_REFRESH


def test_generation_finished_after_requires_a_newer_successful_generation(tmp_path):
    root = _init_repo(tmp_path)
    assert source_graph_daemon._generation_finished_after(root, datetime.now(timezone.utc).isoformat()) == ""
    (root / "h.py").write_text("def h():\n    return 1\n", encoding="utf-8")
    report = source_graph.build_index(root, incremental=False)
    before = (datetime.fromisoformat(report.finished_at) - timedelta(seconds=5)).isoformat()
    after = (datetime.fromisoformat(report.finished_at) + timedelta(seconds=5)).isoformat()
    assert source_graph_daemon._generation_finished_after(root, before) == report.finished_at
    assert source_graph_daemon._generation_finished_after(root, after) == ""
    assert source_graph_daemon._generation_finished_after(root, "") == ""


def test_pid_liveness_is_non_signalling_and_three_valued(monkeypatch):
    assert source_graph_daemon._pid_liveness(os.getpid()) == "alive"
    assert source_graph_daemon._pid_liveness(None) == "unknown"
    assert source_graph_daemon._pid_liveness(True) == "unknown"
    monkeypatch.setattr(source_graph_daemon.platform_io, "is_windows", lambda *_a, **_k: True)
    monkeypatch.setattr(
        source_graph_daemon.platform_io, "windows_pid_is_alive", lambda _pid: False
    )
    assert source_graph_daemon._pid_liveness(999_999_999) == "dead"
    monkeypatch.setattr(source_graph_daemon.platform_io, "is_windows", lambda *_a, **_k: False)

    def gone(_pid, _sig):
        raise ProcessLookupError

    monkeypatch.setattr(source_graph_daemon.os, "kill", gone)
    assert source_graph_daemon._pid_liveness(999_999_999) == "dead"


def test_lease_holder_record_lives_only_while_the_lease_is_held(tmp_path):
    root = _init_repo(tmp_path)
    with source_graph.index_write_lease(root) as acquired:
        assert acquired is True
        record = source_graph.read_index_write_lease_holder(root)
        assert record["pid"] == os.getpid()
        assert record["acquired_at"]
    assert source_graph.read_index_write_lease_holder(root) == {}


# ---------------------------------------------------------------------------
# (b) a never-built repository gets its first generation from refresh_now
# ---------------------------------------------------------------------------


def test_refresh_now_builds_first_generation_on_never_built_repo(tmp_path, cleanup_daemons):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    (root / "first.py").write_text("def first():\n    return 1\n", encoding="utf-8")
    assert source_graph_daemon._repo_has_readable_generation(root) is False
    daemon = source_graph_daemon.SourceGraphDaemon(root, standby_wait_seconds=0.5)

    result = daemon.refresh_now()

    assert result["refresh_job"]["state"] == "succeeded"
    assert result["readable_generation"] is True
    assert source_graph_daemon._repo_has_readable_generation(root) is True


def test_never_built_repo_takes_over_when_first_holder_releases(tmp_path, cleanup_daemons):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    (root / "first.py").write_text("def first():\n    return 1\n", encoding="utf-8")
    daemon = source_graph_daemon.SourceGraphDaemon(
        root, standby_wait_seconds=_HANG_GUARD_SECONDS
    )
    held = threading.Event()

    def holder_releases_after_handoff() -> None:
        with source_graph.index_write_lease(root) as acquired:
            assert acquired is True
            held.set()
            deadline = time.monotonic() + _HANG_GUARD_SECONDS
            while not source_graph_daemon._read_refresh_handoff(root):
                if time.monotonic() > deadline:
                    return
                time.sleep(0.02)

    holder = threading.Thread(target=holder_releases_after_handoff)
    holder.start()
    assert held.wait(timeout=_HANG_GUARD_SECONDS)
    try:
        result = daemon.refresh_now()
    finally:
        holder.join(timeout=_HANG_GUARD_SECONDS)

    job = result["refresh_job"]
    assert job["state"] == "succeeded", job
    assert job["error"] != "refresh_terminal_status:standby"
    assert result["readable_generation"] is True


def test_never_built_repo_with_stuck_holder_names_why(tmp_path, cleanup_daemons):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    daemon = source_graph_daemon.SourceGraphDaemon(root, standby_wait_seconds=0.3)
    with source_graph.index_write_lease(root) as acquired:
        assert acquired is True
        result = daemon.refresh_now()
    job = result["refresh_job"]
    assert job["state"] == "failed"
    assert job["error"] != "refresh_terminal_status:standby"
    assert f"holder_pid={os.getpid()}" in job["error"]


# ---------------------------------------------------------------------------
# (c) doctor flags a never-built or stale index as a problem
# ---------------------------------------------------------------------------


def _doctor_entry(payload: dict) -> dict:
    report = doctor.diagnose({"aiworkhub_source_graph_health": lambda: payload})
    return report["probes"][0]


def test_doctor_flags_never_built_index_as_problem(tmp_path):
    root = _init_repo(tmp_path)
    health = source_graph_daemon.daemon_health(root)
    assert health["readable_generation"] is False
    entry = _doctor_entry(health)
    assert entry["state"] == doctor.STATE_PROBLEM
    assert entry["summary"].startswith("source_graph_never_built")


def test_doctor_flags_stale_beyond_threshold_index_as_problem():
    stale = {
        "ok": True,
        "status": "standby",
        "readable_generation": True,
        "index_age_seconds": 12.8 * 3600,
        "stale_after_seconds": 900.0,
    }
    entry = _doctor_entry(stale)
    assert entry["state"] == doctor.STATE_PROBLEM
    assert entry["summary"].startswith("source_graph_stale")
    # An unregistered daemon carries no threshold: the daemon default applies.
    entry = _doctor_entry({**stale, "stale_after_seconds": None})
    assert entry["state"] == doctor.STATE_PROBLEM
    # ok=false without a named error still names the stale generation.
    entry = _doctor_entry({**stale, "ok": False, "stale_reason": "x"})
    assert entry["summary"].startswith("source_graph_stale")


def test_doctor_keeps_fresh_index_and_named_errors_unchanged(tmp_path):
    root = _init_repo(tmp_path)
    (root / "ok.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    source_graph.build_index(root, incremental=False)
    health = source_graph_daemon.daemon_health(root)
    assert health["readable_generation"] is True
    assert _doctor_entry(health)["state"] == doctor.STATE_OK
    corrupt = {"ok": False, "readable_generation": False, "last_error": "source_graph_index_corrupt:x"}
    assert _doctor_entry(corrupt)["summary"] == "source_graph_index_corrupt:x"
    assert _doctor_entry({"ok": True})["state"] == doctor.STATE_OK


# ---------------------------------------------------------------------------
# (d) a stale or unindexed read nudges a coalesced incremental refresh
# ---------------------------------------------------------------------------


class _FakeDaemon:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self.requests = 0

    def is_running(self) -> bool:
        return True

    def request_refresh(self) -> dict:
        self.requests += 1
        return {"ok": True}


def test_stale_read_nudge_is_coalesced_and_bounded(tmp_path, monkeypatch):
    fake = _FakeDaemon()
    monkeypatch.setattr(source_graph_daemon, "get_daemon", lambda _root: fake)
    monkeypatch.setattr(source_graph_daemon, "STALE_READ_NUDGE_COALESCE_SECONDS", 3600.0)
    nudge = source_graph_daemon._nudge_refresh_on_stale_read
    root = tmp_path / "nudged"

    assert nudge(root, "a.py", "stale", "h1") is True
    assert nudge(root, "a.py", "stale", "h1") is False  # same observation
    assert nudge(root, "b.py", "unindexed", "1:2") is False  # inside the window
    assert fake.requests == 1

    monkeypatch.setattr(source_graph_daemon, "STALE_READ_NUDGE_COALESCE_SECONDS", 0.0)
    assert nudge(root, "b.py", "unindexed", "1:2") is True
    assert nudge(root, "b.py", "unindexed", "1:2") is False
    assert nudge(root, "a.py", "stale", "h2") is True  # edited again
    assert fake.requests == 3
    assert len(source_graph_daemon._STALE_READ_NUDGE_SEEN) <= (
        source_graph_daemon._STALE_READ_NUDGE_SEEN_MAX
    )

    fake._stop_event.set()
    assert nudge(root, "c.py", "stale", "h3") is False
    monkeypatch.setattr(source_graph_daemon, "get_daemon", lambda _root: None)
    assert nudge(root, "d.py", "stale", "h4") is False


def test_freshness_reads_notify_registered_listeners(tmp_path):
    root = _init_repo(tmp_path)
    (root / "seen.py").write_text("def seen():\n    return 1\n", encoding="utf-8")
    source_graph.build_index(root, incremental=False)
    events: list[tuple[str, str]] = []

    def listener(_root: Path, file_path: str, state: str, _fingerprint: str) -> None:
        events.append((file_path, state))

    source_graph.register_stale_read_listener(listener)
    try:
        assert source_graph.file_query(root, "seen.py")["freshness"] == "fresh"
        assert events == []
        (root / "seen.py").write_text("def seen():\n    return 2\n", encoding="utf-8")
        edited = source_graph.file_query(root, "seen.py")
        assert edited["freshness"] == source_graph.STALE_SERVED_FROM_DISK
        (root / "brand_new.py").write_text("def brand_new():\n    return 3\n", encoding="utf-8")
        missing = source_graph.file_query(root, "brand_new.py")
        assert missing["reason"] == "not_yet_indexed"
    finally:
        source_graph.unregister_stale_read_listener(listener)
    assert ("seen.py", "stale") in events
    assert ("brand_new.py", "unindexed") in events


def test_new_and_edited_files_are_indexed_without_manual_refresh(
    tmp_path, monkeypatch, cleanup_daemons,
):
    monkeypatch.setattr(source_graph_daemon, "STALE_READ_NUDGE_COALESCE_SECONDS", 0.0)
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    (root / "base.py").write_text("def base():\n    return 1\n", encoding="utf-8")
    daemon = source_graph_daemon.ensure_started(
        root, refresh_interval_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS
    )
    assert daemon.wait_for_first_build(timeout=_HANG_GUARD_SECONDS)

    (root / "created.py").write_text("def created_now():\n    return 1\n", encoding="utf-8")
    first = source_graph.file_query(root, "created.py")
    assert first["reason"] == "not_yet_indexed"
    deadline = time.monotonic() + _HANG_GUARD_SECONDS
    while not source_graph.file_query(root, "created.py").get("matches"):
        assert time.monotonic() < deadline, "new file never indexed after a nudge"
        time.sleep(0.05)

    (root / "created.py").write_text("def created_now():\n    return 2\n", encoding="utf-8")
    assert source_graph.file_query(root, "created.py")["freshness"] == (
        source_graph.STALE_SERVED_FROM_DISK
    )
    deadline = time.monotonic() + _HANG_GUARD_SECONDS
    while source_graph.file_query(root, "created.py").get("freshness") != "fresh":
        assert time.monotonic() < deadline, "edited file never re-indexed after a nudge"
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# An idle lease holder sees a standby handoff within one poll slice
# ---------------------------------------------------------------------------


def test_idle_lease_holder_honors_standby_handoff_without_waiting_an_interval(
    tmp_path, monkeypatch, cleanup_daemons,
):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    (root / "idle.py").write_text("def idle():\n    return 1\n", encoding="utf-8")
    # The holder: default 300 s interval, first build finished, now idle.
    holder = source_graph_daemon.SourceGraphDaemon(root, refresh_interval_seconds=300.0)
    holder.start()
    try:
        assert holder.wait_for_first_build(timeout=_HANG_GUARD_SECONDS)
        builds = {"n": 0}
        real_execute = holder._execute_build

        def counting_execute(*, incremental):
            outcome = real_execute(incremental=incremental)
            if outcome.get("kind") == "success":
                builds["n"] += 1
            return outcome

        holder._execute_build = counting_execute
        # The requester: a second writer on the same repo that lands in
        # STANDBY and sees a live holder, so it must not take the lease over.
        standby_wait = 15.0
        requester = source_graph_daemon.SourceGraphDaemon(
            root, standby_wait_seconds=standby_wait
        )
        requester._execute_build = lambda *, incremental: {"kind": "standby"}
        monkeypatch.setattr(
            source_graph_daemon,
            "_standby_lease_holder",
            lambda _root: {"held": True, "pid": os.getpid(), "liveness": "alive"},
        )
        (root / "after.py").write_text("def after():\n    return 2\n", encoding="utf-8")

        started = time.monotonic()
        result = requester.refresh_now()
        elapsed = time.monotonic() - started

        job = result["refresh_job"]
        assert job["state"] == "succeeded", job
        assert job["standby"]["resolved_by"] == "lease_holder_generation"
        assert elapsed < standby_wait, elapsed
        deadline = time.monotonic() + _HANG_GUARD_SECONDS
        while builds["n"] < 1:
            assert time.monotonic() < deadline, "holder never built for the handoff"
            time.sleep(0.02)
        # A few more poll slices: the honored handoff is not rebuilt again.
        time.sleep(2 * source_graph_daemon._REFRESH_HANDOFF_POLL_SECONDS + 0.5)
        assert builds["n"] == 1
        assert source_graph.file_query(root, "after.py")["matches"]
    finally:
        holder.stop()


def test_writer_wait_ends_within_one_slice_on_stop_and_ignores_missing_handoff(tmp_path):
    root = _init_repo(tmp_path)
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    daemon._last_build_started_at = datetime.now(timezone.utc).isoformat()
    # No handoff record at all: never a handoff, never an error.
    assert daemon._refresh_handoff_is_newer() is False
    # A handoff older than the last build start does not trigger.
    older = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    assert source_graph_daemon._publish_refresh_handoff(root, older)
    assert daemon._refresh_handoff_is_newer() is False
    # A newer one triggers exactly once per record identity.
    newer = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    assert source_graph_daemon._publish_refresh_handoff(root, newer)
    assert daemon._refresh_handoff_is_newer() is True
    assert daemon._refresh_handoff_is_newer() is False
    # Stop ends a long wait within about one slice.
    timer = threading.Timer(0.1, daemon._stop_event.set)
    timer.start()
    started = time.monotonic()
    daemon._wait_for_tick(_HANG_GUARD_SECONDS)
    timer.join()
    assert time.monotonic() - started < _HANG_GUARD_SECONDS / 2
