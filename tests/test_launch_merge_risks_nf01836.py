from __future__ import annotations

import inspect
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, process_launcher, task_store  # noqa: E402


def _insert_card(repo, task_id, *, status="pending", worker_status="unclaimed",
                 allowed_writes=None, **extra):
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-01-01T00:00:00+00:00"
    card = {
        "task_id": task_id, "runner": "codex_a", "topic": "coding",
        "allowed_writes": allowed_writes or [], "depends_on": [], "priority": "",
        "status": status, "worker_status": worker_status, **extra,
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, archived_at) "
            "VALUES (?, 'codex_a', 'coding', ?, ?, '', '', ?, ?, ?, '')",
            (task_id, status, worker_status, json.dumps(card), now, now),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _repo_env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    return repo


def _report(task_id="candidate"):
    result = core.launch_collision_guard(task_id=task_id, print_json=True)
    return result, json.loads(result["stdout"])


def _launched(repo, task_id, status, paths):
    _insert_card(
        repo,
        task_id,
        status=status,
        worker_status="claimed",
        launch_request_id=f"request-{task_id}",
        allowed_writes=paths,
    )


def test_processing_overlap_is_listed_with_paths():
    repo = core.repo_root()
    _launched(repo, "owner", "processing", ["shared.py", "other.py"])
    _insert_card(repo, "candidate", allowed_writes=["shared.py"])

    result, payload = _report()

    assert result["ok"] is True
    assert payload["collision_free"] is True
    assert payload["blockers"] == []
    assert payload["merge_risks"] == [
        {"task_id": "owner", "lifecycle": "processing", "paths": ["shared.py"]}
    ]


def test_review_overlap_is_listed_sorted_by_task_id():
    repo = core.repo_root()
    _launched(repo, "zeta", "review", ["shared.py"])
    _launched(repo, "alpha", "processing", ["shared.py"])
    _insert_card(repo, "candidate", allowed_writes=["shared.py"])

    result, payload = _report()

    assert result["ok"] is True
    assert payload["blockers"] == []
    assert [risk["task_id"] for risk in payload["merge_risks"]] == ["alpha", "zeta"]
    assert payload["merge_risks"][1]["lifecycle"] == "review"
    assert payload["merge_risks"][1]["paths"] == ["shared.py"]


def test_pending_overlap_is_not_a_merge_risk():
    repo = core.repo_root()
    _insert_card(repo, "other", allowed_writes=["shared.py"])
    _insert_card(repo, "candidate", allowed_writes=["shared.py"])

    result, payload = _report()

    assert result["ok"] is True
    assert payload["collision_free"] is True
    assert payload["blockers"] == []
    assert payload["merge_risks"] == []


def test_non_overlapping_processing_card_is_not_a_merge_risk():
    repo = core.repo_root()
    _launched(repo, "owner", "processing", ["elsewhere.py"])
    _insert_card(repo, "candidate", allowed_writes=["shared.py"])

    result, payload = _report()

    assert result["ok"] is True
    assert payload["blockers"] == []
    assert payload["merge_risks"] == []


def test_in_place_owner_still_blocks_and_is_not_a_merge_risk():
    repo = core.repo_root()
    _insert_card(
        repo, "owner", status="processing", worker_status="claimed",
        allowed_writes=["shared.py"],
    )
    _insert_card(repo, "candidate", allowed_writes=["shared.py"])

    result, payload = _report()

    assert result["ok"] is False
    assert payload["collision_free"] is False
    assert [blocker["task_id"] for blocker in payload["blockers"]] == ["owner"]
    assert payload["merge_risks"] == []


def _launcher_with_guard(guard):
    for _name, cls in inspect.getmembers(process_launcher, inspect.isclass):
        if "_launch_merge_risks" in vars(cls):
            return lambda: cls._launch_merge_risks(
                SimpleNamespace(_collision_guard=guard), "candidate"
            )
    raise AssertionError("launcher class with _launch_merge_risks not found")


def test_launcher_helper_returns_merge_risks_from_valid_guard_json():
    risks = [{"task_id": "owner", "lifecycle": "processing", "paths": ["a.py"]}]

    def guard(**_kwargs):
        return {
            "ok": True,
            "returncode": 0,
            "stdout": json.dumps({"merge_risks": risks}),
        }

    assert _launcher_with_guard(guard)() == risks


def test_launcher_helper_returns_empty_for_garbage_stdout():
    def guard(**_kwargs):
        return {"ok": True, "returncode": 0, "stdout": "not json"}

    assert _launcher_with_guard(guard)() == []


def test_launcher_helper_returns_empty_for_nonzero_returncode():
    def guard(**_kwargs):
        return {
            "ok": False,
            "returncode": 1,
            "stdout": json.dumps({"merge_risks": [{"task_id": "x"}]}),
        }

    assert _launcher_with_guard(guard)() == []


def test_launcher_helper_returns_empty_when_guard_raises():
    def guard(**_kwargs):
        raise RuntimeError("boom")

    assert _launcher_with_guard(guard)() == []
