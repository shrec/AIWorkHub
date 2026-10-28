"""NF-2026-01780: dependency autolaunch spawns a real worker, never a bare claim.

Both production entry points -- the mark_done accept hook (core.py) and the
startup reconcile tool (server.py) -- handed ``core.claim_start_exact`` in as
``launch``.  That only CAS-moves the card to processing/claimed; no worker
process starts.  Since NF-2026-01777 the bare claim is refused up front, so every
dependency-ready child was reported ``launch_not_started`` and had to be
launched by hand.  Both sites now reach the process launcher
(``process_launcher.default_manager().launch``), and a child counts as launched
only when that launcher reports a started process.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import core, process_launcher, server

_B905_PATH = Path(__file__).resolve().parent / "test_aiworkhub_dependency_autolaunch_b905_v7.py"
_spec = importlib.util.spec_from_file_location("_b905_nf01780_helpers", _B905_PATH)
assert _spec is not None and _spec.loader is not None
b905 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(b905)
autolaunch = b905.autolaunch

SRC = Path(__file__).resolve().parents[1] / "src" / "aiworkhub"
RUNNER = "claude_opus-5"
TOPIC = "dependency_autolaunch"


class _FakeManager:
    """Records every spawn; claims the row the way the real launcher does."""

    def __init__(self, repo: Path, reply: dict[str, Any]) -> None:
        self.repo = repo
        self.reply = reply
        self.spawns: list[dict[str, Any]] = []

    def launch(self, **kwargs: Any) -> dict[str, Any]:
        self.spawns.append(dict(kwargs))
        if self.reply.get("ok"):
            _claim(self.repo, kwargs["task_id"], kwargs["runner"], str(self.reply["request_id"]))
        return dict(self.reply)


def _claim(repo: Path, task_id: str, runner: str, request_id: str) -> None:
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    try:
        card = json.loads(
            conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
        )
        card.update(
            launch_request_id=request_id, status="processing",
            worker_status="claimed", claimed_by=runner,
        )
        conn.execute(
            "UPDATE tasks SET card_json=?, status='processing', worker_status='claimed', "
            "claimed_by=? WHERE task_id=?",
            (json.dumps(card), runner, task_id),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def ready_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = b905._init_repo(tmp_path)
    b905._add(repo, "PARENT", status="finished", worker="done")
    b905._add(repo, "CHILD", deps=["PARENT"], runner=RUNNER, topic=TOPIC)
    monkeypatch.setattr(autolaunch, "_registered_launch", None)
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    return repo


def _install(monkeypatch: pytest.MonkeyPatch, manager: _FakeManager) -> None:
    monkeypatch.setattr(process_launcher, "default_manager", lambda: manager)
    autolaunch.register_launcher(server._dependency_autolaunch_launch)


def _delayed(outcome: dict[str, Any]) -> dict[str, Any]:
    [entry] = [row for row in outcome["delayed"] if row["task_id"] == "CHILD"]
    return entry


# ---------------------------------------------------------------------------
# Call sites: neither production entry point passes the bare claim
# ---------------------------------------------------------------------------


def _calls(path: Path, name: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == name
    ]


def _names(call: ast.Call) -> set[str]:
    return {
        getattr(node, "attr", None) or getattr(node, "id", "")
        for arg in [*call.args, *(kw.value for kw in call.keywords)]
        for node in ast.walk(arg)
    }


def test_neither_call_site_passes_the_bare_claim() -> None:
    [accept] = _calls(SRC / "core.py", "reconcile_after_accept")
    [startup] = _calls(SRC / "server.py", "reconcile_startup")

    for call in (accept, startup):
        assert "claim_start_exact" not in _names(call)
    assert ast.unparse(accept.args[2]) == "dependency_autolaunch.launch_registered"
    assert ast.unparse(startup.args[1]) == "_dependency_autolaunch_launch"
    [registration] = _calls(SRC / "server.py", "register_launcher")
    assert ast.unparse(registration.args[0]) == "_dependency_autolaunch_launch"


# ---------------------------------------------------------------------------
# The accept hook reaches a spawned process under the card's runner/topic
# ---------------------------------------------------------------------------


def test_ready_child_after_accept_reaches_the_launcher_and_is_launched(
    ready_child: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _FakeManager(
        ready_child, {"ok": True, "request_id": "worker-req-1", "pid": 4242, "state": "running"}
    )
    _install(monkeypatch, manager)

    outcome = autolaunch.reconcile_after_accept(ready_child, "PARENT", autolaunch.launch_registered)

    assert manager.spawns == [{"task_id": "CHILD", "runner": RUNNER, "topic": TOPIC}]
    assert outcome["launched"] == [{"task_id": "CHILD", "runner": RUNNER, "topic": TOPIC}]
    assert b905._state(ready_child, "CHILD")[:2] == ("processing", "claimed")


def test_startup_tool_adapter_launches_through_the_same_launcher(
    ready_child: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _FakeManager(
        ready_child, {"ok": True, "request_id": "worker-req-2", "pid": 77, "state": "running"}
    )
    monkeypatch.setattr(process_launcher, "default_manager", lambda: manager)

    outcome = autolaunch.reconcile_startup(ready_child, server._dependency_autolaunch_launch)

    assert [spawn["task_id"] for spawn in manager.spawns] == ["CHILD"]
    assert [row["task_id"] for row in outcome["launched"]] == ["CHILD"]


def test_ok_without_a_started_process_releases_the_launchers_own_claim(
    ready_child: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _FakeManager(
        ready_child, {"ok": True, "request_id": "worker-req-3", "pid": None, "state": "exited"}
    )
    _install(monkeypatch, manager)

    outcome = autolaunch.reconcile_after_accept(ready_child, "PARENT", autolaunch.launch_registered)

    assert len(manager.spawns) == 1
    assert outcome["launched"] == []
    entry = _delayed(outcome)
    assert entry["reason"] == autolaunch.LAUNCH_NOT_STARTED
    assert entry["claim_released"] is True
    status, worker_status, card = b905._state(ready_child, "CHILD")
    assert (status, worker_status) == ("pending", "unclaimed")
    assert "launch_request_id" not in card


def test_launcher_denial_is_held_not_launched(
    ready_child: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _FakeManager(ready_child, {"ok": False, "blocked_reason": "workforce_route_absent"})
    _install(monkeypatch, manager)

    outcome = autolaunch.reconcile_after_accept(ready_child, "PARENT", autolaunch.launch_registered)

    assert outcome["launched"] == []
    entry = _delayed(outcome)
    assert entry["reason"] == "launch_not_claimed"
    assert entry["stderr"] == "workforce_route_absent"
    assert entry["denial_kind"] == "deterministic"
    assert b905._state(ready_child, "CHILD")[:2] == ("pending", "unclaimed")


# ---------------------------------------------------------------------------
# Without the launch gate or a registered launcher nothing is ever spawned
# ---------------------------------------------------------------------------


def test_closed_launch_gate_never_spawns_or_claims(
    ready_child: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(process_launcher.ALLOW_LAUNCH_ENV, raising=False)
    manager = _FakeManager(
        ready_child, {"ok": True, "request_id": "never", "pid": 1, "state": "running"}
    )
    _install(monkeypatch, manager)

    outcome = autolaunch.reconcile_after_accept(ready_child, "PARENT", autolaunch.launch_registered)

    assert manager.spawns == []
    assert outcome["launched"] == []
    assert "dual_gate_closed" in _delayed(outcome)["stderr"]
    assert b905._state(ready_child, "CHILD")[:2] == ("pending", "unclaimed")


def test_unregistered_launcher_is_refused_before_any_claim(ready_child: Path) -> None:
    outcome = autolaunch.reconcile_after_accept(ready_child, "PARENT", autolaunch.launch_registered)

    assert outcome["launched"] == []
    assert _delayed(outcome) == {
        "task_id": "CHILD",
        "reason": autolaunch.LAUNCH_NOT_STARTED,
        "detail": autolaunch.NO_WORKER_LAUNCHER,
    }
    assert b905._state(ready_child, "CHILD")[:2] == ("pending", "unclaimed")


def test_the_bare_claim_is_still_refused_up_front(ready_child: Path) -> None:
    outcome = autolaunch.reconcile_startup(ready_child, core.claim_start_exact)

    assert outcome["launched"] == []
    assert _delayed(outcome)["detail"] == "launch_callable_claims_without_spawning_a_worker"
    assert b905._state(ready_child, "CHILD")[:2] == ("pending", "unclaimed")
