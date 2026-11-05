"""NF-2026-01922: a red declared validation returns to the same worker, once per signature."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    core,
    launch_replay_guard,
    process_launcher,
    task_engine,
    task_store,
    validation_auto_rework as auto,
    validation_failed_packet,
)

_TASK = "TASK_AUTO"
_REQ = "req-auto-0001"
_CMD = "python -m pytest -q tests/test_x.py"
_REAL_REJECT_PINNED = auto._reject_pinned
_REAL_COUNTED = launch_replay_guard.counted_candidate_code_rejects


@pytest.fixture(autouse=True)
def pinned_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the pinned core reject to the fake manager's ``reject_review``."""
    monkeypatch.setattr(
        auto, "_reject_pinned",
        lambda manager, task_id, reason, request_id: manager.reject_review(
            task_id, reason, to="pending", predecessor_request_id=request_id),
    )


def _tail(tmp: str = "/tmp/pytest-of-u/pytest-7/t0", secs: str = "1.23", pid: str = "4242", msg: str = "assert 1 == 2") -> str:
    return (
        f"FAILED tests/test_x.py::test_a - AssertionError: {msg}\n"
        f"E   file {tmp}/data.json missing (pid={pid})\n"
        f"1 failed, 3 passed in {secs}s\n"
    )


def _evidence(tail: str | None = None, *, returncode: int = 1, **extra: Any) -> dict[str, Any]:
    return {
        "request_id": _REQ,
        "validation": [
            {"declared_command": _CMD, "command": "/abs/" + _CMD, "returncode": returncode,
             "stdout_tail": tail if tail is not None else _tail(), "stderr_tail": ""},
            {"declared_command": "ruff check", "returncode": 0, "stdout_tail": "ok"},
        ],
        **extra,
    }


_METADATA = {"task_id": _TASK, "runner": "claude", "topic": "t", "adapter_id": "claude_cli", "model": "claude-opus-5-5"}
_CARD = {"task_id": _TASK, "runner": "claude", "topic": "t", "validation_roles": ["reproduction", "lint"]}


@pytest.fixture
def history(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(auto, "_reject_history", lambda repo, task_id: list(events))
    monkeypatch.setattr(launch_replay_guard, "counted_candidate_code_rejects", lambda evs, identity: [])
    monkeypatch.setattr(task_store, "get_task", lambda repo, task_id: dict(_CARD))
    return events


def _plan(evidence: dict[str, Any], *, state: str = "validation_failed", error: str = "validation_failed:x") -> dict[str, Any]:
    return auto.plan(Path("."), _METADATA, state, error, evidence)


def test_first_failure_is_auto_returned_with_command_exit_code_and_tail(history: list) -> None:
    keys = _plan(_evidence())
    record = keys["validation_auto_rework"]
    assert record["decision"] == "auto_return"
    assert record["prior_auto_returns"] == 0
    assert (record["adapter_id"], record["model"]) == ("claude_cli", "claude-opus-5-5")
    # the deferral marker task_engine honours: no manager callback needing a decision
    assert keys["manager_callback_deferred"] == {"reason": "validation_auto_rework"}
    reason = record["reason"]
    assert reason.startswith(f"validation_auto_rework signature={record['signature']} auto_return=1")
    assert f"failing command: {_CMD}" in reason
    assert "exit_code: 1" in reason
    assert "assert 1 == 2" in reason
    assert len(reason.encode("utf-8")) <= auto.REASON_MAX_BYTES < task_store.MAX_REWORK_FEEDBACK_BYTES


def test_deferral_suppresses_the_manager_callback(monkeypatch: pytest.MonkeyPatch, history: list) -> None:
    seen: dict[str, Any] = {}

    def fake_mark(repo, task_id, **kwargs):
        seen.update(kwargs)
        return True, "review", False

    monkeypatch.setattr(task_store, "mark_terminal_review_with_callback", fake_mark)
    evidence = {**_evidence(), **_plan(_evidence())}
    result = task_engine.mark_terminal_review(Path("."), _TASK, "claude", "validation_failed", evidence=evidence)
    assert result["ok"] is True
    assert seen["callback_transition"] == ""


def test_environmental_failure_is_never_auto_returned(history: list) -> None:
    keys = _plan(_evidence(validation_environment_disposition={"kind": "sandbox"}))
    assert keys["validation_auto_rework"]["decision"] == "manager"
    assert keys["validation_auto_rework"]["cause"] == "environmental"
    assert "manager_callback_deferred" not in keys


def test_volatile_tokens_do_not_change_the_signature() -> None:
    def packet(tail: str) -> dict:
        return validation_failed_packet.packet_from_evidence(
            task_id=_TASK, request_id=_REQ, evidence=_evidence(tail), roles=None)

    a = auto.failure_signature(packet(_tail()))
    b = auto.failure_signature(packet(_tail(tmp="C:\\Users\\u\\AppData\\Local\\Temp\\pytest-of-u\\pytest-9\\t1", secs="9.80", pid="777")))
    c = auto.failure_signature(packet(_tail(msg="assert 3 == 4")))
    assert a == b
    assert a != c


def test_cut_tail_hashes_only_complete_lines() -> None:
    """A byte-cut tail moves its cut point with the length of a volatile token."""
    def signature(last_line: str) -> str:
        text = "".join(f"line {i:03d}: tests/test_x.py::test_case_{i} PASSED\n" for i in range(300)) + last_line
        tail = text[-2000:]
        return auto.failure_signature(validation_failed_packet.packet_from_evidence(
            task_id=_TASK, request_id=_REQ, evidence=_evidence(tail), roles=None))

    fast = signature("FAILED tests/test_x.py::test_a\n1 failed in 1.23s")
    slow = signature("FAILED tests/test_x.py::test_a\n1 failed in 12.34s")
    other = signature("FAILED tests/test_x.py::test_b\n1 failed in 1.23s")
    assert fast == slow
    assert fast != other


def test_same_signature_again_goes_to_the_manager_with_the_auto_return_count(history: list) -> None:
    first = _plan(_evidence())["validation_auto_rework"]
    history.append({"reason": first["reason"], "to": "pending"})
    keys = _plan(_evidence(_tail(tmp="/tmp/other/dir", secs="4.5", pid="9")))
    record = keys["validation_auto_rework"]
    assert record["decision"] == "manager"
    assert record["cause"] == "repeat_signature"
    assert record["prior_auto_returns"] == 1
    assert "manager_callback_deferred" not in keys
    packet = validation_failed_packet.packet_from_evidence(
        task_id=_TASK, request_id=_REQ, evidence={**_evidence(), **keys}, roles=None)
    assert packet["prior_auto_returns"] == 1
    assert packet["auto_rework_decision"] == "repeat_signature"


def test_new_signature_after_an_auto_return_is_returned_again(history: list) -> None:
    history.append({"reason": _plan(_evidence())["validation_auto_rework"]["reason"]})
    history.append({"reason": "manager wrote this by hand"})
    record = _plan(_evidence(_tail(msg="assert 5 == 6")))["validation_auto_rework"]
    assert record["decision"] == "auto_return"
    assert record["prior_auto_returns"] == 1
    assert "auto_return=2" in record["reason"].splitlines()[0]


def test_rework_depth_cap_is_respected(monkeypatch: pytest.MonkeyPatch, history: list) -> None:
    """Only manager rejects count toward the cap (NF-2026-02082): cap-1 manager
    rejects still stop an auto-return, any number of auto-returns do not."""
    cap = launch_replay_guard.REJECT_DEPTH_CAP
    monkeypatch.setattr(launch_replay_guard, "counted_candidate_code_rejects", lambda evs, identity: ["r"] * (cap - 1))
    record = _plan(_evidence())["validation_auto_rework"]
    assert record["decision"] == "manager"
    assert record["cause"] == "rework_depth_cap"

    def real_counted(evs, identity):
        return _REAL_COUNTED(evs, "c")

    monkeypatch.setattr(launch_replay_guard, "counted_candidate_code_rejects", real_counted)
    for index in range(cap + 1):
        history.append({
            "to": "pending", "failure_category": "candidate_code", "failure_category_source": "validation",
            "contract_identity": "c", "request_id": f"a{index}",
            "actor": auto.ACTOR, "reason": f"validation_auto_rework signature={index:064x} auto_return={index + 1}",
        })
    record = _plan(_evidence())["validation_auto_rework"]
    assert record["decision"] == "manager"
    assert record["cause"] == "auto_return_cap"
    monkeypatch.setattr(auto, "MAX_AUTO_RETURNS", cap + 5)
    record = _plan(_evidence())["validation_auto_rework"]
    assert record["decision"] == "auto_return"


def test_auto_return_count_is_bounded_so_no_loop_is_possible(history: list) -> None:
    for index in range(auto.MAX_AUTO_RETURNS):
        sig = f"{index:064x}"
        history.append({"reason": f"validation_auto_rework signature={sig} auto_return={index + 1}\n..."})
    record = _plan(_evidence(_tail(msg="brand new failure")))["validation_auto_rework"]
    assert record["decision"] == "manager"
    assert record["cause"] == "auto_return_cap"
    assert record["prior_auto_returns"] == auto.MAX_AUTO_RETURNS


def test_failure_without_a_failing_declared_command_goes_to_the_manager(history: list) -> None:
    record = _plan(_evidence(returncode=0), error="required_output_missing:x")["validation_auto_rework"]
    assert (record["decision"], record["cause"]) == ("manager", "no_failing_declared_command")


def test_other_terminal_states_and_plan_errors(monkeypatch: pytest.MonkeyPatch, history: list) -> None:
    assert _plan(_evidence(), state="review_ready") == {}

    def card_boom(repo, task_id):
        raise RuntimeError("store down")

    # history read fine (one earlier auto-return), then the decision failed
    history.append({"reason": f"validation_auto_rework signature={'a' * 64} auto_return=1"})
    monkeypatch.setattr(task_store, "get_task", card_boom)
    keys = _plan(_evidence())
    assert keys["validation_auto_rework"]["decision"] == "manager"
    assert keys["validation_auto_rework"]["cause"] == "plan_failed:RuntimeError"
    assert keys["validation_auto_rework"]["prior_auto_returns"] == 1
    assert "manager_callback_deferred" not in keys


def test_unreadable_history_records_an_unknown_count_never_zero(monkeypatch: pytest.MonkeyPatch, history: list) -> None:
    def boom(repo, task_id):
        raise OSError("store down")

    monkeypatch.setattr(auto, "_reject_history", boom)
    keys = _plan(_evidence())
    record = keys["validation_auto_rework"]
    assert (record["decision"], record["cause"]) == ("manager", "plan_failed:OSError")
    assert record["prior_auto_returns"] is None
    assert "manager_callback_deferred" not in keys
    packet = validation_failed_packet.packet_from_evidence(
        task_id=_TASK, request_id=_REQ, evidence={**_evidence(), **keys}, roles=None)
    assert packet["prior_auto_returns"] is None


class _FakeManager:
    def __init__(self, reject_ok: bool = True) -> None:
        self.repo = Path(".")
        self.reject_ok = reject_ok
        self.rejects: list[tuple] = []
        self.pins: list[str | None] = []
        self.launches: list[dict] = []

    def reject_review(self, task_id: str, reason: str, *, to: str = "pending",
                      predecessor_request_id: str | None = None) -> dict:
        self.rejects.append((task_id, reason, to))
        self.pins.append(predecessor_request_id)
        return {"ok": True} if self.reject_ok else {"ok": False, "error": "write_gate_closed"}

    def launch(self, **kwargs: Any) -> dict:
        self.launches.append(kwargs)
        return {"ok": True, "request_id": "req-next"}


@pytest.fixture
def review_card(monkeypatch: pytest.MonkeyPatch, history: list) -> dict[str, Any]:
    evidence = _evidence()
    evidence.update(_plan(evidence))
    card = {**_CARD, "worker_status": "review", "terminal_substatus": "validation_failed",
            "terminal_review": {"request_id": _REQ, "evidence": evidence}}
    monkeypatch.setattr(task_store, "get_task", lambda repo, task_id: card)
    return card


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    calls: dict[str, list] = {"events": [], "wakes": []}
    monkeypatch.setattr(auto, "_record_event", lambda repo, task_id, payload: calls["events"].append(payload))
    monkeypatch.setattr(auto, "_wake_manager", lambda repo, task_id, rid: calls["wakes"].append(rid) or True)
    return calls


_EVENT = {"state": "validation_failed", "task_id": _TASK, "request_id": _REQ, "runner": "claude", "topic": "t"}


def test_after_finalize_rejects_through_reject_review_and_relaunches_same_route(review_card: dict, recorded: dict) -> None:
    manager = _FakeManager()
    receipt = auto.after_finalize(manager, _EVENT)
    plan = review_card["terminal_review"]["evidence"]["validation_auto_rework"]
    assert manager.rejects == [(_TASK, plan["reason"], "pending")]
    assert manager.pins == [_REQ]
    assert manager.launches == [{"task_id": _TASK, "runner": "claude", "topic": "t",
                                 "adapter_id": "claude_cli", "model": "claude-opus-5-5"}]
    assert receipt is not None and receipt["state"] == "relaunched"
    assert recorded["wakes"] == []
    [event] = recorded["events"]
    assert (event["actor"], event["reason_code"], event["rework_kind"]) == (
        "aiworkhub_validation_auto_rework", "validation_auto_rework", "mechanical")
    assert event["auto_return"] == 1


def test_failed_reject_wakes_the_manager_and_does_not_launch(review_card: dict, recorded: dict) -> None:
    manager = _FakeManager(reject_ok=False)
    receipt = auto.after_finalize(manager, _EVENT)
    assert receipt is not None and receipt["state"] == "reject_failed"
    assert manager.launches == []
    assert recorded["wakes"] == [_REQ]


def test_card_moved_by_the_manager_is_left_alone(review_card: dict, recorded: dict) -> None:
    review_card["worker_status"] = "unclaimed"
    manager = _FakeManager()
    receipt = auto.after_finalize(manager, _EVENT)
    assert receipt is not None and receipt["state"] == "skipped_card_moved"
    assert manager.rejects == [] and manager.launches == []


def test_manager_decision_and_other_events_do_nothing(monkeypatch: pytest.MonkeyPatch, history: list, recorded: dict) -> None:
    evidence = _evidence(validation_environment_disposition={"kind": "sandbox"})
    evidence.update(_plan(evidence))
    card = {**_CARD, "worker_status": "review", "terminal_substatus": "validation_failed",
            "terminal_review": {"request_id": _REQ, "evidence": evidence}}
    monkeypatch.setattr(task_store, "get_task", lambda repo, task_id: card)
    manager = _FakeManager()
    assert auto.after_finalize(manager, _EVENT) is None
    assert auto.after_finalize(manager, {**_EVENT, "state": "review_ready"}) is None
    assert auto.after_finalize(manager, None) is None
    assert manager.rejects == [] and manager.launches == [] and recorded["events"] == []


def test_after_finalize_never_raises(monkeypatch: pytest.MonkeyPatch, recorded: dict) -> None:
    def boom(repo, task_id):
        raise OSError("disk")

    monkeypatch.setattr(task_store, "get_task", boom)
    receipt = auto.after_finalize(_FakeManager(), _EVENT)
    assert receipt is not None and receipt["state"] == "failed"
    assert recorded["wakes"] == [_REQ]


def test_stale_wake_never_enqueues_for_a_newer_request(monkeypatch: pytest.MonkeyPatch) -> None:
    card = {**_CARD, "worker_status": "review", "terminal_substatus": "validation_failed",
            "terminal_review": {"evidence": {"request_id": "req-newer"}}}
    monkeypatch.setattr(task_store, "get_task", lambda repo, task_id: card)
    enqueued: list[tuple] = []
    monkeypatch.setattr(auto, "_enqueue_wake", lambda *args: enqueued.append(args) or True)
    assert auto._wake_manager(Path("."), _TASK, _REQ) is False
    assert enqueued == []
    assert auto._wake_manager(Path("."), _TASK, "req-newer") is True
    assert [args[3:] for args in enqueued] == [("validation_failed", "req-newer")]


def test_retry_finalization_runs_the_planned_auto_return_after_its_lock(review_card: dict, recorded: dict) -> None:
    """The manager-driven retry completes a deferred finalize outside
    ``_finalize_after_process_exit``; its auto-return must still run."""

    class _Retrying(_FakeManager):
        def _retry_finalization(self, request_id: str, task_id: str, lane: str, heartbeat: Any) -> dict:
            return {"ok": False, "request_id": request_id, "task_id": task_id,
                    "state": "validation_failed", "error": "validation_failed:x"}

    manager = _Retrying()
    result = process_launcher.ProcessManager.retry_finalization(manager, _REQ, _TASK)  # type: ignore[arg-type]
    assert result["state"] == "validation_failed"
    plan = review_card["terminal_review"]["evidence"]["validation_auto_rework"]
    assert manager.rejects == [(_TASK, plan["reason"], "pending")]
    assert [launch["adapter_id"] for launch in manager.launches] == ["claude_cli"]
    [event] = recorded["events"]
    assert event["state"] == "relaunched"


# --- over a real temp task store -------------------------------------------

_RUNNER = "claude_worker_nf01922"
_THREAD = "thread-nf01922"


def _sql(repo: Path, statement: str, params: tuple = ()) -> list:
    conn = task_store._connect(task_store.canonical_db_path(repo))
    try:
        rows = conn.execute(statement, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _store_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    now = datetime.now(timezone.utc).isoformat()
    card = {"task_id": _TASK, "callback_required": True, "coordinator_provider": "claude", "claim_epoch": 1,
            "allowed_writes": ["src/x.py"], "required_outputs": ["src/x.py"], "validation": [_CMD],
            "validation_roles": ["reproduction"]}
    _sql(repo,
         "INSERT INTO tasks(task_id, runner, topic, status, worker_status, card_json,"
         " created_at, updated_at, claimed_by, origin_thread_id)"
         " VALUES (?, ?, 't', 'processing', 'processing', ?, ?, ?, ?, ?)",
         (_TASK, _RUNNER, json.dumps(card), now, now, _RUNNER, _THREAD))
    return repo


class _StoreManager:
    """``reject_review`` writes the row and transition ``core.reject_review``
    writes (same payload keys); ``launch`` claims the card back to processing."""

    def __init__(self, repo: Path, *, counted: bool = False, launch_ok: bool = True) -> None:
        self.repo = repo
        self.counted = counted
        self.launch_ok = launch_ok
        self.rejects: list[str] = []
        self.launches: list[dict] = []

    def _reject_row(self, card: dict, reason: str, *, counted: bool) -> None:
        payload = {
            "topic": card.get("topic"), "reason": reason.strip(), "to": "pending",
            "request_id": card["terminal_review"]["evidence"]["request_id"],
            "failure_category": launch_replay_guard.REJECT_DEPTH_COUNTED_CATEGORY,
            "failure_category_source": "terminal_review" if counted else "",
            "contract_identity": launch_replay_guard.task_contract_identity(card),
        }
        _sql(self.repo,
             "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
             (_TASK, "reject_review", "manager", json.dumps(payload, sort_keys=True),
              datetime.now(timezone.utc).isoformat()))

    def reject_review(self, task_id: str, reason: str, *, to: str = "pending",
                      predecessor_request_id: str | None = None) -> dict:
        card = task_store.get_task(self.repo, task_id)
        assert card is not None and card["worker_status"] == "review"
        terminal = card["terminal_review"]["evidence"]["request_id"]
        if predecessor_request_id != terminal:
            return {"ok": False, "error": f"predecessor_request_id_stale:{predecessor_request_id}"}
        self._reject_row(card, reason, counted=self.counted)
        _sql(self.repo, "UPDATE tasks SET status='pending', worker_status='pending' WHERE task_id=?", (task_id,))
        self.rejects.append(reason)
        return {"ok": True, "returncode": 0, "task_id": task_id, "to": to}

    def manual_rework(self) -> None:
        card = task_store.get_task(self.repo, _TASK)
        assert card is not None
        self._reject_row(card, "manager wrote this by hand", counted=False)
        _sql(self.repo, "UPDATE tasks SET status='processing', worker_status='processing' WHERE task_id=?", (_TASK,))

    def launch(self, **kwargs: Any) -> dict:
        self.launches.append(kwargs)
        if not self.launch_ok:
            return {"ok": False, "error": "adapter_unavailable", "stderr": "provider secret token"}
        _sql(self.repo, "UPDATE tasks SET status='processing', worker_status='processing' WHERE task_id=?",
             (kwargs["task_id"],))
        return {"ok": True, "request_id": f"req-next-{len(self.launches)}"}


def _attempt(repo: Path, manager: _StoreManager, request_id: str, msg: str) -> tuple[dict, Any, dict]:
    evidence = _evidence(_tail(msg=msg))
    evidence["request_id"] = request_id
    evidence.update(auto.plan(repo, _METADATA, "validation_failed", "validation_failed:x", evidence))
    marked = task_engine.mark_terminal_review(repo, _TASK, _RUNNER, "validation_failed", evidence=evidence)
    assert marked["ok"] is True, marked
    receipt = auto.after_finalize(manager, {"state": "validation_failed", "task_id": _TASK, "request_id": request_id})
    return evidence["validation_auto_rework"], receipt, marked


def _auto_events(repo: Path) -> list[dict]:
    rows = _sql(repo, "SELECT payload_json FROM task_events WHERE task_id=? AND event=? ORDER BY event_id",
                (_TASK, auto.EVENT))
    return [json.loads(row[0]) for row in rows]


def test_real_store_once_per_signature_and_auto_return_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auto, "MAX_AUTO_RETURNS", 2)
    repo = _store_repo(tmp_path)
    manager = _StoreManager(repo)

    first, receipt, marked = _attempt(repo, manager, "req-1", "assert 1 == 2")
    assert (first["decision"], first["prior_auto_returns"]) == ("auto_return", 0)
    assert marked["callback_enqueued"] is False
    assert receipt is not None and receipt["state"] == "relaunched"
    assert task_store.get_task(repo, _TASK)["worker_status"] == "processing"

    # same failure, volatile tokens only differ: the manager decides, told of one auto-return
    repeat, receipt, marked = _attempt(repo, manager, "req-2", "assert 1 == 2")
    assert (repeat["decision"], repeat["cause"], repeat["prior_auto_returns"]) == ("manager", "repeat_signature", 1)
    assert repeat["signature"] == first["signature"]
    assert receipt is None and marked["callback_enqueued"] is True
    assert task_store.get_task(repo, _TASK)["worker_status"] == "review"

    manager.manual_rework()
    second, receipt, _ = _attempt(repo, manager, "req-3", "assert 5 == 6")
    assert (second["decision"], second["prior_auto_returns"]) == ("auto_return", 1)
    assert second["reason"].splitlines()[0].endswith("auto_return=2")
    assert receipt is not None and receipt["state"] == "relaunched"

    capped, receipt, _ = _attempt(repo, manager, "req-4", "brand new failure")
    assert (capped["decision"], capped["cause"], capped["prior_auto_returns"]) == ("manager", "auto_return_cap", 2)
    assert receipt is None

    history = launch_replay_guard.recorded_reject_events(repo, _TASK)
    assert len(history) == 3
    assert auto.auto_return_signatures(history) == [first["signature"], second["signature"]]
    assert len(manager.rejects) == 2 and len(manager.launches) == 2
    assert [(e["state"], e["rework_kind"], e["actor"]) for e in _auto_events(repo)] == [
        ("relaunched", "mechanical", auto.ACTOR)] * 2


def test_real_store_counted_auto_return_respects_the_reject_depth_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NF-2026-02082: automatic returns never count toward the manager's reject
    depth; the loop is bounded by its own ``MAX_AUTO_RETURNS`` instead."""
    monkeypatch.setattr(auto, "MAX_AUTO_RETURNS", 2)
    repo = _store_repo(tmp_path)
    assert launch_replay_guard.task_contract_identity(task_store.get_task(repo, _TASK))
    manager = _StoreManager(repo, counted=True)
    first, receipt, _ = _attempt(repo, manager, "req-1", "assert 1 == 2")
    assert first["decision"] == "auto_return" and receipt["state"] == "relaunched"
    second, receipt, _ = _attempt(repo, manager, "req-2", "assert 5 == 6")
    assert second["decision"] == "auto_return" and receipt["state"] == "relaunched"
    identity = launch_replay_guard.task_contract_identity(task_store.get_task(repo, _TASK))
    history = launch_replay_guard.recorded_reject_events(repo, _TASK)
    assert len(history) == 2
    assert launch_replay_guard.counted_candidate_code_rejects(history, identity) == []
    third, receipt, _ = _attempt(repo, manager, "req-3", "brand new failure")
    assert (third["decision"], third["cause"]) == ("manager", "auto_return_cap")
    assert receipt is None and len(manager.launches) == 2


def test_real_store_relaunch_failure_wakes_the_manager(tmp_path: Path) -> None:
    repo = _store_repo(tmp_path)
    manager = _StoreManager(repo, launch_ok=False)
    _record, receipt, _ = _attempt(repo, manager, "req-1", "assert 1 == 2")
    assert receipt is not None
    assert (receipt["state"], receipt["manager_woken"]) == ("relaunch_failed", True)
    assert receipt["relaunch"]["error"] == "adapter_unavailable"
    rows = _sql(repo, "SELECT transition, request_id FROM callback_outbox WHERE task_id=?", (_TASK,))
    assert [(row[0], row[1]) for row in rows] == [("launch_failed", "req-1")]
    [event] = _auto_events(repo)
    assert (event["state"], event["manager_woken"]) == ("relaunch_failed", True)
    assert "provider secret" not in json.dumps(event)


def test_real_store_launch_that_claimed_the_card_then_raised_enqueues_no_wake(tmp_path: Path) -> None:
    repo = _store_repo(tmp_path)

    class _ClaimsThenRaises(_StoreManager):
        def launch(self, **kwargs: Any) -> dict:
            self.launches.append(kwargs)
            card = json.loads(_sql(self.repo, "SELECT card_json FROM tasks WHERE task_id=?", (_TASK,))[0][0])
            card["claimed_request_id"] = "req-next-claimed"
            _sql(self.repo, "UPDATE tasks SET status='processing', worker_status='processing', card_json=?"
                 " WHERE task_id=?", (json.dumps(card), _TASK))
            raise RuntimeError("provider died after claim")

    manager = _ClaimsThenRaises(repo)
    _record, receipt, _ = _attempt(repo, manager, "req-1", "assert 1 == 2")
    assert receipt is not None
    assert (receipt["state"], receipt["manager_woken"]) == ("relaunch_failed", False)
    assert receipt["relaunch"]["error"] == "launch_raised:RuntimeError"
    rows = _sql(repo, "SELECT transition FROM callback_outbox WHERE task_id=?", (_TASK,))
    assert ("launch_failed",) not in [tuple(row) for row in rows]


def test_relaunch_failed_wake_only_for_a_pending_card_no_newer_request_owns(monkeypatch: pytest.MonkeyPatch) -> None:
    card: dict[str, Any] = {**_CARD, "worker_status": "pending"}
    monkeypatch.setattr(task_store, "get_task", lambda repo, task_id: card)
    enqueued: list[tuple] = []
    monkeypatch.setattr(auto, "_enqueue_wake", lambda *args: enqueued.append(args[3:]) or True)
    card["claimed_request_id"] = "req-newer"
    assert auto._wake_relaunch_failed(Path("."), _TASK, _REQ) is False
    del card["claimed_request_id"]
    card["worker_status"] = "processing"
    assert auto._wake_relaunch_failed(Path("."), _TASK, _REQ) is False
    assert enqueued == []
    card["worker_status"] = "pending"
    assert auto._wake_relaunch_failed(Path("."), _TASK, _REQ) is True
    assert enqueued == [("launch_failed", _REQ)]


def _core_reject_double(monkeypatch: pytest.MonkeyPatch, card: dict, *, move_to: str | None = None) -> list[dict]:
    """``core.reject_review`` honouring the pin the way
    ``_resolve_reject_review_predecessor`` does; ``move_to`` races a newer
    terminal request in between the plan read and the reject."""
    calls: list[dict] = []
    monkeypatch.setattr(auto, "_reject_pinned", _REAL_REJECT_PINNED)
    monkeypatch.setattr(core, "repo_root", lambda: Path("."))

    def fake_reject(task_id: str, reason: str, *, to: str, predecessor_request_id: str | None = None) -> dict:
        calls.append({"task_id": task_id, "to": to, "predecessor_request_id": predecessor_request_id})
        if move_to is not None:
            card["terminal_review"] = {"request_id": move_to, "evidence": {"request_id": move_to}}
        terminal = card["terminal_review"]["evidence"]["request_id"]
        if predecessor_request_id != terminal:
            return {"ok": False, "returncode": 126, "stderr": f"predecessor_request_id_stale:{predecessor_request_id}"}
        card["worker_status"] = "pending"
        return {"ok": True, "returncode": 0}

    monkeypatch.setattr(core, "reject_review", fake_reject)
    return calls


def test_reject_is_pinned_to_the_planned_request(monkeypatch: pytest.MonkeyPatch, review_card: dict, recorded: dict) -> None:
    calls = _core_reject_double(monkeypatch, review_card)
    manager = _FakeManager()
    receipt = auto.after_finalize(manager, _EVENT)
    assert receipt is not None and receipt["state"] == "relaunched"
    assert calls == [{"task_id": _TASK, "to": "pending", "predecessor_request_id": _REQ}]
    assert len(manager.launches) == 1


def test_terminal_request_moved_before_the_reject_is_a_stale_plan(monkeypatch: pytest.MonkeyPatch, review_card: dict, recorded: dict) -> None:
    calls = _core_reject_double(monkeypatch, review_card, move_to="req-newer")
    manager = _FakeManager()
    receipt = auto.after_finalize(manager, _EVENT)
    assert calls and calls[0]["predecessor_request_id"] == _REQ
    assert receipt is not None and receipt["state"] == auto.DECISION_STALE_PLAN
    assert receipt["error"].startswith("predecessor_request_id_stale:")
    assert manager.launches == []
    assert recorded["wakes"] == []


def test_reject_refused_for_a_manager_bound_to_another_repo(monkeypatch: pytest.MonkeyPatch, review_card: dict, recorded: dict, tmp_path: Path) -> None:
    calls = _core_reject_double(monkeypatch, review_card)
    manager = _FakeManager()
    manager.repo = tmp_path
    receipt = auto.after_finalize(manager, _EVENT)
    assert receipt is not None and receipt["state"] == "reject_failed"
    assert receipt["error"] == "repo_authority_mismatch"
    assert calls == [] and manager.launches == []
