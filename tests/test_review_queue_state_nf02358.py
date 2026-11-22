"""NF-2026-02358 / NF-2026-02360: review queue, request state and runner routes.

A) ``core.review_queue`` showed ``Codex Review Queue (0)`` while three cards
   were ``status=review``: ``task_store._REVIEW_QUEUE_SQL`` never projected
   ``manager_ready_receipt``/``claim_epoch``, so ``manager_ready_marker`` was
   None for every row and every ``review_ready`` card was dropped. Every
   review card is now listed (one still waiting for its reviewers carries a
   waiting marker) and the title names the current manager, never a
   hard-coded ``Codex``.
B) ``ProcessManager.status`` returned ``state: null`` when the lifecycle tail
   ended in a stateless row; it now falls back to the last stated row, and to
   the card when no lifecycle row has a state.
C) ``create_time_runner_route_warnings`` warned for the bare ``claude`` runner
   that launch resolves to ``claude_cli``; it now asks launch's own adapter
   derivation and warns only when that derivation yields nothing.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    core,
    process_launcher,
    review_lifecycle,
    task_create_warnings,
    task_store,
    workforce_catalog,
)

NOW = "2026-11-22T00:00:00+00:00"
CLAIM_EPOCH = 3


# --- A) review queue ---------------------------------------------------------

@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.delenv("AIWORKHUB_ALLOW_WRITES", raising=False)
    return root


def _review_card(task_id: str, *, receipt_epoch: int | None) -> dict[str, Any]:
    request_id = f"REQ-{task_id}"
    card: dict[str, Any] = {
        "task_id": task_id,
        "claim_epoch": CLAIM_EPOCH,
        "terminal_substatus": "review_ready",
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {"request_identity": {"request_id": request_id}},
        },
    }
    if receipt_epoch is not None:
        card["manager_ready_receipt"] = {
            "manager_ready": {
                "schema_id": review_lifecycle.MANAGER_READY_SCHEMA_ID,
                "target_task_id": task_id,
                "target_request_id": request_id,
                "claim_epoch": receipt_epoch,
                "reviews": [],
            }
        }
    return card


def _insert_review_row(root: Path, task_id: str, card: dict[str, Any], updated_at: str) -> None:
    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, "claude_opus-5", "coding", "solo", "review", "review", "normal",
             "objective", json.dumps(card), NOW, updated_at),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_three_review_cards(root: Path) -> None:
    # One manager-ready, one still waiting for reviewers, one whose receipt is
    # bound to an earlier claim_epoch (the card was re-claimed since).
    _insert_review_row(root, "T_READY", _review_card("T_READY", receipt_epoch=CLAIM_EPOCH),
                       "2026-11-22T00:00:03+00:00")
    _insert_review_row(root, "T_WAITING", _review_card("T_WAITING", receipt_epoch=None),
                       "2026-11-22T00:00:02+00:00")
    _insert_review_row(root, "T_EPOCH_BUMP",
                       _review_card("T_EPOCH_BUMP", receipt_epoch=CLAIM_EPOCH - 1),
                       "2026-11-22T00:00:01+00:00")


def test_projection_carries_the_fields_the_manager_ready_marker_reads(repo: Path) -> None:
    _seed_three_review_cards(repo)
    cards = {c["task_id"]: c for c in task_store.list_review_queue_cards(repo)}
    assert set(cards) == {"T_READY", "T_WAITING", "T_EPOCH_BUMP"}
    assert task_store.manager_ready_marker(cards["T_READY"]) is not None
    assert task_store.manager_ready_marker(cards["T_WAITING"]) is None
    assert task_store.manager_ready_marker(cards["T_EPOCH_BUMP"]) is None


def test_review_queue_lists_every_review_card_under_a_claude_manager(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_current_chat_provider", lambda card=None: "claude")
    _seed_three_review_cards(repo)

    result = core.review_queue()

    assert result["ok"] is True, result
    header, *rows = result["stdout"].splitlines()
    assert header == "=== Claude Review Queue (3) ==="
    assert "Codex" not in header
    assert rows == [
        "  [coding] [claude_opus-5] T_READY",
        "  [coding] [claude_opus-5] T_WAITING (waiting_reviewers)",
        "  [coding] [claude_opus-5] T_EPOCH_BUMP (waiting_reviewers)",
    ]


def test_review_queue_title_is_neutral_without_a_resolved_manager(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_current_chat_provider", lambda card=None: "")
    _seed_three_review_cards(repo)
    assert core.review_queue()["stdout"].splitlines()[0] == "=== Review Queue (3) ==="


def test_waiting_marker_keeps_the_review_summarizer_line_contract(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiworkhub import review_summarizer

    monkeypatch.setattr(core, "_current_chat_provider", lambda card=None: "claude")
    _seed_three_review_cards(repo)
    entries = review_summarizer._parse_review_queue_lines(core.review_queue()["stdout"])
    assert [e["task_id"] for e in entries] == ["T_READY", "T_WAITING", "T_EPOCH_BUMP"]


# --- B) agent_task_status state ----------------------------------------------

REQUEST_ID = "02358" + "0" * 27
TASK_ID = "TASK_NF02358"


def _manager(tmp_path: Path, card: dict[str, Any]) -> process_launcher.ProcessManager:
    return process_launcher.ProcessManager(
        repo=tmp_path / "repo",
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=lambda _t: {"returncode": 0, "stdout": json.dumps(card), "stderr": ""},
        collision_guard=lambda **_k: {
            "returncode": 0, "stdout": '{"collision_free":true}', "stderr": "",
        },
        adapter_builder=lambda **_k: SimpleNamespace(
            argv=[], cwd=str(tmp_path), launchable=True, reason="",
        ),
        isolation_enabled=True,
    )


def _review_status_card() -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "runner": "claude_opus-5",
        "topic": "coding",
        "status": "review",
        "worker_status": "review",
        "terminal_substatus": "review_ready",
        "allowed_writes": ["out/result.txt"],
    }


def test_status_falls_back_to_the_last_stated_row(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _review_status_card())
    manager._append_event({
        "request_id": REQUEST_ID, "task_id": TASK_ID, "runner": "claude_opus-5",
        "state": "review_ready", "pid": 0, "exit_code": 0,
    })
    # A sparse trailing delta row with no state (e.g. a retention overlay).
    manager._append_event({
        "request_id": REQUEST_ID, "task_id": TASK_ID, "event_type": "retention_overlay",
    })

    result = manager.status(REQUEST_ID)

    assert result["ok"] is True
    assert result["state"] == "review_ready"
    assert result["latest_event"]["event_type"] == "retention_overlay"


def test_status_without_any_stated_row_derives_the_state_from_the_card(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, _review_status_card())
    manager._append_event({
        "request_id": REQUEST_ID, "task_id": TASK_ID,
        "event_type": "review_orchestrator_wait",
    })

    result = manager.status(REQUEST_ID)

    assert result["ok"] is True
    assert result["state"] is not None
    assert result["state"] == core._lifecycle_state(_review_status_card()) == "review"


def test_stated_lifecycle_tail_keeps_the_tail_and_names_its_source() -> None:
    from aiworkhub import launch_zero_delta

    stated = {"request_id": "r", "state": "exited", "pid": 7}
    sparse = {"request_id": "r", "event_type": "gc", "pid": 8}
    tail, source = launch_zero_delta.stated_lifecycle_tail([stated, sparse])
    assert (tail["state"], tail["pid"], source) == ("exited", 8, "last_stated_lifecycle_row")
    assert launch_zero_delta.stated_lifecycle_tail([stated])[1] == "lifecycle_tail"
    assert launch_zero_delta.stated_lifecycle_tail([sparse]) == (sparse, "absent")
    # The split contract itself is unchanged.
    assert launch_zero_delta.split_lifecycle_tail([stated, sparse])[0] is sparse


# --- C) create-time runner route warning --------------------------------------

ROUTED = "claude_opus-5"
UNROUTED = "phantom_nf02358_runner"
TEST_FILE = "tests/test_widget.py"


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
    monkeypatch.setattr(workforce_catalog, "catalog_launch_identities", lambda *_a, **_k: [ROUTED])
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    return root


def _create(task_id: str, runner: str) -> dict[str, Any]:
    task_create_warnings.reset_runner_route_dedupe_for_tests()
    try:
        return core.create_task(
            task_id=task_id,
            title="runner route card",
            runner=runner,
            topic="coding",
            objective="fix the bug",
            acceptance=["it works"],
            allowed_writes=["src/widget.py"],
            required_outputs=["src/widget.py"],
            validation=[f"pytest -q {TEST_FILE}"],
            callback_required=False,
            custom_template_escape="audited_custom_unclassified",
        )
    finally:
        task_create_warnings.reset_runner_route_dedupe_for_tests()


def test_bare_claude_runner_is_resolvable_by_launch_and_never_warned(
    manager_repo: Path,
) -> None:
    adapters, from_catalog = workforce_catalog.launch_adapter_candidates(manager_repo, "claude")
    assert adapters[0] == "claude_cli"
    assert from_catalog is False
    assert core.create_time_runner_route_warnings("claude") == []

    result = _create("T_BARE_CLAUDE", "claude")
    assert result["ok"] is True, result
    assert "runner_route_warnings" not in result


def test_a_runner_launch_would_refuse_still_warns(manager_repo: Path) -> None:
    assert workforce_catalog.launch_adapter_candidates(manager_repo, UNROUTED) == ((), False)
    with pytest.raises(process_launcher.LaunchRejected, match="launch_adapter_underivable"):
        process_launcher.derive_launch_identity(
            manager_repo, {"runner": UNROUTED, "topic": "coding"}
        )

    result = _create("T_UNROUTED", UNROUTED)
    assert result["ok"] is True, result
    assert any(
        warning.startswith(f"workforce_route_absent:runner={UNROUTED}")
        for warning in result["runner_route_warnings"]
    )
