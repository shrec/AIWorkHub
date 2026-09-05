"""Regression coverage for independently published Source Graph generations."""

from __future__ import annotations

import sqlite3
import sys
import threading
import json
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import platform_io, source_graph, source_graph_daemon, task_store  # noqa: E402


@pytest.fixture(autouse=True)
def _stop_daemons():
    yield
    source_graph_daemon.stop_all_daemons()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    (root / "app.py").write_text(
        "def committed():\n    return 'searchable marker'\n", encoding="utf-8"
    )
    return root


def _assert_public_reads(root: Path, symbol: str) -> None:
    focused = source_graph.focus(root, symbol, budget=8)
    assert focused["matches"]
    target = focused["matches"][0]["qualname"]
    assert source_graph.slice_(root, symbol, budget=8, target=target)["matches"]
    assert source_graph.bodygrep_query(root, "searchable marker", budget=8)["matches"]
    file_result = source_graph.file_query(root, "app.py", budget=8)
    assert file_result["matches"]
    assert file_result["matches"][0]["file_path"] == "app.py"


def test_active_refresh_keeps_public_retrieval_strictly_readable(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    first = source_graph.build_index(root, incremental=False)
    canonical = source_graph.resolve_db_path(root)
    (root / "app.py").write_text(
        "def refreshed():\n    return 'searchable marker'\n", encoding="utf-8"
    )

    writer_open = threading.Event()
    release_writer = threading.Event()
    original_connect = source_graph.connect

    def gated_connect(path, *, read_only=False):
        conn = original_connect(path, read_only=read_only)
        if not read_only and Path(path) != canonical and not writer_open.is_set():
            writer_open.set()
            assert release_writer.wait(10)
        return conn

    monkeypatch.setattr(source_graph, "connect", gated_connect)
    errors: list[BaseException] = []

    def refresh() -> None:
        try:
            source_graph.build_index(root)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=refresh)
    thread.start()
    assert writer_open.wait(10)
    for _ in range(20):
        _assert_public_reads(root, "committed")
    release_writer.set()
    thread.join(10)
    assert not thread.is_alive()
    assert errors == []
    _assert_public_reads(root, "refreshed")
    assert first.finished_at not in original_connect(
        canonical, read_only=True
    ).execute("SELECT value FROM meta WHERE key='last_build'").fetchone()[0]


def test_daemon_cold_start_refresh_stop_restart_needs_no_later_refresh(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        source_graph_daemon.BUILD_EXECUTION_ENV,
        source_graph_daemon.BUILD_EXECUTION_THREAD,
    )
    root = _repo(tmp_path)
    canonical = source_graph.resolve_db_path(root)
    build_started = [threading.Event(), threading.Event()]
    writer_open = [threading.Event(), threading.Event()]
    release_writer = [threading.Event(), threading.Event()]
    completed = [threading.Event(), threading.Event()]
    staging_paths: list[Path | None] = [None, None]
    original_connect = source_graph.connect
    original_build = source_graph.build_index
    invocation = threading.local()
    next_build = 0
    lock = threading.Lock()

    def gated_connect(path, *, read_only=False):
        conn = original_connect(path, read_only=read_only)
        index = getattr(invocation, "index", None)
        staging_path = Path(path)
        if (
            index is not None
            and index < 2
            and not read_only
            and staging_path != canonical
        ):
            with lock:
                first_staging_open = staging_paths[index] is None
                if first_staging_open:
                    staging_paths[index] = staging_path
            if first_staging_open:
                writer_open[index].set()
                assert release_writer[index].wait(10)
        return conn

    def observed_build(*args, **kwargs):
        nonlocal next_build
        with lock:
            index = next_build
            next_build += 1
        invocation.index = index
        if index < 2:
            build_started[index].set()
        try:
            return original_build(*args, **kwargs)
        finally:
            del invocation.index
            if index < 2:
                completed[index].set()

    monkeypatch.setattr(source_graph, "connect", gated_connect)
    monkeypatch.setattr(source_graph, "build_index", observed_build)

    daemon = source_graph_daemon.ensure_started(root, refresh_interval_seconds=3600)
    assert build_started[0].wait(10)
    assert writer_open[0].wait(10)
    assert staging_paths[0] is not None
    assert staging_paths[0] != canonical
    assert not source_graph_daemon._repo_has_readable_generation(root)
    release_writer[0].set()
    assert completed[0].wait(10)
    _assert_public_reads(root, "committed")

    (root / "app.py").write_text(
        "def refreshed():\n    return 'searchable marker'\n", encoding="utf-8"
    )
    daemon.refresh_now()
    assert build_started[1].wait(10)
    assert writer_open[1].wait(10)
    assert staging_paths[1] is not None
    assert staging_paths[1] != canonical
    assert staging_paths[1] != staging_paths[0]
    _assert_public_reads(root, "committed")
    release_writer[1].set()
    assert completed[1].wait(10)
    _assert_public_reads(root, "refreshed")

    assert source_graph_daemon.stop_daemon(root)
    _assert_public_reads(root, "refreshed")
    restarted = source_graph_daemon.ensure_started(root, refresh_interval_seconds=3600)
    assert restarted is not daemon
    _assert_public_reads(root, "refreshed")


def test_failed_candidate_probe_never_replaces_committed_generation(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    first = source_graph.build_index(root, incremental=False)
    canonical = source_graph.resolve_db_path(root)
    original_bytes = canonical.read_bytes()
    original_connect = source_graph.connect

    def reject_candidate(path, *, read_only=False):
        if read_only and Path(path) != canonical:
            raise sqlite3.OperationalError("simulated retrieval probe failure")
        return original_connect(path, read_only=read_only)

    monkeypatch.setattr(source_graph, "connect", reject_candidate)
    (root / "app.py").write_text(
        "def unpublished():\n    return 'searchable marker'\n", encoding="utf-8"
    )
    with pytest.raises(sqlite3.OperationalError, match="probe failure"):
        source_graph.build_index(root)

    assert canonical.read_bytes() == original_bytes
    assert source_graph_daemon._repo_has_readable_generation(root)
    conn = original_connect(canonical, read_only=True)
    try:
        assert first.finished_at in conn.execute(
            "SELECT value FROM meta WHERE key='last_build'"
        ).fetchone()[0]
    finally:
        conn.close()


def test_candidate_with_missing_fts_is_not_published(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    first = source_graph.build_index(root, incremental=False)
    canonical = source_graph.resolve_db_path(root)
    original_bytes = canonical.read_bytes()
    original_build_locked = source_graph._build_index_locked

    def corrupt_candidate(*args, **kwargs):
        report = original_build_locked(*args, **kwargs)
        candidate = Path(report.db_path)
        conn = source_graph.connect(candidate)
        try:
            # Metadata and the file/entity join remain readable; only the
            # public focus retrieval index is removed after the build closes.
            assert conn.execute(
                "SELECT value FROM meta WHERE key='last_build'"
            ).fetchone()
            assert conn.execute(
                "SELECT f.file_path FROM files AS f "
                "JOIN entities AS e ON e.file_path=f.file_path LIMIT 1"
            ).fetchone()
            conn.execute("DROP TABLE entities_fts")
            conn.commit()
        finally:
            conn.close()
        return report

    monkeypatch.setattr(source_graph, "_build_index_locked", corrupt_candidate)
    (root / "app.py").write_text(
        "def unpublished():\n    return 'searchable marker'\n", encoding="utf-8"
    )

    with pytest.raises(sqlite3.OperationalError, match="entities_fts"):
        source_graph.build_index(root)

    assert canonical.read_bytes() == original_bytes
    conn = source_graph.connect(canonical, read_only=True)
    try:
        generation = conn.execute(
            "SELECT value FROM meta WHERE key='last_build'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert first.finished_at in generation
    _assert_public_reads(root, "committed")


def test_abandoned_staging_generation_is_removed_under_writer_lease(tmp_path):
    root = _repo(tmp_path)
    canonical = source_graph.resolve_db_path(root)
    abandoned = canonical.with_name(f".{canonical.name}.building-dead-process")
    abandoned.write_bytes(b"partial sqlite candidate")

    source_graph.build_index(root, incremental=False)

    assert not abandoned.exists()
    _assert_public_reads(root, "committed")


def test_failed_recommendation_candidate_never_mutates_readonly_canonical(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    source_graph.build_index(root, incremental=False)
    canonical = source_graph.resolve_db_path(root)
    original_bytes = canonical.read_bytes()
    payload = {"ok": True, "finished_at": "staged-but-unpublished"}
    original_publish = source_graph._publish_staged_generation

    def interrupt_after_mutation(staging_path, canonical_path, **kwargs):
        assert canonical_path == canonical
        conn = source_graph.connect(staging_path, read_only=True)
        try:
            assert source_graph.summary(conn)["recommendation_resolvability"] == payload
        finally:
            conn.close()
        raise RuntimeError("simulated interruption before recommendation publication")

    monkeypatch.setattr(
        source_graph, "_publish_staged_generation", interrupt_after_mutation
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        source_graph.record_recommendation_roundtrip(root, payload)

    assert canonical.read_bytes() == original_bytes
    assert not list(canonical.parent.glob(f".{canonical.name}.building-*"))
    source_graph_daemon.stop_daemon(root)
    canonical.chmod(0o444)
    canonical.parent.chmod(0o555)
    try:
        _assert_public_reads(root, "committed")
    finally:
        canonical.parent.chmod(0o755)
        canonical.chmod(0o644)
    monkeypatch.setattr(
        source_graph, "_publish_staged_generation", original_publish
    )


def test_recommendation_becomes_visible_only_after_atomic_publication(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    source_graph.build_index(root, incremental=False)
    canonical = source_graph.resolve_db_path(root)
    payload = {"ok": True, "finished_at": "atomically-published"}
    candidate_ready = threading.Event()
    release_publication = threading.Event()
    original_atomic_replace = source_graph.atomic_replace
    errors: list[BaseException] = []

    def gated_atomic_replace(source, destination):
        if Path(destination) == canonical:
            candidate_ready.set()
            assert release_publication.wait(10)
        return original_atomic_replace(source, destination)

    monkeypatch.setattr(source_graph, "atomic_replace", gated_atomic_replace)

    def record() -> None:
        try:
            source_graph.record_recommendation_roundtrip(root, payload)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=record)
    thread.start()
    assert candidate_ready.wait(10)

    conn = source_graph.connect(canonical, read_only=True)
    try:
        assert source_graph.summary(conn)["recommendation_resolvability"] is None
    finally:
        conn.close()

    release_publication.set()
    thread.join(10)
    assert not thread.is_alive()
    assert errors == []

    conn = source_graph.connect(canonical, read_only=True)
    try:
        assert source_graph.summary(conn)["recommendation_resolvability"] == payload
    finally:
        conn.close()


@pytest.mark.parametrize("payload", [[], 1, "generation"])
def test_non_object_generation_metadata_is_not_readable(tmp_path, payload):
    root = _repo(tmp_path)
    source_graph.build_index(root, incremental=False)
    database = source_graph.resolve_db_path(root)
    conn = source_graph.connect(database)
    try:
        with conn:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='last_build'", (json.dumps(payload),)
            )
    finally:
        conn.close()
    with pytest.raises(source_graph.SourceGraphError, match="metadata_not_object"):
        source_graph.probe_generation(database)
    assert not source_graph_daemon._repo_has_readable_generation(root)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("finished_at", 1),
        ("finished_at", ""),
        ("build_revision", 1),
        ("build_revision", ""),
        ("files_seen", True),
        ("files_seen", "1"),
        ("files_seen", -1),
    ],
)
def test_invalid_generation_field_types_are_not_readable(
    tmp_path, field, invalid
):
    root = _repo(tmp_path)
    source_graph.build_index(root, incremental=False)
    database = source_graph.resolve_db_path(root)
    conn = source_graph.connect(database)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='last_build'"
        ).fetchone()
        payload = json.loads(row["value"])
        payload[field] = invalid
        with conn:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='last_build'",
                (json.dumps(payload),),
            )
    finally:
        conn.close()

    with pytest.raises(source_graph.SourceGraphError, match="incomplete"):
        source_graph.probe_generation(database)
    assert not source_graph_daemon._repo_has_readable_generation(root)


def test_recovery_commit_error_is_retryable_and_releases_locks(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    source_graph.build_index(root, incremental=False)
    real_connect = source_graph.connect

    class FailingCommitConnection:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, statement, *args):
            if statement == "PRAGMA journal_mode=DELETE":
                raise sqlite3.OperationalError("injected journal transition failure")
            return self._connection.execute(statement, *args)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    def failing_connect(path, *, read_only=False):
        connection = real_connect(path, read_only=read_only)
        if not read_only:
            return FailingCommitConnection(connection)
        return connection

    monkeypatch.setattr(source_graph, "connect", failing_connect)
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    result = daemon._recover_database()
    assert result["recovered"] is False
    assert result["retryable"] is True
    assert result["phase"] == "commit"
    assert result["error"].startswith("commit:OperationalError:")
    assert daemon.health()["ok"] is False
    assert daemon.health()["status"] == source_graph_daemon.STATUS_DEGRADED

    monkeypatch.setattr(source_graph, "connect", real_connect)
    assert daemon._recover_database()["recovered"] is True


def test_recovery_journal_unlink_error_is_retryable_and_releases_locks(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    source_graph.build_index(root, incremental=False)
    journal = Path(f"{source_graph.resolve_db_path(root)}-journal")
    journal.write_bytes(b"stale")
    real_unlink = Path.unlink

    def failing_unlink(path, *args, **kwargs):
        if path == journal:
            journal.write_bytes(b"stale")
            raise OSError("injected journal cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    daemon = source_graph_daemon.SourceGraphDaemon(root)
    result = daemon._recover_database()
    assert result["recovered"] is False
    assert result["retryable"] is True
    assert result["phase"] == "commit"
    assert result["error"].startswith("journal_cleanup:OSError:")
    assert daemon.health()["ok"] is False
    assert daemon.health()["status"] == source_graph_daemon.STATUS_DEGRADED
    assert journal.exists()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert daemon._recover_database()["recovered"] is True


def test_durable_publication_syncs_file_before_replace_and_directory_after(
    tmp_path, monkeypatch
):
    source = tmp_path / "candidate"
    destination = tmp_path / "generation"
    source.write_bytes(b"complete")
    events = []
    real_fsync = platform_io.os.fsync

    def recorded_fsync(descriptor):
        events.append("sync")
        real_fsync(descriptor)

    def recorded_replace(candidate, published):
        events.append("replace")
        platform_io.os.replace(candidate, published)

    monkeypatch.setattr(platform_io.os, "fsync", recorded_fsync)
    monkeypatch.setattr(platform_io, "atomic_replace", recorded_replace)
    platform_io.durable_atomic_replace(source, destination)
    assert events == ["sync", "replace", "sync"]


@pytest.mark.parametrize("failure", ["source_sync", "directory_open", "replace"])
def test_durable_publication_precommit_failures_preserve_destination(
    tmp_path, monkeypatch, failure
):
    source = tmp_path / "candidate"
    destination = tmp_path / "generation"
    source.write_bytes(b"candidate")
    destination.write_bytes(b"committed")
    real_open = platform_io.os.open
    real_fsync = platform_io.os.fsync

    if failure == "directory_open":

        def failing_open(path, flags, *args):
            if platform_io.os.fspath(path) == platform_io.os.fspath(tmp_path):
                raise OSError("directory open failed")
            return real_open(path, flags, *args)

        monkeypatch.setattr(platform_io.os, "open", failing_open)
    elif failure == "source_sync":
        def fail_sync(_descriptor):
            raise OSError("source sync failed")

        monkeypatch.setattr(platform_io.os, "fsync", fail_sync)
    else:
        def fail_replace(*_args):
            raise OSError("replace failed")

        monkeypatch.setattr(platform_io, "atomic_replace", fail_replace)

    with pytest.raises(OSError) as caught:
        platform_io.durable_atomic_replace(source, destination)
    assert not isinstance(caught.value, platform_io.PublicationDurabilityError)
    assert destination.read_bytes() == b"committed"


def test_durable_publication_post_replace_sync_failure_identifies_commit(
    tmp_path, monkeypatch
):
    source = tmp_path / "candidate"
    destination = tmp_path / "generation"
    source.write_bytes(b"candidate")
    destination.write_bytes(b"committed")
    real_fsync = platform_io.os.fsync
    calls = 0

    def fail_directory_sync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory sync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(platform_io.os, "fsync", fail_directory_sync)
    with pytest.raises(platform_io.PublicationDurabilityError) as caught:
        platform_io.durable_atomic_replace(source, destination)
    assert caught.value.replacement_committed is True
    assert caught.value.published is True
    assert destination.read_bytes() == b"candidate"


def test_source_graph_reconciles_published_but_uncertain_generation(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    source_graph.build_index(root, incremental=False)
    real_replace = platform_io.atomic_replace

    def publish_then_report_uncertain(source, destination):
        real_replace(source, destination)
        raise platform_io.PublicationDurabilityError("directory sync failed")

    monkeypatch.setattr(source_graph, "atomic_replace", publish_then_report_uncertain)
    with pytest.raises(
        source_graph.SourceGraphError,
        match="durability_uncertain:published=true:canonical_probe_succeeded",
    ):
        source_graph.record_recommendation_roundtrip(root, {"published": True})

    conn = source_graph.connect(source_graph.resolve_db_path(root), read_only=True)
    try:
        assert source_graph.summary(conn)["recommendation_resolvability"] == {
            "published": True
        }
    finally:
        conn.close()


def test_staging_symlink_swap_cannot_publish_or_overwrite_target(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    source_graph.build_index(root, incremental=False)
    canonical = source_graph.resolve_db_path(root)
    original = canonical.read_bytes()
    target = tmp_path / "victim"
    target.write_bytes(b"untouched")
    real_probe = source_graph.probe_generation

    def swap_before_probe(staging_path, **kwargs):
        staging_path.unlink()
        staging_path.symlink_to(target)
        return real_probe(staging_path, **kwargs)

    monkeypatch.setattr(source_graph, "probe_generation", swap_before_probe)
    with pytest.raises((source_graph.SourceGraphError, sqlite3.Error)):
        source_graph.record_recommendation_roundtrip(root, {"ok": True})
    assert canonical.read_bytes() == original
    assert target.read_bytes() == b"untouched"


def test_recovery_writer_contention_is_retryable_and_preserves_journal(tmp_path):
    root = _repo(tmp_path)
    source_graph.build_index(root, incremental=False)
    journal = Path(f"{source_graph.resolve_db_path(root)}-journal")
    journal.write_bytes(b"another writer owns this sidecar")
    daemon = source_graph_daemon.SourceGraphDaemon(root)

    with source_graph.index_write_lease(root) as acquired:
        assert acquired
        result = daemon._recover_database()

    assert result["recovered"] is False
    assert result["retryable"] is True
    assert result["error"] == "writer_lease_held_by_other_process"
    assert journal.read_bytes() == b"another writer owns this sidecar"
