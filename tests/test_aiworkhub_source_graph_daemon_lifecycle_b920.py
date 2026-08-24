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

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, repository_bootstrap, server, source_graph, source_graph_daemon, task_store, worker_ai_tools_mcp  # noqa: E402


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
    assert ".aiworkhub" in source_graph.load_ignore_policy(root).exclude_dirs

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


def test_refresh_job_has_durable_identity_and_terminal_success(
    tmp_path, monkeypatch, cleanup_daemons,
):
    root = _init_repo(tmp_path, "durable_refresh")
    cleanup_daemons.append(root)
    daemon = source_graph_daemon.ensure_started(root)
    assert daemon.wait_for_first_build(timeout=10)

    build_started = threading.Event()
    release_build = threading.Event()

    def controlled_build(repo_root, *, incremental=True, db_path=None):
        build_started.set()
        release_build.wait(timeout=5)
        return source_graph.BuildReport(
            repo_root=str(repo_root), db_path="fake.sqlite", incremental=incremental,
            files_seen=1, files_changed=1, files_unchanged=0, files_removed=0,
            entities_written=1, edges_written=0, errors=[],
            build_revision="refresh-test", finished_at="2026-08-13T00:00:00+00:00",
        )

    monkeypatch.setattr(source_graph, "build_index", controlled_build)
    queued = daemon.request_refresh()
    assert queued["queued"] is True
    assert queued["refresh_job"]["state"] == "queued"
    assert len(queued["job_id"]) == 32
    assert build_started.wait(timeout=5)
    release_build.set()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = daemon.health()["refresh_job"]
        if job and job["state"] == "succeeded":
            break
        time.sleep(0.01)
    else:
        pytest.fail("refresh job did not reach terminal success")
    assert job["job_id"] == queued["job_id"]
    assert job["error"] == ""
    persisted = daemon._refresh_job_path()
    assert persisted.is_file()
    assert queued["job_id"] in persisted.read_text(encoding="utf-8")


def test_ensure_started_and_health_share_canonical_generation(
    tmp_path, monkeypatch, cleanup_daemons,
):
    root = _init_repo(tmp_path, "canonical_health")
    (root / "fresh.py").write_text("def freshly_indexed():\n    return 1\n", encoding="utf-8")
    cleanup_daemons.append(root)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))

    daemon = source_graph_daemon.ensure_started(root)
    assert daemon.wait_for_first_build(timeout=10)
    started = core.source_graph_ensure_started()
    health = core.source_graph_health()

    assert started["build_revision"] == health["build_revision"]
    assert started["last_success_at"] == health["last_success_at"]
    assert started["files_seen"] == health["files_seen"]
    assert started["files_seen"] > 0


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


def test_daemon_health_hydrates_generation_without_registered_daemon(tmp_path):
    root = _init_repo(tmp_path, "one_shot_reader")
    report = source_graph.build_index(root, incremental=False)
    assert source_graph_daemon.get_daemon(root) is None

    health = source_graph_daemon.daemon_health(root)

    assert health["status"] == source_graph_daemon.STATUS_STOPPED
    assert health["registered"] is False
    assert health["readable_generation"] is True
    assert health["last_success_at"] == report.finished_at
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


# ---------------------------------------------------------------------------
# 7. NF149: refresh_now()/refresh-job terminal status stays truthful when a
#    build yields to another writer's lease (STANDBY) instead of fabricating
#    a false failure or silently discarding a valid prior generation.
# ---------------------------------------------------------------------------


def test_refresh_job_maps_standby_with_readable_generation_to_succeeded(
    tmp_path, monkeypatch, cleanup_daemons,
):
    """STANDBY with a readable prior canonical generation is not a failure."""
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    (root / "seed.py").write_text("def seeded():\n    return 1\n", encoding="utf-8")

    # A real prior generation must exist and be readable before a later
    # STANDBY outcome can be truthfully treated as non-failing.
    source_graph.build_index(root)

    def standby_build(repo_root, *, incremental=True, db_path=None):
        raise source_graph.SourceGraphBuildInProgressError("locked_by_other_process")

    monkeypatch.setattr(source_graph, "build_index", standby_build)

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    result = daemon.refresh_now()

    assert result["triggered"] is True
    assert result["status"] == source_graph_daemon.STATUS_STANDBY
    refresh_job = result["refresh_job"]
    assert refresh_job is not None
    assert refresh_job["state"] == "succeeded"
    assert refresh_job["error"] == ""
    assert result["refresh_job_id"] == refresh_job["job_id"]


def test_refresh_job_standby_without_readable_generation_remains_failed(
    tmp_path, monkeypatch, cleanup_daemons,
):
    """A genuinely absent/unreadable generation is a truthful failure."""
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)

    def standby_build(repo_root, *, incremental=True, db_path=None):
        raise source_graph.SourceGraphBuildInProgressError("locked_by_other_process")

    monkeypatch.setattr(source_graph, "build_index", standby_build)

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    result = daemon.refresh_now()

    assert result["triggered"] is True
    assert result["status"] == source_graph_daemon.STATUS_STANDBY
    refresh_job = result["refresh_job"]
    assert refresh_job is not None
    assert refresh_job["state"] == "failed"
    assert refresh_job["error"] == "refresh_terminal_status:standby"


# ---------------------------------------------------------------------------
# 8. NF-2026-00204: an unchanged incremental refresh over a high-fanout
#    index must reach a truthful terminal state quickly. ``_index_quality_
#    scorecard`` used to join files -> entities -> edges in one statement,
#    so re-running the scorecard on every refresh (even a no-op one) scaled
#    with the whole index instead of the changed delta.
# ---------------------------------------------------------------------------


def test_unchanged_incremental_refresh_reaches_succeeded_terminal_state_quickly(
    tmp_path, cleanup_daemons,
):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)

    # High-fanout data: many files, each with several entities that call
    # into several other files, so a reintroduced Cartesian aggregation
    # would show up as a real wall-clock regression here.
    for i in range(40):
        callees = "".join(f"    fn_{j}_0()\n" for j in range(max(0, i - 1), i))
        (root / f"mod_{i}.py").write_text(
            f"def fn_{i}_0():\n    return {i}\n"
            f"def fn_{i}_1():\n{callees}    missing_{i}()\n",
            encoding="utf-8",
        )

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    try:
        daemon.start()
        assert daemon.wait_for_first_build(timeout=20), "first build never completed"
        files_seen_after_full_build = daemon.health()["files_seen"]
        assert files_seen_after_full_build >= 40

        # ``request_refresh`` queues a job the background loop drains via
        # ``_run_one_build`` -- the same path a live daemon uses for its
        # periodic tick, unlike calling ``_run_one_build`` directly.
        started = time.monotonic()
        queued = daemon.request_refresh()
        assert queued["triggered"] is True
        job_id = queued["refresh_job"]["job_id"]

        deadline = time.monotonic() + 15.0
        health = daemon.health()
        while True:
            health = daemon.health()
            job = health.get("refresh_job")
            if job and job.get("job_id") == job_id and job.get("state") in (
                "succeeded", "failed",
            ):
                break
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"unchanged incremental refresh job never reached a "
                    f"terminal state: {job}"
                )
            time.sleep(0.01)
        elapsed = time.monotonic() - started

        assert job["state"] == "succeeded"
        assert job["error"] == ""
        assert health["status"] == source_graph_daemon.STATUS_READY
        assert health["last_report"]["files_changed"] == 0
        assert health["last_report"]["files_seen"] == files_seen_after_full_build

        # Generous bound: a no-op refresh over 40 high-fanout files must stay
        # a small constant, not scale with total entities x edges in the
        # index (the Cartesian aggregation this fix removed).
        assert elapsed < 15.0, f"unchanged incremental refresh took {elapsed:.2f}s"
    finally:
        daemon.stop()


# ---------------------------------------------------------------------------
# 9. NF-2026-00205: health()/refresh must truthfully expose hash-worker and
#    no-op reuse telemetry -- a true no-op generation (nothing changed or
#    removed) must reuse the prior generation's recommendation-roundtrip
#    verdict instead of re-probing, and health() must surface both the hash
#    reconciliation counts and the reuse flags.
# ---------------------------------------------------------------------------


def test_health_exposes_hash_and_noop_reuse_telemetry_on_unchanged_refresh(
    tmp_path, cleanup_daemons, monkeypatch,
):
    root = _init_repo(tmp_path)
    cleanup_daemons.append(root)
    (root / "seed.py").write_text("def seeded():\n    return 1\n", encoding="utf-8")

    probe_calls: list[int] = []

    def fake_gate(context):
        probe_calls.append(1)
        return {
            "schema_id": "aiworkhub.source_graph.recommendation_roundtrip.v1",
            "ok": True,
            "status": "ok",
            "sampled_symbols": 1,
            "emitted": 1,
            "resolved": 1,
            "resolvability_ratio": 1.0,
            "failures": [],
        }

    monkeypatch.setattr(
        worker_ai_tools_mcp, "source_graph_recommendation_roundtrip_gate", fake_gate,
    )

    daemon = source_graph_daemon.SourceGraphDaemon(root)
    try:
        daemon.start()
        assert daemon.wait_for_first_build(timeout=20)
        assert probe_calls == [1]
        first_health = daemon.health()
        assert first_health["last_report"]["quality_reused"] is False
        assert first_health["recommendation_reused"] is False

        queued = daemon.request_refresh()
        assert queued["triggered"] is True
        job_id = queued["refresh_job"]["job_id"]

        deadline = time.monotonic() + 15.0
        health = daemon.health()
        while True:
            health = daemon.health()
            job = health.get("refresh_job")
            if job and job.get("job_id") == job_id and job.get("state") in (
                "succeeded", "failed",
            ):
                break
            if time.monotonic() > deadline:
                raise AssertionError(f"refresh never reached a terminal state: {job}")
            time.sleep(0.01)

        assert job["state"] == "succeeded"
        assert health["last_report"]["files_changed"] == 0
        assert health["last_report"]["files_removed"] == 0
        # The gate must not have been probed a second time -- the no-op
        # generation reused the first probe's verdict verbatim rather than
        # re-running the sampled-symbol round trip.
        assert probe_calls == [1]
        assert health["hash_candidates"] >= 1
        assert health["hash_reused"] == health["hash_candidates"]
        assert health["quality_reused"] is True
        assert health["last_report"]["quality_reused"] is True
        assert health["recommendation_reused"] is True
        recommendation = health["last_report"].get("recommendation_resolvability") or {}
        assert recommendation["reused_from_previous_generation"] is True
        assert recommendation["ok"] is True
    finally:
        daemon.stop()


# ---------------------------------------------------------------------------
# 10. Bounded process-tree shutdown: cancelling/stopping a build must
#     terminate and reap the ENTIRE owned build tree -- including
#     ProcessPoolExecutor-style grandchildren that inherit the daemon's
#     stdout/stderr pipes -- with only bounded waits, using exact process
#     identity so a recycled PID never targets an unrelated process/group.
# ---------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _procfs_available() -> bool:
    """True when this host exposes a Linux-style ``/proc`` filesystem.

    macOS has no ``/proc`` at all, so a missing ``/proc/<pid>/stat`` there
    means procfs is unavailable -- not that the PID is dead. Probing
    ``/proc/self`` distinguishes "procfs present, this PID has no entry"
    (a truly gone PID) from "procfs absent" (must fall back to liveness).
    """
    return os.path.isdir("/proc/self")


def _terminated(pid: int) -> bool:
    """True when ``pid`` is gone or a not-yet-reaped zombie (i.e. dead).

    On a procfs host we can tell a not-yet-reaped zombie from a fully gone
    PID: a killed grandchild whose parent we already reaped is reparented
    and may briefly linger as a zombie before the OS reaper collects it, and
    a zombie is terminated, not running. A missing ``/proc/<pid>/stat`` there
    means the PID has no entry -- it is gone.

    On a host without procfs (e.g. macOS, where ``/proc`` is absent entirely)
    a missing path proves nothing about the PID, so we fall back to exact PID
    liveness and treat the PID as terminated only once it has actually
    disappeared -- never misclassifying a live PID as dead.
    """
    if not _procfs_available():
        return not _alive(pid)
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            state = handle.read().rsplit(")", 1)[1].split()[0]
        return state == "Z"
    except FileNotFoundError:
        # Procfs is present but this PID has no entry -> it is gone.
        return True
    except OSError:
        return not _alive(pid)


def _tree_probe_script(pidfile: Path) -> str:
    """A build-subprocess stand-in that spawns a pipe-inheriting grandchild.

    The grandchild inherits the daemon's stdout/stderr pipes (default fd
    inheritance) and holds them open by sleeping, so a parent-only kill would
    deadlock ``communicate()``. Both PIDs are written to ``pidfile`` so the
    test can prove the exact tree is reaped.
    """
    return textwrap.dedent(
        f"""
        import os, subprocess, sys, time
        gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with open({str(pidfile)!r}, "w") as fh:
            fh.write(str(os.getpid()) + " " + str(gc.pid))
            fh.flush()
            os.fsync(fh.fileno())
        time.sleep(120)
        """
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group reaping")
def test_stop_reaps_entire_build_tree_including_pipe_inheriting_grandchild(
    tmp_path, monkeypatch, cleanup_daemons,
):
    root = _init_repo(tmp_path, "tree_kill")
    cleanup_daemons.append(root)
    # Force the production dedicated-subprocess path (the autouse fixture
    # pins in-process THREAD execution for monkeypatch-based unit tests).
    monkeypatch.setenv(
        source_graph_daemon.BUILD_EXECUTION_ENV,
        source_graph_daemon.BUILD_EXECUTION_SUBPROCESS,
    )
    pidfile = tmp_path / "tree_pids.txt"
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    assert daemon._build_execution == source_graph_daemon.BUILD_EXECUTION_SUBPROCESS
    monkeypatch.setattr(
        daemon,
        "_build_subprocess_command",
        lambda *, incremental: [sys.executable, "-c", _tree_probe_script(pidfile)],
    )

    daemon.start()

    parent_pid = grandchild_pid = None
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if pidfile.is_file():
            parts = pidfile.read_text(encoding="ascii").split()
            if len(parts) == 2:
                parent_pid, grandchild_pid = int(parts[0]), int(parts[1])
                break
        time.sleep(0.02)
    assert parent_pid and grandchild_pid, "build tree never reported its PIDs"
    assert _alive(parent_pid) and _alive(grandchild_pid), "tree not alive pre-stop"
    # The child leads its own session; the grandchild shares that group.
    assert os.getpgid(parent_pid) == parent_pid

    started = time.monotonic()
    daemon.stop(timeout=10.0)
    elapsed = time.monotonic() - started

    assert elapsed < 8.0, f"stop() was not bounded: {elapsed:.2f}s"
    assert daemon.is_running() is False
    for pid in (parent_pid, grandchild_pid):
        for _ in range(250):
            if _terminated(pid):
                break
            time.sleep(0.02)
        assert _terminated(pid), f"pid {pid} survived stop()"


def _tree_probe_script_sigterm_ignoring_grandchild(pidfile: Path) -> str:
    """A build stand-in whose grandchild ignores SIGTERM and inherits pipes.

    The group leader spawns a pipe-inheriting grandchild that installs a
    SIGTERM-ignoring handler, waits for it to settle, records both PIDs, then
    blocks on the default SIGTERM handler. On a group SIGTERM the leader exits
    immediately -- the graceful-leader wait succeeds -- but the grandchild
    survives and keeps the inherited stdout/stderr open, proving leader exit is
    NOT whole-tree death. Only the group SIGKILL escalation reaps it.
    """
    return textwrap.dedent(
        f"""
        import os, subprocess, sys, time
        gc = subprocess.Popen([
            sys.executable, "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(120)",
        ])
        # Let the grandchild install its SIGTERM-ignoring handler before the
        # test can observe the PIDs and stop the daemon, so the group SIGTERM is
        # reliably ignored and the fix's SIGKILL escalation is what reaps it.
        time.sleep(0.5)
        with open({str(pidfile)!r}, "w") as fh:
            fh.write(str(os.getpid()) + " " + str(gc.pid))
            fh.flush()
            os.fsync(fh.fileno())
        time.sleep(120)
        """
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group reaping")
def test_stop_reaps_sigterm_ignoring_pipe_inheriting_grandchild(
    tmp_path, monkeypatch, cleanup_daemons,
):
    """Graceful leader exit is not whole-tree death: a pipe-inheriting
    grandchild that ignores SIGTERM must still be escalated to a group SIGKILL
    and reaped within a bounded stop()."""
    root = _init_repo(tmp_path, "sigterm_ignoring_gc")
    cleanup_daemons.append(root)
    monkeypatch.setenv(
        source_graph_daemon.BUILD_EXECUTION_ENV,
        source_graph_daemon.BUILD_EXECUTION_SUBPROCESS,
    )
    pidfile = tmp_path / "sigterm_ignoring_pids.txt"
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    assert daemon._build_execution == source_graph_daemon.BUILD_EXECUTION_SUBPROCESS
    monkeypatch.setattr(
        daemon,
        "_build_subprocess_command",
        lambda *, incremental: [
            sys.executable, "-c",
            _tree_probe_script_sigterm_ignoring_grandchild(pidfile),
        ],
    )

    daemon.start()

    parent_pid = grandchild_pid = None
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if pidfile.is_file():
            parts = pidfile.read_text(encoding="ascii").split()
            if len(parts) == 2:
                parent_pid, grandchild_pid = int(parts[0]), int(parts[1])
                break
        time.sleep(0.02)
    assert parent_pid and grandchild_pid, "build tree never reported its PIDs"
    assert _alive(parent_pid) and _alive(grandchild_pid), "tree not alive pre-stop"
    # The leader leads its own session; the SIGTERM-ignoring grandchild shares
    # exactly that owned group, so the group SIGKILL escalation targets it.
    assert os.getpgid(parent_pid) == parent_pid
    assert os.getpgid(grandchild_pid) == parent_pid

    started = time.monotonic()
    daemon.stop(timeout=10.0)
    elapsed = time.monotonic() - started

    assert elapsed < 8.0, f"stop() was not bounded: {elapsed:.2f}s"
    assert daemon.is_running() is False
    # The leader exits on the group SIGTERM (graceful-leader wait succeeds); the
    # grandchild is reaped only because that path escalates the exact owned
    # group to SIGKILL and boundedly waits for its inherited pipes to close.
    for pid in (parent_pid, grandchild_pid):
        for _ in range(250):
            if _terminated(pid):
                break
            time.sleep(0.02)
        assert _terminated(pid), f"pid {pid} survived stop()"


def _tree_probe_script_leader_exits(pidfile: Path) -> str:
    """A build stand-in whose group leader exits while its grandchild lives.

    The group leader spawns a grandchild that inherits the daemon's
    stdout/stderr pipes, records both PIDs, then exits immediately. The
    grandchild keeps the inherited pipes open by sleeping, so a leader-only
    reap would strand ``communicate`` and a since-reaped leader PID must
    never be widened into an unrelated group. The still-alive grandchild
    keeps the owned pgid reserved, so the bounded drain-time group SIGKILL
    reaps exactly it.
    """
    return textwrap.dedent(
        f"""
        import os, subprocess, sys, time
        gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with open({str(pidfile)!r}, "w") as fh:
            fh.write(str(os.getpid()) + " " + str(gc.pid))
            fh.flush()
            os.fsync(fh.fileno())
        # Group leader exits now; the pipe-inheriting grandchild survives.
        """
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group reaping")
def test_stop_is_bounded_when_group_leader_exits_but_grandchild_survives(
    tmp_path, monkeypatch, cleanup_daemons,
):
    root = _init_repo(tmp_path, "leader_exit")
    cleanup_daemons.append(root)
    monkeypatch.setenv(
        source_graph_daemon.BUILD_EXECUTION_ENV,
        source_graph_daemon.BUILD_EXECUTION_SUBPROCESS,
    )
    pidfile = tmp_path / "leader_exit_pids.txt"
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    monkeypatch.setattr(
        daemon,
        "_build_subprocess_command",
        lambda *, incremental: [
            sys.executable, "-c", _tree_probe_script_leader_exits(pidfile),
        ],
    )

    daemon.start()

    parent_pid = grandchild_pid = None
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if pidfile.is_file():
            parts = pidfile.read_text(encoding="ascii").split()
            if len(parts) == 2:
                parent_pid, grandchild_pid = int(parts[0]), int(parts[1])
                break
        time.sleep(0.02)
    assert parent_pid and grandchild_pid, "build tree never reported its PIDs"

    # Wait until the owned group leader has exited (zombie or gone) while the
    # pipe-inheriting grandchild is still alive -- the exact residual case.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _terminated(parent_pid) and not _terminated(grandchild_pid):
            break
        time.sleep(0.02)
    assert _terminated(parent_pid), "group leader never exited"
    assert not _terminated(grandchild_pid), "grandchild died before stop()"
    # The surviving grandchild still names the owned group by the leader's pid.
    assert os.getpgid(grandchild_pid) == parent_pid

    started = time.monotonic()
    daemon.stop(timeout=10.0)
    elapsed = time.monotonic() - started

    assert elapsed < 8.0, f"stop() drain was not bounded: {elapsed:.2f}s"
    assert daemon.is_running() is False
    for _ in range(250):
        if _terminated(grandchild_pid):
            break
        time.sleep(0.02)
    assert _terminated(grandchild_pid), "grandchild survived stop() after leader exit"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group reaping")
def test_exited_group_leader_pipe_inheriting_grandchild(
    tmp_path, monkeypatch, cleanup_daemons,
):
    """``_terminate_build_process`` alone reaps the exact owned group when the
    leader has already exited but a same-group, pipe-inheriting grandchild
    survives -- without relying on the drain loop and without ever widening to
    a since-reaped leader's recycled PID.
    """
    root = _init_repo(tmp_path, "exited_leader_grandchild")
    cleanup_daemons.append(root)
    pidfile = tmp_path / "exited_leader_direct_pids.txt"
    daemon = source_graph_daemon.SourceGraphDaemon(root)

    # Spawn the exact residual tree directly and register it as the owned build
    # process (leading its own session, holding real stdout/stderr pipes), so
    # the reaping authority under test is _terminate_build_process itself.
    proc = subprocess.Popen(
        [sys.executable, "-c", _tree_probe_script_leader_exits(pidfile)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    daemon._build_process = proc
    daemon._build_pgid = proc.pid
    try:
        parent_pid = grandchild_pid = None
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if pidfile.is_file():
                parts = pidfile.read_text(encoding="ascii").split()
                if len(parts) == 2:
                    parent_pid, grandchild_pid = int(parts[0]), int(parts[1])
                    break
            time.sleep(0.02)
        assert parent_pid and grandchild_pid, "build tree never reported its PIDs"
        assert proc.pid == parent_pid

        # Reach the exact residual state: owned leader exited (zombie/gone)
        # while the pipe-inheriting grandchild still names the owned group.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if _terminated(parent_pid) and not _terminated(grandchild_pid):
                break
            time.sleep(0.02)
        assert _terminated(parent_pid), "group leader never exited"
        assert not _terminated(grandchild_pid), "grandchild died before terminate"
        assert os.getpgid(grandchild_pid) == parent_pid

        started = time.monotonic()
        daemon._terminate_build_process()
        elapsed = time.monotonic() - started
        assert elapsed < 6.0, f"_terminate_build_process was not bounded: {elapsed:.2f}s"

        for _ in range(250):
            if _terminated(grandchild_pid):
                break
            time.sleep(0.02)
        assert _terminated(grandchild_pid), (
            "grandchild survived _terminate_build_process() after leader exit"
        )
    finally:
        # Bounded drain closes the residual pipes; the group is already dead.
        daemon._drain_after_stop(proc, proc.pid)


def test_new_process_group_popen_kwargs_is_platform_appropriate(monkeypatch):
    monkeypatch.setattr(source_graph_daemon.os, "name", "posix")
    assert (
        source_graph_daemon.SourceGraphDaemon._new_process_group_popen_kwargs()
        == {"start_new_session": True}
    )

    monkeypatch.setattr(source_graph_daemon.os, "name", "nt")
    win_kwargs = source_graph_daemon.SourceGraphDaemon._new_process_group_popen_kwargs()
    assert set(win_kwargs) == {"creationflags"}
    assert win_kwargs["creationflags"] == getattr(
        source_graph_daemon.subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )


class _FakeLiveProcess:
    """A live, un-reaped child stand-in for platform-branch signalling tests."""

    def __init__(self, pid: int, events: list) -> None:
        self.pid = pid
        self.returncode = None
        self._events = events

    def poll(self):
        return None

    def terminate(self):
        self._events.append("terminate")

    def kill(self):
        self._events.append("kill")


def test_posix_and_macos_stop_signals_exact_owned_process_group(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "posix_group")
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    monkeypatch.setattr(source_graph_daemon.os, "name", "posix")
    # Simulated macOS shares the POSIX killpg path (Linux vs. Darwin alike).
    monkeypatch.setattr(source_graph_daemon.sys, "platform", "darwin")
    sent: list = []
    monkeypatch.setattr(
        source_graph_daemon.os, "killpg", lambda pgid, sig: sent.append((pgid, sig))
    )
    proc = _FakeLiveProcess(909, sent)

    daemon._signal_process_tree(proc, 909, graceful=True)
    daemon._signal_process_tree(proc, 909, graceful=False)

    # Exactly the owned group, escalating SIGTERM -> SIGKILL; the single-child
    # fallback (terminate/kill) is never used when the group identity is known.
    assert sent == [(909, signal.SIGTERM), (909, signal.SIGKILL)]


def test_windows_stop_signals_tree_via_taskkill(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "win_group")
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    monkeypatch.setattr(source_graph_daemon.os, "name", "nt")
    calls: list = []
    monkeypatch.setattr(
        source_graph_daemon.subprocess,
        "run",
        lambda args, **kwargs: calls.append(list(args)),
    )
    proc = _FakeLiveProcess(4321, calls)

    daemon._signal_process_tree(proc, None, graceful=True)
    daemon._signal_process_tree(proc, None, graceful=False)

    # /T reaps the whole tree; /F escalates. Exact owned PID, never a guess.
    assert calls == [
        ["taskkill", "/T", "/PID", "4321"],
        ["taskkill", "/F", "/T", "/PID", "4321"],
    ]


def test_terminate_is_noop_without_owned_process(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "no_owned_proc")
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    signalled: list = []
    monkeypatch.setattr(
        source_graph_daemon.os, "killpg", lambda *a: signalled.append(a)
    )

    daemon._build_process = None
    daemon._terminate_build_process()  # must not raise, must signal nothing

    assert signalled == []


def test_terminate_fails_closed_on_already_exited_process(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "exited_proc")
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    signalled: list = []
    monkeypatch.setattr(
        source_graph_daemon.os, "killpg", lambda *a: signalled.append(("killpg", a))
    )

    class _ExitedProcess:
        pid = 777
        returncode = 0

        def poll(self):
            return 0

        def terminate(self):
            signalled.append("terminate")

        def kill(self):
            signalled.append("kill")

    daemon._build_process = _ExitedProcess()  # type: ignore[assignment]
    # A since-recycled group identity must never be targeted once the owned
    # child has exited/been reaped.
    daemon._build_pgid = 777
    daemon._terminate_build_process()

    assert signalled == []


class _FakeDrainStream:
    """A pipe stand-in that records whether the bounded drain closed it."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _DrainProbeProcess:
    """A ``Popen`` stand-in whose ``communicate`` always times out.

    This forces ``_drain_after_stop`` down its lingering-writer branch so the
    force-signal decision is driven purely by whether the leader is still exact
    live and un-reaped (``poll() is None``) -- the drain-after-stop-pid-reuse
    guard under test. ``wait`` never blocks so the bounded reap is observable.
    """

    def __init__(self, *, pid: int, alive: bool) -> None:
        self.pid = pid
        self._alive = alive
        self.returncode = None if alive else 0
        self.stdout = _FakeDrainStream()
        self.stderr = _FakeDrainStream()
        self.wait_calls: list = []

    def communicate(self, timeout=None):
        raise subprocess.TimeoutExpired(cmd="build-drain", timeout=timeout)

    def poll(self):
        return None if self._alive else self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.returncode if self.returncode is not None else 0


def test_drain_after_stop_force_signals_only_live_leader_posix(tmp_path, monkeypatch):
    """POSIX positive path: a still-live, un-reaped leader may be group-killed
    one final time when a writer lingers past the drain timeout -- its PID (==
    the owned pgid) is provably still reserved, so the signal cannot recycle."""
    root = _init_repo(tmp_path, "drain_posix_live")
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    monkeypatch.setattr(source_graph_daemon.os, "name", "posix")
    signalled: list = []
    monkeypatch.setattr(
        source_graph_daemon.os, "killpg", lambda pgid, sig: signalled.append((pgid, sig))
    )
    proc = _DrainProbeProcess(pid=909, alive=True)

    daemon._drain_after_stop(proc, 909, timeout=0.01)

    assert signalled == [(909, signal.SIGKILL)]
    # The bounded drain still closes the residual pipes and reaps the leader.
    assert proc.stdout.closed and proc.stderr.closed
    assert proc.wait_calls


def test_drain_after_stop_never_signals_reaped_leader_posix(tmp_path, monkeypatch):
    """POSIX regression: leader already reaped while a descendant keeps the
    inherited pipes open (communicate times out). The since-reaped leader's
    PID/PGID may already name an unrelated group, so NO second, stale group
    signal is issued -- the drain still fails closed and bounded."""
    root = _init_repo(tmp_path, "drain_posix_reaped")
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    monkeypatch.setattr(source_graph_daemon.os, "name", "posix")
    signalled: list = []
    monkeypatch.setattr(
        source_graph_daemon.os, "killpg", lambda *a: signalled.append(a)
    )
    proc = _DrainProbeProcess(pid=909, alive=False)

    daemon._drain_after_stop(proc, 909, timeout=0.01)

    assert signalled == []
    # Bounded descendant-pipe drain still runs: pipes closed, leader reaped.
    assert proc.stdout.closed and proc.stderr.closed
    assert proc.wait_calls


def test_drain_after_stop_force_signals_only_live_leader_windows(tmp_path, monkeypatch):
    """Windows positive path: a still-live, un-reaped leader may be tree-killed
    via ``taskkill /F /T`` one final time on a lingering writer."""
    root = _init_repo(tmp_path, "drain_win_live")
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    monkeypatch.setattr(source_graph_daemon.os, "name", "nt")
    calls: list = []
    monkeypatch.setattr(
        source_graph_daemon.subprocess,
        "run",
        lambda args, **kwargs: calls.append(list(args)),
    )
    proc = _DrainProbeProcess(pid=4321, alive=True)

    daemon._drain_after_stop(proc, None, timeout=0.01)

    assert calls == [["taskkill", "/F", "/T", "/PID", "4321"]]
    assert proc.stdout.closed and proc.stderr.closed


def test_drain_after_stop_never_signals_reaped_leader_windows(tmp_path, monkeypatch):
    """Windows regression: a since-reaped leader's PID may already be recycled,
    so ``taskkill /F /T`` must never fire a second time after the drain
    timeout; the drain still closes the residual pipes and reaps bounded."""
    root = _init_repo(tmp_path, "drain_win_reaped")
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    monkeypatch.setattr(source_graph_daemon.os, "name", "nt")
    calls: list = []
    monkeypatch.setattr(
        source_graph_daemon.subprocess,
        "run",
        lambda args, **kwargs: calls.append(list(args)),
    )
    proc = _DrainProbeProcess(pid=4321, alive=False)

    daemon._drain_after_stop(proc, None, timeout=0.01)

    assert calls == []
    assert proc.stdout.closed and proc.stderr.closed
    assert proc.wait_calls


# ---------------------------------------------------------------------------
# 11. Cross-platform ``_terminated`` portability: on a host without a Linux
#     ``/proc`` filesystem (e.g. macOS) a missing ``/proc/<pid>/stat`` proves
#     nothing about the PID, so the probe must fall back to exact PID liveness
#     and only declare a PID terminated once it has actually disappeared --
#     never misclassifying a live PID as dead.
# ---------------------------------------------------------------------------


_THIS_MODULE = sys.modules[__name__]


def test_terminated_without_procfs_falls_back_to_exact_pid_liveness(monkeypatch):
    """No procfs -> exact liveness, never procfs FileNotFoundError as dead."""
    monkeypatch.setattr(_THIS_MODULE, "_procfs_available", lambda: False)
    live: set[int] = {4242}
    monkeypatch.setattr(_THIS_MODULE, "_alive", lambda pid: pid in live)

    # A live PID is NOT terminated when procfs is unavailable, even though
    # ``open("/proc/<pid>/stat")`` would raise FileNotFoundError here.
    assert _terminated(4242) is False
    # It becomes terminated only once the PID has actually disappeared.
    live.discard(4242)
    assert _terminated(4242) is True


def test_terminated_on_procfs_host_reports_missing_pid_entry_as_gone(monkeypatch):
    """Procfs present but PID entry absent -> gone; a live entry -> not gone."""
    monkeypatch.setattr(_THIS_MODULE, "_procfs_available", lambda: True)

    # An impossible/never-allocated PID has no ``/proc`` entry on a procfs
    # host, which is an unambiguous "gone" -- FileNotFoundError means absent.
    assert _terminated(2**31 - 1) is True
    # The running test process itself has a live, non-zombie ``/proc`` entry.
    assert _terminated(os.getpid()) is False


def test_procfs_available_matches_this_host():
    """The procfs probe agrees with the host it actually runs on."""
    assert _procfs_available() is os.path.isdir("/proc/self")
