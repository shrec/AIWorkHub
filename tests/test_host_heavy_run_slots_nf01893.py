"""Host-wide heavy-run slots bound parallel detached runs (NF-2026-01893)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aiworkhub import host_heavy_slots as hs
from aiworkhub import parallelism
from aiworkhub import recipe_runner as rr
from aiworkhub import tool_recipes as tr
from aiworkhub import tool_recipes_store as store

_REAL_POPEN = subprocess.Popen


def test_n_acquirers_fill_the_slots_and_the_next_gets_none(tmp_path):
    held = [hs.try_acquire(tmp_path, 3) for _ in range(3)]
    try:
        assert all(slot is not None for slot in held)
        assert sorted(slot.index for slot in held) == [0, 1, 2]
        assert hs.try_acquire(tmp_path, 3) is None

        held[1].release()
        held[1].release()
        again = hs.try_acquire(tmp_path, 3)
        assert again is not None
        assert again.index == 1
        held[1] = again
    finally:
        for slot in held:
            slot.release()
    with hs.try_acquire(tmp_path, 3) as slot:
        assert slot is not None


def test_try_acquire_creates_the_slots_dir(tmp_path):
    target = tmp_path / "nested" / "slots"
    slot = hs.try_acquire(target, 1)
    try:
        assert slot is not None
        assert (target / "slot-0.lock").exists()
    finally:
        slot.release()


def test_default_slots_dir_is_under_the_user_home():
    assert hs.default_slots_dir() == Path.home() / ".aiworkhub" / "host-slots"


@pytest.mark.parametrize(
    ("capacity", "expected"),
    [(0, 1), (1, 1), (2, 1), (4, 1), (8, 3), (16, 3), (32, 3), (64, 3)],
)
def test_slot_count_follows_cpu_capacity(monkeypatch, capacity, expected):
    monkeypatch.setattr(parallelism, "get_cpu_capacity", lambda: capacity)
    assert hs.slot_count() == expected
    assert hs.slot_count(capacity) == expected
    assert hs.slot_count() >= 1


def _recipe() -> tr.Recipe:
    return tr.Recipe(
        id="example.slots",
        version="1.0.0",
        purpose="A runnable probe used only by these tests.",
        task_kind=tr.TaskKind.SCAN,
        parameters=(
            tr.ParamSpec(
                name="mode", type=tr.ParamType.ENUM, required=True, values=("ok",)
            ),
        ),
        outputs=(tr.OutputSpec(name="report", type=tr.OutputType.STDOUT),),
        resource_bounds=tr.ResourceBounds(),
        argv=(tr.lit("python"), tr.lit("probe.py"), tr.slot("mode")),
    )


def test_detached_main_waits_for_a_host_slot_then_runs(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "probe.py").write_text('import sys\nsys.stdout.write("ok\\n")\n', encoding="utf-8")
    slots_dir = tmp_path / "slots"
    monkeypatch.setattr(hs, "default_slots_dir", lambda: slots_dir)
    monkeypatch.setattr(hs, "slot_count", lambda cpu_capacity=None: 1)

    recipe = _recipe()
    store.put_recipe(repo, recipe)
    monkeypatch.setattr(
        rr.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 0})()
    )
    started = rr.start_detached_run(repo, recipe, {"mode": "ok"})
    monkeypatch.setattr(rr.subprocess, "Popen", _REAL_POPEN)

    holder = hs.try_acquire(slots_dir, 1)
    assert holder is not None
    observed: list[str] = []

    def fake_sleep(_seconds):
        record = rr._load_status(Path(started["status_path"]))
        observed.append(record["state"])
        assert record["host_slots"] == 1
        holder.release()

    monkeypatch.setattr(rr, "_sleep", fake_sleep)
    try:
        assert rr._detached_main([started["status_path"]]) == 0
    finally:
        holder.release()

    assert observed == [rr.DETACHED_STATE_WAITING_HOST_SLOT]
    final = rr.read_detached_status(repo, started["run_id"])
    assert final["state"] == "completed", final
    free = hs.try_acquire(slots_dir, 1)
    assert free is not None
    free.release()
