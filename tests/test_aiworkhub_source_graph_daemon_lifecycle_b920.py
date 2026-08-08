"""0.6.30: automatic Source Graph indexing lifecycle (source_graph_daemon.py).

Covers: InitRepo triggers an initial index without blocking the caller,
reload/ensure_started converges on the same repo-bound daemon, the periodic
loop and an explicit refresh_now() never overlap on the same repository's
database, two repositories never share or interfere with each other's
daemon, a failed build reports degraded health without crashing, and stop()
cleanly unregisters.

Every fixture uses its own ``tmp_path`` repository (Parallel-Tests-First
rule) -- no test shares a canonical DB or daemon-registry key with another.
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, repository_bootstrap, server, source_graph, source_graph_daemon, task_store  # noqa: E402


@pytest.fixture(autouse=True)
def in_process_builds_for_monkeypatches(monkeypatch):
    """Keep unit-test monkeypatches in-process.

    Production defaults to a dedicated indexing subprocess so a CPU-heavy
    repository scan cannot starve MCP stdio. These unit tests deliberately
    replace ``source_graph.build_index`` with local closures/events, which
    cannot cross a process boundary.
    """
    monkeypatch.setenv(
        source_graph_daemon.BUILD_EXECUTION_ENV,
        source_graph_daemon.BUILD_EXECUTION_THREAD,
    )


@pytest.fixture
def cleanup_daemons():
    """Stop every daemon this test registered, even on failure -- a stray
    indexing thread from one test must never leak into the next."""
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


# ---------------------------------------------------------------------------
# 1. InitRepo triggers an initial index (full build, since no prior
#    last_build meta) and never blocks the caller on the whole build.
# ---------------------------------------------------------------------------


def test_init_repo_triggers_initial_index_without_blocking(tmp_path, cleanup_daemons):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.php").write_text("<?php\nfunction live_probe(): int { return 1; }\n", encoding="utf-8")
    cleanup_daemons.append(root)

    result = repository_bootstrap.initialize_repository_full(root)

    assert result["ok"]
    assert result["source_graph_ready"] is True
    assert result["source_graph_daemon_started"] is True
    ignore_config = root / ".aiworkhub" / "config" / "source_graph.json"
    assert ignore_config.is_file()
    assert source_graph.load_ignore_policy(root).exclude_dirs >= source_graph.DEFAULT_EXCLUDE_DIR_NAMES

    daemon = source_graph_daemon.get_daemon(root)
    assert daemon is not None
    assert daemon.is_running()

    # Wait on the daemon's build-completion event, not timing-sensitive
    # sleep/poll loops. InitRepo itself remains non-blocking.
    assert daemon.wait_for_first_build(timeout=10), "initial background build never completed"
    health = daemon.health()

    assert health["status"] == source_graph_daemon.STATUS_READY
    # InitRepo also projects the three manager instruction documents; the
    # documentation family intentionally indexes those repository contracts.
    assert health["last_report"]["files_seen"] == 4
    assert health["last_report"]["incremental"] is False
    assert health["language_capabilities"]["php"] == "semantic_lexical"
    assert ".php" in health["indexed_extensions"]


def test_first_build_completion_hands_off_after_build_lock_release(
    tmp_path, cleanup_daemons,
):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    completion = daemon._build_completed
    lock_state_at_completion: list[bool] = []

    class CompletionProbe:
        def set(self) -> None:
            lock_state_at_completion.append(daemon._build_lock.locked())
            completion.set()

        def wait(self, timeout: float | None = None) -> bool:
            return completion.wait(timeout)

    daemon._build_completed = CompletionProbe()  # type: ignore[assignment]
    daemon.start()

    assert daemon.wait_for_first_build(timeout=10)
    assert lock_state_at_completion == [False]
    assert daemon.refresh_now()["triggered"] is True


def test_initialized_repo_indexes_instruction_documents_without_source_files(tmp_path):
    root = _init_repo(tmp_path)
    daemon = source_graph_daemon.SourceGraphDaemon(root)

    assert daemon._run_one_build() is True
    health = daemon.health()

    assert health["ok"] is True
    assert health["status"] == source_graph_daemon.STATUS_READY
    assert health["last_report"]["files_seen"] == 3
    assert health["last_report"]["entities_written"] == 3


def test_health_exposes_current_generation_quality_and_roundtrip_scorecards(
    tmp_path, cleanup_daemons,
):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    (root / "app.py").write_text(
        "def health_target():\n    return 1\n",
        encoding="utf-8",
    )
    daemon = source_graph_daemon.ensure_started(root)

    assert daemon.wait_for_first_build(timeout=10)
    health = source_graph_daemon.daemon_health(root)

    assert health["index_quality"]["current_generation"] is True
    assert health["index_quality"]["build_revision"] == source_graph.BUILD_REVISION
    assert health["recommendation_resolvability"]["current_generation"] is True
    assert health["recommendation_resolvability"]["resolvability_ratio"] == 1.0
    assert health["guidance_degraded"] is False


def test_production_default_build_runs_in_dedicated_subprocess(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    (root / "app.py").write_text("def live_probe():\n    return 1\n", encoding="utf-8")
    monkeypatch.delenv(source_graph_daemon.BUILD_EXECUTION_ENV, raising=False)
    monkeypatch.setenv("PYTHONPATH", str(_SRC))
    daemon = source_graph_daemon.SourceGraphDaemon(root)

    assert daemon._build_execution == source_graph_daemon.BUILD_EXECUTION_SUBPROCESS
    assert daemon._run_one_build() is True
    health = daemon.health()
    assert health["ok"] is True
    assert health["status"] == source_graph_daemon.STATUS_READY
    assert health["last_report"]["files_seen"] == 4


def test_old_success_is_truthfully_stale_until_next_success(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    daemon = source_graph_daemon.SourceGraphDaemon(
        root,
        refresh_interval_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS,
        stale_after_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS,
    )
    daemon._thread = threading.current_thread()
    daemon._status = source_graph_daemon.STATUS_READY
    daemon._started_at = datetime.now(timezone.utc).isoformat()
    daemon._last_success_at = (
        datetime.now(timezone.utc) - timedelta(minutes=2)
    ).isoformat()

    stale = daemon.health()

    assert stale["ok"] is False
    assert stale["status"] == source_graph_daemon.STATUS_STALE
    assert stale["stale_reason"] == "last_success_exceeded_threshold"
    assert stale["index_age_seconds"] >= 119

    def successful_build(repo_root, *, incremental=True, db_path=None):
        return source_graph.BuildReport(
            repo_root=str(repo_root), db_path="fake.sqlite", incremental=incremental,
            files_seen=1, files_changed=1, files_unchanged=0, files_removed=0,
            entities_written=1, edges_written=0, errors=[],
            build_revision="test", finished_at="t",
        )

    monkeypatch.setattr(source_graph, "build_index", successful_build)
    assert daemon._run_one_build() is True
    fresh = daemon.health()
    assert fresh["ok"] is True
    assert fresh["status"] == source_graph_daemon.STATUS_READY
    assert fresh["stale_reason"] == ""


# ---------------------------------------------------------------------------
# 2. Reload / repeated ensure_started converges on the same daemon.
# ---------------------------------------------------------------------------


def test_reload_ensure_started_converges_on_same_daemon(tmp_path, cleanup_daemons):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)

    first = source_graph_daemon.ensure_started(root, refresh_interval_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS)
    again = source_graph_daemon.ensure_started(root, refresh_interval_seconds=9999)

    assert first is again
    assert source_graph_daemon.get_daemon(root) is first
    # The second call's interval kwarg is ignored -- exactly one daemon,
    # its original configuration untouched.
    assert first.refresh_interval_seconds == source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS


def test_core_source_graph_ensure_started_uninitialized_repo_not_degraded(tmp_path, monkeypatch, cleanup_daemons):
    root = tmp_path / "never_initialized"
    root.mkdir()
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    cleanup_daemons.append(root)

    result = core.source_graph_ensure_started()

    assert result["ok"] is True
    assert result["status"] == "uninitialized"
    assert result["daemon_started"] is False
    assert source_graph_daemon.get_daemon(root) is None


def test_core_source_graph_ensure_started_then_stop(tmp_path, monkeypatch, cleanup_daemons):
    root = _init_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    cleanup_daemons.append(root)

    started = core.source_graph_ensure_started()
    assert started["ok"] is True
    assert started["daemon_started"] is True

    # A second "handshake" converges -- never a second thread.
    started_again = core.source_graph_ensure_started()
    assert started_again["daemon_started"] is True
    assert started_again["repo_root"] == str(root.resolve())

    stopped = core.source_graph_stop()
    assert stopped["ok"] is True
    assert stopped["stopped"] is True
    assert source_graph_daemon.get_daemon(root) is None


def test_server_main_bootstraps_source_graph_before_stdio(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(core, "source_graph_ensure_started", lambda: calls.append("source_graph"))
    monkeypatch.setattr(server.core, "repo_root", lambda: Path("/repo"))
    monkeypatch.setattr(server.task_reconciler, "ensure_started", lambda _repo: calls.append("reconciler"))
    monkeypatch.setattr(server.task_reconciler, "stop_reconciler", lambda _repo: calls.append("reconciler_stop"))
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("mcp"))

    server.main()

    assert calls == ["source_graph", "reconciler", "mcp", "reconciler_stop"]


def test_server_main_keeps_mcp_available_when_source_graph_bootstrap_fails(monkeypatch):
    calls: list[str] = []

    def fail_start():
        calls.append("source_graph")
        raise RuntimeError("indexer_failed")

    monkeypatch.setattr(core, "source_graph_ensure_started", fail_start)
    monkeypatch.setattr(server.core, "repo_root", lambda: Path("/repo"))
    monkeypatch.setattr(server.task_reconciler, "ensure_started", lambda _repo: calls.append("reconciler"))
    monkeypatch.setattr(server.task_reconciler, "stop_reconciler", lambda _repo: calls.append("reconciler_stop"))
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("mcp"))

    server.main()

    assert calls == ["source_graph", "reconciler", "mcp", "reconciler_stop"]


# ---------------------------------------------------------------------------
# 3. Periodic loop and refresh_now() never overlap on the same repository.
# ---------------------------------------------------------------------------


def test_periodic_and_refresh_now_never_overlap(tmp_path, monkeypatch, cleanup_daemons):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)

    build_started = threading.Event()
    release_build = threading.Event()
    call_count = {"n": 0}

    def fake_build_index(repo_root, *, incremental=True, db_path=None):
        call_count["n"] += 1
        build_started.set()
        release_build.wait(timeout=5)
        return source_graph.BuildReport(
            repo_root=str(repo_root), db_path="fake.sqlite", incremental=incremental,
            files_seen=0, files_changed=0, files_unchanged=0, files_removed=0,
            entities_written=0, edges_written=0, errors=[],
            build_revision="test", finished_at="t",
        )

    monkeypatch.setattr(source_graph, "build_index", fake_build_index)

    daemon = source_graph_daemon.SourceGraphDaemon(
        root, refresh_interval_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS,
    )
    daemon.start()
    try:
        assert build_started.wait(timeout=5), "background build never started"

        # The periodic loop's first build is still in flight (blocked on
        # release_build) -- an explicit refresh_now() must not join it.
        result = daemon.refresh_now()
        assert result["triggered"] is False
        assert result["reason"] == "build_in_progress"

        release_build.set()
    finally:
        daemon.stop()

    # The armed follow-up may lose a race with stop(); non-overlap is the
    # invariant this test owns, while the dedicated coalescing test below
    # proves that a live daemon consumes the follow-up.
    assert 1 <= call_count["n"] <= 2


def test_core_refresh_queues_without_blocking_mcp_caller(
    tmp_path, monkeypatch, cleanup_daemons,
):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    daemon = source_graph_daemon.ensure_started(root)
    assert daemon.wait_for_first_build(timeout=10)

    build_started = threading.Event()
    release_build = threading.Event()

    def blocking_build(repo_root, *, incremental=True, db_path=None):
        build_started.set()
        release_build.wait(timeout=5)
        return source_graph.BuildReport(
            repo_root=str(repo_root), db_path="fake.sqlite", incremental=incremental,
            files_seen=0, files_changed=0, files_unchanged=0, files_removed=0,
            entities_written=0, edges_written=0, errors=[],
            build_revision="test", finished_at="t",
        )

    monkeypatch.setattr(source_graph, "build_index", blocking_build)
    result = core.source_graph_refresh_now()
    assert result["ok"] is True
    assert result["queued"] is True
    assert result["reason"] == "refresh_queued"
    assert build_started.wait(timeout=5), "queued refresh did not reach daemon"
    release_build.set()


def test_cross_process_writer_contention_is_healthy_standby(tmp_path, cleanup_daemons):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    with source_graph.index_write_lease(root) as acquired:
        assert acquired is True
        assert daemon._run_one_build() is True
    health = daemon.health()
    assert health["ok"] is True
    assert health["status"] == source_graph_daemon.STATUS_STANDBY
    assert health["writer_state"] == "standby"
    assert health["last_error"] == ""


def test_daemon_health_hydrates_fresh_canonical_generation_for_standby(
    tmp_path, monkeypatch,
):
    root = _init_repo(tmp_path, "standby_reader")
    report = source_graph.build_index(root, incremental=False)
    assert report.files_seen > 0
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    daemon._status = source_graph_daemon.STATUS_STANDBY
    monkeypatch.setattr(source_graph_daemon, "get_daemon", lambda _root: daemon)

    health = source_graph_daemon.daemon_health(root)

    assert health["status"] == source_graph_daemon.STATUS_STANDBY
    assert health["readable_generation"] is True
    assert health["last_success_at"]
    assert health["build_revision"] == report.build_revision
    assert health["files_seen"] == report.files_seen


def test_daemon_health_hydrates_canonical_generation_while_indexing(
    tmp_path, monkeypatch,
):
    root = _init_repo(tmp_path, "indexing_reader")
    report = source_graph.build_index(root, incremental=False)
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    daemon._status = source_graph_daemon.STATUS_INDEXING
    monkeypatch.setattr(daemon, "is_running", lambda: True)
    monkeypatch.setattr(source_graph_daemon, "get_daemon", lambda _root: daemon)

    health = source_graph_daemon.daemon_health(root)

    assert health["status"] == source_graph_daemon.STATUS_INDEXING
    assert health["readable_generation"] is True
    assert health["build_revision"] == report.build_revision
    assert health["files_seen"] == report.files_seen


def test_daemon_health_probe_lock_preserves_known_readable_generation(
    tmp_path, monkeypatch,
):
    root = _init_repo(tmp_path, "locked_health_probe")
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    daemon._status = source_graph_daemon.STATUS_READY
    daemon._last_success_at = "2026-08-05T08:00:00+00:00"
    daemon._last_report = {
        "build_revision": source_graph.BUILD_REVISION,
        "files_seen": 7,
    }
    monkeypatch.setattr(source_graph_daemon, "get_daemon", lambda _root: daemon)
    monkeypatch.setattr(
        source_graph,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            source_graph_daemon.sqlite3.OperationalError("database is locked")
        ),
    )

    health = source_graph_daemon.daemon_health(root)

    assert health["readable_generation"] is True
    assert health["build_revision"] == source_graph.BUILD_REVISION
    assert health["files_seen"] == 7
    assert "database is locked" in health["generation_read_error"]


def test_health_exposes_successful_build_identity_at_top_level(tmp_path, cleanup_daemons):
    root = tmp_path / "never_initialized"
    root.mkdir()
    never_registered = source_graph_daemon.daemon_health(root)
    assert never_registered["build_revision"] == ""
    assert never_registered["files_seen"] == 0

    real_root = _init_repo(tmp_path, "revision_repo")
    cleanup_daemons.append(real_root)
    daemon = source_graph_daemon.SourceGraphDaemon(real_root)
    assert daemon._run_one_build() is True
    health = daemon.health()
    assert health["status"] == source_graph_daemon.STATUS_READY
    assert health["build_revision"] == health["last_report"]["build_revision"]
    assert health["build_revision"]
    assert health["files_seen"] == health["last_report"]["files_seen"]
    assert health["files_seen"] > 0


def test_prior_build_probe_failure_is_degraded_not_dead_indexing(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    daemon = source_graph_daemon.SourceGraphDaemon(root)

    def fail_probe():
        raise RuntimeError("transient probe failure")

    monkeypatch.setattr(daemon, "_has_prior_build", fail_probe)
    assert daemon._run_one_build() is True
    health = daemon.health()
    assert health["status"] == source_graph_daemon.STATUS_DEGRADED
    assert health["ok"] is False
    assert "transient probe failure" in health["last_error"]


# ---------------------------------------------------------------------------
# 4. Two repositories never share or interfere with each other's daemon.
# ---------------------------------------------------------------------------


def test_repo_isolation_distinct_daemons(tmp_path, cleanup_daemons):
    root_a = _init_repo(tmp_path, "repo_a")
    root_b = _init_repo(tmp_path, "repo_b")
    cleanup_daemons.append(root_a)
    cleanup_daemons.append(root_b)

    daemon_a = source_graph_daemon.ensure_started(root_a)
    daemon_b = source_graph_daemon.ensure_started(root_b)

    assert daemon_a is not daemon_b
    assert daemon_a.repo_root != daemon_b.repo_root
    assert daemon_a.is_running()
    assert daemon_b.is_running()

    assert source_graph_daemon.stop_daemon(root_a) is True
    assert source_graph_daemon.get_daemon(root_a) is None
    assert source_graph_daemon.get_daemon(root_b) is daemon_b
    assert daemon_b.is_running()


# ---------------------------------------------------------------------------
# 5. A failed build reports degraded health, never crashes the process.
# ---------------------------------------------------------------------------


def test_failed_build_reports_degraded_health_never_raises(tmp_path, monkeypatch, cleanup_daemons):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)

    def boom(repo_root, *, incremental=True, db_path=None):
        raise RuntimeError("simulated index failure")

    monkeypatch.setattr(source_graph, "build_index", boom)

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    result = daemon.refresh_now()  # must not raise

    assert result["triggered"] is True
    assert result["ok"] is False
    assert result["status"] == source_graph_daemon.STATUS_DEGRADED
    assert "simulated index failure" in result["last_error"]

    health = daemon.health()
    assert health["status"] == source_graph_daemon.STATUS_DEGRADED
    assert health["ok"] is False


def test_core_source_graph_health_reports_not_registered_not_degraded(tmp_path, monkeypatch, cleanup_daemons):
    root = _init_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    cleanup_daemons.append(root)

    health = core.source_graph_health()

    assert health["ok"] is True
    assert health["registered"] is False
    assert health["status"] == source_graph_daemon.STATUS_STOPPED


# ---------------------------------------------------------------------------
# 6. stop() cleanly unregisters and the underlying thread actually exits.
# ---------------------------------------------------------------------------


def test_stop_unregisters_and_thread_exits(tmp_path, cleanup_daemons):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)

    daemon = source_graph_daemon.ensure_started(root)
    assert daemon.is_running()

    assert source_graph_daemon.stop_daemon(root) is True
    assert daemon.is_running() is False
    assert source_graph_daemon.get_daemon(root) is None
    # Idempotent: stopping again (already unregistered) is a clean no-op.
    assert source_graph_daemon.stop_daemon(root) is False


def test_refresh_now_coalescing_arms_event_and_runs_one_follow_up_build(
    tmp_path, monkeypatch, cleanup_daemons,
):
    """A refresh request during a build schedules one later generation."""
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)

    build_started = threading.Event()
    release_build = threading.Event()
    follow_up_started = threading.Event()
    call_count = {"n": 0}

    def fake_build_index(repo_root, *, incremental=True, db_path=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            build_started.set()
            release_build.wait(timeout=5)
        else:
            follow_up_started.set()
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

    monkeypatch.setattr(source_graph, "build_index", fake_build_index)
    daemon = source_graph_daemon.SourceGraphDaemon(
        root,
        refresh_interval_seconds=source_graph_daemon.MIN_REFRESH_INTERVAL_SECONDS,
    )
    daemon.start()
    try:
        assert build_started.wait(timeout=5), "background build never started"
        result = daemon.refresh_now()
        assert result["triggered"] is False
        assert result["reason"] == "build_in_progress"
        assert daemon._refresh_event.is_set()

        release_build.set()
        assert follow_up_started.wait(timeout=5), "follow-up build never started"

        deadline = time.monotonic() + 5.0
        while True:
            health = daemon.health()
            if health["status"] == source_graph_daemon.STATUS_READY and call_count["n"] == 2:
                break
            if time.monotonic() > deadline:
                raise AssertionError(
                    "daemon did not reach ready after follow-up build: "
                    f"status={health['status']} calls={call_count['n']}"
                )
            time.sleep(0.01)

        assert health["last_success_at"]
        assert health["build_revision"] == "test-rev"
        assert health["files_seen"] == 3
    finally:
        daemon.stop()
