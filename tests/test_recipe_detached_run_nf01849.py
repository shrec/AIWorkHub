"""Detached host-run mode for persisted recipes (NF-2026-01849)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from aiworkhub import platform_io
from aiworkhub import recipe_runner as rr
from aiworkhub import tool_recipes as tr
from aiworkhub import tool_recipes_store as store

_PROBE_SOURCE = 'import sys\nsys.stdout.write("ok\\n")\n'
_REAL_POPEN = subprocess.Popen


def _recipe(*, max_memory_mb: int | None = None) -> tr.Recipe:
    return tr.Recipe(
        id="example.detached",
        version="1.0.0",
        purpose="A runnable probe used only by these tests.",
        task_kind=tr.TaskKind.SCAN,
        parameters=(
            tr.ParamSpec(
                name="mode",
                type=tr.ParamType.ENUM,
                required=True,
                values=("ok", "other"),
            ),
        ),
        outputs=(tr.OutputSpec(name="report", type=tr.OutputType.STDOUT),),
        resource_bounds=tr.ResourceBounds(max_memory_mb=max_memory_mb),
        argv=(tr.lit("python"), tr.lit("probe.py"), tr.slot("mode")),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "probe.py").write_text(_PROBE_SOURCE, encoding="utf-8")
    return tmp_path


@pytest.fixture
def install(repo: Path):
    def _install(**kwargs) -> tr.Recipe:
        recipe = _recipe(**kwargs)
        store.put_recipe(repo, recipe)
        return recipe

    return _install


class _FakeProcess:
    pid = 0


@pytest.fixture
def no_spawn(monkeypatch):
    spawned: list[object] = []

    def fake_popen(*args, **kwargs):
        spawned.append(args)
        return _FakeProcess()

    monkeypatch.setattr(rr.subprocess, "Popen", fake_popen)
    return spawned


@pytest.mark.parametrize(
    "bad", ["", "abc", "A" * 32, "g" * 32, "0" * 31, "0" * 33, "../" + "0" * 29]
)
def test_run_id_must_be_32_lowercase_hex(repo, bad):
    with pytest.raises(rr.RecipeRunError) as caught:
        rr.read_detached_status(repo, bad)
    assert caught.value.reason == rr.REASON_UNKNOWN_RUN


def test_unknown_run_id_is_refused(repo):
    with pytest.raises(rr.RecipeRunError) as caught:
        rr.read_detached_status(repo, "0" * 32)
    assert caught.value.reason == rr.REASON_UNKNOWN_RUN


def test_bad_param_refuses_synchronously_and_spawns_nothing(repo, install, no_spawn):
    recipe = install()
    with pytest.raises(tr.RecipeError):
        rr.start_detached_run(repo, recipe, {"mode": "not-a-value"})
    assert no_spawn == []
    runs = repo / ".aiworkhub" / "runtime" / "recipe_runs"
    assert not runs.exists() or not list(runs.glob("*.status.json"))


def test_host_end_to_end_reaches_completed(repo, install):
    recipe = install()
    started = rr.start_detached_run(repo, recipe, {"mode": "ok"})
    assert started["state"] == "queued"
    assert len(started["run_id"]) == 32

    record = rr.read_detached_status(repo, started["run_id"], wait_seconds=60)

    assert record["state"] == "completed", record
    assert record["digest"]["returncode"] == 0
    assert Path(record["digest"]["output_path"]).exists()
    assert record["child_log_path"] == started["child_log_path"]


def test_memory_probe_unavailable_refuses_only_a_memory_bounded_recipe(
    repo, install, no_spawn, monkeypatch
):
    monkeypatch.setattr(platform_io, "available_memory_bytes", lambda *a, **k: None)

    heavy = install(max_memory_mb=64)
    started = rr.start_detached_run(repo, heavy, {"mode": "ok"})
    assert rr._detached_main(["--detached-run", started["status_path"]]) == 0
    refused = rr.read_detached_status(repo, started["run_id"])
    assert refused["state"] == "refused"
    assert refused["reason"] == rr.REASON_MEMORY_PROBE_UNAVAILABLE

    light_repo_recipe = _recipe()
    assert light_repo_recipe.digest != heavy.digest
    # A recipe without max_memory_mb must not consult the probe at all.
    other = repo / "light"
    other.mkdir()
    (other / "probe.py").write_text(_PROBE_SOURCE, encoding="utf-8")
    store.put_recipe(other, light_repo_recipe)
    light = rr.start_detached_run(other, light_repo_recipe, {"mode": "ok"})
    monkeypatch.setattr(rr.subprocess, "Popen", _REAL_POPEN)
    assert rr._detached_main([light["status_path"]]) == 0
    done = rr.read_detached_status(other, light["run_id"])
    assert done["state"] == "completed", done
    assert done["digest"]["returncode"] == 0


def test_actor_round_trips_through_the_status_record(repo, install, no_spawn, monkeypatch):
    recipe = install()
    actor = tr.ActorIdentity(kind="manager", provider="claude", session_id="s1")
    started = rr.start_detached_run(repo, recipe, {"mode": "ok"}, actor=actor)
    monkeypatch.setattr(rr.subprocess, "Popen", _REAL_POPEN)
    assert rr._detached_main([started["status_path"]]) == 0
    record = rr.read_detached_status(repo, started["run_id"], wait_seconds=60)
    assert record["state"] == "completed", record
    assert record["digest"]["returncode"] == 0


def test_dead_pid_on_a_non_terminal_record_reads_as_lost(repo):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    run_id = "a" * 32
    path = rr._status_path(repo, run_id)
    rr._write_status(
        path, {"schema_id": rr.DETACHED_SCHEMA_ID, "run_id": run_id, "state": "running", "pid": child.pid}
    )

    record = rr.read_detached_status(repo, run_id)

    assert record["state"] == "lost"
    assert rr._load_status(path)["state"] == "running"


def test_a_held_host_run_lock_keeps_the_run_queued_until_released(repo, install):
    recipe = install()
    lock_path = repo / ".aiworkhub" / "runtime" / "recipe_runs" / "host-run.lock"
    fd = platform_io.open_lock_file(lock_path)
    platform_io.lock_fd(fd, blocking=True)
    released = False
    try:
        started = rr.start_detached_run(repo, recipe, {"mode": "ok"})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            record = rr.read_detached_status(repo, started["run_id"])
            if record.get("pid"):
                break
            time.sleep(0.2)
        assert record.get("pid"), record
        time.sleep(1.5)
        held = rr.read_detached_status(repo, started["run_id"])
        assert held["state"] == "queued", held
        platform_io.unlock_fd(fd)
        os.close(fd)
        released = True
        final = rr.read_detached_status(repo, started["run_id"], wait_seconds=60)
        assert final["state"] == "completed", final
    finally:
        if not released:
            platform_io.unlock_fd(fd)
            os.close(fd)
