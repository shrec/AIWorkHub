"""NF-2026-01022: task_amend_pending can fix objective/required_outputs pre-launch.

- ``objective`` and ``required_outputs`` replace the card's values on a pending
  card that was never launched; the change is recorded in the card history and
  the card id is unchanged.
- Any launch history (a live launch, or a terminal one the card returned to
  pending from) refuses with ``amend_contract_after_launch``.
- Every required_outputs entry must be an exact allowed_writes entry (this
  call's own additions included).
- The MCP tool exposes and passes both parameters.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, manager_card_amend, task_store  # noqa: E402

TEST_FILE = "tests/test_widget.py"
VALIDATION = f"pytest -q {TEST_FILE}"
AFTER_LAUNCH = "task_amend_pending_refused:amend_contract_after_launch"


@pytest.fixture
def manager_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / TEST_FILE).write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    task_store.initialize_repository(root)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {"provider": "claude", "session_id": "SESSION-A", "route_state": "verified"},
    )
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **k: None)
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda _runner: (True, "test_route"))
    return root


def _insert_card(repo: Path, task_id: str, **overrides: Any) -> None:
    _readiness, db_path = task_store._require_ready(repo)
    card: dict[str, Any] = {
        "task_id": task_id,
        "runner": "claude",
        "topic": "coding",
        "status": "pending",
        "worker_status": "unclaimed",
        "objective": "fix the tpyo",
        "allowed_writes": ["src/widget.py", TEST_FILE],
        "required_outputs": ["src/widgte.py"],
        "read_first": [],
        "forbidden": [],
        "validation": [VALIDATION],
        "validation_roles": ["generic"],
        "work_kind": "generic",
        "needfix_ids": ["NF-2026-01022"],
        "history": [{"event": "created"}],
    }
    card.update(overrides)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, archived_at, started_at, claimed_by) "
            "VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, card["runner"], card["topic"], card["status"], card["worker_status"],
                card["objective"], json.dumps(card), "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00", str(overrides.get("archived_at") or ""),
                overrides.get("started_at"), overrides.get("claimed_by"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _stored(repo: Path, task_id: str) -> tuple[str, dict[str, Any]]:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT objective, card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0]), json.loads(row[1])


def test_objective_and_required_outputs_amend_never_launched_card(manager_repo: Path) -> None:
    _insert_card(manager_repo, "T_FIX")
    result = manager_card_amend.amend_pending(
        "T_FIX", objective="  fix the typo  ", required_outputs=["src/widget.py"]
    )
    assert result["ok"] is True, result
    assert result["state"] == "amended"
    assert result["task_id"] == "T_FIX"
    assert result["fields"] == ["objective", "required_outputs"]
    assert result["objective_before"] == "fix the tpyo"
    assert result["objective_after"] == "fix the typo"
    assert result["required_outputs_after"] == ["src/widget.py"]
    column, card = _stored(manager_repo, "T_FIX")
    assert column == "fix the typo"
    assert card["objective"] == "fix the typo"
    assert card["required_outputs"] == ["src/widget.py"]
    assert card["task_id"] == "T_FIX"
    assert card["needfix_ids"] == ["NF-2026-01022"]
    last = card["history"][-1]
    assert last["event"] == task_store.CARD_AMENDED_PENDING_EVENT
    assert last["fields"] == ["objective", "required_outputs"]
    assert last["required_outputs_before"] == ["src/widgte.py"]
    # Still a never-launched pending card, so it stays launchable/amendable.
    assert task_store.unlaunched_pending_refusal(card, card) == ""
    assert manager_card_amend.contract_launch_history(card, card) is False


def test_contract_amend_combines_with_allowed_writes_addition(manager_repo: Path) -> None:
    _insert_card(manager_repo, "T_COMBO")
    result = manager_card_amend.amend_pending(
        "T_COMBO", allowed_writes=["src/extra.py"], required_outputs=["src/extra.py"]
    )
    assert result["ok"] is True, result
    assert result["fields"] == ["allowed_writes", "required_outputs"]
    _column, card = _stored(manager_repo, "T_COMBO")
    assert "src/extra.py" in card["allowed_writes"]
    assert card["required_outputs"] == ["src/extra.py"]


def test_same_objective_is_unchanged(manager_repo: Path) -> None:
    _insert_card(manager_repo, "T_SAME")
    before = _stored(manager_repo, "T_SAME")
    result = manager_card_amend.amend_pending("T_SAME", objective="fix the tpyo")
    assert result["ok"] is True, result
    assert result["state"] == "unchanged"
    assert result["fields"] == []
    assert _stored(manager_repo, "T_SAME") == before


@pytest.mark.parametrize(
    "launch",
    [
        {"started_at": "2026-01-02T00:00:00+00:00", "launch_request_id": "R1"},
        # Back to pending after a reject: amendable for other fields, but the
        # contract a worker already read stays fixed.
        {"launch_request_id": "R1", "rejection_disposition": {"request_id": "R1"}},
        {"history": [{"event": "created"}, {"event": "launched", "request_id": "R0"}]},
    ],
)
@pytest.mark.parametrize(
    "amendment",
    [{"objective": "fix the typo"}, {"required_outputs": ["src/widget.py"]}],
)
def test_contract_amend_after_launch_refuses(
    manager_repo: Path, launch: dict[str, Any], amendment: dict[str, Any]
) -> None:
    _insert_card(manager_repo, "T_LAUNCHED", **launch)
    before = _stored(manager_repo, "T_LAUNCHED")
    result = manager_card_amend.amend_pending("T_LAUNCHED", **amendment)
    assert result["ok"] is False
    assert result["error"] == AFTER_LAUNCH
    assert _stored(manager_repo, "T_LAUNCHED") == before
    # Even the cancel-first path refuses: a cancel is itself launch history.
    cancelled = manager_card_amend.amend_pending(
        "T_LAUNCHED", cancel_live_launch=True, **amendment
    )
    assert cancelled["error"] == AFTER_LAUNCH
    assert _stored(manager_repo, "T_LAUNCHED") == before


def test_contract_amend_after_launch_store_gate(manager_repo: Path) -> None:
    """The transaction re-decides launch history, not only the pre-check."""
    _insert_card(manager_repo, "T_RACE", started_at="2026-01-02T00:00:00+00:00")
    ok, reason, _detail = manager_card_amend._amend_pending_contract(
        manager_repo, "T_RACE", actor="manager", objective="x", required_outputs=None
    )
    assert (ok, reason) == (False, "amend_contract_after_launch")


@pytest.mark.parametrize(
    "outputs", [["src/elsewhere.py"], ["src/widget.py", "src/elsewhere.py"], ["src/*.py"]]
)
def test_required_outputs_outside_allowed_writes_refuses(
    manager_repo: Path, outputs: list[str]
) -> None:
    _insert_card(manager_repo, "T_SCOPE")
    before = _stored(manager_repo, "T_SCOPE")
    result = manager_card_amend.amend_pending("T_SCOPE", required_outputs=outputs)
    assert result["ok"] is False
    outside = next(path for path in outputs if path not in ("src/widget.py", TEST_FILE))
    assert result["error"] == (
        f"task_amend_pending_refused:required_output_outside_allowed_writes:{outside}"
    )
    assert _stored(manager_repo, "T_SCOPE") == before


def test_required_outputs_store_gate_refuses_outside_scope(manager_repo: Path) -> None:
    _insert_card(manager_repo, "T_STORE_SCOPE")
    ok, reason, _detail = manager_card_amend._amend_pending_contract(
        manager_repo, "T_STORE_SCOPE", actor="manager", objective=None,
        required_outputs=["src/elsewhere.py"],
    )
    assert ok is False
    assert reason == "required_output_outside_allowed_writes:src/elsewhere.py"


@pytest.mark.parametrize(
    ("amendment", "code"),
    [
        ({"objective": "   "}, "invalid_objective:empty"),
        ({"objective": 7}, "invalid_objective:not_a_string:int"),
        ({"objective": "x" * 4001}, "invalid_objective:too_long:4001_chars_exceeds_limit_4000"),
        ({"required_outputs": []}, "invalid_required_outputs"),
        ({"required_outputs": [""]}, "invalid_required_outputs"),
    ],
)
def test_invalid_contract_values_refuse(
    manager_repo: Path, amendment: dict[str, Any], code: str
) -> None:
    _insert_card(manager_repo, "T_BAD")
    before = _stored(manager_repo, "T_BAD")
    result = manager_card_amend.amend_pending("T_BAD", **amendment)
    assert result["ok"] is False
    assert result["error"] == code
    assert _stored(manager_repo, "T_BAD") == before


def test_mcp_tool_exposes_and_passes_contract_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiworkhub import server

    target = inspect.unwrap(server.aiworkhub_task_amend_pending)
    parameters = inspect.signature(target).parameters
    assert "objective" in parameters
    assert "required_outputs" in parameters
    assert "amend_contract_after_launch" in (target.__doc__ or "")
    captured: dict[str, Any] = {}

    def _fake(task_id: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(task_id=task_id, **kwargs)
        return {"ok": False, "error": "captured"}

    monkeypatch.setattr(manager_card_amend, "amend_pending", _fake)
    result = target("T_MCP", objective="new", required_outputs=["src/widget.py"])
    assert result == {"ok": False, "error": "captured"}
    assert captured["objective"] == "new"
    assert captured["required_outputs"] == ["src/widget.py"]
