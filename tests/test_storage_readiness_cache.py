"""NF-2026-00560: readiness re-scanned 222 MB on every task operation.

``storage_readiness`` guards all 33 ``_require_ready`` call sites, so every task
operation pays for it. Measured on this repository's canonical store:

    storage_readiness (whole)   61.6 ms
      inspect_repository         0.6 ms
      load_storage_registry      1.1 ms
      _schema_ok                 0.4 ms
      quick_check               59.1 ms   <- 96%

One ``core.roadmap_snapshot()`` made 94 readiness calls and spent 11.03 s of its
11.22 s wall time inside them -- a classic N+1 where every N is a full database
integrity scan.

The fix caches only the integrity scan, never the authority. These tests pin
both halves of that: the expensive scan is not repeated, and every authority
check still runs on every call, so a store that stops being canonical is still
refused immediately.

Note on ``PRAGMA quick_check(1)``: it is not a cheaper scan. Measured on the
222.9 MB canonical database, ``quick_check`` is 60.9 ms and ``quick_check(1)``
is 63.5 ms -- the bound caps how many errors are *reported*, so it only helps a
database that is already corrupt. Caching is the fix; the bounded form is not.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_store  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    task_store.reset_storage_readiness_cache()
    yield
    task_store.reset_storage_readiness_cache()


def _ready_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    readiness = task_store.storage_readiness(repo)
    assert readiness.ready, readiness.reason
    return repo


def test_integrity_scan_runs_once_across_many_readiness_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The N+1 is gone: 94 readiness calls must not be 94 integrity scans."""
    repo = _ready_repo(tmp_path)
    task_store.reset_storage_readiness_cache()

    scans: list[str] = []
    real = task_store.quick_check

    def counting(path: Path) -> str:
        scans.append(str(path))
        return real(path)

    monkeypatch.setattr(task_store, "quick_check", counting)

    for _ in range(94):
        assert task_store.storage_readiness(repo).ready

    assert len(scans) == 1, f"expected one integrity scan, ran {len(scans)}"


def test_authority_is_rechecked_on_every_call_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the scan is cached. Ownership must be re-verified every time."""
    repo = _ready_repo(tmp_path)

    inspections: list[object] = []
    real = task_store.inspect_repository

    def counting(root):
        inspections.append(root)
        return real(root)

    monkeypatch.setattr(task_store, "inspect_repository", counting)

    for _ in range(5):
        task_store.storage_readiness(repo)

    assert len(inspections) == 5, "authority must not be served from a cache"


def test_a_store_that_stops_being_canonical_is_refused_immediately(
    tmp_path: Path,
) -> None:
    """A primed cache must not keep a de-authorised store looking ready."""
    repo = _ready_repo(tmp_path)
    for _ in range(3):
        assert task_store.storage_readiness(repo).ready

    registry_path = repo / ".aiworkhub" / "config" / "storage.json"
    assert registry_path.is_file(), "storage registry not found"
    registry_path.unlink()

    readiness = task_store.storage_readiness(repo)
    assert not readiness.ready
    assert readiness.reason


def test_a_missing_database_is_refused_even_with_a_primed_cache(
    tmp_path: Path,
) -> None:
    repo = _ready_repo(tmp_path)
    primed = task_store.storage_readiness(repo)
    assert primed.ready

    Path(primed.canonical_db).unlink()

    readiness = task_store.storage_readiness(repo)
    assert not readiness.ready
    assert readiness.reason == "canonical_db_missing"


def test_a_corrupt_result_is_never_cached(tmp_path: Path, monkeypatch) -> None:
    """A repair must be seen at once, so only 'ok' is remembered."""
    repo = _ready_repo(tmp_path)
    task_store.reset_storage_readiness_cache()

    answers = iter(["*** in database main", "*** in database main", "ok"])
    calls: list[int] = []

    def flaky(path: Path) -> str:
        calls.append(1)
        return next(answers, "ok")

    monkeypatch.setattr(task_store, "quick_check", flaky)

    assert not task_store.storage_readiness(repo).ready
    assert not task_store.storage_readiness(repo).ready
    assert task_store.storage_readiness(repo).ready
    assert len(calls) == 3, "a non-ok scan must be retried, never cached"

    # Once ok, it is remembered.
    assert task_store.storage_readiness(repo).ready
    assert len(calls) == 3


def test_cache_is_bounded_and_keyed_by_database_identity(tmp_path: Path) -> None:
    repo = _ready_repo(tmp_path)
    assert task_store.storage_readiness(repo).ready
    assert len(task_store._QUICK_CHECK_CACHE) <= task_store._QUICK_CHECK_CACHE_MAX_ENTRIES

    key = next(iter(task_store._QUICK_CHECK_CACHE))
    path, device, inode = key
    assert Path(path).is_file()
    assert isinstance(device, int) and isinstance(inode, int)


def test_reset_clears_the_cache(tmp_path: Path) -> None:
    repo = _ready_repo(tmp_path)
    assert task_store.storage_readiness(repo).ready
    assert task_store._QUICK_CHECK_CACHE
    task_store.reset_storage_readiness_cache()
    assert not task_store._QUICK_CHECK_CACHE
