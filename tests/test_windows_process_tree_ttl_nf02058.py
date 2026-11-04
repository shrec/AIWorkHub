"""NF-2026-02058: core._windows_process_tree reuses one snapshot for a short TTL.

Manager identity probes used to rebuild a full Toolhelp32 snapshot on every
call. The core seam now caches a successful snapshot for
``WINDOWS_PROCESS_TREE_TTL_SECONDS`` while ``platform_io.windows_process_tree``
itself (used by ``terminate_process_tree``) stays uncached.
"""

from __future__ import annotations

import types

import pytest

from aiworkhub import core, platform_io


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _Snapshots:
    def __init__(self, *results: dict[int, tuple[int, str]] | None) -> None:
        self._results = list(results)
        self.calls = 0

    def __call__(self) -> dict[int, tuple[int, str]] | None:
        self.calls += 1
        index = min(self.calls - 1, len(self._results) - 1)
        result = self._results[index]
        return None if result is None else dict(result)


@pytest.fixture(autouse=True)
def _fresh_cache():
    core.reset_windows_process_tree_cache()
    yield
    core.reset_windows_process_tree_cache()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    # Replace only core's view of ``time`` so the global clock stays intact.
    monkeypatch.setattr(core, "time", types.SimpleNamespace(monotonic=fake))
    return fake


TREE = {4: (0, "System"), 100: (4, "python.exe"), 200: (100, "claude.exe")}


def test_ttl_is_a_two_second_module_constant() -> None:
    assert core.WINDOWS_PROCESS_TREE_TTL_SECONDS == 2.0


def test_back_to_back_identity_probes_take_one_snapshot(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    snapshots = _Snapshots(TREE)
    monkeypatch.setattr(platform_io, "windows_process_tree", snapshots)

    first = core._windows_process_image_names()
    clock.now += 0.5
    second = core._windows_process_image_names()
    tree = core._windows_process_tree()

    assert snapshots.calls == 1
    assert first == second == {4: "System", 100: "python.exe", 200: "claude.exe"}
    assert tree == TREE


def test_snapshot_is_retaken_after_ttl(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    newer = dict(TREE)
    newer[300] = (200, "child.exe")
    snapshots = _Snapshots(TREE, newer)
    monkeypatch.setattr(platform_io, "windows_process_tree", snapshots)

    assert core._windows_process_tree() == TREE
    clock.now += core.WINDOWS_PROCESS_TREE_TTL_SECONDS - 0.01
    assert core._windows_process_tree() == TREE
    assert snapshots.calls == 1

    clock.now += 0.02
    assert core._windows_process_tree() == newer
    assert snapshots.calls == 2


def test_clock_moving_backwards_does_not_extend_cache(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    snapshots = _Snapshots(TREE)
    monkeypatch.setattr(platform_io, "windows_process_tree", snapshots)

    core._windows_process_tree()
    clock.now -= 10.0
    core._windows_process_tree()
    assert snapshots.calls == 2


def test_failed_snapshot_is_never_cached(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    snapshots = _Snapshots(None, TREE)
    monkeypatch.setattr(platform_io, "windows_process_tree", snapshots)

    assert core._windows_process_tree() is None
    assert core._windows_process_image_names() == {
        pid: image for pid, (_parent, image) in TREE.items()
    }
    assert snapshots.calls == 2


def test_mutating_result_does_not_poison_cache(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    snapshots = _Snapshots(TREE)
    monkeypatch.setattr(platform_io, "windows_process_tree", snapshots)

    first = core._windows_process_tree()
    assert first is not None
    first.clear()
    first[999] = (1, "evil.exe")

    second = core._windows_process_tree()
    assert second == TREE
    assert second is not first
    assert snapshots.calls == 1


def test_reset_helper_forces_a_new_snapshot(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    snapshots = _Snapshots(TREE)
    monkeypatch.setattr(platform_io, "windows_process_tree", snapshots)

    core._windows_process_tree()
    core.reset_windows_process_tree_cache()
    core._windows_process_tree()
    assert snapshots.calls == 2


def test_terminate_process_tree_snapshots_fresh_every_call(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    snapshots = _Snapshots(TREE)
    monkeypatch.setattr(platform_io, "windows_process_tree", snapshots)
    monkeypatch.setattr(platform_io, "windows_system_taskkill_path", lambda: None)

    # Warm the core cache first: terminate_process_tree must not use it.
    core._windows_process_tree()
    assert snapshots.calls == 1

    terminated: list[int] = []
    for expected in (2, 3):
        assert platform_io.terminate_process_tree(
            100,
            platform_name="windows",
            timeout=0.0,
            poll_interval=0.0,
            probe=lambda pid: False,
            native_terminate=lambda pid: terminated.append(pid) or True,
            creation_time=lambda pid: 1,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
        )
        assert snapshots.calls == expected
    assert 100 in terminated and 200 in terminated
