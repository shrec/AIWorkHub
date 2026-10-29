"""NF-2026-01832: a base-drift rework reject is infrastructure, proven by git."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, launch_replay_guard, task_store, terminal_failure_classification, workforce_catalog  # noqa: E402,E501
from aiworkhub.learning_commit import (  # noqa: E402
    INFRASTRUCTURE_FAILURE_CATEGORIES,
    FailureCategory,
)

_NOW = "2026-10-27T00:00:00+00:00"
_RUNNER = "claude_task_mcp_runtime_wiring"
_ADAPTER = "claude_cli"
_TOPIC = "task_mcp"
_MANAGER = "nf01832-manager"
_CAPPED = "reject_depth_cap_reached:2:2:decompose_or_supersede_required"
_CANDIDATE_PATH = "src/existing.py"
_CONTRACT = {
    "objective": "objective",
    "acceptance": ["the cap holds"],
    "allowed_writes": [_CANDIDATE_PATH],
    "validation": ["pytest -q tests/existing.py"],
    "required_outputs": [_CANDIDATE_PATH],
}


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git", "-c", "user.name=nf01832", "-c", "user.email=nf01832@example.invalid",
            "-c", "commit.gpgsign=false", *args,
        ],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


def _commit(root: Path, relative: str, text: str) -> str:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(root, "add", "--", relative)
    _git(root, "commit", "-m", f"change {relative}")
    return _git(root, "rev-parse", "HEAD")


def _db(root: Path) -> str:
    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    return readiness.canonical_db


def _insert_task(root: Path, task_id: str) -> None:
    card = {"origin_thread_id": "nf01832-thread", **_CONTRACT}
    conn = sqlite3.connect(_db(root))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, origin_thread_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, _RUNNER, _TOPIC, "solo", "pending", "unclaimed", "normal", "objective",
             json.dumps(card), _NOW, _NOW, None, "nf01832-thread"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_review(root: Path, task_id: str, request_id: str, base_oid: str | None) -> None:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        card = json.loads(row[0])
        terminal = card.get("terminal_review")
        terminal = terminal if isinstance(terminal, dict) else {}
        evidence = terminal.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        evidence["request_identity"] = {"request_id": request_id, "task_id": task_id}
        if base_oid is not None:
            evidence["workspace"] = {"base_oid": base_oid}
            evidence["changed_paths"] = [_CANDIDATE_PATH]
        terminal["evidence"] = evidence
        card["terminal_review"] = terminal
        conn.execute("UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), task_id))
        conn.commit()
    finally:
        conn.close()


def _to_review(root: Path, task_id: str, request_id: str, base_oid: str | None = None) -> None:
    assert core.claim_start_exact(task_id, _RUNNER, _TOPIC)["ok"] is True
    assert core.mark_review(task_id, runner=_RUNNER, topic=_TOPIC)["ok"] is True
    _seed_review(root, task_id, request_id, base_oid)


def _launch_card(task_id: str) -> dict:
    return json.loads(core.show_task(task_id)["stdout"])


def _refusal(root: Path, task_id: str) -> str:
    return launch_replay_guard.identical_relaunch_refusal(
        _launch_card(task_id), runner=_RUNNER, adapter_id=_ADAPTER, repo=root
    )


def _reject_rows(root: Path, task_id: str) -> list[dict]:
    conn = sqlite3.connect(_db(root))
    try:
        rows = conn.execute(
            "SELECT payload_json FROM task_events WHERE task_id=? AND event='reject_review' "
            "ORDER BY event_id",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row[0]) for row in rows]


def _store_snapshot(root: Path, task_id: str) -> tuple:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute(
            "SELECT status, worker_status, card_json, updated_at FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        events = conn.execute(
            "SELECT event_id, event, payload_json FROM task_events WHERE task_id=? "
            "ORDER BY event_id",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    return tuple(row), tuple(events)


@pytest.fixture
def repo(monkeypatch, tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: _MANAGER)
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)
    _git(root, "init", "-q")
    return root


@pytest.fixture
def drifted(repo) -> tuple[str, str]:
    """(base_oid, head_oid): canonical main changed the candidate path after base."""
    base = _commit(repo, _CANDIDATE_PATH, "value = 1\n")
    head = _commit(repo, _CANDIDATE_PATH, "value = 2\n")
    return base, head


@pytest.fixture
def category(monkeypatch) -> dict:
    chosen = {"value": FailureCategory.CANDIDATE_CODE}
    monkeypatch.setattr(core, "classify_terminal_disposition", lambda _card: chosen["value"])
    return chosen


def _reject_drift(task_id: str, **kwargs) -> dict:
    return core.reject_review(
        task_id, "rebase needed", topic=_TOPIC, failure_category="base_drift", **kwargs
    )


def test_base_drift_is_a_registered_infrastructure_category():
    assert FailureCategory.BASE_DRIFT.value == "base_drift"
    assert FailureCategory.BASE_DRIFT in INFRASTRUCTURE_FAILURE_CATEGORIES
    assert "failure_category:base_drift" in workforce_catalog._RATE_EXCLUDED_CLASSES
    assert "failure_category:base_drift" not in workforce_catalog._RATE_DENOMINATOR_CLASSES
    assert "base_drift" in terminal_failure_classification.MANAGER_REJECTION_INTENT_CAUSES


def test_proven_base_drift_reject_records_the_category_and_git_proof(repo, drifted):
    base, head = drifted
    task_id = "NF01832_PROVEN"
    _insert_task(repo, task_id)
    _to_review(repo, task_id, "a" * 32, base)

    result = _reject_drift(task_id)

    assert result["ok"] is True, result
    (row,) = _reject_rows(repo, task_id)
    assert row["failure_category"] == "base_drift"
    assert row["terminal_disposition"] == "base_drift"
    assert row["failure_category_source"] == "manager_explicit_infrastructure"
    assert row["base_drift_proof"]["base_oid"] == base
    assert row["base_drift_proof"]["head_oid"] == head
    assert row["base_drift_proof"]["drifted_paths"] == [_CANDIDATE_PATH]
    pinned = task_store.get_task(repo, task_id)["rejection_disposition"]
    assert pinned["failure_category"] == "base_drift"
    assert pinned["base_drift_proof"] == row["base_drift_proof"]


def _no_drift_head_unchanged(repo: Path) -> str:
    base = _commit(repo, _CANDIDATE_PATH, "value = 1\n")
    _commit(repo, "src/unrelated.py", "other = 1\n")
    return base


def _no_drift_head_is_base(repo: Path) -> str:
    return _commit(repo, _CANDIDATE_PATH, "value = 1\n")


def _base_not_ancestor(repo: Path) -> str:
    _commit(repo, _CANDIDATE_PATH, "value = 1\n")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _git(repo, "checkout", "-q", "--orphan", "elsewhere")
    stray = _commit(repo, _CANDIDATE_PATH, "value = 99\n")
    _git(repo, "checkout", "-q", "-f", branch)
    return stray


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        (_no_drift_head_unchanged, "no_candidate_path_drift"),
        (_no_drift_head_is_base, "no_candidate_path_drift"),
        (_base_not_ancestor, "base_not_ancestor_of_head"),
    ],
    ids=["head_moved_elsewhere", "head_is_base", "base_not_ancestor"],
)
def test_base_drift_without_git_proof_is_refused_and_writes_nothing(repo, scenario, reason):
    base = scenario(repo)
    task_id = "NF01832_UNPROVEN"
    _insert_task(repo, task_id)
    _to_review(repo, task_id, "b" * 32, base)
    before = _store_snapshot(repo, task_id)

    result = _reject_drift(task_id)

    assert result["ok"] is False
    assert f"base_drift_unproven:{reason}" in json.dumps(result)
    assert _store_snapshot(repo, task_id) == before


def test_base_drift_without_a_recorded_base_oid_is_refused(repo, drifted):
    task_id = "NF01832_NO_BASE"
    _insert_task(repo, task_id)
    _to_review(repo, task_id, "c" * 32, None)
    before = _store_snapshot(repo, task_id)

    result = _reject_drift(task_id)

    assert result["ok"] is False
    assert "base_drift_unproven:base_oid_unrecorded" in json.dumps(result)
    assert _store_snapshot(repo, task_id) == before


def test_two_base_drift_rejects_do_not_cap_the_relaunch(repo, drifted):
    base, _head = drifted
    task_id = "NF01832_NO_CAP"
    _insert_task(repo, task_id)
    for request_id in ("d" * 32, "e" * 32):
        _to_review(repo, task_id, request_id, base)
        result = _reject_drift(task_id)
        assert result["ok"] is True, result
        assert "rejection_depth" not in result

    assert _refusal(repo, task_id) == ""


def test_two_candidate_code_rejects_still_cap_the_relaunch(repo, category):
    task_id = "NF01832_STILL_CAPPED"
    _insert_task(repo, task_id)
    for request_id in ("f" * 32, "1" * 32):
        _to_review(repo, task_id, request_id)
        result = core.reject_review(task_id, "still wrong", topic=_TOPIC)
        assert result["ok"] is True, result

    assert _refusal(repo, task_id) == _CAPPED
