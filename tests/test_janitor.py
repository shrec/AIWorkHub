"""Automatic cleanup janitor: six isolated steps, NeedFix on failure, loss-free hints.

Ages are injected through ``_janitor_mtime`` and an explicit ``now`` rather than
``os.utime``: the worker sandbox denies timestamp mutation, and the 24-hour
guard must be testable there.
"""

from __future__ import annotations

import inspect
import json
import os
import threading
from pathlib import Path

import pytest

from aiworkhub import (
    core,
    needfix_store,
    process_launcher,
    storage_retention,
    task_store,
    terminal_log_retention,
    worker_workspace,
)

_HOUR = 3600.0
_NOW = 2_000_000_000.0
_FUTURE = "2099-01-01T00:00:00+00:00"
_STEPS = ["worktrees", "rework_deltas", "logs", "unattributed", "registrations", "records"]


def _stub_steps(monkeypatch, calls: list[str], **overrides) -> None:
    """Replace every janitor step with a recorder; ``overrides`` swap in others."""
    stubs = {
        "_janitor_worktrees": lambda root: calls.append("worktrees") or {"gc_scanned": 0},
        "_prune_decided_rework_deltas": (
            lambda root, now: calls.append("rework_deltas") or {"removed": 0}
        ),
        "_janitor_logs": lambda root: calls.append("logs") or {"ok": True},
        "_janitor_unattributed": (
            lambda root, base, now: calls.append("unattributed") or {"worktrees": {}}
        ),
        "_janitor_registrations": (
            lambda root, base: calls.append("registrations") or {"pruned": 0}
        ),
        "_janitor_records": lambda root: calls.append("records") or {"tasks": {}},
    }
    stubs.update(overrides)
    for name, stub in stubs.items():
        monkeypatch.setattr(storage_retention, name, stub)


def _janitor_needfix(repo: Path) -> list[dict]:
    return [
        row
        for row in needfix_store.list_needfix(repo, limit=500)
        if str(row["title"]).startswith("janitor:")
    ]


def _wait_until_idle(repo: Path) -> None:
    for _ in range(1000):
        if not storage_retention.repository_cleanup_status(repo)["running"]:
            return
        threading.Event().wait(0.01)
    raise AssertionError("janitor sweep did not finish")


# (a) step order, isolation and one deduplicated NeedFix per step/exception class
def test_failing_step_is_isolated_and_files_one_deduplicated_needfix(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    errors = iter([OSError("disk"), OSError("other text")])

    def failing_logs(root: Path) -> dict:
        calls.append("logs")
        raise next(errors)

    _stub_steps(monkeypatch, calls, _janitor_logs=failing_logs)

    first = storage_retention._run_repository_cleanup(tmp_path, base=tmp_path, now=_NOW)

    assert calls == _STEPS
    assert first["logs"] == {"ok": False, "error": "OSError: disk"}
    assert first["records"] == {"tasks": {}}
    assert first["ok"] is False
    rows = _janitor_needfix(tmp_path)
    assert [row["title"] for row in rows] == ["janitor:logs:OSError"]
    assert rows[0]["description"] == "Automatic cleanup step 'logs' failed."
    assert rows[0]["evidence"] == {"ok": False, "error": "OSError: disk"}
    assert first["needfix"] == [rows[0]["id"]]

    calls.clear()
    second = storage_retention._run_repository_cleanup(tmp_path, base=tmp_path, now=_NOW)

    assert calls == _STEPS
    assert second["logs"]["error"] == "OSError: other text"
    assert [row["id"] for row in _janitor_needfix(tmp_path)] == [rows[0]["id"]]
    assert second["needfix"] == [rows[0]["id"]]


# (b) GC failures become one NeedFix carrying the failure list
def test_rework_seal_failures_become_one_needfix(monkeypatch, tmp_path: Path) -> None:
    failures = [
        {
            "request_id": "req-1",
            "task_id": "T1",
            "reason": "rework_seal_failed:successful_rework_hash_mismatch",
        }
    ]
    _stub_steps(
        monkeypatch,
        [],
        _janitor_worktrees=lambda root: {
            "gc_scanned": 1, "gc_cleaned": 0, "gc_skipped": 1, "failures": failures,
        },
    )

    result = storage_retention._run_repository_cleanup(tmp_path, base=tmp_path, now=_NOW)
    storage_retention._run_repository_cleanup(tmp_path, base=tmp_path, now=_NOW)

    rows = _janitor_needfix(tmp_path)
    assert [row["title"] for row in rows] == ["janitor:worktrees:rework_seal_failed"]
    assert rows[0]["evidence"] == {"failures": failures}
    assert result["needfix"] == [rows[0]["id"]]
    assert result["ok"] is True


def _rework_delta_dir(monkeypatch, repo: Path) -> Path:
    monkeypatch.delenv(worker_workspace.RUNTIME_ROOT_ENV, raising=False)
    directory = worker_workspace.configured_runtime_root(repo) / "rework_deltas"
    directory.mkdir(parents=True)
    return directory


# (c) rework-delta prune keeps referenced and young files
def test_rework_delta_prune_keeps_referenced_and_young_files(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path.resolve()
    directory = _rework_delta_dir(monkeypatch, repo)
    ages = {
        f"{'a' * 64}.json": 25 * _HOUR,  # unreferenced, old -> removed
        f"{'b' * 64}.json": 25 * _HOUR,  # referenced -> kept
        f"{'c' * 64}.json": 1 * _HOUR,  # unreferenced, young -> kept
        ".rework-delta-x.tmp": 25 * _HOUR,  # interrupted seal, old -> removed
    }
    for name in ages:
        (directory / name).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        storage_retention, "_janitor_mtime", lambda path: _NOW - ages[path.name]
    )
    monkeypatch.setattr(task_store, "referenced_rework_delta_digests", lambda root: {"b" * 64})

    result = storage_retention._prune_decided_rework_deltas(repo, _NOW)

    assert sorted(path.name for path in directory.iterdir()) == [
        f"{'b' * 64}.json",
        f"{'c' * 64}.json",
    ]
    assert result == {
        "scanned": 4, "removed": 2, "bytes_freed": 4, "errors": [], "ok": True,
    }


def test_rework_delta_prune_parallel_result_equals_sequential(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(task_store, "referenced_rework_delta_digests", lambda root: set())
    monkeypatch.setattr(storage_retention, "_janitor_mtime", lambda path: _NOW - 30 * _HOUR)
    results = []
    for label, workers in (("parallel", None), ("sequential", 1)):
        repo = (tmp_path / label).resolve()
        repo.mkdir()
        directory = _rework_delta_dir(monkeypatch, repo)
        for index in range(12):
            (directory / f"{index:064x}.json").write_text("x" * index, encoding="utf-8")
        if workers is not None:
            monkeypatch.setattr(storage_retention, "_janitor_worker_count", lambda: workers)
        results.append(storage_retention._prune_decided_rework_deltas(repo, _NOW))
        assert list(directory.iterdir()) == []
    assert results[0] == results[1]
    assert results[0]["removed"] == 12


def _unattributed_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path.resolve() / "repo"
    base = repo / ".aiworkhub" / "runtime" / "worktrees"
    base.mkdir(parents=True)
    return repo, base


def _run_unattributed(monkeypatch, repo: Path, base: Path, ages: dict[str, float], ledger):
    removed: list[str] = []
    real_step = storage_retention._janitor_unattributed
    _stub_steps(monkeypatch, [], _janitor_unattributed=real_step)
    monkeypatch.setattr(storage_retention, "_ledger_latest_by_request", lambda root: ledger)
    monkeypatch.setattr(
        storage_retention, "_janitor_mtime", lambda path: _NOW - ages[path.name]
    )
    monkeypatch.setattr(
        storage_retention,
        "cleanup_workspace",
        lambda repo_root, path, home: removed.append(path.parent.name),
    )
    monkeypatch.setattr(
        storage_retention, "_purge_retention_batches", lambda root, base, now: {"purged": 0}
    )
    result = storage_retention._run_repository_cleanup(repo, base=base, now=_NOW)
    return result, removed


# (d) unattributed rule: only released directories older than 24 hours
def test_unattributed_rule_removes_only_old_released_directories(
    monkeypatch, tmp_path: Path
) -> None:
    repo, base = _unattributed_repo(tmp_path)
    terminal = sorted(process_launcher.TERMINAL_PROCESS_STATES)[0]
    ages = {
        "orphan-old": 25 * _HOUR,
        "orphan-young": 1 * _HOUR,
        "released-old": 25 * _HOUR,
        "retained-old": 25 * _HOUR,
        "processing-old": 25 * _HOUR,
        storage_retention.QUARANTINE_DIRNAME: 25 * _HOUR,
        ".hidden": 25 * _HOUR,
    }
    for name in ages:
        (base / name).mkdir()
    ledger = {
        "released-old": {"state": terminal, "workspace_retained": False},
        "retained-old": {"state": terminal, "workspace_retained": True},
        "processing-old": {"state": "processing"},
    }

    result, removed = _run_unattributed(monkeypatch, repo, base, ages, ledger)

    assert sorted(removed) == ["orphan-old", "released-old"]
    assert result["unattributed"]["worktrees"]["removed"] == ["orphan-old", "released-old"]
    assert result["unattributed"]["worktrees"]["kept"] == 3
    assert result["unattributed"]["worktrees"]["errors"] == []


def test_unattributed_rule_never_follows_a_link_out_of_the_root(
    monkeypatch, tmp_path: Path
) -> None:
    repo, base = _unattributed_repo(tmp_path)
    outside = tmp_path.resolve() / "outside-target"
    outside.mkdir()
    try:
        os.symlink(outside, base / "linked-old", target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("symlink privilege not held in this sandbox")
        raise

    result, removed = _run_unattributed(
        monkeypatch, repo, base, {"linked-old": 25 * _HOUR}, {}
    )

    assert removed == []
    assert result["unattributed"]["worktrees"]["scanned"] == 0
    assert outside.is_dir()


def test_unattributed_rule_skips_a_worktree_root_outside_the_repository(
    monkeypatch, tmp_path: Path
) -> None:
    repo, _inside = _unattributed_repo(tmp_path)
    shared = tmp_path.resolve() / "shared-worktrees"
    (shared / "orphan-old").mkdir(parents=True)

    result, removed = _run_unattributed(
        monkeypatch, repo, shared, {"orphan-old": 25 * _HOUR}, {}
    )

    assert result["unattributed"] == {"skipped": "shared_worktree_root"}
    assert removed == []
    assert (shared / "orphan-old").is_dir()


def test_unattributed_rule_skips_a_worktree_root_equal_to_the_repository(
    monkeypatch, tmp_path: Path
) -> None:
    repo, _inside = _unattributed_repo(tmp_path)
    (repo / "src").mkdir()
    monkeypatch.setattr(storage_retention, "_janitor_mtime", lambda path: _NOW - 25 * _HOUR)
    monkeypatch.setattr(
        storage_retention,
        "cleanup_workspace",
        lambda *args, **kwargs: pytest.fail("repository directories are never candidates"),
    )

    result = storage_retention._janitor_unattributed(repo, repo, _NOW)

    assert result == {"skipped": "worktree_root_is_repository"}
    assert (repo / "src").is_dir()


def _signed_batch(
    repo: Path, qroot: Path, batch_id: str, *, source: str | None
) -> tuple[Path, dict]:
    batch = qroot / batch_id
    batch.mkdir()
    (batch / "held.bin").write_bytes(b"x" * 5)
    manifest: dict = {
        "schema_id": storage_retention.SCHEMA_ID,
        "repo_id": "repo-test",
        "batch_id": batch_id,
        "created_at": "2026-09-01T00:00:00+00:00",
        "restore_deadline": _FUTURE,
        "preview_digest": "d" * 12,
        "status": "quarantined",
        "quarantined_bytes": 5,
        "items": [{"id": "held", "state": "quarantined", "size_bytes": 5}],
    }
    if source is not None:
        manifest["source"] = source
    manifest["deadline_authentication"] = storage_retention._manifest_authentication(
        repo, manifest
    )
    return batch, manifest


def _write_manifest(batch: Path, manifest: dict) -> None:
    (batch / storage_retention.MANIFEST_NAME).write_text(
        json.dumps(manifest), encoding="utf-8"
    )


# (e) sweep-made batches purge at once; legacy (source-less) and manual batches
#     keep the deadline; a tampered source fails authentication and is skipped
def test_batch_source_decides_purge_and_tampering_is_skipped(
    monkeypatch, tmp_path: Path
) -> None:
    repo, base = _unattributed_repo(tmp_path)
    monkeypatch.setattr(storage_retention, "_repo_id", lambda root: "repo-test")
    monkeypatch.setattr(storage_retention, "_append_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        storage_retention.worktree_storage, "_git", lambda *args, **kwargs: (0, "")
    )
    qroot = storage_retention._ensure_quarantine_root(repo, base)

    sweep, sweep_manifest = _signed_batch(repo, qroot, "q20260901T000000-sweep", source="sweep")
    legacy, legacy_manifest = _signed_batch(repo, qroot, "q20260901T000000-legacy", source=None)
    manual, manual_manifest = _signed_batch(repo, qroot, "q20260901T000000-manual", source="manual")
    tampered, tampered_manifest = _signed_batch(
        repo, qroot, "q20260901T000000-tampered", source="manual"
    )
    # Legacy manifests (no source) still authenticate; stripping or editing a
    # signed source does not.
    assert storage_retention._authenticated_manifest(repo, sweep_manifest)
    assert storage_retention._authenticated_manifest(repo, legacy_manifest)
    assert storage_retention._authenticated_manifest(repo, manual_manifest)
    edited = dict(tampered_manifest, source="sweep")
    assert not storage_retention._authenticated_manifest(repo, edited)
    tampered_manifest.pop("source")
    assert not storage_retention._authenticated_manifest(repo, tampered_manifest)
    for batch, manifest in (
        (sweep, sweep_manifest),
        (legacy, legacy_manifest),
        (manual, manual_manifest),
        (tampered, tampered_manifest),
    ):
        _write_manifest(batch, manifest)

    real_purge = storage_retention._purge_batch
    purge_calls: list[tuple[str, bool]] = []

    def spy(*args, **kwargs):
        purge_calls.append((kwargs["batch_id"], kwargs["ignore_deadline"]))
        return real_purge(*args, **kwargs)

    monkeypatch.setattr(storage_retention, "_purge_batch", spy)

    result = storage_retention._purge_retention_batches(repo, base, _NOW)

    # Only the sweep-made batch loses its undo window; a legacy batch with a
    # live deadline and held items keeps it, like a manual one.
    assert purge_calls == [(sweep.name, True)]
    assert not sweep.exists()
    assert legacy.is_dir()
    assert manual.is_dir()
    assert tampered.is_dir()
    assert result == {
        "purged": 1,
        "bytes_freed": 5,
        "kept": 2,
        "skipped": 1,
        "next_deadline": _FUTURE,
    }


def test_unclassified_gc_failures_become_one_needfix(monkeypatch, tmp_path: Path) -> None:
    failures = [{"request_id": "req-1", "task_id": "T1", "reason": "cleanup_failed"}]
    _stub_steps(
        monkeypatch,
        [],
        _janitor_worktrees=lambda root: {"gc_scanned": 1, "failures": failures},
    )

    result = storage_retention._run_repository_cleanup(tmp_path, base=tmp_path, now=_NOW)

    rows = _janitor_needfix(tmp_path)
    assert [row["title"] for row in rows] == ["janitor:worktrees:unclassified"]
    assert rows[0]["evidence"] == {"failures": failures}
    assert result["needfix"] == [rows[0]["id"]]


def test_quarantine_stamps_a_signed_manual_source(monkeypatch, tmp_path: Path) -> None:
    repo, base = _unattributed_repo(tmp_path)
    monkeypatch.setattr(storage_retention, "_repo_id", lambda root: "repo-test")
    monkeypatch.setattr(storage_retention, "_measure_key", lambda *args: "measure-key")
    monkeypatch.setattr(
        storage_retention,
        "_measure_within_deadline",
        lambda key, measure, deadline: (
            {
                "preview_digest": "d" * 64,
                "candidates": [
                    {"id": "bad id!", "size_bytes": 1, "modified_at_epoch": 0, "head": ""}
                ],
            },
            True,
        ),
    )
    monkeypatch.setattr(storage_retention.worktree_storage, "_git_common_dir", lambda root: "")
    monkeypatch.setattr(storage_retention, "_append_audit", lambda *args, **kwargs: None)
    written: list[dict] = []
    monkeypatch.setattr(
        storage_retention, "_atomic_json", lambda path, value: written.append(dict(value))
    )

    storage_retention.quarantine(repo, preview_digest="d" * 64, confirm=True, base=base)

    assert written and written[0]["source"] == "manual"
    assert storage_retention._authenticated_manifest(repo, written[0])


# (f) a hint during an active run is not lost: exactly one rerun follows
def test_hint_during_an_active_run_yields_exactly_one_rerun(
    monkeypatch, tmp_path: Path
) -> None:
    entered = threading.Event()
    release = threading.Event()
    runs: list[int] = []

    def cleanup(repo_root, *, base=None):
        runs.append(len(runs) + 1)
        if len(runs) == 1:
            entered.set()
            release.wait(5)
        return {"ok": True}

    monkeypatch.setattr(storage_retention, "run_repository_cleanup", cleanup)
    monkeypatch.setattr(storage_retention, "lock_fd", lambda fd, *, blocking: None)
    monkeypatch.setattr(storage_retention, "unlock_fd", lambda fd: None)

    assert storage_retention.schedule_repository_cleanup(tmp_path) is True
    assert entered.wait(5)
    assert storage_retention.schedule_repository_cleanup(tmp_path) is False
    assert storage_retention.schedule_repository_cleanup(tmp_path) is False
    release.set()
    _wait_until_idle(tmp_path)

    assert runs == [1, 2]


# (g) no scheduling from inside a janitor step
def test_hint_raised_inside_a_janitor_step_schedules_nothing(
    monkeypatch, tmp_path: Path
) -> None:
    inner: list[bool] = []
    swept: list[Path] = []
    monkeypatch.setattr(
        storage_retention,
        "run_repository_cleanup",
        lambda repo_root, *, base=None: swept.append(repo_root) or {"ok": True},
    )

    def logs_step(root: Path) -> dict:
        inner.append(storage_retention.schedule_repository_cleanup(root))
        return {"ok": True}

    _stub_steps(monkeypatch, [], _janitor_logs=logs_step)

    storage_retention._run_repository_cleanup(tmp_path, base=tmp_path, now=_NOW)

    assert inner == [False]
    assert storage_retention.repository_cleanup_status(tmp_path)["running"] is False
    assert swept == []
    assert getattr(storage_retention._JANITOR_ACTIVE, "active", False) is False


# (h) mark_done reaches the janitor; the private GC thread is gone
def test_decisions_reach_the_janitor_not_a_private_gc_thread(
    monkeypatch, tmp_path: Path
) -> None:
    scheduled: list[Path] = []
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        storage_retention,
        "schedule_repository_cleanup",
        lambda root: scheduled.append(root) or True,
    )

    result = core._reconcile_retained_workspaces({"ok": True})

    assert result["workspace_retention"] == {"ok": True, "queued": True, "mode": "janitor"}
    assert result["workspace_retention"]["mode"] == "janitor"
    assert scheduled == [tmp_path.resolve()]
    assert "_reconcile_retained_workspaces(result)" in inspect.getsource(core.mark_done)
    assert not hasattr(core, "_WORKSPACE_GC_JOBS")
    assert not hasattr(core, "_WORKSPACE_GC_JOBS_LOCK")
    assert "workspace_retention" not in core._reconcile_retained_workspaces({"ok": False})


def test_a_scheduling_failure_never_fails_a_committed_decision(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)

    def boom(root: Path) -> bool:
        raise RuntimeError("thread start refused")

    monkeypatch.setattr(storage_retention, "schedule_repository_cleanup", boom)

    result = core._reconcile_retained_workspaces({"ok": True})

    assert result["ok"] is True
    assert result["workspace_retention"]["ok"] is False
    assert result["workspace_retention"]["queued"] is False
    assert result["workspace_retention"]["error"] == "RuntimeError: thread start refused"


def test_janitor_logs_step_calls_terminal_log_retention(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(terminal_log_retention, "enforce", lambda root: {"deleted": 3})
    assert storage_retention._janitor_logs(tmp_path) == {"deleted": 3}


def _mtime_vanishing(name: str):
    def mtime(path: Path) -> float:
        if path.name == name:
            raise FileNotFoundError(path)
        return _NOW - 25 * _HOUR

    return mtime


def test_a_rework_delta_vanishing_mid_scan_is_skipped(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path.resolve()
    directory = _rework_delta_dir(monkeypatch, repo)
    gone, aged = f"{'a' * 64}.json", f"{'b' * 64}.json"
    for name in (gone, aged):
        (directory / name).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(storage_retention, "_janitor_mtime", _mtime_vanishing(gone))
    monkeypatch.setattr(task_store, "referenced_rework_delta_digests", lambda root: set())

    result = storage_retention._prune_decided_rework_deltas(repo, _NOW)

    assert [path.name for path in directory.iterdir()] == [gone]
    assert result["removed"] == 1
    assert result["ok"] is True


def test_a_worktree_vanishing_mid_scan_is_skipped(monkeypatch, tmp_path: Path) -> None:
    repo, base = _unattributed_repo(tmp_path)
    for name in ("vanished-old", "orphan-old"):
        (base / name).mkdir()
    removed: list[str] = []
    monkeypatch.setattr(storage_retention, "_ledger_latest_by_request", lambda root: {})
    monkeypatch.setattr(storage_retention, "_janitor_mtime", _mtime_vanishing("vanished-old"))
    monkeypatch.setattr(
        storage_retention,
        "cleanup_workspace",
        lambda repo_root, path, home: removed.append(path.parent.name),
    )

    result = storage_retention._remove_unattributed_worktrees(repo, base, _NOW)

    assert removed == ["orphan-old"]
    assert result["removed"] == ["orphan-old"]
    assert result["errors"] == []


def test_per_item_delete_failures_fail_the_sweep_and_file_one_needfix(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path.resolve()
    directory = _rework_delta_dir(monkeypatch, repo)
    (directory / f"{'a' * 64}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(storage_retention, "_janitor_mtime", lambda path: _NOW - 25 * _HOUR)
    monkeypatch.setattr(task_store, "referenced_rework_delta_digests", lambda root: set())
    monkeypatch.setattr(
        storage_retention,
        "_janitor_parallel",
        lambda targets, fn: [(targets[0], "PermissionError: denied")],
    )
    real_prune = storage_retention._prune_decided_rework_deltas
    _stub_steps(monkeypatch, [], _prune_decided_rework_deltas=real_prune)

    first = storage_retention._run_repository_cleanup(repo, base=repo, now=_NOW)
    second = storage_retention._run_repository_cleanup(repo, base=repo, now=_NOW)

    assert first["ok"] is False
    assert first["rework_deltas"]["ok"] is False
    rows = _janitor_needfix(repo)
    assert [row["title"] for row in rows] == ["janitor:rework_deltas:delete_failed"]
    assert rows[0]["evidence"] == {"errors": ["PermissionError: denied"]}
    assert first["needfix"] == second["needfix"] == [rows[0]["id"]]


def test_a_thread_that_fails_to_start_releases_the_running_marker(
    monkeypatch, tmp_path: Path
) -> None:
    swept: list[Path] = []
    monkeypatch.setattr(
        storage_retention,
        "run_repository_cleanup",
        lambda repo_root, *, base=None: swept.append(repo_root) or {"ok": True},
    )
    monkeypatch.setattr(storage_retention, "lock_fd", lambda fd, *, blocking: None)
    monkeypatch.setattr(storage_retention, "unlock_fd", lambda fd: None)
    real_start = threading.Thread.start
    refused: list[str] = []

    def start(self: threading.Thread) -> None:
        if not refused:
            refused.append(self.name)
            raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)

    with pytest.raises(RuntimeError):
        storage_retention.schedule_repository_cleanup(tmp_path)
    assert storage_retention.repository_cleanup_status(tmp_path)["running"] is False
    assert storage_retention.schedule_repository_cleanup(tmp_path) is True
    _wait_until_idle(tmp_path)

    assert refused == ["aiworkhub-storage-retention"]
    assert swept == [tmp_path.resolve()]
