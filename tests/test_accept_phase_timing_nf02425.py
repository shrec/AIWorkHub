"""NF-2026-02425: an accept reply attributes its wall-clock time to named phases.

A medium accept took ~190-234 s and only the ~42 s of neighbour tests could be
attributed. The MCP accept tool now replies with ``accept_phase_seconds`` --
``{phase: seconds}`` for every phase the accept reached -- on success and on a
refusal alike, and nothing else in the reply changes.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import accept_commit
from aiworkhub import core
from aiworkhub import process_launcher
from aiworkhub import process_launcher_accept_review as accept_module
from aiworkhub import server
from aiworkhub import task_store

KEY = accept_module.ACCEPT_PHASE_SECONDS_KEY
REQUEST_ID = "R-NF02425"
TASK_ID = "T-NF02425"
# The sub-phases the real ``accept_review`` body times, in the order it reaches them.
LAUNCHER_PHASES = (
    "combined_tree_preparation",
    "declared_validation",
    "quality_gates",
    "neighbour_tests",
    "promotion_lock_wait",
    "promotion",
)
COMMIT = {"state": "skipped", "reason": "auto_commit_disabled", "paths": ["src/x.py"]}


def _assert_seconds(phases: dict[str, Any]) -> None:
    for name, seconds in phases.items():
        assert isinstance(seconds, float), (name, seconds)
        assert seconds >= 0.0, (name, seconds)


class _Manager:
    """``default_manager()`` whose accept enters ``phases`` and replies ``reply``."""

    def __init__(self, phases: tuple[str, ...], reply: dict[str, Any]) -> None:
        self.phases = phases
        self.reply = reply

    def accept_review(self, request_id: str, task_id: str, **_kwargs: Any) -> dict[str, Any]:
        for name in self.phases:
            with accept_module.accept_phase(name):
                pass
        return dict(self.reply)


@pytest.fixture
def through_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    commits: list[dict[str, Any]] = []

    def install(manager: _Manager) -> list[dict[str, Any]]:
        monkeypatch.setattr(process_launcher, "default_manager", lambda: manager)
        monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
        monkeypatch.setattr(
            task_store, "get_task", lambda _root, task_id: {"task_id": task_id, "status": "done"}
        )
        monkeypatch.setattr(
            accept_commit, "snapshot_before_accept", lambda _root, auto_commit: object()
        )

        def commit_promoted(_snapshot, receipt, _card, **_kwargs):
            commits.append(dict(receipt))
            return dict(COMMIT)

        monkeypatch.setattr(accept_commit, "commit_promoted", commit_promoted)
        return commits

    return install


def test_a_successful_accept_reports_every_phase_it_ran(through_server) -> None:
    reply = {
        "ok": True, "request_id": REQUEST_ID, "task_id": TASK_ID, "promoted_paths": ["src/x.py"],
    }
    commits = through_server(_Manager(LAUNCHER_PHASES, reply))

    receipt = server.aiworkhub_agent_accept_review(request_id=REQUEST_ID, task_id=TASK_ID)

    assert receipt["ok"] is True
    phases = receipt[KEY]
    assert set(phases) == {
        "snapshot_before_accept", "accept_review", *LAUNCHER_PHASES, "commit_promoted",
    }
    _assert_seconds(phases)
    # ``accept_review`` spans the sub-phases it reported itself.
    assert phases["accept_review"] >= max(phases[name] for name in LAUNCHER_PHASES)
    # Every other receipt field is what it was without the timing.
    assert receipt["schema_id"] == "aiworkhub.accept_review_receipt.v1"
    assert receipt["promoted_paths"] == ["src/x.py"]
    assert receipt["commit"] == COMMIT
    assert len(commits) == 1 and KEY not in commits[0]


def test_a_refused_accept_reports_only_the_phases_it_reached(through_server) -> None:
    refusal = {
        "ok": False,
        "error": "revalidation_failed:neighbour_tests_failed:x",
        "request_id": REQUEST_ID,
        "task_id": TASK_ID,
    }
    reached = LAUNCHER_PHASES[:4]
    commits = through_server(_Manager(reached, refusal))

    reply = server.aiworkhub_agent_accept_review(request_id=REQUEST_ID, task_id=TASK_ID)

    assert set(reply[KEY]) == {"snapshot_before_accept", "accept_review", *reached}
    _assert_seconds(reply[KEY])
    assert {key: value for key, value in reply.items() if key != KEY} == refusal
    assert commits == []


def test_a_refusal_before_any_sub_phase_still_reports_what_it_reached(through_server) -> None:
    refusal = {"ok": False, "error": "request_not_found", "request_id": REQUEST_ID}
    through_server(_Manager((), refusal))

    reply = server.aiworkhub_agent_accept_review(request_id=REQUEST_ID, task_id=TASK_ID)

    assert set(reply[KEY]) == {"snapshot_before_accept", "accept_review"}
    assert {key: value for key, value in reply.items() if key != KEY} == refusal


def _fake_clock(monkeypatch: pytest.MonkeyPatch, *ticks: float) -> None:
    """Only this module's ``time`` is replaced, never the interpreter-wide clock."""
    clock = iter(ticks)
    monkeypatch.setattr(accept_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))


def test_phase_seconds_accumulate_count_a_raising_block_and_unbind_on_exit(monkeypatch) -> None:
    _fake_clock(monkeypatch, 10.0, 12.5, 20.0, 21.0, 30.0, 34.0, 40.0, 41.0)

    with accept_module.accept_phases_recorded() as phases:
        with accept_module.accept_phase("declared_validation"):
            pass
        with accept_module.accept_phase("declared_validation"):
            pass
        with pytest.raises(RuntimeError):
            with accept_module.accept_phase("neighbour_tests"):
                raise RuntimeError("red")

    assert phases == {"declared_validation": 3.5, "neighbour_tests": 4.0}
    # Unbound again: a phase outside any recorder times nothing and records nowhere.
    with accept_module.accept_phase("promotion"):
        pass
    assert phases == {"declared_validation": 3.5, "neighbour_tests": 4.0}


def test_a_clock_that_steps_backwards_never_reports_negative_seconds(monkeypatch) -> None:
    _fake_clock(monkeypatch, 5.0, 4.0)

    with accept_module.accept_phases_recorded() as phases:
        with accept_module.accept_phase("promotion"):
            pass

    assert phases == {"promotion": 0.0}


def test_the_real_accept_body_times_exactly_the_launcher_phases() -> None:
    """Ties the stub's phase names above to the ``with accept_phase(...)`` sites."""
    tree = ast.parse(Path(accept_module.__file__).read_text(encoding="utf-8"))
    body = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "accept_review"
    )
    timed = {
        call.args[0].value
        for call in ast.walk(body)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "accept_phase"
        and call.args
        and isinstance(call.args[0], ast.Constant)
    }
    assert timed == set(LAUNCHER_PHASES)
