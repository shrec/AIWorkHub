"""NF-2026-01449: the janitor's seventh step, ``scratch``, and the shared
dead-owner-temp removal path it shares with ``terminal_log_retention``.

Ages are injected through ``storage_retention._janitor_mtime`` rather than
``os.utime``, matching the convention already used by ``tests/test_janitor.py``:
the worker sandbox denies timestamp mutation. The read-only-file tests below
use real ``os.chmod`` because that is the exact scenario under test (the
shared removal helper repairing a read-only leftover); if the sandbox denies
``chmod`` those two tests error here and are decided by the coordinator's
canonical validation instead.
"""

from __future__ import annotations

import json
import os
import pytest
import stat
from pathlib import Path

from aiworkhub import (
    needfix_store,
    runtime_temp,
    storage_retention,
    task_store,
    terminal_log_retention,
    worker_workspace,
)

_HOUR = 3600.0
_NOW = 2_000_000_000.0


def _stub_non_scratch_steps(monkeypatch) -> None:
    """Stub the six pre-existing janitor steps so only ``scratch`` is live."""
    monkeypatch.setattr(storage_retention, "_janitor_worktrees", lambda root: {"gc_scanned": 0})
    monkeypatch.setattr(
        storage_retention, "_prune_decided_rework_deltas", lambda root, now: {"removed": 0}
    )
    monkeypatch.setattr(storage_retention, "_janitor_logs", lambda root: {"ok": True})
    monkeypatch.setattr(
        storage_retention,
        "_janitor_unattributed",
        lambda root, base, now: {"worktrees": {}},
    )
    monkeypatch.setattr(
        storage_retention, "_janitor_registrations", lambda root, base: {"pruned": 0}
    )
    monkeypatch.setattr(storage_retention, "_janitor_records", lambda root: {"tasks": {}})


def _scratch_needfix(repo: Path) -> list[dict]:
    return [
        row
        for row in needfix_store.list_needfix(repo, limit=500)
        if str(row["title"]).startswith("janitor:scratch:")
    ]


def test_container_children_removed_when_aged_kept_when_young(
    monkeypatch, tmp_path: Path
) -> None:
    containers = [
        tmp_path / ".aiworkhub" / "temp" / "test",
        tmp_path / ".aiworkhub" / "temp" / "validation",
        tmp_path / ".aiworkhub" / "runtime" / "node-test-scratch",
    ]
    old_children: list[Path] = []
    young_children: list[Path] = []
    for container in containers:
        old_child = container / "old-run"
        young_child = container / "young-run"
        old_child.mkdir(parents=True)
        young_child.mkdir(parents=True)
        (old_child / "f.txt").write_text("x")
        (young_child / "f.txt").write_text("x")
        old_children.append(old_child)
        young_children.append(young_child)

    monkeypatch.setattr(
        storage_retention,
        "_janitor_mtime",
        lambda path: _NOW - (47 * _HOUR if path.name == "young-run" else 49 * _HOUR),
    )

    result = storage_retention._janitor_scratch(tmp_path.resolve(), _NOW)

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["removed"] == 3
    for old_child in old_children:
        assert not old_child.exists()
    for young_child in young_children:
        assert young_child.exists()


def test_oneoff_runtime_dirs_matched_by_name_others_kept(
    monkeypatch, tmp_path: Path
) -> None:
    runtime = tmp_path / ".aiworkhub" / "runtime"
    removed_names = ["nf1282-x", "hostval"]
    kept_names = ["opencode-compat-2016-v1", "tmpabc"]
    for name in removed_names + kept_names:
        (runtime / name).mkdir(parents=True)
    worktrees_child = runtime / "worktrees" / "some-tree"
    worktrees_child.mkdir(parents=True)
    worker_child = tmp_path / ".aiworkhub" / "temp" / "worker" / "req-1"
    worker_child.mkdir(parents=True)

    monkeypatch.setattr(storage_retention, "_janitor_mtime", lambda path: _NOW - 49 * _HOUR)

    result = storage_retention._janitor_scratch(tmp_path.resolve(), _NOW)

    assert result["ok"] is True
    assert result["removed"] == len(removed_names)
    for name in removed_names:
        assert not (runtime / name).exists()
    for name in kept_names:
        assert (runtime / name).exists()
    assert worktrees_child.exists()
    assert worker_child.exists()


def test_live_owner_candidate_is_kept(monkeypatch, tmp_path: Path) -> None:
    candidate = tmp_path / ".aiworkhub" / "runtime" / "nf9999-owned"
    candidate.mkdir(parents=True)

    monkeypatch.setattr(storage_retention, "_janitor_mtime", lambda path: _NOW - 49 * _HOUR)
    monkeypatch.setattr(
        runtime_temp,
        "read_owner_manifest",
        lambda path: {"schema_id": "aiworkhub.runtime_temp_owner.v1", "pid": 4242, "starttime": 1},
    )
    monkeypatch.setattr(runtime_temp, "owner_alive", lambda manifest: True)

    result = storage_retention._janitor_scratch(tmp_path.resolve(), _NOW)

    assert result == {"ok": True, "removed": 0, "bytes": 0, "errors": []}
    assert candidate.exists()


def test_scratch_step_removes_candidate_with_read_only_file(
    monkeypatch, tmp_path: Path
) -> None:
    candidate = tmp_path / ".aiworkhub" / "runtime" / "nf1111-ro"
    candidate.mkdir(parents=True)
    locked = candidate / "locked.txt"
    locked.write_text("protected")
    os.chmod(locked, stat.S_IREAD)

    monkeypatch.setattr(storage_retention, "_janitor_mtime", lambda path: _NOW - 49 * _HOUR)

    result = storage_retention._janitor_scratch(tmp_path.resolve(), _NOW)

    assert result == {"ok": True, "removed": 1, "bytes": 0, "errors": []}
    assert not candidate.exists()


def test_dead_owner_temp_gc_removes_read_only_file(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path.resolve()
    dead_dir = repo / ".aiworkhub" / "temp" / "worker" / "req-dead"
    dead_dir.mkdir(parents=True)
    locked = dead_dir / "locked.txt"
    locked.write_text("protected")
    os.chmod(locked, stat.S_IREAD)

    monkeypatch.setattr(
        runtime_temp,
        "identify_dead_owner_dirs",
        lambda root: [{"path": dead_dir, "bytes": 9}],
    )

    count, released, failures = terminal_log_retention._dead_owner_temp_gc(repo)

    assert (count, released, failures) == (1, 9, [])
    assert not dead_dir.exists()


def test_dead_owner_temp_gc_reports_failure_instead_of_swallowing(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path.resolve()
    broken = repo / ".aiworkhub" / "temp" / "worker" / "req-broken"
    broken.mkdir(parents=True)

    monkeypatch.setattr(
        runtime_temp,
        "identify_dead_owner_dirs",
        lambda root: [{"path": broken, "bytes": 5}],
    )

    def fail(path: Path) -> None:
        raise OSError("locked")

    monkeypatch.setattr(worker_workspace, "_rmtree_workspace_owned", fail)

    count, released, failures = terminal_log_retention._dead_owner_temp_gc(repo)

    assert count == 0
    assert released == 0
    assert len(failures) == 1
    assert "req-broken" in failures[0]
    assert broken.exists()


def test_scratch_removal_failure_reports_errors_and_files_one_needfix(
    monkeypatch, tmp_path: Path
) -> None:
    _stub_non_scratch_steps(monkeypatch)
    repo = tmp_path.resolve()
    runtime = repo / ".aiworkhub" / "runtime"
    failing = runtime / "nf2222-a"
    ok = runtime / "nf2222-b"
    failing.mkdir(parents=True)
    ok.mkdir(parents=True)

    monkeypatch.setattr(storage_retention, "_janitor_mtime", lambda path: _NOW - 49 * _HOUR)

    removed: list[str] = []

    def fake_rmtree(path: Path) -> None:
        if path.name == failing.name:
            raise OSError("boom")
        removed.append(path.name)

    monkeypatch.setattr(worker_workspace, "_rmtree_workspace_owned", fake_rmtree)

    result = storage_retention._run_repository_cleanup(repo, base=repo, now=_NOW)

    assert result["scratch"]["ok"] is False
    assert removed == [ok.name]
    assert any(failing.name in error for error in result["scratch"]["errors"])
    assert result["ok"] is False

    rows = _scratch_needfix(repo)
    assert [row["title"] for row in rows] == ["janitor:scratch:delete_failed"]
    assert result["needfix"] == [rows[0]["id"]]


def test_enforce_exposes_temp_gc_errors(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path.resolve()
    assert task_store.initialize_repository(repo)["ok"]
    monkeypatch.setattr(
        terminal_log_retention,
        "_dead_owner_temp_gc",
        lambda root: (0, 0, ["req-x: OSError: boom"]),
    )

    result = terminal_log_retention.enforce(repo)

    assert result["temp_gc_errors"] == ["req-x: OSError: boom"]


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows-only reparse point")
def test_container_junction_escape_is_not_followed(monkeypatch, tmp_path: Path) -> None:
    import _winapi

    repo = tmp_path.resolve()
    worktrees = repo / ".aiworkhub" / "runtime" / "worktrees"
    child = worktrees / "some-tree"
    child.mkdir(parents=True)
    temp_test = repo / ".aiworkhub" / "temp" / "test"
    temp_test.parent.mkdir(parents=True)
    _winapi.CreateJunction(str(worktrees), str(temp_test))

    monkeypatch.setattr(storage_retention, "_janitor_mtime", lambda path: _NOW - 49 * _HOUR)

    result = storage_retention._janitor_scratch(repo, _NOW)

    assert result["removed"] == 0
    assert child.exists()


def test_container_regular_file_candidate_is_removed(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path.resolve()
    container = repo / ".aiworkhub" / "temp" / "test"
    container.mkdir(parents=True)
    stray_file = container / "stray.log"
    stray_file.write_text("x")

    monkeypatch.setattr(storage_retention, "_janitor_mtime", lambda path: _NOW - 49 * _HOUR)

    result = storage_retention._janitor_scratch(repo, _NOW)

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["removed"] == 1
    assert not stray_file.exists()


def test_candidate_vanishing_before_age_check_is_skipped(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path.resolve()
    container = repo / ".aiworkhub" / "temp" / "test"
    kept = container / "a"
    kept.mkdir(parents=True)
    gone = container / "b"
    gone.mkdir()
    real_mtime = storage_retention._janitor_mtime

    def vanishing_mtime(path: Path) -> float:
        if path.name == "b":
            gone.rmdir()
            return real_mtime(path)
        return _NOW - 49 * _HOUR

    monkeypatch.setattr(storage_retention, "_janitor_mtime", vanishing_mtime)

    result = storage_retention._janitor_scratch(repo, _NOW)

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["removed"] == 1
    assert not kept.exists()


def test_candidate_failing_pre_deletion_recheck_is_kept_and_not_counted(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path.resolve()
    swapped = repo / ".aiworkhub" / "temp" / "test" / "swapped"
    swapped.mkdir(parents=True)
    monkeypatch.setattr(storage_retention, "_janitor_mtime", lambda path: _NOW - 49 * _HOUR)
    real_is_junction = Path.is_junction
    checks: list[Path] = []

    def junction_after_selection(self: Path) -> bool:
        if self.name == "swapped":
            checks.append(self)
            return len(checks) > 1
        return real_is_junction(self)

    monkeypatch.setattr(Path, "is_junction", junction_after_selection)

    result = storage_retention._janitor_scratch(repo, _NOW)

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["removed"] == 0
    assert swapped.exists()


def test_read_only_file_candidate_is_repaired_and_removed(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path.resolve()
    container = repo / ".aiworkhub" / "temp" / "test"
    container.mkdir(parents=True)
    read_only = container / "ro.bin"
    read_only.write_bytes(b"x")
    os.chmod(read_only, stat.S_IREAD)
    monkeypatch.setattr(storage_retention, "_janitor_mtime", lambda path: _NOW - 49 * _HOUR)

    result = storage_retention._janitor_scratch(repo, _NOW)

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["removed"] == 1
    assert not read_only.exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_fifo_candidate_is_removed(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path.resolve()
    container = repo / ".aiworkhub" / "temp" / "test"
    container.mkdir(parents=True)
    fifo = container / "pipe"
    os.mkfifo(fifo)
    monkeypatch.setattr(storage_retention, "_janitor_mtime", lambda path: _NOW - 49 * _HOUR)

    result = storage_retention._janitor_scratch(repo, _NOW)

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["removed"] == 1
    assert not fifo.exists()


@pytest.mark.skipif(os.name != "nt", reason="extended-length prefix retry is Windows-only")
def test_short_path_removal_failure_retries_with_extended_length_prefix(
    monkeypatch, tmp_path: Path
) -> None:
    target = tmp_path / "short"
    target.mkdir()

    calls: list[Path] = []

    def fake_rmtree(path, *args, **kwargs):
        calls.append(Path(path))
        if len(calls) == 1:
            raise OSError("boom")

    monkeypatch.setattr(worker_workspace.shutil, "rmtree", fake_rmtree)

    worker_workspace._rmtree_workspace_owned(target)

    assert len(calls) == 2
    assert str(calls[1]).startswith("\\\\?\\")


_COMPAT = "opencode-compat-2016-v1"
_REGISTERED_RELATIVE = f".aiworkhub/runtime/{_COMPAT}/artifact-win-x64-v4/opencode.exe"
_BACKUP_RELATIVE = f"{_COMPAT}/artifact-win-x64-v3/opencode.exe"


def _registration_document(*relatives: str) -> str:
    return json.dumps(
        {
            "schema": "aiworkhub.executable_registration.v1",
            "registrations": {
                f"adapter_{index}": {"executable_relative": relative}
                for index, relative in enumerate(relatives)
            },
        }
    )


def _compat_workspace(repo: Path, *, registered: bool = True) -> dict[str, Path]:
    runtime = repo / ".aiworkhub" / "runtime"
    compat = runtime / _COMPAT
    paths = {
        "compat": compat,
        "bun_cache": compat / "tools" / "bun-cache",
        "node_modules": compat / "source" / "node_modules",
        "manifest": compat / "compat-build-manifest.json",
        "source_file": compat / "source" / "package.json",
        "registered": compat / "artifact-win-x64-v4",
        "backup": compat / "artifact-win-x64-v3",
        "unregistered": compat / "artifact-win-x64-v1",
    }
    for key in ("bun_cache", "node_modules", "registered", "backup", "unregistered"):
        paths[key].mkdir(parents=True)
        (paths[key] / "opencode.exe").write_text("x")
    paths["manifest"].write_text("{}")
    paths["source_file"].write_text("{}")
    if registered:
        (runtime / "executable_registration.json").write_text(
            _registration_document(_REGISTERED_RELATIVE)
        )
        (runtime / "executable_registration.20261019.backup.json").write_text(
            _registration_document(_BACKUP_RELATIVE)
        )
    return paths


def _age_all(monkeypatch, fresh: tuple[Path, ...] = ()) -> None:
    monkeypatch.setattr(
        storage_retention,
        "_janitor_mtime",
        lambda path: _NOW - (47 * _HOUR if path in fresh else 49 * _HOUR),
    )


def test_compat_aged_caches_and_unregistered_artifact_removed_registered_kept(
    monkeypatch, tmp_path: Path
) -> None:
    paths = _compat_workspace(tmp_path.resolve())
    _age_all(monkeypatch)

    result = storage_retention._janitor_scratch(tmp_path.resolve(), _NOW)

    assert result == {"ok": True, "removed": 3, "bytes": 0, "errors": []}
    assert not paths["bun_cache"].exists()
    assert not paths["node_modules"].exists()
    assert not paths["unregistered"].exists()
    assert paths["registered"].joinpath("opencode.exe").exists()
    assert paths["backup"].joinpath("opencode.exe").exists()
    assert paths["compat"].is_dir()
    assert paths["manifest"].exists()
    assert paths["source_file"].exists()
    assert paths["source_file"].parent.is_dir()


def test_compat_fresh_caches_and_artifact_are_kept(monkeypatch, tmp_path: Path) -> None:
    paths = _compat_workspace(tmp_path.resolve())
    _age_all(
        monkeypatch,
        fresh=(paths["bun_cache"], paths["node_modules"], paths["unregistered"]),
    )

    result = storage_retention._janitor_scratch(tmp_path.resolve(), _NOW)

    assert result == {"ok": True, "removed": 0, "bytes": 0, "errors": []}
    for key in ("bun_cache", "node_modules", "unregistered", "registered", "backup"):
        assert paths[key].exists()


def test_compat_missing_registration_keeps_every_artifact(
    monkeypatch, tmp_path: Path
) -> None:
    paths = _compat_workspace(tmp_path.resolve(), registered=False)
    _age_all(monkeypatch)

    result = storage_retention._janitor_scratch(tmp_path.resolve(), _NOW)

    assert result == {"ok": True, "removed": 2, "bytes": 0, "errors": []}
    assert not paths["bun_cache"].exists()
    assert not paths["node_modules"].exists()
    for key in ("registered", "backup", "unregistered"):
        assert paths[key].exists()
    assert paths["manifest"].exists()
    assert paths["source_file"].exists()


@pytest.mark.parametrize(
    "registration_text",
    [
        "{not json",
        json.dumps({"schema": "aiworkhub.executable_registration.v1", "registrations": {}}),
        json.dumps({"schema": "aiworkhub.executable_registration.v1", "registrations": []}),
        json.dumps({"registrations": {"a": {"executable_relative": 7}}}),
    ],
)
def test_compat_unreadable_or_empty_registration_keeps_every_artifact(
    monkeypatch, tmp_path: Path, registration_text: str
) -> None:
    paths = _compat_workspace(tmp_path.resolve())
    (tmp_path / ".aiworkhub" / "runtime" / "executable_registration.json").write_text(
        registration_text
    )
    _age_all(monkeypatch)

    result = storage_retention._janitor_scratch(tmp_path.resolve(), _NOW)

    assert result["ok"] is True
    assert result["removed"] == 2
    for key in ("registered", "backup", "unregistered"):
        assert paths[key].exists()


def test_compat_unreadable_backup_registration_keeps_every_artifact(
    monkeypatch, tmp_path: Path
) -> None:
    paths = _compat_workspace(tmp_path.resolve())
    (
        tmp_path / ".aiworkhub" / "runtime" / "executable_registration.20261019.backup.json"
    ).write_text("{not json")
    _age_all(monkeypatch)

    result = storage_retention._janitor_scratch(tmp_path.resolve(), _NOW)

    assert result["removed"] == 2
    for key in ("registered", "backup", "unregistered"):
        assert paths[key].exists()
