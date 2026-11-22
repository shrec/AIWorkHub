"""NF-2026-02368: ``reject_review(relaunch=true)`` at the reject depth cap continues the card.

At the cap the launch of the rejected card is refused.  The same call now runs
the capped continuation ``aiworkhub_task_supersede(continue_capped=true)`` runs,
reusing the stored reject reason as the successor's feedback, and launches the
successor.  These tests run the real ``server.aiworkhub_task_reject_review``,
``core.reject_review`` and ``core.continue_capped_task`` over a tmp repo; only the
launcher (``server._launch_task_internal``) is a recorder that applies the real
reject-depth guard, so no provider CLI ever starts.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import (  # noqa: E402
    core,
    feature_settings,
    launch_replay_guard,
    process_launcher,
    rework_delta_artifact,
    server,
    task_store,
)
from aiworkhub.learning_commit import FailureCategory  # noqa: E402

NOW = "2026-11-22T00:00:00+00:00"
SESSION_ID = "019f5097-6dbe-7172-870a-945afc5f3bfb"
RUNNER = "claude_task_mcp_runtime_wiring"
TOPIC = "task_mcp"
MANAGER = "nf02368-manager"
TASK_ID = "NF2368_CAP"
SUCCESSOR_ID = f"{TASK_ID}-c1"
FIRST_REASON = "first round: still wrong"
REASON = "1. fix the lane shim\n2. keep PYTHONPATH extended\n3. re-run validation"
DIGEST = "ab" * 32
NEXT_ACTION = f"aiworkhub_task_supersede(task_id={TASK_ID}, continue_capped=true)"
CONTRACT = {
    "objective": "objective",
    "acceptance": ["the successor keeps the contract"],
    "allowed_writes": ["src/existing.py"],
    "validation": ["pytest -q tests/existing.py"],
    "required_outputs": ["src/existing.py"],
}


def _predecessor(task_id: str) -> dict[str, Any]:
    return {
        "request_id": "9" * 32,
        "task_id": task_id,
        "claim_epoch": 1,
        "changed_path_hashes": {"src/a.py": "1" * 64},
        "delta_artifact": {"path": f"/runtime/rework_deltas/{DIGEST}.json", "digest": DIGEST},
        "rework_delta": {
            "schema_id": "aiworkhub.rework_delta_descriptor.v1",
            "sealed": True,
            "task_id": task_id,
            "request_id": "9" * 32,
            "claim_epoch": 1,
            "artifact_path": f"/runtime/rework_deltas/{DIGEST}.json",
            "artifact_sha256": DIGEST,
        },
    }


def _db(root: Path) -> str:
    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    return readiness.canonical_db


def _insert_task(root: Path, task_id: str, extra_card: dict[str, Any] | None = None) -> None:
    card = {"origin_thread_id": "nf02368-thread", **CONTRACT, **(extra_card or {})}
    conn = sqlite3.connect(_db(root))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, origin_thread_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, RUNNER, TOPIC, "solo", "pending", "unclaimed", "normal", "objective",
             json.dumps(card), NOW, NOW, None, "nf02368-thread"),
        )
        conn.commit()
    finally:
        conn.close()


def _rewrite_card(root: Path, task_id: str, **fields: Any) -> None:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        card = json.loads(row[0])
        card.update(fields)
        conn.execute("UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), task_id))
        conn.commit()
    finally:
        conn.close()


def _raw_card(root: Path, task_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0])


def _archived(root: Path, task_id: str) -> bool:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute("SELECT archived_at FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    return bool(row and row[0])


def _reject_events(root: Path, task_id: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(_db(root))
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in conn.execute("SELECT * FROM task_events")]
    finally:
        conn.close()
    return [
        row for row in rows if row["event"] == "reject_review" and row["task_id"] == task_id
    ]


def _to_review(root: Path, task_id: str, request_id: str) -> None:
    assert core.claim_start_exact(task_id, RUNNER, TOPIC)["ok"] is True
    assert core.mark_review(task_id, runner=RUNNER, topic=TOPIC)["ok"] is True
    card = _raw_card(root, task_id)
    terminal = card.get("terminal_review")
    terminal = terminal if isinstance(terminal, dict) else {}
    evidence = terminal.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    evidence["request_identity"] = {
        "request_id": request_id, "task_id": task_id, "runner": RUNNER, "topic": TOPIC,
    }
    terminal["evidence"] = evidence
    _rewrite_card(root, task_id, terminal_review=terminal)


def _card(root: Path, task_id: str) -> dict[str, Any]:
    card = task_store.get_task(root, task_id)
    assert card is not None, task_id
    return card


class _Launches:
    """A launcher recorder that applies the real reject-depth guard, as the launcher does."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[dict[str, Any]] = []
        self.successor_reply: dict[str, Any] = {"ok": True, "request_id": "REQ_SUCCESSOR"}

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        task_id = kwargs["task_id"]
        refusal = launch_replay_guard.reject_depth_refusal(
            task_store.get_task(self.root, task_id), repo=self.root
        )
        if refusal:
            return {"ok": False, "task_id": task_id, "state": "blocked", "blocked_reason": refusal}
        if task_id == SUCCESSOR_ID:
            return dict(self.successor_reply)
        return {"ok": True, "request_id": "REQ_RETRY"}


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    feature_settings.update(root, changes={"context_graph": True}, expected_revision=0)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "manager_bootstrap", lambda: {
        "ok": True,
        "role": "manager",
        "provider": "codex",
        "repo": str(root),
        "manager_route": {"provider": "codex", "session_id": SESSION_ID, "thread_id": SESSION_ID},
    })
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    # The coordinator token file needs chmod 0600, which the sandbox denies;
    # the capability itself is not what this regression is about.
    monkeypatch.setattr(
        core, "_verify_coordinator_capability", lambda _runner: (True, "test_coordinator")
    )
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: MANAGER)
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)
    monkeypatch.setattr(
        core, "classify_terminal_disposition", lambda _card: FailureCategory.CANDIDATE_CODE
    )
    monkeypatch.setattr(
        rework_delta_artifact,
        "has_verified_rework_delta",
        lambda predecessor, *, authority_repo: isinstance(predecessor, dict)
        and isinstance(predecessor.get("rework_delta"), dict),
    )

    def fake_create_task(task_id, **payload):
        stored = {key: value for key, value in payload.items() if value not in ("", None)}
        _insert_task(root, task_id, {**stored, "task_id": task_id})
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(core, "create_task", fake_create_task)
    ledger_dir = root / "process_ledger"
    ledger_dir.mkdir()
    manager = process_launcher.ProcessManager(
        repo=root,
        process_log_path=ledger_dir / "processes.jsonl",
        process_dir=ledger_dir / "requests",
    )
    monkeypatch.setattr(process_launcher, "default_manager", lambda: manager)
    _insert_task(root, TASK_ID)
    return root


@pytest.fixture
def pin_sealed_delta(repo, monkeypatch) -> None:
    """Every landed reject pins a sealed rework delta, as a retained candidate would."""

    real_reject_review = core.reject_review

    def reject_and_pin(*args: Any, **kwargs: Any) -> Any:
        result = real_reject_review(*args, **kwargs)
        task_id = kwargs.get("task_id", args[0] if args else "")
        if isinstance(result, dict) and result.get("ok") is True:
            _rewrite_card(repo, task_id, rework_predecessor=_predecessor(task_id))
        return result

    monkeypatch.setattr(core, "reject_review", reject_and_pin)


@pytest.fixture
def launches(repo, monkeypatch) -> _Launches:
    recorder = _Launches(repo)
    monkeypatch.setattr(server, "_launch_task_internal", recorder)
    return recorder


def _one_round_rejected(root: Path) -> None:
    """The first counted reject; the second one (via the server) reaches the cap."""

    _to_review(root, TASK_ID, "a" * 32)
    assert core.reject_review(TASK_ID, FIRST_REASON, topic=TOPIC)["ok"] is True
    _to_review(root, TASK_ID, "b" * 32)


def _capped_reject(**kwargs: Any) -> dict[str, Any]:
    return server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, relaunch=True, **kwargs
    )


def test_capped_relaunch_continues_and_launches_the_successor(repo, pin_sealed_delta, launches):
    _one_round_rejected(repo)

    reply = _capped_reject()

    assert reply["ok"] is True, reply
    relaunch = reply["relaunch"]
    assert relaunch["ok"] is True, relaunch
    assert relaunch["request_id"] == "REQ_SUCCESSOR"
    assert relaunch["continued_capped"] == {
        "predecessor_task_id": TASK_ID,
        "successor_task_id": SUCCESSOR_ID,
        "delta_digest": DIGEST,
    }
    assert launches.calls == [
        {"task_id": TASK_ID, "adapter_id": None},
        {"task_id": SUCCESSOR_ID, "adapter_id": None},
    ]
    # The reject landed and stays; the capped card is superseded by the successor.
    assert len(_reject_events(repo, TASK_ID)) == 2
    assert _archived(repo, TASK_ID)
    assert _card(repo, TASK_ID)["superseded_by"] == SUCCESSOR_ID
    successor = _card(repo, SUCCESSOR_ID)
    assert successor["rework_predecessor"]["rework_delta"]["artifact_sha256"] == DIGEST
    # The reject reason is carried verbatim; nothing was retyped as a supersede reason.
    assert successor["review_feedback"]["instruction"] == f"Previous reject reason:\n{REASON}"
    assert "Supersede reason" not in successor["review_feedback"]["instruction"]


def test_capped_relaunch_launches_the_successor_with_the_same_adapter(
    repo, pin_sealed_delta, launches
):
    _one_round_rejected(repo)

    reply = _capped_reject(relaunch_adapter_id=" claude_coding ")

    assert reply["relaunch"]["ok"] is True, reply
    assert launches.calls == [
        {"task_id": TASK_ID, "adapter_id": "claude_coding"},
        {"task_id": SUCCESSOR_ID, "adapter_id": "claude_coding"},
    ]


def test_a_failed_successor_launch_still_reports_the_continuation(
    repo, pin_sealed_delta, launches
):
    launches.successor_reply = {"ok": False, "error": "launch_blocked"}
    _one_round_rejected(repo)

    reply = _capped_reject()

    assert reply["ok"] is True, reply
    assert reply["relaunch"] == {
        "ok": False,
        "error": "launch_blocked",
        "continued_capped": {
            "predecessor_task_id": TASK_ID,
            "successor_task_id": SUCCESSOR_ID,
            "delta_digest": DIGEST,
        },
    }
    assert _archived(repo, TASK_ID)
    assert _card(repo, SUCCESSOR_ID)["status"] == "pending"


def test_a_refused_continuation_names_the_follow_up_and_keeps_the_reject(repo, launches):
    # No sealed delta is pinned, so the continuation is refused before any write.
    _one_round_rejected(repo)

    reply = _capped_reject()

    assert reply["ok"] is True, reply
    assert reply["relaunch"] == {
        "ok": False,
        "error": "continuation_predecessor_unverified",
        "next_action": NEXT_ACTION,
    }
    assert launches.calls == [{"task_id": TASK_ID, "adapter_id": None}]
    assert len(_reject_events(repo, TASK_ID)) == 2
    assert _card(repo, TASK_ID)["status"] == "pending"
    assert not _archived(repo, TASK_ID)
    assert task_store.get_task(repo, SUCCESSOR_ID) is None


def test_a_raising_continuation_is_reported_without_its_message(
    repo, pin_sealed_delta, launches, monkeypatch
):
    def explode(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(core, "continue_capped_task", explode)
    _one_round_rejected(repo)

    reply = _capped_reject()

    assert reply["ok"] is True, reply
    assert reply["relaunch"] == {
        "ok": False,
        "error": "continuation_raised:RuntimeError",
        "next_action": NEXT_ACTION,
    }
    assert "secret detail" not in json.dumps(reply)
    assert len(_reject_events(repo, TASK_ID)) == 2
    assert _card(repo, TASK_ID)["status"] == "pending"


def test_under_the_cap_relaunch_is_unchanged(repo, pin_sealed_delta, launches):
    _to_review(repo, TASK_ID, "a" * 32)

    reply = _capped_reject()

    assert reply["ok"] is True, reply
    assert reply["relaunch"] == {"ok": True, "request_id": "REQ_RETRY"}
    assert launches.calls == [{"task_id": TASK_ID, "adapter_id": None}]
    assert not _archived(repo, TASK_ID)
    assert task_store.get_task(repo, SUCCESSOR_ID) is None


def test_an_unrelated_launch_refusal_does_not_continue(repo, pin_sealed_delta, monkeypatch):
    calls: list[dict[str, Any]] = []

    def refuse(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"ok": False, "blocked_reason": "launch_gate_closed"}

    monkeypatch.setattr(server, "_launch_task_internal", refuse)
    _one_round_rejected(repo)

    reply = _capped_reject()

    assert reply["relaunch"] == {"ok": False, "blocked_reason": "launch_gate_closed"}
    assert calls == [{"task_id": TASK_ID, "adapter_id": None}]
    assert not _archived(repo, TASK_ID)


def test_supersede_continue_capped_reuses_the_stored_reject_reason(repo, pin_sealed_delta):
    _one_round_rejected(repo)
    assert server.aiworkhub_task_reject_review(task_id=TASK_ID, reason=REASON)["ok"] is True

    reply = server.aiworkhub_task_supersede(task_id=TASK_ID, continue_capped=True)

    assert reply["ok"] is True, reply
    successor = _card(repo, SUCCESSOR_ID)
    assert successor["review_feedback"]["instruction"] == f"Previous reject reason:\n{REASON}"
