"""NF-2026-02026: accept validates outside the repo-wide promotion lock.

Measured: the writable accept path held ``_promotion_lock`` through the whole
combined-tree validation, so every accept serialized behind minutes of test
runs and a second seat failed with ``promotion_busy`` after 20 seconds.

These tests drive the real ``accept_review`` body over the NF-2026-01436 stub
manager with the PRODUCTION promotion lock. "The lock is held" is read from the
holder sidecar the real lock writes on entry and removes on release. Canonical
movement is a real commit (or real uncommitted write) in a real git repository,
made from inside the fake validation, which is exactly the unlocked window.
"""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    accept_neighbour_tests,
    platform_io,
    process_launcher,
    process_launcher_accept_review,
    task_engine,
    task_store,
)

import test_accept_with_manager_correction_nf01436 as _nf01436  # noqa: E402

TARGET = _nf01436.TARGET
NEIGHBOUR_TEST = "tests/test_neighbour.py"
UNRELATED = "docs/other.md"
THREAD_WAIT_SECONDS = 30


def _sidecar(process_dir: Path) -> Path:
    return process_dir / ".promotion.lock.holder.json"


class _RealPromotionLockManager(_nf01436._StubManager):
    """The stub manager with the real promotion lock, recording it at promotion."""

    _promotion_lock = process_launcher.ProcessManager._promotion_lock

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.lock_held_at_promotion: list[bool] = []

    def _promote_accepted_candidate(self, workspace, changed, merged_hashes=None):
        self.lock_held_at_promotion.append(_sidecar(self.process_dir).exists())
        return super()._promote_accepted_candidate(workspace, changed, merged_hashes)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git", "-c", "user.name=nf02026", "-c", "user.email=nf02026@example.invalid",
            "-c", "commit.gpgsign=false", *args,
        ],
        cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()


def _write(repo: Path, relative: str, text: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(repo: Path, relative: str, text: str) -> None:
    _write(repo, relative, text)
    _git(repo, "add", "--", relative)
    _git(repo, "commit", "-q", "-m", f"touch {relative}")


def _init_git(repo: Path) -> None:
    _git(repo, "init", "-q")
    _write(repo, ".gitignore", ".aiworkhub/\nprocess.log\n")
    for relative in (TARGET, NEIGHBOUR_TEST, UNRELATED):
        _write(repo, relative, "seed\n")
    _git(repo, "add", "--", ".gitignore", TARGET, NEIGHBOUR_TEST, UNRELATED)
    _git(repo, "commit", "-q", "-m", "seed")


def _env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, git: bool = True
) -> _RealPromotionLockManager:
    monkeypatch.setattr(_nf01436, "_StubManager", _RealPromotionLockManager)
    manager = _nf01436._accept_env(tmp_path, monkeypatch)
    if git:
        _init_git(manager.repo)
    return manager


def _accept(manager: _RealPromotionLockManager) -> dict:
    return _nf01436._accept(manager, manager_correction=_nf01436._correction_payload())


def _install_validation(
    manager: _RealPromotionLockManager,
    monkeypatch: pytest.MonkeyPatch,
    *,
    during_first_run: Callable[[], None] | None = None,
) -> list[bool]:
    """Fake the full-snapshot validation; return whether the lock was held per run."""
    held: list[bool] = []

    def _full_snapshot(workspace, card, latest, changed, **_kwargs):
        held.append(_sidecar(manager.process_dir).exists())
        if len(held) == 1 and during_first_run is not None:
            during_first_run()
        return [], {"combined_tree": {}}

    monkeypatch.setattr(process_launcher, "_run_full_snapshot_validations", _full_snapshot)
    return held


def _install_combined_tree(
    manager: _RealPromotionLockManager, monkeypatch: pytest.MonkeyPatch
) -> list[bool]:
    """Select one neighbour test, so the combined tree runs and pins its own ``HEAD``."""
    built: list[bool] = []

    def _build(workspace, card, changed):
        built.append(_sidecar(manager.process_dir).exists())
        union = SimpleNamespace(
            repo=manager.repo, path=workspace.path, home=workspace.home,
            base_oid=_git(manager.repo, "rev-parse", "HEAD"),
        )
        return union, {"schema_id": "aiworkhub.combined_tree.v1", "merged_paths": {}}

    monkeypatch.setattr(
        accept_neighbour_tests, "select_neighbour_tests",
        lambda *a, **k: accept_neighbour_tests.NeighbourSelection(
            tests=(NEIGHBOUR_TEST,), reason="", limit=1
        ),
    )
    monkeypatch.setattr(
        accept_neighbour_tests, "run_neighbour_tests",
        lambda *a, **k: {"selected": [NEIGHBOUR_TEST], "passed": True},
    )
    monkeypatch.setattr(process_launcher, "create_combined_validation_workspace", _build)
    return built


def test_two_accepts_on_disjoint_paths_validate_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second accept's validation starts while the first's is still running.

    With the old lock scope the second accept could not reach validation until
    the first released, so ``second_started`` would time out here. The two
    repositories share one ``process_dir``, and therefore one promotion lock.
    """
    first = _env(tmp_path / "first", monkeypatch, git=False)
    second = _env(tmp_path / "second", monkeypatch, git=False)
    second.process_dir = first.process_dir
    # The two fixtures share one (task, request) identity, which is what the
    # in-flight marker is keyed by; distinct cards would hold distinct markers.
    monkeypatch.setattr(
        process_launcher_accept_review, "_accept_in_flight_marker",
        lambda *_a: process_launcher_accept_review._AcceptInFlight(),
    )
    current = threading.local()
    bootstrap = process_launcher.core.manager_bootstrap
    process_launcher.core.manager_bootstrap = lambda: {
        **bootstrap(), "repo": str(current.manager.repo),
    }

    first_running = threading.Event()
    second_started = threading.Event()
    observed: dict[str, bool] = {}

    def _full_snapshot(workspace, card, latest, changed, **_kwargs):
        if Path(workspace.repo) == first.repo:
            first_running.set()
            observed["overlapped"] = second_started.wait(THREAD_WAIT_SECONDS)
        else:
            observed["first_was_running"] = first_running.wait(THREAD_WAIT_SECONDS)
            second_started.set()
        return [], {"combined_tree": {}}

    monkeypatch.setattr(process_launcher, "_run_full_snapshot_validations", _full_snapshot)
    results: dict[str, object] = {}

    def _worker(name: str, manager: _RealPromotionLockManager) -> None:
        current.manager = manager
        try:
            results[name] = _accept(manager)
        except BaseException as exc:  # noqa: BLE001 -- reported by the assertion below
            results[name] = exc

    threads = [
        threading.Thread(target=_worker, args=("first", first), daemon=True),
        threading.Thread(target=_worker, args=("second", second), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=THREAD_WAIT_SECONDS * 2)

    assert observed == {"first_was_running": True, "overlapped": True}
    for name in ("first", "second"):
        assert isinstance(results[name], dict), results[name]
        assert results[name].get("error") != "promotion_busy"
        assert results[name]["ok"] is True, results[name].get("error")
    assert first.lock_held_at_promotion == [True]
    assert second.lock_held_at_promotion == [True]


def test_lock_spans_recheck_promotion_and_the_finish_but_not_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _env(tmp_path, monkeypatch)
    validation_held = _install_validation(manager, monkeypatch)
    states: dict[str, bool] = {}

    real_drift = process_launcher_accept_review._canonical_drift_paths
    real_finish = task_engine.accept_review

    def _spy_drift(*args, **kwargs):
        states["recheck"] = _sidecar(manager.process_dir).exists()
        return real_drift(*args, **kwargs)

    def _spy_finish(*args, **kwargs):
        states["finish"] = _sidecar(manager.process_dir).exists()
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(process_launcher_accept_review, "_canonical_drift_paths", _spy_drift)
    monkeypatch.setattr(task_engine, "accept_review", _spy_finish)

    result = _accept(manager)

    assert result["ok"] is True, result.get("error")
    assert validation_held == [False]
    assert states == {"recheck": True, "finish": True}
    assert manager.lock_held_at_promotion == [True]
    assert not _sidecar(manager.process_dir).exists()


@pytest.mark.parametrize(
    "move",
    [
        pytest.param(lambda repo: _commit(repo, TARGET, "peer\n"), id="commit_on_candidate"),
        pytest.param(lambda repo: _write(repo, TARGET, "peer\n"), id="uncommitted_on_candidate"),
    ],
)
def test_canonical_moved_under_the_candidate_revalidates_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, move: Callable[[Path], None]
) -> None:
    manager = _env(tmp_path, monkeypatch)
    held = _install_validation(
        manager, monkeypatch, during_first_run=lambda: move(manager.repo)
    )

    result = _accept(manager)

    assert result["ok"] is True, result.get("error")
    assert held == [False, True]
    assert manager.lock_held_at_promotion == [True]


def test_commit_on_a_declared_validation_path_revalidates_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _env(tmp_path, monkeypatch)
    manager.cards[_nf01436.TASK_ID]["validation"] = [
        f"python -m pytest -q {NEIGHBOUR_TEST}"
    ]
    held = _install_validation(
        manager, monkeypatch,
        during_first_run=lambda: _commit(manager.repo, NEIGHBOUR_TEST, "peer\n"),
    )

    result = _accept(manager)

    assert result["ok"] is True, result.get("error")
    assert held == [False, True]


def test_commit_on_a_neighbour_test_rebuilds_the_combined_tree_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The combined tree's own ``base_oid`` is the pin, and the helper runs twice."""
    manager = _env(tmp_path, monkeypatch)
    built = _install_combined_tree(manager, monkeypatch)
    held = _install_validation(
        manager, monkeypatch,
        during_first_run=lambda: _commit(manager.repo, NEIGHBOUR_TEST, "peer\n"),
    )

    result = _accept(manager)

    assert result["ok"] is True, result.get("error")
    assert built == [False, True]
    assert held == [False, True]


@pytest.mark.parametrize(
    "move",
    [
        pytest.param(lambda repo: _commit(repo, UNRELATED, "peer\n"), id="disjoint_commit"),
        pytest.param(lambda repo: _write(repo, UNRELATED, "peer\n"), id="disjoint_uncommitted"),
    ],
)
def test_disjoint_canonical_movement_promotes_without_revalidating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, move: Callable[[Path], None]
) -> None:
    manager = _env(tmp_path, monkeypatch)
    held = _install_validation(
        manager, monkeypatch, during_first_run=lambda: move(manager.repo)
    )

    result = _accept(manager)

    assert result["ok"] is True, result.get("error")
    assert held == [False]
    assert manager.lock_held_at_promotion == [True]


def _write_holder(process_dir: Path) -> None:
    _sidecar(process_dir).write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "task_id": "T-holder",
                "request_id": "R-holder",
                "operation": "accept_review",
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )


def _lock_timeout() -> platform_io.AdvisoryLockTimeout:
    return platform_io.AdvisoryLockTimeout(
        "windows_advisory_lock_timeout after 20s (last errno EACCES)", errno=errno.EACCES
    )


def test_promotion_lock_timeout_after_validation_answers_busy_and_restores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _env(tmp_path, monkeypatch)
    held = _install_validation(manager, monkeypatch)
    _write_holder(manager.process_dir)
    waits: list[int] = []

    def _times_out(fd: int, *, blocking: bool = True) -> None:
        waits.append(fd)
        raise _lock_timeout()

    monkeypatch.setattr(process_launcher, "lock_fd", _times_out)
    finished: list[object] = []
    monkeypatch.setattr(
        task_engine, "accept_review", lambda *a, **k: finished.append(a) or {"ok": True}
    )

    result = _accept(manager)

    assert result["ok"] is False
    assert result["error"] == "promotion_busy"
    assert result["holder"]["task_id"] == "T-holder"
    assert result["holder"]["request_id"] == "R-holder"
    assert result["holder_stale"] is False
    assert result["retry_after_s"] > 0
    assert result["task_id"] == _nf01436.TASK_ID
    assert result["request_id"] == _nf01436.REQUEST_ID
    # The finished validation was not thrown away on the first bounded wait.
    assert len(held) == 1
    assert len(waits) == process_launcher_accept_review._PROMOTION_LOCK_WAIT_ATTEMPTS
    assert manager.promoted == []
    assert finished == []
    workspace_file = tmp_path / "workspace" / TARGET
    assert workspace_file.read_text(encoding="utf-8") == _nf01436.CANDIDATE
    _readiness, db_path = task_store._require_ready(manager.repo)
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE task_id=?", (_nf01436.TASK_ID,)
        ).fetchone()
    assert row == ("review",)


def test_promotion_lock_wait_survives_a_timed_out_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _env(tmp_path, monkeypatch)
    held = _install_validation(manager, monkeypatch)
    real_lock_fd = process_launcher.lock_fd
    attempts: list[int] = []

    def _busy_twice(fd: int, *, blocking: bool = True) -> None:
        attempts.append(fd)
        if len(attempts) <= 2:
            raise _lock_timeout()
        real_lock_fd(fd, blocking=blocking)

    monkeypatch.setattr(process_launcher, "lock_fd", _busy_twice)

    result = _accept(manager)

    assert result["ok"] is True, result.get("error")
    assert len(attempts) == 3
    assert held == [False]
    assert manager.lock_held_at_promotion == [True]


def test_uncommitted_bytes_later_committed_are_not_drift(tmp_path: Path) -> None:
    """A peer's promotion the validated tree already held is not a move."""
    _init_git(tmp_path)
    _write(tmp_path, TARGET, "peer\n")
    pinned_head = _git(tmp_path, "rev-parse", "HEAD")
    pinned_delta = process_launcher_accept_review._canonical_delta_hashes(tmp_path)
    assert set(pinned_delta) == {TARGET}

    _git(tmp_path, "add", "--", TARGET)
    _git(tmp_path, "commit", "-q", "-m", "peer auto-commit")
    assert (
        process_launcher_accept_review._canonical_drift_paths(
            tmp_path, pinned_head=pinned_head, pinned_delta=pinned_delta
        )
        == set()
    )

    _commit(tmp_path, UNRELATED, "later\n")
    assert process_launcher_accept_review._canonical_drift_paths(
        tmp_path, pinned_head=pinned_head, pinned_delta=pinned_delta
    ) == {UNRELATED}


def test_drift_is_unknown_for_an_unresolvable_pin(tmp_path: Path) -> None:
    _init_git(tmp_path)
    assert (
        process_launcher_accept_review._canonical_drift_paths(
            tmp_path, pinned_head="0" * 40, pinned_delta={}
        )
        is None
    )
    assert (
        process_launcher_accept_review._canonical_drift_paths(
            tmp_path, pinned_head=None, pinned_delta={}
        )
        is None
    )


def test_a_tree_that_is_not_a_git_checkout_has_nothing_to_pin(tmp_path: Path) -> None:
    assert process_launcher_accept_review._canonical_head_oid(tmp_path) is None
    assert process_launcher_accept_review._canonical_delta_hashes(tmp_path) == {}
    assert (
        process_launcher_accept_review._canonical_drift_paths(
            tmp_path, pinned_head="anything", pinned_delta={}
        )
        == set()
    )


@pytest.mark.parametrize(
    ("drift", "expected"),
    [
        (set(), False),
        ({UNRELATED}, False),
        ({"src/demo/widget.py"}, True),
        ({"tests/sub/test_a.py"}, True),
        ({"tests/test_selected.py"}, True),
        ({"data/input.json"}, True),
    ],
)
def test_validation_neighbourhood_membership(drift: set[str], expected: bool) -> None:
    assert (
        process_launcher_accept_review._validation_neighbourhood_touched(
            drift,
            changed=["src/demo/widget.py"],
            neighbour_tests=["tests/test_selected.py"],
            immutable_inputs=["data/input.json"],
            declared_commands=["python -m pytest -q tests/sub"],
        )
        is expected
    )


def test_dead_reviewer_running_states_copy_is_gone() -> None:
    assert not hasattr(process_launcher_accept_review, "_REVIEWER_RUNNING_STATES")
