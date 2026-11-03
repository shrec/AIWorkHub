"""NF-2026-02029: an ended manager seat's ``.scratch/<session_id>`` is reclaimed early.

A seat stamps its scratch dir with the runtime_temp owner manifest
(``kind == "manager_seat"``).  Once that owner is provably dead the janitor
reclaims the dir 2h after its last write instead of 48h; a live seat is never
reclaimed and an unstamped dir keeps the 48h rule.  Process liveness is
monkeypatched, and ages are injected because utime is denied in the sandbox.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from aiworkhub import core, repo_policy, runtime_temp, storage_retention  # noqa: E402

_HOUR = 3600.0


def _repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    (root / ".aiworkhub").mkdir(parents=True)
    return root


def _set_owner_dead(monkeypatch: pytest.MonkeyPatch, dead: bool) -> None:
    monkeypatch.setattr(runtime_temp, "_pid_alive", lambda pid: not dead)
    monkeypatch.setattr(runtime_temp, "process_start_ticks", lambda pid: None)


def _sweep(
    root: Path, monkeypatch: pytest.MonkeyPatch, ages: dict[Path, float]
) -> dict[str, object]:
    now = time.time()
    monkeypatch.setattr(
        storage_retention, "_janitor_mtime", lambda path: now - ages.get(path, 0.0)
    )
    return storage_retention._janitor_scratch(root, now)


def _seat(root: Path, session_id: str) -> Path:
    manifest = runtime_temp.write_seat_stamp(root, session_id)
    assert manifest is not None
    assert manifest["kind"] == runtime_temp.SEAT_MANIFEST_KIND
    seat = root / ".scratch" / session_id
    (seat / "probe.py").write_text("print('probe')\n", encoding="utf-8")
    return seat


def test_dead_seat_dir_three_hours_old_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    seat = _seat(root, "dead-seat")
    _set_owner_dead(monkeypatch, True)

    result = _sweep(root, monkeypatch, {seat: 3 * _HOUR})

    assert result["ok"] is True, result
    assert result["removed"] == 1
    assert not seat.exists()


def test_dead_seat_removal_depends_on_the_seat_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the seat branch disabled the same 3h dir falls back to the 48h rule
    # and stays: test (a) above fails without the new branch.
    root = _repo(tmp_path)
    seat = _seat(root, "dead-seat")
    _set_owner_dead(monkeypatch, True)
    monkeypatch.setattr(runtime_temp, "SEAT_MANIFEST_KIND", "branch_disabled")

    result = _sweep(root, monkeypatch, {seat: 3 * _HOUR})

    assert result["removed"] == 0
    assert seat.is_dir()


def test_dead_seat_dir_one_hour_old_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    seat = _seat(root, "recent-seat")
    _set_owner_dead(monkeypatch, True)

    result = _sweep(root, monkeypatch, {seat: 1 * _HOUR})

    assert result["removed"] == 0
    assert seat.is_dir()


def test_live_seat_dir_is_never_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    seat = _seat(root, "live-seat")
    _set_owner_dead(monkeypatch, False)

    result = _sweep(root, monkeypatch, {seat: 72 * _HOUR})

    assert result["removed"] == 0
    assert seat.is_dir()


def test_unstamped_dir_keeps_the_48h_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    young = root / ".scratch" / "young"
    old = root / ".scratch" / "old"
    young.mkdir(parents=True)
    old.mkdir(parents=True)
    _set_owner_dead(monkeypatch, True)

    result = _sweep(root, monkeypatch, {young: 3 * _HOUR, old: 49 * _HOUR})

    assert result["removed"] == 1
    assert young.is_dir()
    assert not old.exists()


def test_dead_request_manifest_in_aiworkhub_container_keeps_the_48h_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    container = root / storage_retention._SCRATCH_CONTAINER_RELATIVE_PATHS[0]
    request_dir = container / "req-nf02029"
    request_dir.mkdir(parents=True)
    runtime_temp.write_owner_manifest(request_dir, "req-nf02029", root)
    _set_owner_dead(monkeypatch, True)

    result = _sweep(root, monkeypatch, {request_dir.resolve(): 3 * _HOUR, request_dir: 3 * _HOUR})

    assert result["removed"] == 0
    assert request_dir.is_dir()


@pytest.mark.parametrize("session_id", ["../x", "a/b", ".", "..", "", "x" * 129])
def test_seat_stamp_refuses_an_unsafe_session_id(tmp_path: Path, session_id: str) -> None:
    root = _repo(tmp_path)

    assert runtime_temp.write_seat_stamp(root, session_id) is None
    assert not (root / ".scratch").exists()
    assert not (tmp_path / "x").exists()


def test_manager_bootstrap_stamps_the_seat_once_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    calls: list[str] = []
    real_stamp = runtime_temp.write_seat_stamp

    def counting_stamp(repo: Path, session_id: str) -> object:
        calls.append(session_id)
        return real_stamp(repo, session_id)

    class _StopAfterStamp(Exception):
        pass

    def stop(*_args: object, **_kwargs: object) -> object:
        raise _StopAfterStamp

    identity = {"provider": "claude", "session_id": "seat-nf02029"}
    monkeypatch.setattr(core, "_SEAT_SCRATCH_STAMPED", set())
    monkeypatch.setattr(core, "_live_manager_identity", lambda: dict(identity))
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(core, "_schedule_task_hygiene", lambda **_kwargs: {})
    monkeypatch.setattr(runtime_temp, "write_seat_stamp", counting_stamp)
    # The stamp runs before the workforce-cap lookup; stopping there keeps the
    # rest of the bootstrap (database, dispatcher, probes) out of this test.
    monkeypatch.setattr(repo_policy, "resolve_workforce_cap", stop)

    for _ in range(2):
        with pytest.raises(_StopAfterStamp):
            core.manager_bootstrap()

    assert calls == ["seat-nf02029"]
    manifest = runtime_temp.read_owner_manifest(root / ".scratch" / "seat-nf02029")
    assert manifest is not None
    assert manifest["kind"] == runtime_temp.SEAT_MANIFEST_KIND
    assert manifest["session_id"] == "seat-nf02029"

    identity["session_id"] = ""
    with pytest.raises(_StopAfterStamp):
        core.manager_bootstrap()
    assert calls == ["seat-nf02029"]
    assert sorted(p.name for p in (root / ".scratch").iterdir()) == ["seat-nf02029"]


def test_manager_bootstrap_survives_a_failing_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_stamp(repo: Path, session_id: str) -> object:
        raise OSError("disk full")

    monkeypatch.setattr(core, "_SEAT_SCRATCH_STAMPED", set())
    monkeypatch.setattr(runtime_temp, "write_seat_stamp", broken_stamp)

    core._stamp_seat_scratch_once(_repo(tmp_path), "seat-broken")  # must not raise
