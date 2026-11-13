"""NF-2026-02252: task_amend_pending can change the runner of an unlaunched card.

- ``amend_pending(task_id, runner=...)`` folds the runner like task_create,
  refuses the reserved manager identity and a runner with no workforce route,
  and persists it (card_json, the ``runner`` column and the card history)
  inside the store's compare-and-swap, returning ``runner_before``/``runner_after``.
- A launched card refuses the runner amendment like any other amendment.
- The MCP tool ``aiworkhub_task_amend_pending`` exposes and passes ``runner``.
- task_create still accepts an unroutable runner (advisory only) and its
  receipt names the amend remedy.
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

from aiworkhub import core, manager_card_amend, task_create_warnings  # noqa: E402
from aiworkhub import task_store, workforce_catalog  # noqa: E402

ROUTED = "claude_opus-5"
UNROUTED = "phantom_nf02252_runner"
TEST_FILE = "tests/test_widget.py"
VALIDATION = f"pytest -q {TEST_FILE}"


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
    # Configuration-only route facts: ROUTED is a catalog launch identity,
    # UNROUTED is neither a catalog identity nor a canonical workforce row.
    monkeypatch.setattr(workforce_catalog, "catalog_launch_identities", lambda *_a, **_k: [ROUTED])
    return root


def _insert_card(repo: Path, task_id: str, **overrides: Any) -> None:
    _readiness, db_path = task_store._require_ready(repo)
    card: dict[str, Any] = {
        "task_id": task_id,
        "runner": "claude",
        "topic": "coding",
        "status": "pending",
        "worker_status": "unclaimed",
        "objective": "do the thing",
        "allowed_writes": ["src/widget.py"],
        "read_first": [],
        "forbidden": [],
        "validation": [VALIDATION],
        "validation_roles": ["generic"],
        "work_kind": "generic",
        "history": [{"event": "created"}],
    }
    card.update(overrides)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, archived_at, started_at, claimed_by) "
            "VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?)",
            (
                task_id, card["runner"], card["topic"], card["status"], card["worker_status"],
                json.dumps(card), "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
                str(overrides.get("archived_at") or ""), overrides.get("started_at"),
                overrides.get("claimed_by"),
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
            "SELECT runner, card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0]), json.loads(row[1])


def test_runner_only_amend_changes_runner_and_reports_before_after(manager_repo: Path) -> None:
    _insert_card(manager_repo, "T_RUN")
    result = manager_card_amend.amend_pending("T_RUN", runner=ROUTED)
    assert result["ok"] is True, result
    assert result["state"] == "amended"
    assert result["fields"] == ["runner"]
    assert result["runner_before"] == "claude"
    assert result["runner_after"] == ROUTED
    column, card = _stored(manager_repo, "T_RUN")
    assert column == ROUTED
    assert card["runner"] == ROUTED
    last = card["history"][-1]
    assert last["event"] == task_store.CARD_AMENDED_PENDING_EVENT
    assert last["fields"] == ["runner"]
    assert (last["runner_before"], last["runner_after"]) == ("claude", ROUTED)
    # The card is still an unlaunched pending card after the amendment.
    assert task_store.unlaunched_pending_refusal(card, card) == ""


def test_runner_amend_folds_variant_spelling(manager_repo: Path) -> None:
    _insert_card(manager_repo, "T_FOLD")
    result = manager_card_amend.amend_pending("T_FOLD", runner="CLAUDE_OPUS_5")
    assert result["ok"] is True, result
    assert result["runner_after"] == ROUTED
    assert _stored(manager_repo, "T_FOLD")[0] == ROUTED


def test_runner_amend_on_launched_card_refuses(manager_repo: Path) -> None:
    _insert_card(manager_repo, "T_LAUNCHED", started_at="2026-01-02T00:00:00+00:00")
    before = _stored(manager_repo, "T_LAUNCHED")
    result = manager_card_amend.amend_pending("T_LAUNCHED", runner=ROUTED)
    assert result["ok"] is False
    assert result["error"] == "task_amend_pending_refused:card_already_launched"
    assert _stored(manager_repo, "T_LAUNCHED") == before


@pytest.mark.parametrize(
    ("runner", "code"),
    [
        ("codex", "task_amend_pending_refused:runner_reserved:codex"),
        (UNROUTED, f"task_amend_pending_refused:runner_route_absent:{UNROUTED}"),
    ],
)
def test_reserved_or_unroutable_runner_refuses_and_card_is_unchanged(
    manager_repo: Path, runner: str, code: str
) -> None:
    assert core.CODEX_RUNNER == "codex"
    _insert_card(manager_repo, "T_BAD")
    before = _stored(manager_repo, "T_BAD")
    result = manager_card_amend.amend_pending(
        "T_BAD", runner=runner, allowed_writes=["src/other.py"]
    )
    assert result["ok"] is False
    assert result["error"] == code
    assert _stored(manager_repo, "T_BAD") == before


def test_same_runner_is_unchanged_and_empty_amend_still_refuses(manager_repo: Path) -> None:
    _insert_card(manager_repo, "T_SAME", runner=ROUTED)
    same = manager_card_amend.amend_pending("T_SAME", runner=ROUTED)
    assert same["ok"] is True, same
    assert same["state"] == "unchanged"
    assert (same["runner_before"], same["runner_after"]) == (ROUTED, ROUTED)
    empty = manager_card_amend.amend_pending("T_SAME")
    assert empty["ok"] is False
    assert empty["error"] == "task_amend_pending_empty"


def test_mcp_tool_exposes_and_passes_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiworkhub import server

    tool = server.aiworkhub_task_amend_pending
    target = inspect.unwrap(tool)
    assert "runner" in inspect.signature(target).parameters
    captured: dict[str, Any] = {}

    def _fake(task_id: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(task_id=task_id, **kwargs)
        return {"ok": False, "error": "captured"}

    monkeypatch.setattr(manager_card_amend, "amend_pending", _fake)
    result = target("T_MCP", runner=ROUTED)
    assert result == {"ok": False, "error": "captured"}
    assert captured["task_id"] == "T_MCP"
    assert captured["runner"] == ROUTED


def test_create_accepts_unroutable_runner_with_remedy_hint(
    manager_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIWORKHUB_REPO", str(manager_repo))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    task_create_warnings.reset_runner_route_dedupe_for_tests()
    try:
        result = core.create_task(
            task_id="T_CREATE",
            title="unroutable runner card",
            runner=UNROUTED,
            topic="coding",
            objective="fix the bug",
            acceptance=["it works"],
            allowed_writes=["src/widget.py"],
            required_outputs=["src/widget.py"],
            validation=[VALIDATION],
            callback_required=False,
            custom_template_escape="audited_custom_unclassified",
        )
    finally:
        task_create_warnings.reset_runner_route_dedupe_for_tests()
    assert result["ok"] is True, result
    assert any(
        w.startswith(f"workforce_route_absent:runner={UNROUTED}")
        for w in result["runner_route_warnings"]
    )
    hint = result["runner_route_hint"]
    assert "aiworkhub_task_amend_pending runner=" in hint
    assert UNROUTED in hint
    assert hint == manager_card_amend.runner_route_remedy_hint(UNROUTED)
