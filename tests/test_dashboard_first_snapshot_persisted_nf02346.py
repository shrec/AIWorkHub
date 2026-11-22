"""NF-2026-02346: a fresh dashboard reader serves persisted ledger/history.

A new dashboard reader process starts with empty in-memory memos and paid the
full cost ledger and history series build on its first snapshot. Both are
now persisted under ``.aiworkhub/runtime/`` keyed by a cheap input
fingerprint. These tests simulate the fresh process by dropping the
in-memory memos and assert that:

* unchanged inputs answer from the persisted file, identical to a cold build,
  without the expensive inner collection running (counted);
* any input change (new task row, process log, DB file) forces a rebuild;
* a corrupt, truncated or foreign-schema file is ignored and rebuilt;
* no thread is started on the reader path.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import cost_ledger, dashboard, history_series, sqlite_readonly, task_store


_FILTERS = {"include_tasks": True}


def _ready_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    readiness = task_store.storage_readiness(repo)
    assert readiness.ready, readiness.reason
    task_store.reset_storage_readiness_cache()
    return repo


def _synthetic_rows() -> list[dict[str, Any]]:
    return [
        {
            "source": "canonical_usage_event",
            "task_id": f"T-{index % 3}",
            "runner": "runner_a" if index % 2 else "runner_b",
            "topic": "dashboard-performance",
            "model": "model-a",
            "provider": "provider-a",
            "role": "worker" if index % 4 else "reviewer",
            "role_observed": True,
            "records": 1,
            "input_tokens": 5 + index,
            "output_tokens": 3,
            "total_tokens": 8 + index,
            "cached_input_tokens": index,
            "cache_creation_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "cache_metrics_observed": bool(index % 2),
            "cost_usd": 0.01 * index,
            "cost_known": index != 2,
            "day": f"2026-11-{10 + index % 3:02d}",
        }
        for index in range(7)
    ]


@pytest.fixture
def counted_inputs(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count the expensive ledger collection; enrich it with real-shaped rows."""
    calls = {"count": 0}
    original = cost_ledger._read_ledger_inputs  # noqa: SLF001

    def counting(repo_root: Any, *, through_event_id: int | None) -> dict[str, Any]:
        calls["count"] += 1
        inputs = original(repo_root, through_event_id=through_event_id)
        inputs["rows"] = list(inputs["rows"]) + _synthetic_rows()
        inputs["manager_decisions"] = {
            "T-0": {"decision": "accepted"},
            "T-1": {"decision": "rejected"},
        }
        return inputs

    monkeypatch.setattr(cost_ledger, "_read_ledger_inputs", counting)
    # Claude Code transcripts are outside the persisted payload; pin them so
    # the comparison is about the persisted ledger only.
    monkeypatch.setattr(
        cost_ledger.claude_code_usage,
        "collect_claude_code_usage",
        lambda _root: {"sessions": [], "source_status": {"ok": True}},
    )
    cost_ledger.reset_cost_ledger_cache()
    yield calls
    cost_ledger.reset_cost_ledger_cache()


def _fresh_process() -> None:
    """What a new reader process sees: no in-memory memo of either payload."""
    cost_ledger.reset_cost_ledger_cache()
    history_series._cache.clear()  # noqa: SLF001


def _ledger(repo: Path) -> dict[str, Any]:
    return cost_ledger.build_cost_ledger(
        repo_root=repo, shared_read_only=True, persist=True, **_FILTERS
    )


def _canonical(value: Any) -> str:
    """Order- and type-exact rendering (tuples stay tuples, key order kept)."""
    return repr(copy.deepcopy(value))


def _ledger_file(repo: Path) -> Path:
    return cost_ledger.persisted_payload_path(
        repo, cost_ledger._PERSISTED_LEDGER_NAME  # noqa: SLF001
    )


def _history_file(repo: Path) -> Path:
    return cost_ledger.persisted_payload_path(
        repo, history_series._PERSISTED_HISTORY_NAME  # noqa: SLF001
    )


def _insert_task_row(repo: Path, task_id: str) -> None:
    conn = sqlite3.connect(str(task_store.canonical_db_path(repo)))
    try:
        now = "2026-11-22T00:00:00+00:00"
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, mode, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at, claimed_by, "
            "claimed_at, started_at, completed_at, origin_thread_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, "runner_a", "dashboard-performance", "", "pending",
                "unclaimed", "", "", json.dumps({"task_id": task_id}), now, now,
                None, None, None, None, "",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _touch_db_file(repo: Path) -> None:
    conn = sqlite3.connect(str(task_store.canonical_db_path(repo)))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS nf02346_probe(x INTEGER)")
        conn.execute("INSERT INTO nf02346_probe(x) VALUES (1)")
        conn.commit()
    finally:
        conn.close()


def _append_process_log(repo: Path) -> None:
    path = repo / ".aiworkhub" / "runtime" / "process_logs" / "process_events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "nf02346_probe"}) + "\n")


# ------------------------------------------------------------------ cost ledger


def test_fresh_reader_serves_persisted_ledger_identical_without_rebuild(
    tmp_path: Path, counted_inputs: dict[str, int]
) -> None:
    repo = _ready_repo(tmp_path)
    cold = _ledger(repo)
    assert counted_inputs["count"] == 1
    assert _ledger_file(repo).is_file()
    assert cold["counts"]["usage_rows"] == 7

    _fresh_process()
    threads_before = {thread.ident for thread in threading.enumerate()}
    warm = _ledger(repo)
    threads_after = {thread.ident for thread in threading.enumerate()}

    assert counted_inputs["count"] == 1, "unchanged inputs: no full collection"
    assert _canonical(warm) == _canonical(cold)
    assert warm == cold
    assert threads_after == threads_before, "no thread on the reader path"
    assert cost_ledger.LEDGER_CACHE_STATS["full_builds"] == 0
    # The persisted ledger is as read-only as a memoized one.
    with pytest.raises(TypeError):
        warm["aggregates"]["by_runner"]["x"] = 1  # type: ignore[index]


def test_fresh_dashboard_provider_serves_persisted_ledger(
    tmp_path: Path, counted_inputs: dict[str, int]
) -> None:
    repo = _ready_repo(tmp_path)
    cold = dashboard.DashboardProvider(repo_root=repo)._shared_cost_ledger(  # noqa: SLF001
        include_tasks=True
    )
    assert counted_inputs["count"] == 1

    _fresh_process()
    warm = dashboard.DashboardProvider(repo_root=repo)._shared_cost_ledger(  # noqa: SLF001
        include_tasks=True
    )
    assert counted_inputs["count"] == 1
    assert _canonical(warm) == _canonical(cold)


def test_non_persisting_callers_never_read_or_write_the_file(
    tmp_path: Path, counted_inputs: dict[str, int]
) -> None:
    repo = _ready_repo(tmp_path)
    cost_ledger.build_cost_ledger(repo_root=repo, include_tasks=True)
    assert not _ledger_file(repo).exists()
    _fresh_process()
    _ledger(repo)
    assert _ledger_file(repo).is_file()
    _fresh_process()
    cost_ledger.build_cost_ledger(repo_root=repo, include_tasks=True)
    assert counted_inputs["count"] == 3, "the default caller stays memo-only"


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda repo: _insert_task_row(repo, "T-new"), id="new_task_row"),
        pytest.param(_append_process_log, id="new_process_log"),
        pytest.param(_touch_db_file, id="changed_db_file"),
    ],
)
def test_any_input_change_invalidates_the_persisted_ledger(
    tmp_path: Path, counted_inputs: dict[str, int], change: Any
) -> None:
    repo = _ready_repo(tmp_path)
    _ledger(repo)
    assert counted_inputs["count"] == 1

    change(repo)
    _fresh_process()
    rebuilt = _ledger(repo)
    assert counted_inputs["count"] == 2, "a changed input forces a full rebuild"
    assert cost_ledger.LEDGER_CACHE_STATS["full_builds"] == 1

    # The rebuild re-persisted under the new key: the next fresh reader hits.
    _fresh_process()
    again = _ledger(repo)
    assert counted_inputs["count"] == 2
    assert _canonical(again) == _canonical(rebuilt)


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(lambda raw: b"{not json", id="corrupt"),
        pytest.param(lambda raw: raw[: len(raw) // 2], id="truncated"),
        pytest.param(lambda raw: b"", id="empty"),
        pytest.param(
            lambda raw: json.dumps(
                {**json.loads(raw), "schema_id": "someone.else.v9"}
            ).encode(),
            id="foreign_schema",
        ),
        pytest.param(
            lambda raw: json.dumps(
                {**json.loads(raw), "value": {"x": [1, 2]}}
            ).encode(),
            id="foreign_container_tag",
        ),
        pytest.param(lambda raw: b"[1, 2, 3]", id="not_an_object"),
    ],
)
def test_damaged_persisted_ledger_is_ignored_and_rebuilt(
    tmp_path: Path, counted_inputs: dict[str, int], damage: Any
) -> None:
    repo = _ready_repo(tmp_path)
    cold = _ledger(repo)
    path = _ledger_file(repo)
    path.write_bytes(damage(path.read_bytes()))

    _fresh_process()
    rebuilt = _ledger(repo)
    assert counted_inputs["count"] == 2
    assert _canonical(rebuilt) == _canonical(cold)
    # The rebuild replaced the damaged file with a readable one.
    _fresh_process()
    assert _canonical(_ledger(repo)) == _canonical(cold)
    assert counted_inputs["count"] == 2


def test_codec_round_trips_tuples_non_string_keys_and_order() -> None:
    value = {
        "b": (1, "x", (2.5, None)),
        "a": [True, {3: "int-key", ("k", 1): "tuple-key"}],
    }
    encoded = json.loads(json.dumps(cost_ledger._persist_encode(value)))  # noqa: SLF001
    decoded = cost_ledger._persist_decode(encoded)  # noqa: SLF001
    assert repr(decoded) == repr(value)
    with pytest.raises(TypeError):
        cost_ledger._persist_encode({"bad": object()})  # noqa: SLF001


def test_unpersistable_value_is_counted_never_raised(tmp_path: Path) -> None:
    path = tmp_path / ".aiworkhub" / "runtime" / "x.json"
    before = cost_ledger.PERSISTED_CACHE_STATS["write_errors"]
    assert cost_ledger.write_persisted_payload(path, ["k"], {"v": object()}) is False
    assert cost_ledger.PERSISTED_CACHE_STATS["write_errors"] == before + 1
    assert not path.exists()


# --------------------------------------------------------------- history series


@pytest.fixture
def counted_history(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"count": 0}
    original = sqlite_readonly.connect_readonly

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(history_series.sqlite_readonly, "connect_readonly", counting)
    history_series._cache.clear()  # noqa: SLF001
    yield calls
    history_series._cache.clear()  # noqa: SLF001


def _history(repo: Path) -> dict[str, Any]:
    return history_series.build_history_series(repo, persist=True)


def test_fresh_reader_serves_persisted_history_identical_without_queries(
    tmp_path: Path, counted_history: dict[str, int]
) -> None:
    repo = _ready_repo(tmp_path)
    cold = _history(repo)
    assert cold["measured"] is True
    assert counted_history["count"] == 1
    assert _history_file(repo).is_file()

    _fresh_process()
    threads_before = {thread.ident for thread in threading.enumerate()}
    warm = _history(repo)
    assert {thread.ident for thread in threading.enumerate()} == threads_before
    assert counted_history["count"] == 1, "unchanged store: no queries"
    assert _canonical(warm) == _canonical(cold)

    _fresh_process()
    provider_warm = dashboard.DashboardProvider(repo_root=repo).get_history_series()
    assert counted_history["count"] == 1
    assert _canonical(provider_warm) == _canonical(cold)


def test_history_without_persist_never_writes(
    tmp_path: Path, counted_history: dict[str, int]
) -> None:
    repo = _ready_repo(tmp_path)
    history_series.build_history_series(repo)
    assert not _history_file(repo).exists()


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda repo: _insert_task_row(repo, "T-new"), id="new_task_row"),
        pytest.param(_touch_db_file, id="changed_db_file"),
    ],
)
def test_store_change_invalidates_persisted_history(
    tmp_path: Path, counted_history: dict[str, int], change: Any
) -> None:
    repo = _ready_repo(tmp_path)
    _history(repo)
    change(repo)
    _fresh_process()
    _history(repo)
    assert counted_history["count"] == 2


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"\x00garbage", id="corrupt"),
        pytest.param(b'{"schema_id": "other", "key": [], "value": []}', id="foreign"),
    ],
)
def test_damaged_persisted_history_is_ignored_and_rebuilt(
    tmp_path: Path, counted_history: dict[str, int], payload: bytes
) -> None:
    repo = _ready_repo(tmp_path)
    cold = _history(repo)
    _history_file(repo).write_bytes(payload)
    _fresh_process()
    rebuilt = _history(repo)
    assert counted_history["count"] == 2
    assert rebuilt["measured"] is True
    assert rebuilt["daily_outcomes"] == cold["daily_outcomes"]


# ------------------------------------------------------------------ code stamp


def test_persisted_code_stamp_covers_every_package_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review finding: a release editing only task_store.py must miss too."""
    before = cost_ledger.persisted_code_stamp.__wrapped__()
    assert before == cost_ledger.persisted_code_stamp()
    original = cost_ledger.persisted_stamp

    def edited(path: Path | str) -> list[Any]:
        stamp = original(path)
        return [*stamp, "edited"] if Path(path).name == "task_store.py" else stamp

    monkeypatch.setattr(cost_ledger, "persisted_stamp", edited)
    assert cost_ledger.persisted_code_stamp.__wrapped__() != before


def test_a_code_stamp_change_invalidates_both_persisted_payloads(
    tmp_path: Path, counted_inputs: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _ready_repo(tmp_path)
    _ledger(repo)
    history_series.build_history_series(repo, persist=True)
    history_key = json.loads(_history_file(repo).read_text(encoding="utf-8"))["key"]
    assert counted_inputs["count"] == 1

    monkeypatch.setattr(cost_ledger, "persisted_code_stamp", lambda: "other-code")
    _fresh_process()
    _ledger(repo)
    history_series.build_history_series(repo, persist=True)
    assert counted_inputs["count"] == 2, "a changed package stamp forces a rebuild"
    assert json.loads(_history_file(repo).read_text(encoding="utf-8"))["key"] != history_key
