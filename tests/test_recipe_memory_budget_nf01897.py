"""Detached heavy recipe runs carry a memory budget (NF-2026-01897 phase B)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import host_heavy_slots as hs
from aiworkhub import platform_io
from aiworkhub import recipe_runner as rr
from aiworkhub import tool_recipes as tr
from aiworkhub import tool_recipes_store as store

_REAL_POPEN = subprocess.Popen
_REAL_RUN = subprocess.run
_MIB = 1024 * 1024
_PROBE_SOURCE = (
    "import sys\n"
    "if sys.argv[1] == 'hog':\n"
    "    block = bytearray(768 * 1024 * 1024)\n"
    "sys.stdout.write('ok\\n')\n"
)


def _recipe(*, max_memory_mb: int | None = None) -> tr.Recipe:
    return tr.Recipe(
        id="example.membudget",
        version="1.0.0",
        purpose="A runnable probe used only by these tests.",
        task_kind=tr.TaskKind.SCAN,
        parameters=(
            tr.ParamSpec(
                name="mode",
                type=tr.ParamType.ENUM,
                required=True,
                values=("ok", "hog"),
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


def test_exit_status_vocabulary_names_the_memory_terminal():
    assert tr.EXIT_STATUS_MEMORY_BUDGET_EXCEEDED == "memory_budget_exceeded"
    assert tr.EXIT_STATUS_MEMORY_BUDGET_EXCEEDED in tr.EXIT_STATUSES


def test_total_memory_bytes_reports_physical_memory():
    total = platform_io.total_memory_bytes()
    assert total is None or total > 0
    available = platform_io.available_memory_bytes()
    if total is not None and available is not None:
        assert available <= total


def test_a_child_allocating_past_its_budget_ends_memory_budget_exceeded(repo):
    digest = rr.run_recipe(
        repo,
        _recipe(),
        {"mode": "hog"},
        persist_receipt=False,
        memory_budget_bytes=128 * _MIB,
    )
    assert digest["exit_status"] == tr.EXIT_STATUS_MEMORY_BUDGET_EXCEEDED, digest
    assert digest["returncode"] != 0


def test_a_child_under_budget_matches_the_unbudgeted_run(repo):
    recipe = _recipe()
    plain = rr.run_recipe(repo, recipe, {"mode": "ok"}, persist_receipt=False)
    budgeted = rr.run_recipe(
        repo,
        recipe,
        {"mode": "ok"},
        persist_receipt=False,
        memory_budget_bytes=128 * _MIB,
    )
    assert plain["exit_status"] == tr.EXIT_STATUS_COMPLETED
    assert budgeted["exit_status"] == tr.EXIT_STATUS_COMPLETED, budgeted
    assert budgeted["returncode"] == plain["returncode"] == 0
    assert budgeted["stdout_tail"] == plain["stdout_tail"]


def test_no_budget_takes_the_plain_subprocess_run_path(repo, monkeypatch):
    calls: list[list[str]] = []

    def spy_run(argv, **kwargs):
        calls.append(list(argv))
        return _REAL_RUN(argv, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the budgeted spawn path must not run without a budget")

    monkeypatch.setattr(rr.subprocess, "run", spy_run)
    monkeypatch.setattr(rr.platform_io, "run_with_memory_limit", forbidden)

    digest = rr.run_recipe(repo, _recipe(), {"mode": "ok"}, persist_receipt=False)

    assert len(calls) == 1
    assert digest["exit_status"] == tr.EXIT_STATUS_COMPLETED


def test_a_timeout_keeps_precedence_over_the_budget(repo, monkeypatch):
    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["python"], 1.0)

    monkeypatch.setattr(rr.platform_io, "run_with_memory_limit", timed_out)
    digest = rr.run_recipe(
        repo,
        _recipe(),
        {"mode": "ok"},
        persist_receipt=False,
        memory_budget_bytes=128 * _MIB,
    )
    assert digest["exit_status"] == tr.EXIT_STATUS_TIMEOUT


def _queue_detached(repo: Path, recipe: tr.Recipe, monkeypatch) -> dict:
    store.put_recipe(repo, recipe)
    monkeypatch.setattr(
        rr.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 0})()
    )
    started = rr.start_detached_run(repo, recipe, {"mode": "ok"})
    monkeypatch.setattr(rr.subprocess, "Popen", _REAL_POPEN)
    return started


def _run_detached_capturing_budget(
    repo: Path, tmp_path: Path, monkeypatch, recipe: tr.Recipe
) -> int | None:
    monkeypatch.setattr(hs, "default_slots_dir", lambda: tmp_path / "slots")
    monkeypatch.setattr(hs, "slot_count", lambda cpu_capacity=None: 4)
    monkeypatch.setattr(
        rr.platform_io, "available_memory_bytes", lambda *_a, **_k: 1 << 50
    )
    started = _queue_detached(repo, recipe, monkeypatch)
    seen: list[dict] = []

    def fake_run_recipe(_root, _recipe, _params, **kwargs):
        seen.append(kwargs)
        return {"exit_status": tr.EXIT_STATUS_COMPLETED, "returncode": 0}

    monkeypatch.setattr(rr, "run_recipe", fake_run_recipe)
    assert rr._detached_main([started["status_path"]]) == 0
    final = rr.read_detached_status(repo, started["run_id"])
    assert final["state"] == "completed", final
    assert len(seen) == 1
    return seen[0]["memory_budget_bytes"]


def test_detached_main_budgets_total_ram_divided_by_host_slots(
    repo, tmp_path, monkeypatch
):
    monkeypatch.setattr(rr.platform_io, "total_memory_bytes", lambda *_a: 8 * 1024 * _MIB)
    budget = _run_detached_capturing_budget(repo, tmp_path, monkeypatch, _recipe())
    assert budget == 2 * 1024 * _MIB


def test_detached_main_budget_is_none_when_total_ram_is_unknown(
    repo, tmp_path, monkeypatch
):
    monkeypatch.setattr(rr.platform_io, "total_memory_bytes", lambda *_a: None)
    budget = _run_detached_capturing_budget(repo, tmp_path, monkeypatch, _recipe())
    assert budget is None


def test_detached_main_budget_follows_a_declared_max_memory_mb(
    repo, tmp_path, monkeypatch
):
    monkeypatch.setattr(rr.platform_io, "total_memory_bytes", lambda *_a: 8 * 1024 * _MIB)
    budget = _run_detached_capturing_budget(
        repo, tmp_path, monkeypatch, _recipe(max_memory_mb=96)
    )
    assert budget == 96 * _MIB


def test_a_declared_zero_max_memory_mb_takes_the_host_share(
    repo, tmp_path, monkeypatch
):
    monkeypatch.setattr(rr.platform_io, "total_memory_bytes", lambda *_a: 8 * 1024 * _MIB)
    budget = _run_detached_capturing_budget(
        repo, tmp_path, monkeypatch, _recipe(max_memory_mb=0)
    )
    assert budget == 2 * 1024 * _MIB


def test_a_child_that_survives_its_refused_allocation_is_not_a_limit_hit(tmp_path):
    source = (
        "try:\n"
        "    block = bytearray(768 * 1024 * 1024)\n"
        "except MemoryError:\n"
        "    pass\n"
    )
    run = platform_io.run_with_memory_limit(
        [sys.executable, "-c", source],
        cwd=tmp_path,
        env=os.environ,
        memory_limit_bytes=128 * _MIB,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    assert run.limit_hit is False


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_AS is POSIX-only")
def test_a_budget_above_the_inherited_hard_rlimit_is_clamped(tmp_path):
    src_root = str(Path(platform_io.__file__).resolve().parents[1])
    script = (
        "import os, resource, sys\n"
        "resource.setrlimit(resource.RLIMIT_AS, (1 << 32, 1 << 32))\n"
        "from aiworkhub import platform_io\n"
        "run = platform_io.run_with_memory_limit(\n"
        "    [sys.executable, '-c', 'pass'], cwd='.', env=os.environ,\n"
        "    memory_limit_bytes=1 << 40, timeout=60)\n"
        "sys.exit(run.returncode)\n"
    )
    env = {**os.environ, "PYTHONPATH": src_root}
    completed = _REAL_RUN(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
