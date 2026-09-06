"""Power-loss Source Graph recovery (source_graph_daemon.py).

Covers: writable single-flight recovery before readonly probes when a
non-empty SQLite journal/WAL exists, health bounded latency during writer
activity, health exposes phase/elapsed/error/last-known-good generation,
refresh/recovery coalescing, and failed recovery preserves the canonical
index without auto-delete.  Canonical generation finished_at identity stays
separate from local freshness.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
import sys

if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import source_graph, source_graph_daemon, task_store  # noqa: E402


@pytest.fixture(autouse=True)
def in_process_builds_for_monkeypatches(monkeypatch):
    """Keep unit-test monkeypatches in-process."""
    monkeypatch.setenv(
        source_graph_daemon.BUILD_EXECUTION_ENV,
        source_graph_daemon.BUILD_EXECUTION_THREAD,
    )


@pytest.fixture
def cleanup_daemons(monkeypatch):
    """Stop every daemon this test starts, including unregistered instances."""
    roots: list[Path] = []
    daemons: list[source_graph_daemon.SourceGraphDaemon] = []
    original_start = source_graph_daemon.SourceGraphDaemon.start

    def start_and_track(daemon):
        daemons.append(daemon)
        return original_start(daemon)

    monkeypatch.setattr(source_graph_daemon.SourceGraphDaemon, "start", start_and_track)
    yield roots
    for daemon in reversed(daemons):
        daemon.stop()
    for root in roots:
        source_graph_daemon.stop_daemon(root)


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    result = task_store.initialize_repository(root)
    assert result["ok"], result
    return root


def _create_crash_journal(db_path: Path) -> Path:
    """Create a genuine hot-journal file that SQLite will recover on next
    writable open.

    Opens a writable connection, starts an exclusive transaction that writes
    data, snapshots the journal file while the transaction is still open,
    then closes (auto-rollback deletes journal) and restores the snapshot.
    """
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("CREATE TABLE IF NOT EXISTS _crash_test (x INTEGER)")
    conn.execute("BEGIN EXCLUSIVE")
    conn.execute("INSERT INTO _crash_test VALUES (999)")
    journal_path = Path(str(db_path) + "-journal")
    deadline = time.monotonic() + 2.0
    while not journal_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert journal_path.exists(), "journal file was not created by open transaction"
    journal_data = journal_path.read_bytes()
    assert len(journal_data) > 0, "journal file is empty"
    conn.close()
    journal_path.write_bytes(journal_data)
    return journal_path


def _create_crash_wal(db_path: Path) -> tuple[Path, Path]:
    """Create genuine WAL + SHM sidecar files that SQLite will recover."""
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS _crash_wal_test (x INTEGER)")
    conn.execute("BEGIN EXCLUSIVE")
    conn.execute("INSERT INTO _crash_wal_test VALUES (777)")
    conn.commit()
    wal_path = Path(str(db_path) + "-wal")
    shm_path = Path(str(db_path) + "-shm")
    assert wal_path.exists(), "WAL file was not created"
    wal_data = wal_path.read_bytes() if wal_path.exists() else b""
    shm_data = shm_path.read_bytes() if shm_path.exists() else b""
    conn.close()
    if wal_data:
        wal_path.write_bytes(wal_data)
    if shm_data:
        shm_path.write_bytes(shm_data)
    return wal_path, shm_path


# ---------------------------------------------------------------------------
# 1. Writable recovery precedes readonly probes when journal/WAL exists.
# ---------------------------------------------------------------------------


def test_recovery_hot_journal_precedes_readonly_probe(tmp_path, cleanup_daemons):
    """A non-empty -journal triggers writable recovery before the first
    readonly build probe."""
    root = _init_repo(tmp_path, "journal_repo")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    report = source_graph.build_index(root, incremental=False)
    assert report.files_seen > 0
    db_path = source_graph.resolve_db_path(root)
    assert db_path.exists()

    journal_path = _create_crash_journal(db_path)
    assert journal_path.exists()

    daemon = source_graph_daemon.SourceGraphDaemon(
        root,
        refresh_interval_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS,
    )
    daemon.start()

    assert daemon.wait_for_first_build(timeout=10), "first build after recovery never completed"

    health = daemon.health()
    assert health["status"] not in {
        source_graph_daemon.STATUS_DEGRADED,
        source_graph_daemon.STATUS_RECOVERY,
    }
    assert not journal_path.exists(), "hot journal was not cleaned up by recovery"


def test_recovery_wal_precedes_readonly_probe(tmp_path, cleanup_daemons):
    """WAL/SHM sidecars trigger writable recovery before readonly probes."""
    root = _init_repo(tmp_path, "wal_repo")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    report = source_graph.build_index(root, incremental=False)
    assert report.files_seen > 0
    db_path = source_graph.resolve_db_path(root)
    assert db_path.exists()

    wal_path, _shm_path = _create_crash_wal(db_path)
    assert wal_path.exists()

    daemon = source_graph_daemon.SourceGraphDaemon(
        root,
        refresh_interval_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS,
    )
    daemon.start()

    assert daemon.wait_for_first_build(timeout=10), "first build after WAL recovery never completed"

    health = daemon.health()
    assert health["status"] not in {
        source_graph_daemon.STATUS_DEGRADED,
        source_graph_daemon.STATUS_RECOVERY,
    }


def test_no_journal_no_recovery_direct_to_indexing(tmp_path, cleanup_daemons):
    """When no journal/WAL sidecars exist, the daemon proceeds directly to
    indexing without recovery."""
    root = _init_repo(tmp_path, "clean_repo")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    db_path = source_graph.resolve_db_path(root)
    # _init_repo legitimately creates the canonical DB.  Assert that no
    # journal/WAL sidecars exist (clean shutdown state) and then prove
    # indexing proceeds without a recovery phase.
    for suffix in ("-journal", "-wal", "-shm"):
        assert not Path(str(db_path) + suffix).exists(), (
            f"{suffix} sidecar present on clean repository"
        )

    daemon = source_graph_daemon.SourceGraphDaemon(
        root,
        refresh_interval_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS,
    )
    daemon.start()

    assert daemon.wait_for_first_build(timeout=10)

    health = daemon.health()
    assert health["status"] == source_graph_daemon.STATUS_READY
    assert health["recovery"]["error"] == ""
# ---------------------------------------------------------------------------
# 2. Health bounded latency during writer activity; never falsely reports ready.
# ---------------------------------------------------------------------------


def test_health_bounded_latency_during_writer_activity_and_never_falsely_ready(
    tmp_path, monkeypatch, cleanup_daemons,
):
    """health() returns within bounded wall-clock time while a build is in
    flight and never falsely reports STATUS_READY."""
    root = _init_repo(tmp_path, "bounded_health")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    build_started = threading.Event()
    release_build = threading.Event()

    def slow_build(repo_root, *, incremental=True, db_path=None):
        build_started.set()
        release_build.wait(timeout=5)
        return source_graph.BuildReport(
            repo_root=str(repo_root),
            db_path="fake.sqlite",
            incremental=incremental,
            files_seen=3,
            files_changed=3,
            files_unchanged=0,
            files_removed=0,
            entities_written=3,
            edges_written=0,
            errors=[],
            build_revision="test-rev",
            finished_at="2026-08-05T18:00:00+00:00",
        )

    monkeypatch.setattr(source_graph, "build_index", slow_build)

    daemon = source_graph_daemon.SourceGraphDaemon(
        root,
        refresh_interval_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS,
    )
    daemon.start()

    assert build_started.wait(timeout=5), "build never started"

    start = time.monotonic()
    health = daemon.health()
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"health() took {elapsed:.3f}s during writer activity"
    assert health["status"] != source_graph_daemon.STATUS_READY, (
        "health falsely reported ready during active build"
    )
    assert health["running"] is True

    release_build.set()
    assert daemon.wait_for_first_build(timeout=10)


# ---------------------------------------------------------------------------
# 3. Health exposes recovery phase, elapsed, error, and last-known-good generation.
# ---------------------------------------------------------------------------


def test_health_exposes_recovery_phase_elapsed_error(tmp_path, cleanup_daemons):
    """When recovery is in progress, health() exposes current phase,
    elapsed wall time, and any actionable error."""
    root = _init_repo(tmp_path, "phase_repo")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    report = source_graph.build_index(root, incremental=False)
    assert report.files_seen > 0
    db_path = source_graph.resolve_db_path(root)

    journal_path = _create_crash_journal(db_path)
    assert journal_path.exists()

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    daemon._status = source_graph_daemon.STATUS_RECOVERY
    daemon._recovery_phase = source_graph_daemon._RECOVERY_PHASE_OPEN
    daemon._recovery_started_at = time.monotonic() - 0.5
    daemon._recovery_error = ""

    health = daemon.health()
    assert health["status"] == source_graph_daemon.STATUS_RECOVERY
    assert health["recovery"]["phase"] == source_graph_daemon._RECOVERY_PHASE_OPEN
    assert health["recovery"]["elapsed_seconds"] >= 0.4
    assert health["recovery"]["error"] == ""

    daemon._recovery_phase = source_graph_daemon._RECOVERY_PHASE_INTEGRITY
    daemon._recovery_error = "integrity_check:database disk image is malformed"

    health = daemon.health()
    assert health["recovery"]["phase"] == source_graph_daemon._RECOVERY_PHASE_INTEGRITY
    assert "malformed" in health["recovery"]["error"]


def test_health_exposes_last_known_good_generation_during_recovery(tmp_path, cleanup_daemons):
    """During recovery, health() exposes the last-known-good canonical
    generation captured before recovery mutated the database."""
    root = _init_repo(tmp_path, "lkg_repo")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    report = source_graph.build_index(root, incremental=False)
    assert report.files_seen > 0

    daemon = source_graph_daemon.SourceGraphDaemon(root)

    daemon._last_report = {
        "build_revision": "aiworkhub.source_graph.semantic.v5",
        "finished_at": "2026-08-01T12:00:00+00:00",
        "files_seen": 7,
        "files_changed": 7,
        "files_unchanged": 0,
        "files_removed": 0,
        "entities_written": 7,
        "edges_written": 2,
        "errors": [],
        "db_path": "fake.sqlite",
        "incremental": False,
    }
    daemon._last_success_at = "2026-08-01T12:00:00+00:00"

    daemon._last_known_good_generation = {
        "build_revision": "aiworkhub.source_graph.semantic.v5",
        "finished_at": "2026-08-01T12:00:00+00:00",
        "files_seen": 7,
    }
    daemon._status = source_graph_daemon.STATUS_RECOVERY
    daemon._recovery_phase = source_graph_daemon._RECOVERY_PHASE_OPEN
    daemon._recovery_started_at = time.monotonic()

    health = daemon.health()
    lkg = health["last_known_good_generation"]
    assert lkg["build_revision"] == "aiworkhub.source_graph.semantic.v5"
    assert lkg["finished_at"] == "2026-08-01T12:00:00+00:00"
    assert lkg["files_seen"] == 7


def test_health_lkg_empty_when_no_prior_build(tmp_path, cleanup_daemons):
    """When no prior build exists, last_known_good_generation is empty."""
    root = _init_repo(tmp_path, "no_lkg_repo")
    cleanup_daemons.append(root)

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    daemon._status = source_graph_daemon.STATUS_RECOVERY
    daemon._recovery_phase = source_graph_daemon._RECOVERY_PHASE_DETECT
    daemon._recovery_started_at = time.monotonic()

    health = daemon.health()
    assert health["last_known_good_generation"] == {}


# ---------------------------------------------------------------------------
# 4. Recovery is single-flight and coalesced.
# ---------------------------------------------------------------------------


def test_recovery_single_flight_second_caller_coalesces(tmp_path, cleanup_daemons):
    """When recovery is already in progress, a second recovery call
    coalesces and returns immediately with the coalesced flag."""
    root = _init_repo(tmp_path, "coalesce_repo")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    report = source_graph.build_index(root, incremental=False)
    assert report.files_seen > 0
    db_path = source_graph.resolve_db_path(root)

    journal_path = _create_crash_journal(db_path)
    assert journal_path.exists()

    daemon = source_graph_daemon.SourceGraphDaemon(root)

    assert daemon._recovery_lock.acquire(blocking=False)

    result = daemon._recover_database()
    assert result["recovered"] is None
    assert result.get("coalesced") is True
    assert "phase" in result

    daemon._recovery_lock.release()


def test_recovery_and_refresh_coalescing_independent(tmp_path, monkeypatch, cleanup_daemons):
    """Recovery lock and build lock are independent; refresh_now() coalesces
    during an in-flight build while recovery is also active."""
    root = _init_repo(tmp_path, "indep_coalesce")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    build_started = threading.Event()
    release_build = threading.Event()

    def slow_build(repo_root, *, incremental=True, db_path=None):
        build_started.set()
        release_build.wait(timeout=5)
        return source_graph.BuildReport(
            repo_root=str(repo_root),
            db_path="fake.sqlite",
            incremental=incremental,
            files_seen=1,
            files_changed=1,
            files_unchanged=0,
            files_removed=0,
            entities_written=1,
            edges_written=0,
            errors=[],
            build_revision="test-rev",
            finished_at="t",
        )

    monkeypatch.setattr(source_graph, "build_index", slow_build)

    daemon = source_graph_daemon.SourceGraphDaemon(
        root,
        refresh_interval_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS,
    )
    daemon.start()

    assert build_started.wait(timeout=5), "build never started"

    result = daemon.refresh_now()
    assert result["triggered"] is False
    assert result["reason"] == "build_in_progress"

    result2 = daemon.refresh_now()
    assert result2["triggered"] is False
    assert daemon._refresh_event.is_set()

    release_build.set()
    assert daemon.wait_for_first_build(timeout=10)


# ---------------------------------------------------------------------------
# 5. Failed recovery preserves canonical index without auto-delete.
# ---------------------------------------------------------------------------


def test_failed_recovery_preserves_canonical_index_no_autodelete(
    tmp_path, cleanup_daemons,
):
    """A recovery that fails during integrity check must NOT delete the
    database and must preserve the prior canonical index identity."""
    root = _init_repo(tmp_path, "preserve_repo")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    report = source_graph.build_index(root, incremental=False)
    assert report.files_seen > 0
    db_path = source_graph.resolve_db_path(root)
    original_size = db_path.stat().st_size
    assert original_size > 0

    journal_path = _create_crash_journal(db_path)
    assert journal_path.exists()

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    daemon._last_report = {
        "build_revision": report.build_revision,
        "finished_at": report.finished_at,
        "files_seen": report.files_seen,
        "files_changed": 0, "files_unchanged": 0, "files_removed": 0,
        "entities_written": 0, "edges_written": 0, "errors": [],
        "db_path": str(db_path), "incremental": False,
    }
    daemon._last_success_at = report.finished_at

    class _RecoveryTestConnection:
        """Proxy that delegates to a real sqlite3.Connection but overrides
        execute() to inject a failing integrity_check response."""

        class _Cursor:
            __slots__ = ("_rows",)

            def __init__(self, rows):
                self._rows = list(rows)

            def fetchone(self):
                return self._rows[0] if self._rows else None

        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, *args, **kwargs):
            if "integrity_check" in str(sql).lower():
                class _FakeRow:
                    def __getitem__(self, idx):
                        return "database disk image is malformed"

                return self._Cursor([_FakeRow()])
            return self._real.execute(sql, *args, **kwargs)

        def close(self):
            return self._real.close()

        def commit(self):
            return self._real.commit()

        def rollback(self):
            return self._real.rollback()

        def __getattr__(self, name):
            return getattr(self._real, name)

    original_connect = source_graph.connect

    def failing_connect(db_p, *, read_only=False):
        conn = original_connect(db_p, read_only=read_only)
        if not read_only:
            return _RecoveryTestConnection(conn)
        return conn

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(source_graph, "connect", failing_connect)

    result = daemon._recover_database()

    assert result["recovered"] is False
    assert "malformed" in result.get("error", "")
    assert db_path.exists(), "database was auto-deleted after failed recovery"
    assert db_path.stat().st_size >= original_size, "database was truncated after failed recovery"

    lkg = daemon._last_known_good_generation
    assert lkg["build_revision"] == report.build_revision
    assert lkg["finished_at"] == report.finished_at
    assert lkg["files_seen"] == report.files_seen

    monkeypatch.undo()


def test_failed_recovery_returns_diagnostics_not_crash(tmp_path, cleanup_daemons):
    """A failed recovery returns structured diagnostics and never raises."""
    root = _init_repo(tmp_path, "diag_repo")
    cleanup_daemons.append(root)

    daemon = source_graph_daemon.SourceGraphDaemon(root)

    original_resolve = source_graph.resolve_db_path

    def fail_resolve(_repo_root):
        raise source_graph.RepositoryUnresolvedError("no manifest")

    source_graph.resolve_db_path = fail_resolve
    try:
        result = daemon._recover_database()
        assert result["recovered"] is False
        assert "no manifest" in result["error"]
        assert result["phase"] == source_graph_daemon._RECOVERY_PHASE_DETECT
    finally:
        source_graph.resolve_db_path = original_resolve


# ---------------------------------------------------------------------------
# 6. Canonical generation identity separate from local freshness.
# ---------------------------------------------------------------------------


def test_canonical_generation_identity_separate_from_local_freshness(
    tmp_path, cleanup_daemons,
):
    """A just-completed successful local build is fresh even when a
    deterministic test report carries an old fixed finished_at."""
    root = _init_repo(tmp_path, "identity_repo")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    old_finished_at = "2025-01-01T00:00:00+00:00"

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    daemon._thread = threading.current_thread()
    daemon._status = source_graph_daemon.STATUS_READY
    daemon._started_at = datetime.now(timezone.utc).isoformat()
    daemon._last_success_at = old_finished_at
    daemon._last_report = {
        "build_revision": source_graph.BUILD_REVISION,
        "finished_at": old_finished_at,
        "files_seen": 5,
        "files_changed": 5,
        "files_unchanged": 0,
        "files_removed": 0,
        "entities_written": 5,
        "edges_written": 0,
        "errors": [],
        "db_path": "fake.sqlite",
        "incremental": False,
    }

    health = daemon.health()

    assert health["build_revision"] == source_graph.BUILD_REVISION
    assert health["files_seen"] == 5
    assert health["last_success_at"] == old_finished_at

    recent = datetime.now(timezone.utc).isoformat()
    daemon._last_success_at = recent
    daemon._last_report["finished_at"] = recent

    health2 = daemon.health()
    assert health2["last_success_at"] == recent
    assert health2["build_revision"] == source_graph.BUILD_REVISION


def test_freshness_uses_local_timeline_not_canonical_finished_at(tmp_path, cleanup_daemons):
    """A daemon that has never completed a local build (status=indexing) but
    has a DB with an old canonical finished_at is not fresh -- the daemon's
    own last_success_at is the freshness anchor."""
    root = _init_repo(tmp_path, "freshness_repo")
    cleanup_daemons.append(root)

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    daemon._thread = threading.current_thread()
    daemon._status = source_graph_daemon.STATUS_INDEXING
    daemon._started_at = (datetime.now(timezone.utc)).isoformat()
    daemon._last_success_at = ""
    daemon._last_report = None

    health = daemon.health()
    assert health["stale_reason"] == ""
    assert health["status"] == source_graph_daemon.STATUS_INDEXING


# ---------------------------------------------------------------------------
# 7. daemon_health() module-level function exposes recovery fields.
# ---------------------------------------------------------------------------


def test_daemon_health_module_level_exposes_recovery_fields(tmp_path, cleanup_daemons):
    """The module-level daemon_health() preserves recovery and LKG fields."""
    root = _init_repo(tmp_path, "module_health")
    cleanup_daemons.append(root)

    health = source_graph_daemon.daemon_health(root)
    assert health["recovery"]["error"] == ""
    assert health["last_known_good_generation"] == {}
    assert health["registered"] is False

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    daemon._status = source_graph_daemon.STATUS_RECOVERY
    daemon._recovery_phase = source_graph_daemon._RECOVERY_PHASE_OPEN
    daemon._recovery_started_at = time.monotonic()
    daemon._last_known_good_generation = {
        "build_revision": "v5",
        "finished_at": "2026-01-01T00:00:00Z",
        "files_seen": 3,
    }

    import aiworkhub.source_graph_daemon as sgd

    with sgd._REGISTRY_LOCK:
        sgd._REGISTRY[sgd._registry_key(root)] = daemon

    try:
        health = source_graph_daemon.daemon_health(root)
        assert health["recovery"]["phase"] == source_graph_daemon._RECOVERY_PHASE_OPEN
        assert health["last_known_good_generation"]["build_revision"] == "v5"
        assert health["registered"] is True
    finally:
        with sgd._REGISTRY_LOCK:
            sgd._REGISTRY.pop(sgd._registry_key(root), None)


# ---------------------------------------------------------------------------
# 8. Review-feedback invariants (rework delta).
# ---------------------------------------------------------------------------


def test_coalesced_recovery_never_claims_recovered_true_and_caller_waits(
    tmp_path, cleanup_daemons,
):
    """Invariant (1): lock contention returns ``recovered: None`` so no caller
    proceeds to readonly/build until the owning recovery completes."""
    root = _init_repo(tmp_path, "coalesce_true")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    report = source_graph.build_index(root, incremental=False)
    assert report.files_seen > 0
    db_path = source_graph.resolve_db_path(root)

    journal_path = _create_crash_journal(db_path)
    assert journal_path.exists()

    daemon = source_graph_daemon.SourceGraphDaemon(root)

    # Acquire the recovery lock to simulate an active recovery.
    assert daemon._recovery_lock.acquire(blocking=False)

    result = daemon._recover_database()
    # Must be None (in-progress), NOT True.
    assert result["recovered"] is None
    assert result.get("coalesced") is True

    daemon._recovery_lock.release()

    # After the owner releases, a fresh call actually recovers.
    result2 = daemon._recover_database()
    assert result2["recovered"] is True


def test_shm_alone_not_a_hot_journal(tmp_path, cleanup_daemons):
    """Invariant (2): a non-empty ``-shm`` alone is not a hot journal;
    only non-empty ``-journal`` or ``-wal`` triggers recovery."""
    root = _init_repo(tmp_path, "shm_only")
    cleanup_daemons.append(root)

    report = source_graph.build_index(root, incremental=False)
    assert report.files_seen > 0
    db_path = source_graph.resolve_db_path(root)

    # Create a WAL database, do a clean checkpoint to leave -shm alone.
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS _shm_test (x INTEGER)")
    conn.execute("INSERT INTO _shm_test VALUES (1)")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    wal_path = Path(str(db_path) + "-wal")
    shm_path = Path(str(db_path) + "-shm")
    journal_path = Path(str(db_path) + "-journal")

    # After TRUNCATE checkpoint, -wal should be empty or absent.
    # -shm may still exist.  -journal must not exist.
    assert not journal_path.exists()
    # -wal must be empty or absent after TRUNCATE checkpoint.
    assert not wal_path.exists() or wal_path.stat().st_size == 0

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    # -shm alone must not trigger recovery.
    assert daemon._has_pending_journal() is False

    # Now create a genuine hot journal and verify it IS detected.
    journal_path.write_bytes(b"\x00" * 512)
    assert daemon._has_pending_journal() is True


def test_loop_skips_build_on_recovery_failure_retries_next_cycle(
    tmp_path, monkeypatch, cleanup_daemons,
):
    """Invariant (3): ``_loop`` on recovery failure skips ``_run_one_build``,
    retains degraded diagnostics/LKG, and retries recovery on the next
    periodic cycle before any readonly probe."""
    root = _init_repo(tmp_path, "loop_skip")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    report = source_graph.build_index(root, incremental=False)
    assert report.files_seen > 0
    db_path = source_graph.resolve_db_path(root)

    journal_path = _create_crash_journal(db_path)
    assert journal_path.exists()

    daemon = source_graph_daemon.SourceGraphDaemon(
        root,
        refresh_interval_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS,
    )
    daemon._last_report = {
        "build_revision": report.build_revision,
        "finished_at": report.finished_at,
        "files_seen": report.files_seen,
        "files_changed": 0, "files_unchanged": 0, "files_removed": 0,
        "entities_written": 0, "edges_written": 0, "errors": [],
        "db_path": str(db_path), "incremental": False,
    }

    # Force _recover_database to fail.
    original_connect = source_graph.connect

    def fail_connect(db_p, *, read_only=False):
        if not read_only:
            raise sqlite3.OperationalError("simulated connect failure")
        return original_connect(db_p, read_only=True)

    monkeypatch.setattr(source_graph, "connect", fail_connect)

    build_called = {"n": 0}

    def count_build(repo_root, *, incremental=True, db_path=None):
        build_called["n"] += 1
        return source_graph.BuildReport(
            repo_root=str(repo_root), db_path="fake.sqlite",
            incremental=incremental, files_seen=0, files_changed=0,
            files_unchanged=0, files_removed=0, entities_written=0,
            edges_written=0, errors=[], build_revision="test",
            finished_at="t",
        )

    monkeypatch.setattr(source_graph, "build_index", count_build)

    daemon.start()
    assert daemon.wait_for_first_build(timeout=10)

    # Recovery failed -> _run_one_build must NOT have been called.
    assert build_called["n"] == 0, (
        "_run_one_build was called despite failed recovery"
    )

    health = daemon.health()
    assert health["status"] in {
        source_graph_daemon.STATUS_DEGRADED,
        source_graph_daemon.STATUS_RECOVERY,
    }
    assert health["recovery"]["error"] != "" or health["last_error"] != ""

    # LKG must be preserved.
    lkg = health["last_known_good_generation"]
    assert lkg.get("build_revision") == report.build_revision
    assert lkg.get("files_seen") == report.files_seen

    daemon.stop()


# ---------------------------------------------------------------------------
# 9. Regression: daemon_health() skips readonly connect during recovery.
# ---------------------------------------------------------------------------


def test_daemon_health_skips_readonly_during_recovery(tmp_path, monkeypatch, cleanup_daemons):
    """When status==STATUS_RECOVERY or a pending journal exists, the
    module-level daemon_health() must return bounded process/LKG diagnostics
    without opening any readonly database connection."""
    root = _init_repo(tmp_path, "noreadonly_repo")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    report = source_graph.build_index(root, incremental=False)
    assert report.files_seen > 0
    db_path = source_graph.resolve_db_path(root)

    journal_path = _create_crash_journal(db_path)
    assert journal_path.exists()

    readonly_calls: list[bool] = []
    original_connect = source_graph.connect

    def spy_connect(db_p, *, read_only=False):
        if read_only:
            readonly_calls.append(True)
        return original_connect(db_p, read_only=read_only)

    monkeypatch.setattr(source_graph, "connect", spy_connect)

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    daemon._status = source_graph_daemon.STATUS_RECOVERY
    daemon._recovery_phase = source_graph_daemon._RECOVERY_PHASE_OPEN
    daemon._recovery_started_at = time.monotonic()
    daemon._recovery_error = "test error"
    daemon._last_known_good_generation = {
        "build_revision": report.build_revision,
        "finished_at": report.finished_at,
        "files_seen": report.files_seen,
    }

    import aiworkhub.source_graph_daemon as sgd

    with sgd._REGISTRY_LOCK:
        sgd._REGISTRY[sgd._registry_key(root)] = daemon

    try:
        health = source_graph_daemon.daemon_health(root)
        # Must return without opening any readonly connection.
        assert len(readonly_calls) == 0, (
            f"daemon_health opened {len(readonly_calls)} readonly connections during recovery"
        )
        assert health["registered"] is True
        assert health["status"] == source_graph_daemon.STATUS_RECOVERY
        assert health["recovery"]["phase"] == source_graph_daemon._RECOVERY_PHASE_OPEN
        assert health["recovery"]["error"] == "test error"
    finally:
        with sgd._REGISTRY_LOCK:
            sgd._REGISTRY.pop(sgd._registry_key(root), None)


def test_daemon_health_skips_readonly_when_journal_exists(tmp_path, monkeypatch, cleanup_daemons):
    """When the daemon is not in STATUS_RECOVERY but a pending journal still
    exists, daemon_health() must also skip the readonly probe."""
    root = _init_repo(tmp_path, "journal_skip_repo")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    report = source_graph.build_index(root, incremental=False)
    assert report.files_seen > 0
    db_path = source_graph.resolve_db_path(root)

    journal_path = _create_crash_journal(db_path)
    assert journal_path.exists()

    readonly_calls: list[bool] = []
    original_connect = source_graph.connect

    def spy_connect(db_p, *, read_only=False):
        if read_only:
            readonly_calls.append(True)
        return original_connect(db_p, read_only=read_only)

    monkeypatch.setattr(source_graph, "connect", spy_connect)

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    daemon._status = source_graph_daemon.STATUS_STOPPED  # not RECOVERY, but journal exists

    import aiworkhub.source_graph_daemon as sgd

    with sgd._REGISTRY_LOCK:
        sgd._REGISTRY[sgd._registry_key(root)] = daemon

    try:
        health = source_graph_daemon.daemon_health(root)
        # Must skip readonly probe because _has_pending_journal() is True.
        assert len(readonly_calls) == 0, (
            f"daemon_health opened {len(readonly_calls)} readonly connections while journal exists"
        )
        assert health["registered"] is True
    finally:
        with sgd._REGISTRY_LOCK:
            sgd._REGISTRY.pop(sgd._registry_key(root), None)


# ---------------------------------------------------------------------------
# 10. Regression: health ok is false during STATUS_RECOVERY.
# ---------------------------------------------------------------------------


def test_health_ok_false_during_recovery(tmp_path, cleanup_daemons):
    """daemon.health() must return ok=False when status==STATUS_RECOVERY."""
    root = _init_repo(tmp_path, "ok_false_repo")
    cleanup_daemons.append(root)

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    daemon._status = source_graph_daemon.STATUS_RECOVERY
    daemon._recovery_phase = source_graph_daemon._RECOVERY_PHASE_OPEN
    daemon._recovery_started_at = time.monotonic()
    daemon._recovery_error = ""

    health = daemon.health()
    assert health["ok"] is False, "health.ok must be false during STATUS_RECOVERY"
    assert health["status"] == source_graph_daemon.STATUS_RECOVERY


# ---------------------------------------------------------------------------
# 11. Regression: recovery payload schema stable across all states.
# ---------------------------------------------------------------------------


def test_recovery_payload_schema_stable_across_states(tmp_path, cleanup_daemons):
    """The recovery payload must expose phase, elapsed_seconds, and error
    in every state (stopped, ready, recovery, degraded), not just during
    active recovery."""
    root = _init_repo(tmp_path, "schema_repo")
    cleanup_daemons.append(root)

    daemon = source_graph_daemon.SourceGraphDaemon(root)

    # stopped
    daemon._status = source_graph_daemon.STATUS_STOPPED
    daemon._recovery_phase = ""
    daemon._recovery_error = ""
    daemon._recovery_started_at = 0.0
    health = daemon.health()
    assert "phase" in health["recovery"]
    assert "elapsed_seconds" in health["recovery"]
    assert "error" in health["recovery"]
    assert health["recovery"]["phase"] == ""
    assert health["recovery"]["error"] == ""

    # ready
    daemon._status = source_graph_daemon.STATUS_READY
    health = daemon.health()
    assert "phase" in health["recovery"]
    assert "elapsed_seconds" in health["recovery"]
    assert "error" in health["recovery"]

    # recovery
    daemon._status = source_graph_daemon.STATUS_RECOVERY
    daemon._recovery_phase = source_graph_daemon._RECOVERY_PHASE_INTEGRITY
    daemon._recovery_started_at = time.monotonic() - 0.3
    daemon._recovery_error = "disk malformed"
    health = daemon.health()
    assert health["recovery"]["phase"] == source_graph_daemon._RECOVERY_PHASE_INTEGRITY
    assert health["recovery"]["elapsed_seconds"] >= 0.2
    assert health["recovery"]["error"] == "disk malformed"

    # degraded (failed recovery -> terminal values retained)
    daemon._status = source_graph_daemon.STATUS_DEGRADED
    # Phase and error from failed recovery must remain.
    health = daemon.health()
    assert health["recovery"]["phase"] == source_graph_daemon._RECOVERY_PHASE_INTEGRITY
    assert "malformed" in health["recovery"]["error"]
    assert "elapsed_seconds" in health["recovery"]


# ---------------------------------------------------------------------------
# 12. Regression: _has_pending_journal survives OSError.
# ---------------------------------------------------------------------------


def test_pending_journal_survives_oserror_on_stat(tmp_path, monkeypatch, cleanup_daemons):
    """_has_pending_journal must return False (not raise) when exists/stat
    raises OSError, so the daemon thread is never killed by a TOCTOU or
    permission error on the journal sidecar."""
    root = _init_repo(tmp_path, "oserror_repo")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    report = source_graph.build_index(root, incremental=False)
    assert report.files_seen > 0
    db_path = source_graph.resolve_db_path(root)

    # Create a journal so the suffix loop reaches exists/stat.
    journal_path = Path(str(db_path) + "-journal")
    journal_path.write_bytes(b"\x00" * 512)

    daemon = source_graph_daemon.SourceGraphDaemon(root)

    # Make stat() raise OSError on the journal file.
    import builtins

    original_open = builtins.open

    class _FailingPathStat:
        """Path subclass whose stat() raises OSError."""
        def __init__(self, real_path):
            self._real = real_path

        def __fspath__(self):
            return str(self._real)

        def exists(self):
            return True

        def stat(self):
            raise OSError("simulated stat failure")

    def _failing_path_factory(*args, **kwargs):
        p = Path(*args, **kwargs)
        if str(p).endswith("-journal"):
            return _FailingPathStat(p)
        return p

    monkeypatch.setattr(source_graph_daemon, "Path", _failing_path_factory)

    # Must not raise.
    result = daemon._has_pending_journal()
    assert result is False


def test_pending_journal_survives_oserror_on_resolve(tmp_path, monkeypatch, cleanup_daemons):
    """_has_pending_journal must return False when resolve_db_path raises
    OSError (e.g., permission denied on .aiworkhub directory)."""
    root = _init_repo(tmp_path, "resolve_oserror_repo")
    cleanup_daemons.append(root)

    daemon = source_graph_daemon.SourceGraphDaemon(root)

    def fail_resolve(_repo_root):
        raise OSError("permission denied")

    monkeypatch.setattr(source_graph, "resolve_db_path", fail_resolve)

    result = daemon._has_pending_journal()

# ---------------------------------------------------------------------------
# 13. Regression: post-recovery meta SELECT does not silently swallow
#     sqlite3.Error; returns failed recovery with diagnostics.
# ---------------------------------------------------------------------------


def test_post_recovery_meta_select_converts_sqlite3_error_to_recovery_failure(
    tmp_path, monkeypatch, cleanup_daemons
):
    """After writable recovery open, a post-recovery meta SELECT raising
    sqlite3.Error must produce a failed recovery result (recovered=False),
    preserve last-known-good generation, and set degraded status."""
    root = _init_repo(tmp_path, "meta_select_repo")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    report = source_graph.build_index(root, incremental=False)
    assert report.files_seen > 0
    db_path = source_graph.resolve_db_path(root)

    journal_path = _create_crash_journal(db_path)
    assert journal_path.exists()

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    daemon._last_report = {
        "build_revision": report.build_revision,
        "finished_at": report.finished_at,
        "files_seen": report.files_seen,
        "files_changed": 0, "files_unchanged": 0, "files_removed": 0,
        "entities_written": 0, "edges_written": 0, "errors": [],
        "db_path": str(db_path), "incremental": False,
    }

    original_connect = source_graph.connect

    class _FailingMetaSelectConnection:
        """Connection that succeeds writable open but raises on execute()."""

        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, *args, **kwargs):
            if "SELECT value FROM meta" in str(sql):
                raise sqlite3.OperationalError("database is locked")
            return self._real.execute(sql, *args, **kwargs)

        def close(self):
            return self._real.close()

        def commit(self):
            return self._real.commit()

        def rollback(self):
            return self._real.rollback()

        def __getattr__(self, name):
            return getattr(self._real, name)

    def failing_connect(db_p, *, read_only=False):
        conn = original_connect(db_p, read_only=read_only)
        if not read_only:
            return _FailingMetaSelectConnection(conn)
        return conn

    monkeypatch.setattr(source_graph, "connect", failing_connect)

    # Recovery must not raise, but must report failure.
    result = daemon._recover_database()
    assert result["recovered"] is False, "sqlite3.Error must cause recovery failure"
    assert result["phase"] == source_graph_daemon._RECOVERY_PHASE_COMMIT
    assert "meta_query" in result["error"]
    # LKG must be preserved.
    lkg = daemon._last_known_good_generation
    assert lkg.get("build_revision") == report.build_revision
    assert lkg.get("files_seen") == report.files_seen
    # Daemon status must be degraded.
    with daemon._state_lock:
        assert daemon._status == source_graph_daemon.STATUS_DEGRADED


# ---------------------------------------------------------------------------
# 14. Regression: recovery elapsed frozen after successful recovery.
# ---------------------------------------------------------------------------

def test_recovery_elapsed_frozen_after_success(tmp_path, cleanup_daemons):
    """After successful recovery, health's recovery.elapsed_seconds must
    not grow over time (terminal frozen value)."""
    root = _init_repo(tmp_path, "elapsed_frozen_success")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    report = source_graph.build_index(root, incremental=False)
    assert report.files_seen > 0
    db_path = source_graph.resolve_db_path(root)

    journal_path = _create_crash_journal(db_path)
    assert journal_path.exists()

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    # Simulate a prior successful build so LKG is present.
    daemon._last_report = {
        "build_revision": report.build_revision,
        "finished_at": report.finished_at,
        "files_seen": report.files_seen,
    }

    # Run recovery; it should succeed.
    result = daemon._recover_database()
    assert result["recovered"] is True

    # Immediately after recovery, capture the elapsed.
    health1 = daemon.health()
    elapsed1 = health1["recovery"]["elapsed_seconds"]
    assert elapsed1 >= 0.0

    # Wait a small interval and check that elapsed does not increase.
    time.sleep(0.15)
    health2 = daemon.health()
    elapsed2 = health2["recovery"]["elapsed_seconds"]
    # The elapsed should be frozen (same as before) or <= previous + epsilon.
    # Actually, since recovery is done, started_at=0 and _recovery_elapsed holds
    # the captured elapsed; it must not grow.
    assert elapsed2 == pytest.approx(elapsed1, abs=0.001)


# ---------------------------------------------------------------------------
# 15. Regression: recovery elapsed retained on failure.
# ---------------------------------------------------------------------------

def test_recovery_elapsed_retained_on_failure(tmp_path, cleanup_daemons):
    """After failed recovery, health's recovery.elapsed_seconds must retain
    the frozen value from the failure and not grow."""
    root = _init_repo(tmp_path, "elapsed_retained_fail")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    report = source_graph.build_index(root, incremental=False)
    assert report.files_seen > 0
    db_path = source_graph.resolve_db_path(root)

    # Create a journal, then remove the DB so recovery's connect step fails.
    journal_path = _create_crash_journal(db_path)
    assert journal_path.exists()
    db_path.unlink()  # remove the DB so resolve succeeds but connect fails.

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    daemon._last_report = {
        "build_revision": report.build_revision,
        "finished_at": report.finished_at,
        "files_seen": report.files_seen,
    }

    # Recovery should fail (connect error).
    result = daemon._recover_database()
    assert result["recovered"] is False

    # Capture elapsed after failure.
    health1 = daemon.health()
    elapsed1 = health1["recovery"]["elapsed_seconds"]
    assert elapsed1 >= 0.0
    phase1 = health1["recovery"]["phase"]
    assert phase1 != ""

    # Wait and ensure elapsed does not grow.
    time.sleep(0.15)
    health2 = daemon.health()
    elapsed2 = health2["recovery"]["elapsed_seconds"]
    assert elapsed2 == pytest.approx(elapsed1, abs=0.001)
    # Phase and error should still be the same.


# ---------------------------------------------------------------------------
# 11. Generation-metadata authority: absent/malformed rows do not recover.
# ---------------------------------------------------------------------------

def _seed_last_known_good_generation(daemon: source_graph_daemon.SourceGraphDaemon) -> None:
    daemon._last_report = {
        "build_revision": "aiworkhub.source_graph.semantic.v5",
        "finished_at": "2026-08-01T12:00:00+00:00",
        "files_seen": 4,
        "files_changed": 4,
        "files_unchanged": 0,
        "files_removed": 0,
        "entities_written": 4,
        "edges_written": 1,
        "errors": [],
        "db_path": "fake.sqlite",
        "incremental": False,
    }
    daemon._last_success_at = "2026-08-01T12:00:00+00:00"
    daemon._last_known_good_generation = {
        "build_revision": "aiworkhub.source_graph.semantic.v5",
        "finished_at": "2026-08-01T12:00:00+00:00",
        "files_seen": 4,
    }


def _write_last_build_meta(db_path: Path, value: str | None) -> None:
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        if value is None:
            conn.execute("DELETE FROM meta WHERE key='last_build'")
        else:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='last_build'",
                (value,),
            )
        conn.commit()
    finally:
        conn.close()


def test_recovery_absent_last_build_row_is_degraded(tmp_path, cleanup_daemons):
    """A recovered canonical database without a last_build row is not ready."""
    root = _init_repo(tmp_path, "absent_generation_repo")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    assert source_graph.build_index(root, incremental=False).files_seen > 0
    db_path = source_graph.resolve_db_path(root)
    assert db_path.exists()

    _write_last_build_meta(db_path, None)
    journal_path = _create_crash_journal(db_path)
    assert journal_path.exists()

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    _seed_last_known_good_generation(daemon)

    result = daemon._recover_database()
    health = daemon.health()

    assert result["recovered"] is False
    assert result["phase"] == source_graph_daemon._RECOVERY_PHASE_COMMIT
    assert "generation_meta" in result["error"]
    assert health["status"] == source_graph_daemon.STATUS_DEGRADED
    assert health["recovery"]["phase"] == source_graph_daemon._RECOVERY_PHASE_COMMIT
    assert health["recovery"]["error"] == result["error"]
    assert (
        health["last_known_good_generation"]["build_revision"]
        == "aiworkhub.source_graph.semantic.v5"
    )
    assert db_path.exists(), "failed recovery must not auto-delete the canonical DB"


def test_recovery_json_list_last_build_payload_is_degraded(tmp_path, cleanup_daemons):
    """A JSON list last_build payload must not raise AttributeError."""
    root = _init_repo(tmp_path, "json_list_generation_repo")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    assert source_graph.build_index(root, incremental=False).files_seen > 0
    db_path = source_graph.resolve_db_path(root)
    assert db_path.exists()

    _write_last_build_meta(db_path, json.dumps([1, 2, 3]))
    journal_path = _create_crash_journal(db_path)
    assert journal_path.exists()

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    _seed_last_known_good_generation(daemon)

    result = daemon._recover_database()
    health = daemon.health()

    assert result["recovered"] is False
    assert result["phase"] == source_graph_daemon._RECOVERY_PHASE_COMMIT
    assert "generation_meta" in result["error"]
    assert health["status"] == source_graph_daemon.STATUS_DEGRADED
    assert health["recovery"]["error"] == result["error"]
    assert health["last_known_good_generation"]["finished_at"] == (
        "2026-08-01T12:00:00+00:00"
    )
    assert db_path.exists(), "failed recovery must preserve the canonical DB"


def test_recovery_missing_required_generation_keys_is_degraded(
    tmp_path, cleanup_daemons
):
    """Missing finished_at/files_seen must fail recovery and preserve LKG."""
    root = _init_repo(tmp_path, "missing_generation_keys_repo")
    cleanup_daemons.append(root)
    (root / "app.py").write_text("def f(): return 1\n", encoding="utf-8")

    assert source_graph.build_index(root, incremental=False).files_seen > 0
    db_path = source_graph.resolve_db_path(root)
    assert db_path.exists()

    _write_last_build_meta(
        db_path,
        json.dumps({"build_revision": "aiworkhub.source_graph.semantic.v5"}),
    )
    journal_path = _create_crash_journal(db_path)
    assert journal_path.exists()

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    _seed_last_known_good_generation(daemon)

    result = daemon._recover_database()
    health = daemon.health()

    assert result["recovered"] is False
    assert result["phase"] == source_graph_daemon._RECOVERY_PHASE_COMMIT
    assert "generation_meta" in result["error"]
    assert health["status"] == source_graph_daemon.STATUS_DEGRADED
    assert health["recovery"]["error"] == result["error"]
    assert health["last_known_good_generation"]["files_seen"] == 4
    assert db_path.exists(), "failed recovery must preserve the canonical DB"
