"""NF-2026-02419: card-local fences run before the dependency scan, and the
dependency scan never examines archived or decided rows' ``card_json``."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import task_retention, task_store


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _task_row(
    repo: Path,
    task_id: str,
    status: str,
    *,
    worker_status: str = "done",
    card: Mapping[str, Any] | None = None,
    archived_at: str = "",
) -> dict[str, Any]:
    stamp = "2026-08-30T00:00:00+00:00"
    body = {
        "task_id": task_id,
        "launch_request_id": f"req-{task_id}",
        "status": status,
        "worker_status": worker_status,
        "updated_at": stamp,
        **dict(card or {}),
    }
    connection = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        connection.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,"
            "created_at,updated_at,completed_at,archived_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, "runner", "coding", status, worker_status, json.dumps(body),
             stamp, stamp, stamp, archived_at),
        )
        connection.commit()
    finally:
        connection.close()
    return body


def _terminal_ledger():
    def latest(_root: Path, request_id: str) -> dict[str, Any]:
        return {
            "task_id": request_id.removeprefix("req-"),
            "request_id": request_id,
            "state": "accepted",
            "timestamp": "2026-08-31T00:00:01+00:00",
        }

    return latest


def _forbid_dependency_scan(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def spy(_root: Any, task_id: str) -> bool:
        calls.append(task_id)
        return False

    monkeypatch.setattr(task_retention, "_has_undecided_dependent", spy)
    return calls


def test_reserved_card_is_fenced_without_dependency_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    card = _task_row(repo, "DEP_RESERVED", "finished", card={"launch_reservation": {"id": "r1"}})
    calls = _forbid_dependency_scan(monkeypatch)
    monkeypatch.setattr(task_store, "get_task", lambda _root, _task_id: card)
    monkeypatch.setattr(task_retention, "_latest_process_row", _terminal_ledger())

    fenced, reason = task_retention._final_archive_fence(repo, "DEP_RESERVED", "req-DEP_RESERVED")

    assert fenced is None and reason == "task_reserved"
    assert calls == []


def test_retained_evidence_is_fenced_without_dependency_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    card = _task_row(
        repo, "DEP_RETAINED", "finished", card={"workspace": {"path": str(workspace)}}
    )
    calls = _forbid_dependency_scan(monkeypatch)
    monkeypatch.setattr(task_store, "get_task", lambda _root, _task_id: card)
    monkeypatch.setattr(task_retention, "_latest_process_row", _terminal_ledger())

    fenced, reason = task_retention._final_archive_fence(repo, "DEP_RETAINED", "req-DEP_RETAINED")

    assert fenced is None and reason == "retained_evidence"
    assert calls == []


def test_unfenced_candidate_still_reaches_dependency_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    card = _task_row(repo, "DEP_PLAIN", "finished")
    calls = _forbid_dependency_scan(monkeypatch)
    monkeypatch.setattr(task_store, "get_task", lambda _root, _task_id: card)
    monkeypatch.setattr(task_retention, "_latest_process_row", _terminal_ledger())

    fenced, reason = task_retention._final_archive_fence(repo, "DEP_PLAIN", "req-DEP_PLAIN")

    assert reason == "" and fenced == card
    assert calls == ["DEP_PLAIN"]


def test_archived_and_decided_dependents_do_not_fence_but_live_one_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    dependency = _task_row(repo, "DEP_DONE", "finished")
    depends = {"depends_on": ["DEP_DONE"]}
    _task_row(
        repo, "DEPENDENT_ARCHIVED", "pending", worker_status="unclaimed", card=depends,
        archived_at="2026-08-31T00:00:00+00:00",
    )
    _task_row(repo, "DEPENDENT_FINISHED", "finished", card=depends)
    _task_row(repo, "DEPENDENT_DONE_WORKER", "review", worker_status="done", card=depends)
    _task_row(repo, "DEPENDENT_SUPERSEDED", "superseded", worker_status="unclaimed", card=depends)
    monkeypatch.setattr(task_store, "get_task", lambda _root, _task_id: dependency)
    monkeypatch.setattr(task_retention, "_latest_process_row", _terminal_ledger())

    fenced, reason = task_retention._final_archive_fence(repo, "DEP_DONE", "req-DEP_DONE")
    assert reason == "" and fenced == dependency

    _task_row(repo, "DEPENDENT_LIVE", "pending", worker_status="unclaimed", card=depends)
    fenced, reason = task_retention._final_archive_fence(repo, "DEP_DONE", "req-DEP_DONE")
    assert fenced is None and reason == "dependency_live"


def test_dependency_scan_never_examines_archived_or_decided_card_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _task_row(repo, "DEP_DONE", "finished")
    depends = {"depends_on": ["DEP_DONE"]}
    _task_row(
        repo, "DEPENDENT_ARCHIVED", "pending", worker_status="unclaimed", card=depends,
        archived_at="2026-08-31T00:00:00+00:00",
    )
    _task_row(repo, "DEPENDENT_FINISHED", "finished", card=depends)
    _task_row(repo, "DEPENDENT_COMPLETED", "completed", worker_status="unclaimed", card=depends)
    _task_row(repo, "DEPENDENT_DONE_WORKER", "review", worker_status="done", card=depends)
    live = _task_row(repo, "DEPENDENT_LIVE", "pending", worker_status="unclaimed", card=depends)

    examined: list[str] = []

    def counting_instr(haystack: Any, needle: Any) -> int | None:
        if haystack is None or needle is None:
            return None
        examined.append(str(haystack))
        return str(haystack).find(str(needle)) + 1

    original = task_retention._connect

    def counting_connect(root: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original(root, **kwargs)
        connection.create_function("instr", 2, counting_instr)
        return connection

    monkeypatch.setattr(task_retention, "_connect", counting_connect)

    assert task_retention._has_undecided_dependent(repo, "DEP_DONE") is True
    assert examined == [json.dumps(live)]

    assert task_retention._has_undecided_dependent(repo, "NOBODY_DEPENDS_ON_ME") is False
