"""NF-2026-02059: every get_task opened a read-only connection for readiness.

``get_task`` -> ``_db_path_for`` -> ``_require_ready`` -> ``storage_readiness``
re-read the manifest, the storage registry and opened a read-only SQLite
connection for ``_schema_ok`` on every call, so per-row ``get_task`` loops were
N+1 connection opens. A positive verdict is now memoized per resolved root and
served only while the stat fingerprint of the manifest, registry and canonical
database is unchanged. These tests pin both halves: the unchanged store is
checked once, and any change, missing file or not-ready store still fails
closed with the same reason.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_store  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache():
    task_store.reset_storage_readiness_cache()
    yield
    task_store.reset_storage_readiness_cache()


def _ready_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    readiness = task_store.storage_readiness(repo)
    assert readiness.ready, readiness.reason
    task_store.reset_storage_readiness_cache()
    return repo


class _Counters:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.schema = 0
        self.inspect = 0
        self.registry = 0
        real_schema = task_store._schema_ok
        real_inspect = task_store.inspect_repository
        real_registry = task_store.load_storage_registry

        def schema(path):
            self.schema += 1
            return real_schema(path)

        def inspect(root):
            self.inspect += 1
            return real_inspect(root)

        def registry(root, **kwargs):
            self.registry += 1
            return real_registry(root, **kwargs)

        monkeypatch.setattr(task_store, "_schema_ok", schema)
        monkeypatch.setattr(task_store, "inspect_repository", inspect)
        monkeypatch.setattr(task_store, "load_storage_registry", registry)

    def snapshot(self) -> tuple[int, int, int]:
        return (self.schema, self.inspect, self.registry)


def _registry_path(repo: Path) -> Path:
    path = repo / ".aiworkhub" / "config" / "storage.json"
    assert path.is_file()
    return path


def _manifest_path(repo: Path) -> Path:
    path = Path(task_store.inspect_repository(repo).manifest_path)
    assert path.is_file()
    return path


def _db_path(repo: Path) -> Path:
    readiness = task_store.storage_readiness(repo)
    assert readiness.ready, readiness.reason
    return Path(readiness.canonical_db)


def _prime(repo: Path) -> None:
    """Record the root's inputs: the first ready check never memoizes, because
    it has no prior fingerprint to bracket the check with."""
    assert task_store.storage_readiness(repo).ready
    assert str(repo.resolve()) in task_store._READINESS_PATHS


def test_hundred_get_task_calls_run_the_full_check_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _ready_repo(tmp_path)
    _prime(repo)
    counters = _Counters(monkeypatch)

    for index in range(100):
        assert task_store.get_task(repo, f"absent-{index}") is None

    assert counters.snapshot() == (1, 1, 1), counters.snapshot()


def _append_newline(path: Path) -> None:
    path.write_bytes(path.read_bytes() + b"\n")


def _replace_database(path: Path) -> None:
    copy = path.with_name(path.name + ".nf02059-copy")
    shutil.copyfile(path, copy)
    os.replace(copy, path)


@pytest.mark.parametrize("which", ["registry", "manifest", "database"])
def test_changing_an_input_file_forces_a_full_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    repo = _ready_repo(tmp_path)
    targets = {
        "registry": (_registry_path(repo), _append_newline),
        "manifest": (_manifest_path(repo), _append_newline),
        "database": (_db_path(repo), _replace_database),
    }
    task_store.reset_storage_readiness_cache()
    _prime(repo)
    counters = _Counters(monkeypatch)

    for _ in range(5):
        assert task_store.storage_readiness(repo).ready
    assert counters.snapshot() == (1, 1, 1)

    path, mutate = targets[which]
    mutate(path)

    assert task_store.storage_readiness(repo).ready
    assert counters.snapshot() == (2, 2, 2), f"{which} change must force a re-check"
    assert task_store.storage_readiness(repo).ready
    assert counters.snapshot() == (2, 2, 2), "the re-checked verdict is memoized again"


def test_uninitialized_store_fails_closed_with_the_same_reason(tmp_path: Path) -> None:
    repo = tmp_path / "bare"
    repo.mkdir()
    expected = task_store.storage_readiness(repo)
    assert not expected.ready
    for _ in range(3):
        with pytest.raises(task_store.StorageNotReadyError) as info:
            task_store.get_task(repo, "anything")
        assert str(info.value) == str(task_store.StorageNotReadyError(expected.reason))
        assert task_store.storage_readiness(repo).reason == expected.reason


def test_removed_database_after_a_primed_memo_fails_closed(tmp_path: Path) -> None:
    repo = _ready_repo(tmp_path)
    db = _db_path(repo)
    assert task_store.get_task(repo, "absent") is None

    db.unlink()
    for _ in range(2):
        with pytest.raises(task_store.StorageNotReadyError) as info:
            task_store.get_task(repo, "absent")
        assert str(info.value) == str(task_store.StorageNotReadyError("canonical_db_missing"))
        assert task_store.storage_readiness(repo).reason == "canonical_db_missing"


def test_negative_verdict_is_not_cached_and_a_repair_is_seen_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _ready_repo(tmp_path)
    assert task_store.storage_readiness(repo).ready
    registry = _registry_path(repo)
    parked = registry.with_name("storage.json.parked")
    registry.rename(parked)

    counters = _Counters(monkeypatch)
    first = task_store.storage_readiness(repo)
    second = task_store.storage_readiness(repo)
    assert not first.ready and not second.ready
    assert first.reason == second.reason
    assert first.reason.startswith("registry_invalid:")
    assert counters.inspect == 2, "a not-ready verdict must be re-checked every call"

    parked.rename(registry)
    assert task_store.storage_readiness(repo).ready
    assert task_store.get_task(repo, "absent") is None


def test_reset_clears_the_readiness_memo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _ready_repo(tmp_path)
    _prime(repo)
    assert task_store.storage_readiness(repo).ready
    assert task_store._READINESS_MEMO
    task_store.reset_storage_readiness_cache()
    assert not task_store._READINESS_MEMO
    assert not task_store._READINESS_PATHS

    counters = _Counters(monkeypatch)
    assert task_store.storage_readiness(repo).ready
    assert counters.snapshot() == (1, 1, 1)
    assert not task_store._READINESS_MEMO, "the first call after a reset only records inputs"
    assert task_store.storage_readiness(repo).ready
    assert counters.snapshot() == (2, 2, 2)
    assert task_store.storage_readiness(repo).ready
    assert counters.snapshot() == (2, 2, 2)


def _count_full_checks(monkeypatch: pytest.MonkeyPatch, on_call=None) -> list[int]:
    calls: list[int] = []
    real_full = task_store._storage_readiness_full

    def wrapper(root):
        calls.append(1)
        result = real_full(root)
        if on_call is not None:
            on_call(len(calls))
        return result

    monkeypatch.setattr(task_store, "_storage_readiness_full", wrapper)
    return calls


def test_first_call_records_second_memoizes_third_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _ready_repo(tmp_path)
    key = str(repo.resolve())
    calls = _count_full_checks(monkeypatch)

    assert task_store.storage_readiness(repo).ready
    assert len(calls) == 1
    assert key not in task_store._READINESS_MEMO
    assert key in task_store._READINESS_PATHS

    assert task_store.storage_readiness(repo).ready
    assert len(calls) == 2
    assert key in task_store._READINESS_MEMO

    assert task_store.storage_readiness(repo).ready
    assert len(calls) == 2, "the third call is served from the memo"


def _replace_database_with_larger_copy(path: Path) -> None:
    import sqlite3

    copy = path.with_name(path.name + ".nf02059-grown")
    shutil.copyfile(path, copy)
    conn = sqlite3.connect(str(copy))
    try:
        conn.execute("CREATE TABLE nf02059_pad (blob BLOB)")
        conn.execute("INSERT INTO nf02059_pad VALUES (?)", (b"\x00" * 65536,))
        conn.commit()
    finally:
        conn.close()
    before = path.stat().st_size
    os.replace(copy, path)
    assert path.stat().st_size != before


def test_file_replaced_during_the_check_is_not_memoized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _ready_repo(tmp_path)
    db = _db_path(repo)
    task_store.reset_storage_readiness_cache()

    def on_call(count: int) -> None:
        if count == 2:
            _replace_database_with_larger_copy(db)

    calls = _count_full_checks(monkeypatch, on_call)

    task_store.storage_readiness(repo)  # primes the known inputs
    assert len(calls) == 1
    task_store.storage_readiness(repo)  # db replaced while this check runs
    assert len(calls) == 2
    assert str(repo.resolve()) not in task_store._READINESS_MEMO

    task_store.storage_readiness(repo)
    assert len(calls) == 3, "a verdict about the replaced bytes must not be served"


def test_memo_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_store, "_READINESS_MEMO_MAX_ENTRIES", 2)
    keys: list[str] = []
    for name in ("first", "second", "third"):
        repo = tmp_path / name
        repo.mkdir()
        task_store.initialize_repository(repo)
        _prime(repo)
        assert task_store.storage_readiness(repo).ready
        keys.append(str(repo.resolve()))

    assert list(task_store._READINESS_MEMO) == keys[1:], "the oldest root is evicted"
    assert list(task_store._READINESS_PATHS) == keys[1:]
