"""NF-2026-01701: allowed_writes_amendment on recover_blocked_rework."""
from __future__ import annotations

import inspect
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, task_store, task_templates  # noqa: E402

_NOW = "2026-10-26T00:00:00+00:00"
_RUNNER = "codex_worker_test"
_TOPIC = "aiworkhub_blocked_rework_recovery"
_MANAGER = "nf01701-manager"
_LAUNCH_REQUEST_ID = "d" * 32
_FEEDBACK = "NeedFix: the new test file must be in scope"
_MAX = task_templates.MAX_PATHS_PER_FIELD
_CONFLICT = "allowed_writes_amendment_conflicts_with_validation_only_replay"

_INVALID_WRITES = "invalid_allowed_writes"
_BAD_PATH = "invalid_allowed_write_path"
_RESERVED = "allowed_write_reserved_path"
_DIRECTORY = "allowed_write_is_directory"
_CONTRADICTORY = "contradictory_task_path_contract"

_TERMINAL_REVIEW_EVIDENCE = {
    "validation": [
        {"command": "pytest -q", "returncode": 1, "stdout": "1 failed", "stderr": ""}
    ],
    "required_outputs": [],
    "changed_path_hashes": {"src/aiworkhub/task_store.py": "abc123hash"},
}
_FAILED_VERIFICATION = {
    "applicable": True,
    "pass": False,
    "substatus": "validation_failed",
    "reason": "evidence_verdict_failed",
    "claim_epoch": 0,
    "evidence_verdict": {"passed": False, "validation_count": 1, "failed_validation_count": 1},
}


@pytest.fixture
def repo(monkeypatch, tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    (root / "docs_dir").mkdir()
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: _MANAGER)
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)
    return root


def _insert_blocked(root: Path, task_id: str, extra_card: dict | None = None) -> None:
    """A blocked validation_failed card with a retained terminal-review candidate."""
    _readiness, db_path = task_store._require_ready(root)
    card = {
        "task_id": task_id,
        "runner": _RUNNER,
        "topic": _TOPIC,
        "mode": "",
        "allowed_writes": ["src/aiworkhub/task_store.py"],
        "forbidden": ["secrets/"],
        "objective": "Implement blocked rework recovery",
        "launch_request_id": _LAUNCH_REQUEST_ID,
        "terminal_review": {
            "substatus": "validation_failed",
            "evidence": dict(_TERMINAL_REVIEW_EVIDENCE),
            "deterministic_verification": dict(_FAILED_VERIFICATION),
            "recorded_at": _NOW,
            "runner": _RUNNER,
            "claim_epoch": 0,
        },
        "terminal_substatus": "validation_failed",
        "deterministic_verification": dict(_FAILED_VERIFICATION),
        "blocker_reason": "validation_failed: tests failed",
        "blocked_at": _NOW,
        "blocked_by": _RUNNER,
        "claim_epoch": 0,
        "reject_review": {"to": "pending", "reason": _FEEDBACK, "recorded_at": _NOW},
        **(extra_card or {}),
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, started_at, "
            "completed_at) VALUES (?, ?, ?, 'blocked', 'validation_failed', '', '', "
            "?, ?, ?, '', '', '', ?)",
            (task_id, _RUNNER, _TOPIC, json.dumps(card), _NOW, _NOW, _NOW),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, 'terminal_review', ?, ?, ?)",
            (
                task_id,
                _RUNNER,
                json.dumps(
                    {"substatus": "validation_failed", "evidence": _TERMINAL_REVIEW_EVIDENCE}
                ),
                _NOW,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row_snapshot(root: Path, task_id: str) -> tuple:
    conn = sqlite3.connect(task_store.storage_readiness(root).canonical_db)
    try:
        row = conn.execute(
            "SELECT status, worker_status, card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=?", (task_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    return (*row, events)


def _must_not_run(*_args, **_kwargs):
    pytest.fail("refusal must happen before any read of the card or the write gate")


def _recover(task_id: str, **kwargs) -> dict:
    return core.recover_blocked_rework(
        task_id, topic=_TOPIC, feedback_reason=_FEEDBACK, **kwargs
    )


def test_recover_adds_the_path_keeps_the_candidate_and_records_the_amendment(repo):
    amended, control = "NF01701_AMENDED", "NF01701_CONTROL"
    _insert_blocked(repo, amended)
    _insert_blocked(repo, control)

    result = _recover(
        amended,
        allowed_writes_amendment=[
            "tests/test_new_nf01701.py",
            "src/aiworkhub/task_store.py",
            "./tests/test_new_nf01701.py",
        ],
    )
    baseline = _recover(control)

    assert result["ok"] is True, result
    assert baseline["ok"] is True, baseline
    assert result["allowed_writes_amendment_applied"] == ["tests/test_new_nf01701.py"]
    assert "allowed_writes_amendment_applied" not in baseline
    card = task_store.get_task(repo, amended)
    control_card = task_store.get_task(repo, control)
    assert (card["status"], card["worker_status"]) == ("pending", "unclaimed")
    assert card["allowed_writes"] == [
        "src/aiworkhub/task_store.py",
        "tests/test_new_nf01701.py",
    ]
    assert control_card["allowed_writes"] == ["src/aiworkhub/task_store.py"]
    # Same entry shape reject_review records (NF-2026-01641).
    (entry,) = card["allowed_writes_amendments"]
    assert set(entry) == {"paths", "actor", "request_id", "at"}
    assert entry["paths"] == ["tests/test_new_nf01701.py"]
    assert entry["actor"] == _MANAGER
    assert entry["request_id"] == _LAUNCH_REQUEST_ID
    assert datetime.fromisoformat(entry["at"]).utcoffset() == timedelta(0)
    assert "allowed_writes_amendments" not in control_card
    # The retained candidate is kept exactly as an unamended recovery keeps it.
    predecessor = card["recovery_predecessor"]
    assert predecessor["changed_path_hashes"] == {"src/aiworkhub/task_store.py": "abc123hash"}
    for key in ("recovery_predecessor", "recovery_epoch", "claim_epoch", "recovery_feedback"):
        assert card.get(key) == control_card.get(key), key
    assert ("rework_predecessor" in card) == ("rework_predecessor" in control_card)
    events = [event["event"] for event in task_store.get_task_events(repo, amended)]
    assert events.count("blocked_rework_recovery") == 1


def test_recover_applies_both_amendments_in_one_transaction(repo):
    task_id = "NF01701_BOTH"
    _insert_blocked(repo, task_id)

    result = _recover(
        task_id,
        validation_amendment=["pytest -q tests/test_new_nf01701.py"],
        allowed_writes_amendment=["tests/test_new_nf01701.py"],
    )

    assert result["ok"] is True, result
    assert result["validation_amendment_applied"] == ["pytest -q tests/test_new_nf01701.py"]
    assert result["allowed_writes_amendment_applied"] == ["tests/test_new_nf01701.py"]
    card = task_store.get_task(repo, task_id)
    assert card["validation"] == ["pytest -q tests/test_new_nf01701.py"]
    assert card["allowed_writes"] == ["src/aiworkhub/task_store.py", "tests/test_new_nf01701.py"]


def test_recover_amendment_of_an_existing_path_is_a_no_op(repo):
    task_id = "NF01701_NOOP"
    _insert_blocked(repo, task_id)

    result = _recover(task_id, allowed_writes_amendment=["./src/aiworkhub/task_store.py"])

    assert result["ok"] is True, result
    assert result["allowed_writes_amendment_applied"] == []
    card = task_store.get_task(repo, task_id)
    assert card["allowed_writes"] == ["src/aiworkhub/task_store.py"]
    assert "allowed_writes_amendments" not in card


@pytest.mark.parametrize("omitted", ["absent", "none", "empty"])
def test_recover_without_an_amendment_leaves_the_card_as_before(repo, omitted):
    task_id = "NF01701_OMIT_" + omitted.upper()
    _insert_blocked(repo, task_id)
    kwargs = {
        "absent": {},
        "none": {"allowed_writes_amendment": None},
        "empty": {"allowed_writes_amendment": []},
    }[omitted]

    result = _recover(task_id, **kwargs)

    assert result["ok"] is True, result
    assert "allowed_writes_amendment_applied" not in result
    card = task_store.get_task(repo, task_id)
    assert card["allowed_writes"] == ["src/aiworkhub/task_store.py"]
    assert "allowed_writes_amendments" not in card


def test_idempotent_replay_reports_no_phantom_paths(repo):
    task_id = "NF01701_TWICE"
    _insert_blocked(repo, task_id)

    first = _recover(task_id, allowed_writes_amendment=["tests/test_first_nf01701.py"])
    second = _recover(task_id, allowed_writes_amendment=["tests/test_second_nf01701.py"])

    assert first["ok"] is True, first
    assert first["allowed_writes_amendment_applied"] == ["tests/test_first_nf01701.py"]
    assert second["ok"] is True, second
    assert second["allowed_writes_amendment_applied"] == []
    card = task_store.get_task(repo, task_id)
    assert card["allowed_writes"] == [
        "src/aiworkhub/task_store.py",
        "tests/test_first_nf01701.py",
    ]
    assert len(card["allowed_writes_amendments"]) == 1


def test_recover_refuses_invalid_amendments_with_reject_review_codes(monkeypatch, repo):
    task_id = "NF01701_REFUSED"
    _insert_blocked(repo, task_id)
    monkeypatch.setattr(core, "_verified_manager_actor", _must_not_run)
    monkeypatch.setattr(core, "_canonical_write_gate", _must_not_run)
    before = _row_snapshot(repo, task_id)
    refused = [
        (["../outside.py"], _BAD_PATH),
        (["src/../../outside.py"], _BAD_PATH),
        (["/etc/passwd"], _BAD_PATH),
        (["C:/Windows/x.py"], _BAD_PATH),
        (["\\\\server\\share\\x.py"], _BAD_PATH),
        ([".git/config"], _RESERVED),
        ([".aiworkhub/runtime/state.db"], _RESERVED),
        (["docs/"], _DIRECTORY),
        (["docs_dir"], _DIRECTORY),
        (["secrets/keys.py"], _CONTRADICTORY),
        (["src/good.py", "../bad.py"], _BAD_PATH),
        ([f"src/file_{i}.py" for i in range(_MAX + 1)], _INVALID_WRITES),
        ("src/new.py", _INVALID_WRITES),
    ]

    for paths, code in refused:
        result = _recover(task_id, allowed_writes_amendment=paths)

        assert result["ok"] is False, paths
        assert result["returncode"] == 2, paths
        assert result["stderr"] == code, paths
        assert _row_snapshot(repo, task_id) == before, paths


def test_recover_refuses_a_merged_total_over_the_limit_without_state_change(repo):
    task_id = "NF01701_MERGED_LIMIT"
    existing = [f"src/existing_{i}.py" for i in range(_MAX - 1)]
    _insert_blocked(repo, task_id, {"allowed_writes": list(existing)})
    before = _row_snapshot(repo, task_id)

    refused = _recover(task_id, allowed_writes_amendment=["src/one.py", "src/two.py"])

    assert refused["ok"] is False
    assert refused["stderr"] == (
        "recover_blocked_rework_failed:"
        "invalid_allowed_writes_amendment:allowed_writes_amendment_exceeds_limit"
    )
    assert _row_snapshot(repo, task_id) == before


def test_amendment_with_validation_only_replay_is_refused_before_any_write(monkeypatch, repo):
    task_id = "NF01701_REPLAY"
    _insert_blocked(repo, task_id)
    monkeypatch.setattr(core, "_live_card", _must_not_run)
    monkeypatch.setattr(core, "_verified_manager_actor", _must_not_run)
    monkeypatch.setattr(core, "_canonical_write_gate", _must_not_run)
    monkeypatch.setattr(task_store, "recover_blocked_rework", _must_not_run)
    before = _row_snapshot(repo, task_id)

    result = _recover(
        task_id,
        validation_only_replay=True,
        allowed_writes_amendment=["tests/test_new_nf01701.py"],
    )

    assert result["ok"] is False
    assert result["stderr"] == _CONFLICT
    assert _row_snapshot(repo, task_id) == before


def test_task_store_refuses_the_replay_conflict_before_reading(monkeypatch, repo):
    task_id = "NF01701_STORE_REPLAY"
    _insert_blocked(repo, task_id)
    monkeypatch.setattr(task_store, "_require_ready", _must_not_run)

    assert task_store.recover_blocked_rework(
        repo,
        task_id,
        feedback_reason=_FEEDBACK,
        validation_only_replay=True,
        allowed_writes_amendment=["tests/test_new_nf01701.py"],
    ) == (False, _CONFLICT)


def test_signatures_default_the_amendment_to_none():
    for function in (core.recover_blocked_rework, task_store.recover_blocked_rework):
        parameter = inspect.signature(function).parameters["allowed_writes_amendment"]
        assert parameter.default is None


def test_server_forwards_allowed_writes_amendment_only_when_non_empty(monkeypatch, repo):
    server = pytest.importorskip("aiworkhub.server")
    calls: list[tuple[str, dict]] = []

    def fake_recover(task_id, **kwargs):
        calls.append((task_id, kwargs))
        return {"ok": False, "returncode": 1, "command": [], "stdout": "", "stderr": "stub"}

    monkeypatch.setattr(core, "recover_blocked_rework", fake_recover)

    server.aiworkhub_task_recover_blocked_rework("T1")
    server.aiworkhub_task_recover_blocked_rework("T1", allowed_writes_amendment=None)
    server.aiworkhub_task_recover_blocked_rework("T1", allowed_writes_amendment=[])
    server.aiworkhub_task_recover_blocked_rework(
        "T1", allowed_writes_amendment=["tests/test_new.py"]
    )

    base = {
        "feedback_reason": "",
        "validation_only_replay": False,
        "clean_root_if_predecessor_missing": False,
    }
    assert calls == [
        ("T1", base),
        ("T1", base),
        ("T1", base),
        ("T1", {**base, "allowed_writes_amendment": ["tests/test_new.py"]}),
    ]


def test_server_tool_declares_and_documents_the_parameter():
    server = pytest.importorskip("aiworkhub.server")
    tool = server.aiworkhub_task_recover_blocked_rework

    parameter = inspect.signature(tool).parameters["allowed_writes_amendment"]

    assert parameter.default is None
    args_lines = [
        line.strip()
        for line in (tool.__doc__ or "").splitlines()
        if line.strip().startswith("allowed_writes_amendment:")
    ]
    assert len(args_lines) == 1
    assert len(args_lines[0]) > len("allowed_writes_amendment:") + 20
