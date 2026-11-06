"""NF-2026-02103: the auto-accept lane.

Eligibility is pure and names one reason per failing condition; the lane runs
the accept off the drain loop, publishes exactly one ``auto_accepted`` callback
for a clean chain instead of ``manager_ready``, and falls back to
``manager_ready`` exactly as before for an ineligible chain, a disabled lane or
a failed accept. A replayed chain never accepts twice.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    agent_tool_instructions,
    auto_accept_lane,
    quality_evidence,
    review_orchestrator,
    sdlc_outcome_metrics,
    task_store,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RULE = (
    "Clean low/medium candidates (all gates green, zero reviewer defects) are accepted by "
    "the auto-accept lane; the manager reviews every candidate with findings or high risk "
    "and may revert an auto-accept."
)


def _packet() -> dict[str, Any]:
    return {
        "ok": True,
        "risk": {"effective_tier": "low", "required_reviewer_lenses": ["correctness"]},
        "gates": {
            "quality_gate_passed": True,
            "worker_mcp_gate": {
                "present": True,
                "gated": True,
                "satisfied": True,
                "receipt_conformance_blocking": False,
            },
            "destructive_diff_checks": [{"check_id": "deleted_tests", "status": "passed"}],
        },
        "review": {
            "lenses": [
                {
                    "lens": "correctness",
                    "usable": True,
                    "required": True,
                    "selected": True,
                    "finding_count": 0,
                    "observation_count": 2,
                }
            ],
            "findings": [],
            "observations": [{"id": "obs-1"}, {"id": "obs-2"}],
            "refine_required": False,
        },
        "accept_preview": {"evaluated": True, "blocked": False, "blockers": []},
        "truncation": {"dropped_sections": []},
        "diff": {"changed_path_count": 2},
    }


def _lens(packet: dict[str, Any]) -> dict[str, Any]:
    return packet["review"]["lenses"][0]


# ---------------------------------------------------------------------------
# eligibility(): one named reason per failing condition
# ---------------------------------------------------------------------------


def test_clean_low_and_medium_packets_are_eligible_with_observations() -> None:
    assert auto_accept_lane.eligibility(_packet()) == (True, [])
    medium = _packet()
    medium["risk"]["effective_tier"] = "Medium"
    assert auto_accept_lane.eligibility(medium) == (True, [])


def _set(path: tuple[str, ...], value: Any):
    def mutate(packet: dict[str, Any]) -> None:
        target = packet
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _lens_set(key: str, value: Any):
    return lambda packet: _lens(packet).__setitem__(key, value)


def _preview(**values: Any):
    return lambda packet: packet["accept_preview"].update(values)


CASES = [
    ("not_ok", _set(("ok",), False), "packet_unavailable"),
    ("dropped", _set(("truncation", "dropped_sections"), ["gates"]), "packet_truncated"),
    ("gate_red", _set(("gates", "quality_gate_passed"), False), "quality_gate_not_passed"),
    ("gate_unknown", _set(("gates", "quality_gate_passed"), None), "quality_gate_not_passed"),
    ("mcp_absent", _set(("gates", "worker_mcp_gate", "present"), False), "worker_mcp_gate_absent"),
    (
        "mcp_unsatisfied",
        _set(("gates", "worker_mcp_gate", "satisfied"), False),
        "worker_mcp_gate_not_satisfied",
    ),
    (
        "mcp_conformance",
        _set(("gates", "worker_mcp_gate", "receipt_conformance_blocking"), True),
        "worker_mcp_gate_not_satisfied",
    ),
    ("reports", _set(("reviewer_reports_unavailable",), True), "reviewer_reports_unavailable"),
    ("unusable", _lens_set("usable", False), "required_lens_not_usable:correctness"),
    ("unselected", _lens_set("selected", False), "required_lens_not_usable:correctness"),
    (
        "missing_lens",
        _set(("risk", "required_reviewer_lenses"), ["correctness", "security"]),
        "required_lens_not_usable:security",
    ),
    ("defect_count", _lens_set("finding_count", 1), "reviewer_defect_findings:correctness"),
    ("defect_row", _set(("review", "findings"), [{"id": "f-1"}]), "reviewer_defect_findings:1"),
    ("refine", _set(("review", "refine_required"), True), "refinement_required"),
    (
        "refine_blocker",
        _preview(blocked=True, blockers=[{"kind": "refinement_required", "detail": "x"}]),
        "refinement_required",
    ),
    ("not_evaluated", _preview(evaluated=False), "accept_preview_not_evaluated"),
    (
        "blocked",
        _preview(blocked=True, blockers=[{"kind": "required_reviewer_missing"}]),
        "accept_preview_blocked:required_reviewer_missing",
    ),
    ("blocked_unknown", _preview(blocked=None), "accept_preview_blocked"),
    (
        "destructive_check",
        _set(
            ("gates", "destructive_diff_checks"),
            [{"check_id": "deleted_tests", "status": quality_evidence.STATUS_FAILED}],
        ),
        "destructive_diff_blocker:deleted_tests",
    ),
    (
        "destructive_blocker",
        _preview(
            blocked=True,
            blockers=[
                {"kind": "destructive_diff_requires_manager_confirmation", "detail": "rm_tree"}
            ],
        ),
        "destructive_diff_blocker:rm_tree",
    ),
    ("high", _set(("risk", "effective_tier"), "high"), "risk_tier_not_low_or_medium:high"),
    ("no_tier", _set(("risk", "effective_tier"), ""), "risk_tier_not_low_or_medium:unknown"),
]


@pytest.mark.parametrize("name,mutate,reason", CASES, ids=[case[0] for case in CASES])
def test_each_failing_condition_yields_its_named_reason(name, mutate, reason) -> None:
    packet = _packet()
    mutate(packet)
    eligible, reasons = auto_accept_lane.eligibility(packet)
    assert eligible is False
    assert reasons == [reason]
    assert reason.split(":", 1)[0] in auto_accept_lane.ELIGIBILITY_REASONS


def test_no_reviewer_evidence_is_never_a_vacuous_pass() -> None:
    packet = _packet()
    packet["risk"]["required_reviewer_lenses"] = []
    packet["review"]["lenses"] = []
    assert auto_accept_lane.eligibility(packet) == (False, ["no_required_reviewer_lens"])
    assert auto_accept_lane.eligibility(None) == (False, ["packet_unavailable"])


def test_eligibility_is_pure() -> None:
    packet = _packet()
    before = copy.deepcopy(packet)
    assert auto_accept_lane.eligibility(packet) == auto_accept_lane.eligibility(packet)
    assert packet == before


# ---------------------------------------------------------------------------
# load_config(): missing/unreadable is disabled; the repository ships enabled
# ---------------------------------------------------------------------------


def _write_config(repo: Path, text: str) -> None:
    path = auto_accept_lane.config_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "text,reason",
    [
        ("{not json", "config_invalid_json"),
        ("[true]", "config_not_an_object"),
        ('{"enabled": "yes"}', "config_enabled_not_boolean"),
        ('{"enabled": false}', "config_disabled"),
    ],
)
def test_unreadable_or_non_boolean_config_is_disabled(tmp_path: Path, text, reason) -> None:
    _write_config(tmp_path, text)
    assert auto_accept_lane.load_config(tmp_path) == {"enabled": False, "reason": reason}


def test_missing_config_is_disabled_and_explicit_true_enables(tmp_path: Path) -> None:
    assert auto_accept_lane.load_config(tmp_path)["enabled"] is False
    _write_config(tmp_path, '{"enabled": true}')
    assert auto_accept_lane.load_config(tmp_path)["enabled"] is True


# ---------------------------------------------------------------------------
# run_attempt() / run_accept(): the same accept path, as the lane actor
# ---------------------------------------------------------------------------


def _accept_ok(*_args, **_kwargs) -> dict[str, Any]:
    return {
        "ok": True,
        "commit": {"state": "committed", "sha": "c" * 40},
        "promoted_path_count": 2,
        "learning_disposition": {"disposition": "no_new_lesson", "reason": "mechanical_change_only"},
    }


def test_ineligible_attempt_never_calls_accept() -> None:
    calls: list[Any] = []
    packet = _packet()
    packet["risk"]["effective_tier"] = "high"
    outcome = auto_accept_lane.run_attempt(
        object(), repo=".", task_id="T", request_id="R",
        packet_fn=lambda *_a, **_k: packet, accept_fn=lambda *a, **k: calls.append(1),
    )
    assert outcome["state"] == auto_accept_lane.STATE_REFUSED
    assert outcome["reasons"] == ["risk_tier_not_low_or_medium:high"]
    assert calls == []


def test_attempt_outcomes_for_accept_refusal_error_and_success() -> None:
    def run(accept_fn):
        return auto_accept_lane.run_attempt(
            object(), repo=".", task_id="T", request_id="R",
            packet_fn=lambda *_a, **_k: _packet(), accept_fn=accept_fn,
        )

    refused = run(lambda *_a, **_k: {"ok": False, "error": "neighbour_tests_failed:t.py"})
    assert refused["state"] == auto_accept_lane.STATE_FAILED
    assert refused["reasons"] == ["accept_refused:neighbour_tests_failed:t.py"]

    def boom(*_a, **_k):
        raise RuntimeError("store down")

    failed = run(boom)
    assert failed["reasons"] == ["auto_accept_internal_error:RuntimeError"]

    accepted = run(_accept_ok)
    assert accepted["state"] == auto_accept_lane.STATE_ACCEPTED
    assert accepted["commit"]["sha"] == "c" * 40
    assert accepted["actor"] == "auto_accept_lane"
    assert accepted["eligibility"]["eligible"] is True


def test_run_accept_uses_the_tool_accept_commit_and_lesson_path(monkeypatch, tmp_path) -> None:
    # Stand-ins registered under the real module names: run_accept imports
    # them lazily, and a sparse validation checkout may not carry them.
    accept_commit = types.ModuleType("aiworkhub.accept_commit")
    learning_commit_store = types.ModuleType("aiworkhub.learning_commit_store")
    learning_commit_store.DISPOSITION_NO_NEW_LESSON = "no_new_lesson"
    monkeypatch.setitem(sys.modules, "aiworkhub.accept_commit", accept_commit)
    monkeypatch.setitem(sys.modules, "aiworkhub.learning_commit_store", learning_commit_store)
    import aiworkhub

    monkeypatch.setattr(aiworkhub, "accept_commit", accept_commit, raising=False)
    monkeypatch.setattr(aiworkhub, "learning_commit_store", learning_commit_store, raising=False)

    calls: dict[str, Any] = {}

    class _Mgr:
        def accept_review(self, request_id, task_id, **kwargs):
            calls["accept"] = (request_id, task_id, kwargs)
            return {"ok": True, "promoted_paths": ["src/a.py"]}

    def snapshot(repo, *, auto_commit):
        calls["snapshot"] = auto_commit
        return "snap"

    accept_commit.snapshot_before_accept = snapshot

    def commit(snapshot, receipt, card, *, task_id, request_id, commit_message=None):
        calls["commit"] = (snapshot, receipt["schema_id"], task_id, request_id, commit_message)
        # accept_review takes no actor; the lane's accept receipt names it.
        assert receipt["accepted_by"] == "auto_accept_lane"
        return {"state": "committed", "sha": "d" * 40}

    accept_commit.commit_promoted = commit
    monkeypatch.setattr(task_store, "get_task", lambda _repo, _task: {"task_id": "T"})

    def disposition(repo, **kwargs):
        calls["disposition"] = kwargs
        return {"disposition": kwargs["disposition"], "reason": kwargs["reason"]}

    learning_commit_store.record_disposition = disposition
    result = auto_accept_lane.run_accept(_Mgr(), repo=tmp_path, task_id="T", request_id="R")
    assert result["ok"] is True
    assert result["commit"]["sha"] == "d" * 40
    # Every accept override stays at the tool's default.
    assert calls["accept"] == ("R", "T", {})
    assert calls["snapshot"] is True
    assert calls["commit"] == (
        "snap", auto_accept_lane.ACCEPT_RECEIPT_SCHEMA_ID, "T", "R", None
    )
    assert calls["disposition"]["reason"] == "mechanical_change_only"
    assert calls["disposition"]["recorded_by"] == "auto_accept_lane"
    assert calls["disposition"]["disposition"] == learning_commit_store.DISPOSITION_NO_NEW_LESSON


# ---------------------------------------------------------------------------
# The orchestrator hook
# ---------------------------------------------------------------------------


class _Manager:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.events: list[dict[str, Any]] = []

    def _append_event(self, event):
        self.events.append(dict(event))


class _Lane:
    """A configured repository, one completed chain, and recorded publishes."""

    def __init__(self, tmp_path: Path, monkeypatch, *, enabled: bool | None, accept_fn, packet=None):
        task_store.initialize_repository(tmp_path)
        _readiness, self.db_path = task_store._require_ready(tmp_path)
        if enabled is not None:
            _write_config(tmp_path, json.dumps({"enabled": enabled}))
        self.manager = _Manager(tmp_path)
        self.accept_calls = 0
        packet = packet if packet is not None else _packet()

        def accept(*args, **kwargs):
            self.accept_calls += 1
            return accept_fn(*args, **kwargs)

        self.runner = auto_accept_lane.AutoAcceptRunner(
            packet_fn=lambda *_a, **_k: copy.deepcopy(packet), accept_fn=accept
        )
        self.receipts = [
            {
                "manager_ready": {
                    "chain_id": 7,
                    "target_task_id": "TARGET",
                    "target_request_id": "target-request",
                    "claim_epoch": "1",
                }
            }
        ]
        monkeypatch.setattr(
            review_orchestrator.review_lifecycle,
            "completed_manager_ready_receipts",
            lambda _db: self.receipts,
        )
        self.published: list[str] = []

        def publish(_repo, *, task_id, request_id, claim_epoch):
            self.published.append(request_id)
            return True, "review", True

        monkeypatch.setattr(review_orchestrator.task_store, "publish_manager_ready", publish)

    def driver(self):
        return review_orchestrator.ReviewOrchestrator(
            self.manager, db_path=self.db_path, auto_accept_runner=self.runner
        )

    def events(self, event: str) -> list[dict[str, Any]]:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT runner, payload_json FROM task_events WHERE task_id='TARGET' AND event=?",
                (event,),
            ).fetchall()
        finally:
            conn.close()
        return [{"runner": row[0], **json.loads(row[1])} for row in rows]

    def settle(self) -> int:
        assert self.runner.wait(timeout=30)
        return self.driver()._publish_completed_manager_ready()


@pytest.fixture
def lanes():
    created: list[_Lane] = []
    yield created
    for lane in created:
        lane.runner.shutdown()


def test_clean_chain_is_auto_accepted_once_without_manager_ready(
    tmp_path, monkeypatch, lanes
) -> None:
    lane = _Lane(tmp_path, monkeypatch, enabled=True, accept_fn=_accept_ok)
    lanes.append(lane)
    # The first pass only starts the attempt: nothing is published yet.
    assert lane.driver()._publish_completed_manager_ready() == 0
    assert lane.settle() == 1
    assert lane.published == []
    callbacks = lane.events(auto_accept_lane.EVENT_AUTO_ACCEPTED)
    assert len(callbacks) == 1
    callback = callbacks[0]
    assert callback["runner"] == "auto_accept_lane"
    assert (callback["task_id"], callback["request_id"]) == ("TARGET", "target-request")
    assert callback["commit_sha"] == "c" * 40
    assert callback["eligibility_facts"]["effective_tier"] == "low"
    assert [e["event_type"] for e in lane.manager.events] == ["auto_accepted"]
    # Replays -- the same orchestrator, a fresh one, the lane switched off --
    # never accept twice and never publish a second callback.
    assert lane.settle() == 0
    _write_config(tmp_path, '{"enabled": false}')
    assert lane.settle() == 0
    assert lane.accept_calls == 1
    assert len(lane.events(auto_accept_lane.EVENT_AUTO_ACCEPTED)) == 1
    assert lane.published == []


def test_drain_is_not_blocked_while_the_accept_runs(tmp_path, monkeypatch, lanes) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_accept(*args, **kwargs):
        started.set()
        release.wait(timeout=30)
        return _accept_ok()

    lane = _Lane(tmp_path, monkeypatch, enabled=True, accept_fn=slow_accept)
    lanes.append(lane)
    try:
        assert lane.driver()._publish_completed_manager_ready() == 0
        assert started.wait(timeout=30)
        # The accept is still running: further passes return at once and defer.
        begun = time.monotonic()
        assert lane.driver()._publish_completed_manager_ready() == 0
        assert time.monotonic() - begun < 10
        assert lane.published == []
        assert lane.events(auto_accept_lane.EVENT_AUTO_ACCEPTED) == []
    finally:
        release.set()
    assert lane.settle() == 1
    assert lane.accept_calls == 1


def test_ineligible_chain_publishes_manager_ready_with_the_reason(
    tmp_path, monkeypatch, lanes
) -> None:
    packet = _packet()
    _lens(packet)["finding_count"] = 2
    lane = _Lane(tmp_path, monkeypatch, enabled=True, accept_fn=_accept_ok, packet=packet)
    lanes.append(lane)
    assert lane.driver()._publish_completed_manager_ready() == 0
    assert lane.settle() == 1
    assert lane.published == ["target-request"]
    assert lane.accept_calls == 0
    declined = lane.events(auto_accept_lane.EVENT_DECLINED)
    assert len(declined) == 1
    assert declined[0]["reasons"] == ["reviewer_defect_findings:correctness"]
    assert lane.events(auto_accept_lane.EVENT_AUTO_ACCEPTED) == []
    assert lane.settle() == 0
    assert lane.published == ["target-request"]


def test_failed_accept_publishes_manager_ready_with_the_reason(
    tmp_path, monkeypatch, lanes
) -> None:
    lane = _Lane(
        tmp_path, monkeypatch, enabled=True,
        accept_fn=lambda *_a, **_k: {"ok": False, "error": "neighbour_tests_failed:tests/x.py"},
    )
    lanes.append(lane)
    assert lane.driver()._publish_completed_manager_ready() == 0
    assert lane.settle() == 1
    assert lane.published == ["target-request"]
    declined = lane.events(auto_accept_lane.EVENT_DECLINED)
    assert [row["reasons"] for row in declined] == [
        ["accept_refused:neighbour_tests_failed:tests/x.py"]
    ]
    assert lane.events(auto_accept_lane.EVENT_AUTO_ACCEPTED) == []


@pytest.mark.parametrize("enabled", [None, False])
def test_disabled_lane_publishes_manager_ready_exactly_as_before(
    tmp_path, monkeypatch, lanes, enabled
) -> None:
    lane = _Lane(tmp_path, monkeypatch, enabled=enabled, accept_fn=_accept_ok)
    lanes.append(lane)
    assert lane.driver()._publish_completed_manager_ready() == 1
    assert lane.published == ["target-request"]
    assert lane.accept_calls == 0
    assert lane.events(auto_accept_lane.EVENT_DECLINED) == []
    assert lane.events(auto_accept_lane.EVENT_AUTO_ACCEPTED) == []
    assert lane.driver()._publish_completed_manager_ready() == 0


def test_running_row_without_owner_identity_is_never_declared_dead(
    tmp_path, monkeypatch, lanes
) -> None:
    lane = _Lane(tmp_path, monkeypatch, enabled=True, accept_fn=_accept_ok)
    lanes.append(lane)
    conn = sqlite3.connect(lane.db_path)
    try:
        auto_accept_lane.claim_attempt(
            conn, chain_id=7, task_id="TARGET", request_id="target-request",
            # A fresh claim: unknown liveness defers until ATTEMPT_STALE_SECONDS.
            owner="auto_accept_lane:unknown-process", now=_iso(),
        )
        conn.commit()
    finally:
        conn.close()
    # No recorded pid/start identity: liveness is unknown, so the row is left
    # running and nothing is published -- no wall-clock guess declares it dead.
    assert auto_accept_lane.owner_liveness(0, "") == auto_accept_lane.OWNER_UNKNOWN
    assert lane.driver()._publish_completed_manager_ready() == 0
    assert lane.published == []
    assert lane.accept_calls == 0
    assert lane.events(auto_accept_lane.EVENT_DECLINED) == []


# ---------------------------------------------------------------------------
# Counting and policy
# ---------------------------------------------------------------------------


def test_sdlc_metrics_count_auto_accepted_tasks() -> None:
    per_task = {
        "A": [{"event": "accept_review"}, {"event": "auto_accepted"}],
        "B": [{"event": "accept_review"}],
    }
    assert sdlc_outcome_metrics.auto_accepted_count(per_task, ("A", "B", "C")) == 1
    result = sdlc_outcome_metrics.aggregate([], [], repository_id="repo-one")
    assert result["auto_accepted"] == {
        "numerator": 0,
        "denominator": 0,
        "event": auto_accept_lane.EVENT_AUTO_ACCEPTED,
    }


def test_manager_role_rule_is_in_the_policy_and_every_rendered_file() -> None:
    assert RULE in agent_tool_instructions.POLICY.role
    for name in (
        "CLAUDE.md",
        "AGENTS.md",
        ".github/copilot-instructions.md",
        "docs/AIWORKHUB_TOOL_USE_POLICY.md",
    ):
        assert (REPO_ROOT / name).read_text(encoding="utf-8").count(RULE) == 1, name


# ---------------------------------------------------------------------------
# Fail-closed gates: an unknown value is never eligible
# ---------------------------------------------------------------------------


def test_missing_or_null_finding_count_is_ineligible() -> None:
    packet = _packet()
    _lens(packet)["finding_count"] = None
    assert auto_accept_lane.eligibility(packet) == (
        False, ["reviewer_defect_findings:correctness"]
    )
    del _lens(packet)["finding_count"]
    assert auto_accept_lane.eligibility(packet) == (
        False, ["reviewer_defect_findings:correctness"]
    )


def test_present_mcp_gate_needs_an_explicit_satisfied_true() -> None:
    packet = _packet()
    packet["gates"]["worker_mcp_gate"] = {"present": True, "satisfied": False}
    assert auto_accept_lane.eligibility(packet) == (False, ["worker_mcp_gate_not_satisfied"])
    for gated in (None, False):
        packet["gates"]["worker_mcp_gate"] = {"present": True, "gated": gated}
        assert auto_accept_lane.eligibility(packet) == (
            False, ["worker_mcp_gate_not_satisfied"]
        )
    packet["gates"]["worker_mcp_gate"] = {"present": True, "satisfied": True}
    assert auto_accept_lane.eligibility(packet) == (True, [])


# ---------------------------------------------------------------------------
# Cross-process liveness of a running attempt row
# ---------------------------------------------------------------------------


def _probe(monkeypatch, verdict) -> list[tuple[Any, Any]]:
    from aiworkhub import process_identity

    probed: list[tuple[Any, Any]] = []

    def probe(pid, ticks):
        probed.append((pid, ticks))
        return types.SimpleNamespace(verdict=verdict)

    monkeypatch.setattr(process_identity, "_pid_identity_evidence", probe)
    return probed


def _claim_foreign(
    lane: "_Lane", *, pid: int = 4242, ticks: Any = "99", now: str = ""
) -> None:
    conn = sqlite3.connect(lane.db_path)
    try:
        assert auto_accept_lane.claim_attempt(
            conn, chain_id=7, task_id="TARGET", request_id="target-request",
            owner="auto_accept_lane:other-process", now=now or _iso(),
            owner_pid=pid, owner_start_ticks=ticks,
        )
        conn.commit()
    finally:
        conn.close()


def _attempt(lane: "_Lane") -> auto_accept_lane.Attempt | None:
    conn = sqlite3.connect(lane.db_path)
    try:
        return auto_accept_lane.read_attempt(conn, 7)
    finally:
        conn.close()


def test_claimed_row_carries_the_runner_process_identity(tmp_path, monkeypatch, lanes) -> None:
    lane = _Lane(tmp_path, monkeypatch, enabled=True, accept_fn=_accept_ok)
    lanes.append(lane)
    assert lane.driver()._publish_completed_manager_ready() == 0
    attempt = _attempt(lane)
    assert attempt is not None
    assert attempt.owner == lane.runner.owner
    assert attempt.owner_pid == os.getpid() == lane.runner.owner_pid
    ticks = lane.runner.owner_start_ticks
    assert attempt.owner_start_ticks == ("" if ticks is None else str(ticks))
    assert lane.settle() == 1


@pytest.mark.parametrize("verdict_name", ["MATCH", "UNKNOWN"])
def test_foreign_live_or_unknown_owner_defers_and_leaves_the_row_running(
    tmp_path, monkeypatch, lanes, verdict_name
) -> None:
    from aiworkhub import process_identity

    lane = _Lane(tmp_path, monkeypatch, enabled=True, accept_fn=_accept_ok)
    lanes.append(lane)
    _claim_foreign(lane)
    probed = _probe(monkeypatch, getattr(process_identity.PidIdentityVerdict, verdict_name))
    assert lane.driver()._publish_completed_manager_ready() == 0
    assert lane.driver()._publish_completed_manager_ready() == 0
    assert probed and probed[0] == (4242, "99")
    attempt = _attempt(lane)
    assert attempt is not None
    assert (attempt.state, attempt.owner_pid, attempt.owner_start_ticks) == ("running", 4242, "99")
    assert lane.published == []
    assert lane.accept_calls == 0
    assert lane.events(auto_accept_lane.EVENT_DECLINED) == []


def test_foreign_dead_owner_is_interrupted_and_falls_back(tmp_path, monkeypatch, lanes) -> None:
    from aiworkhub import process_identity

    lane = _Lane(tmp_path, monkeypatch, enabled=True, accept_fn=_accept_ok)
    lanes.append(lane)
    _claim_foreign(lane)
    _probe(monkeypatch, process_identity.PidIdentityVerdict.MISMATCH)
    assert lane.driver()._publish_completed_manager_ready() == 1
    assert lane.published == ["target-request"]
    assert lane.accept_calls == 0
    assert lane.events(auto_accept_lane.EVENT_DECLINED)[0]["reasons"] == [
        "auto_accept_interrupted"
    ]
    attempt = _attempt(lane)
    assert attempt is not None and attempt.state == auto_accept_lane.STATE_FAILED


# ---------------------------------------------------------------------------
# An accept whose commit did not land is not auto-accepted
# ---------------------------------------------------------------------------


def _stub_accept_modules(monkeypatch, commit_result: dict[str, Any]) -> None:
    accept_commit = types.ModuleType("aiworkhub.accept_commit")
    learning_commit_store = types.ModuleType("aiworkhub.learning_commit_store")
    learning_commit_store.DISPOSITION_NO_NEW_LESSON = "no_new_lesson"
    monkeypatch.setitem(sys.modules, "aiworkhub.accept_commit", accept_commit)
    monkeypatch.setitem(sys.modules, "aiworkhub.learning_commit_store", learning_commit_store)
    import aiworkhub

    monkeypatch.setattr(aiworkhub, "accept_commit", accept_commit, raising=False)
    monkeypatch.setattr(aiworkhub, "learning_commit_store", learning_commit_store, raising=False)
    accept_commit.snapshot_before_accept = lambda repo, *, auto_commit: "snap"
    accept_commit.commit_promoted = lambda *_a, **_k: dict(commit_result)
    learning_commit_store.record_disposition = lambda repo, **kw: {
        "disposition": kw["disposition"], "reason": kw["reason"]
    }
    monkeypatch.setattr(task_store, "get_task", lambda _repo, _task: {"task_id": "TARGET"})


class _AcceptingManager:
    def accept_review(self, request_id, task_id, **kwargs):
        return {"ok": True, "promoted_paths": ["src/a.py", "src/b.py"]}


@pytest.mark.parametrize(
    "commit",
    [
        {"state": "failed", "reason": "git_commit_failed"},
        {"state": "skipped", "reason": "nothing_to_commit"},
    ],
    ids=["failed", "skipped"],
)
def test_accept_whose_commit_did_not_land_publishes_manager_ready(
    tmp_path, monkeypatch, lanes, commit
) -> None:
    _stub_accept_modules(monkeypatch, commit)
    expected = [f"auto_commit_{commit['state']}:{commit['reason']}"]
    outcome = auto_accept_lane.run_attempt(
        _AcceptingManager(), repo=tmp_path, task_id="TARGET", request_id="target-request",
        packet_fn=lambda *_a, **_k: _packet(),
    )
    assert outcome["state"] == auto_accept_lane.STATE_ACCEPTED_UNCOMMITTED
    assert outcome["state"] != auto_accept_lane.STATE_ACCEPTED
    assert outcome["reasons"] == expected
    assert outcome["accepted_by"] == "auto_accept_lane"

    lane = _Lane(
        tmp_path, monkeypatch, enabled=True,
        accept_fn=lambda _manager, **kw: auto_accept_lane.run_accept(_AcceptingManager(), **kw),
    )
    lanes.append(lane)
    assert lane.driver()._publish_completed_manager_ready() == 0
    assert lane.settle() == 1
    assert lane.published == ["target-request"]
    assert lane.events(auto_accept_lane.EVENT_AUTO_ACCEPTED) == []
    declined = lane.events(auto_accept_lane.EVENT_DECLINED)
    assert [(row["state"], row["reasons"]) for row in declined] == [
        (auto_accept_lane.STATE_ACCEPTED_UNCOMMITTED, expected)
    ]


# ---------------------------------------------------------------------------
# The auto_accepted manager callback
# ---------------------------------------------------------------------------


def _seed_task_route(db_path: Path, *, provider: str = "codex", origin: str = "thread-aa") -> None:
    """One TARGET task row bound to a manager route, as publish_manager_ready reads it."""
    conn = sqlite3.connect(db_path)
    try:
        values: dict[str, Any] = {}
        for _cid, name, decl, notnull, default, _pk in conn.execute("PRAGMA table_info(tasks)"):
            if notnull and default is None:
                values[name] = 0 if "INT" in str(decl).upper() else ""
        values.update(
            task_id="TARGET",
            card_json=json.dumps({"coordinator_provider": provider, "claim_epoch": "1"}),
            origin_thread_id=origin,
        )
        conn.execute(
            f"INSERT INTO tasks({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
            tuple(values.values()),
        )
        conn.commit()
    finally:
        conn.close()


def _callback_rows(db_path: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT transition, episode_id, request_id, provider, origin_thread_id "
            "FROM callback_outbox WHERE task_id='TARGET'"
        ).fetchall()
    finally:
        conn.close()


def test_auto_accepted_chain_enqueues_exactly_one_callback(tmp_path, monkeypatch, lanes) -> None:
    lane = _Lane(tmp_path, monkeypatch, enabled=True, accept_fn=_accept_ok)
    lanes.append(lane)
    _seed_task_route(lane.db_path)
    # The manager's wake is the review_ready callback it already consumes,
    # enqueued through the real in-transaction callback authority, unpatched.
    assert lane.driver()._publish_completed_manager_ready() == 0
    assert _callback_rows(lane.db_path) == []
    assert lane.settle() == 1
    assert _callback_rows(lane.db_path) == [
        ("review_ready", "1", "target-request", "codex", "thread-aa")
    ]
    (event,) = lane.events(auto_accept_lane.EVENT_AUTO_ACCEPTED)
    assert event["callback_enqueued"] is True
    assert event["accepted_by"] == "auto_accept_lane"
    assert event["commit_sha"] == "c" * 40
    assert event["promoted_path_count"] == 2
    assert event["claim_epoch"] == "1"
    assert lane.published == []
    # Replays enqueue nothing more.
    assert lane.settle() == 0
    assert lane.driver()._publish_completed_manager_ready() == 0
    assert len(_callback_rows(lane.db_path)) == 1
    assert len(lane.events(auto_accept_lane.EVENT_AUTO_ACCEPTED)) == 1


def test_unroutable_auto_accepted_callback_is_recorded_never_faked(
    tmp_path, monkeypatch, lanes
) -> None:
    lane = _Lane(tmp_path, monkeypatch, enabled=True, accept_fn=_accept_ok)
    lanes.append(lane)
    # No origin thread: the callback authority has nowhere to route the wake.
    _seed_task_route(lane.db_path, origin="")
    assert lane.driver()._publish_completed_manager_ready() == 0
    assert lane.settle() == 1
    assert _callback_rows(lane.db_path) == []
    (event,) = lane.events(auto_accept_lane.EVENT_AUTO_ACCEPTED)
    assert event["callback_enqueued"] is False


# ---------------------------------------------------------------------------
# A disabled lane never touches the lane's tables
# ---------------------------------------------------------------------------


def _guard_lane_connections(monkeypatch) -> None:
    real = review_orchestrator._side_table_connection

    def guarded(*args, **kwargs):
        if sys._getframe(1).f_code.co_name == "_auto_accept_projection":
            raise AssertionError("auto-accept lane opened a side-table connection")
        return real(*args, **kwargs)

    monkeypatch.setattr(review_orchestrator, "_side_table_connection", guarded)


def test_disabled_lane_never_opens_the_lane_side_table(tmp_path, monkeypatch, lanes) -> None:
    lane = _Lane(tmp_path, monkeypatch, enabled=False, accept_fn=_accept_ok)
    lanes.append(lane)
    _guard_lane_connections(monkeypatch)
    assert lane.driver()._publish_completed_manager_ready() == 1
    assert lane.published == ["target-request"]
    conn = sqlite3.connect(lane.db_path)
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='auto_accept_lane_attempts'"
        ).fetchall() == []
    finally:
        conn.close()
    # The guard is not vacuous: the enabled lane does reach it.
    _write_config(tmp_path, '{"enabled": true}')
    lane.receipts[0]["manager_ready"]["chain_id"] = 8
    with pytest.raises(AssertionError, match="side-table connection"):
        lane.driver()._publish_completed_manager_ready()


# ---------------------------------------------------------------------------
# The review_ready wake, switch-off, stale claims and the claim/submit window
# ---------------------------------------------------------------------------


def _iso(seconds_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _outbox(db_path: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT state, transition, episode_id FROM callback_outbox WHERE task_id='TARGET'"
        ).fetchall()
    finally:
        conn.close()


def _settle_direct(lane: "_Lane", *, enabled: bool = True) -> auto_accept_lane.Decision:
    conn = sqlite3.connect(lane.db_path)
    try:
        decision = auto_accept_lane.settle(
            conn, lane.runner, chain_id=7, task_id="TARGET", request_id="target-request",
            enabled=enabled, now=_iso(), claim_epoch="1",
        )
        conn.commit()
    finally:
        conn.close()
    return decision


def test_settled_auto_accept_leaves_one_pending_review_ready_wake(
    tmp_path, monkeypatch, lanes
) -> None:
    lane = _Lane(tmp_path, monkeypatch, enabled=True, accept_fn=_accept_ok)
    lanes.append(lane)
    _seed_task_route(lane.db_path)
    assert lane.driver()._publish_completed_manager_ready() == 0
    assert lane.runner.wait(timeout=30)
    decision = _settle_direct(lane)
    assert decision.action == auto_accept_lane.ACTION_AUTO_ACCEPTED
    assert _outbox(lane.db_path) == [("pending", "review_ready", "1")]
    (event,) = lane.events(auto_accept_lane.EVENT_AUTO_ACCEPTED)
    assert event["callback_enqueued"] is True
    assert lane.events("manager_ready") == []
    assert lane.published == []


def test_delivered_review_ready_row_is_rearmed_not_duplicated(
    tmp_path, monkeypatch, lanes
) -> None:
    lane = _Lane(tmp_path, monkeypatch, enabled=True, accept_fn=_accept_ok)
    lanes.append(lane)
    _seed_task_route(lane.db_path)
    conn = sqlite3.connect(lane.db_path)
    try:
        conn.execute(
            "INSERT INTO callback_outbox(task_id, provider, origin_thread_id, transition, "
            "episode_id, event_id, request_id, state, created_at, updated_at) "
            "VALUES ('TARGET', 'codex', 'thread-aa', 'review_ready', '1', '', "
            "'target-request', 'delivered', ?, ?)",
            (_iso(60), _iso(60)),
        )
        conn.commit()
    finally:
        conn.close()
    assert lane.driver()._publish_completed_manager_ready() == 0
    assert lane.settle() == 1
    assert _outbox(lane.db_path) == [("pending", "review_ready", "1")]
    rearmed = lane.events("callback_rearmed")
    assert [(row["transition"], row["episode_id"], row["rows"]) for row in rearmed] == [
        ("review_ready", "1", 1)
    ]
    (event,) = lane.events(auto_accept_lane.EVENT_AUTO_ACCEPTED)
    assert event["callback_enqueued"] is True
    assert lane.published == []


def test_running_attempt_is_settled_after_the_lane_is_switched_off(
    tmp_path, monkeypatch, lanes
) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_accept(*args, **kwargs):
        started.set()
        release.wait(timeout=30)
        return _accept_ok()

    lane = _Lane(tmp_path, monkeypatch, enabled=True, accept_fn=slow_accept)
    lanes.append(lane)
    try:
        assert lane.driver()._publish_completed_manager_ready() == 0
        assert started.wait(timeout=30)
        _write_config(tmp_path, '{"enabled": false}')
        # Its own runner still tracks the row: defer, never manager_ready.
        assert _settle_direct(lane, enabled=False).action == auto_accept_lane.ACTION_DEFER
        assert lane.driver()._publish_completed_manager_ready() == 0
        assert lane.published == []
    finally:
        release.set()
    # The terminal outcome is projected exactly once, with the lane still off.
    assert lane.settle() == 1
    assert lane.settle() == 0
    assert lane.accept_calls == 1
    assert len(lane.events(auto_accept_lane.EVENT_AUTO_ACCEPTED)) == 1
    assert lane.published == []


@pytest.mark.parametrize(
    "verdict_name,age,action",
    [
        ("UNKNOWN", auto_accept_lane.ATTEMPT_STALE_SECONDS + 60, "manager_ready"),
        ("UNKNOWN", 60, "defer"),
        ("MATCH", auto_accept_lane.ATTEMPT_STALE_SECONDS + 60, "defer"),
    ],
    ids=["unknown_stale", "unknown_young", "alive_stale"],
)
def test_unknown_owner_defers_only_until_its_claim_is_stale(
    tmp_path, monkeypatch, lanes, verdict_name, age, action
) -> None:
    from aiworkhub import process_identity

    lane = _Lane(tmp_path, monkeypatch, enabled=True, accept_fn=_accept_ok)
    lanes.append(lane)
    _claim_foreign(lane, now=_iso(age))
    _probe(monkeypatch, getattr(process_identity.PidIdentityVerdict, verdict_name))
    decision = _settle_direct(lane)
    assert decision.action == action
    attempt = _attempt(lane)
    assert attempt is not None
    if action == auto_accept_lane.ACTION_MANAGER_READY:
        assert decision.outcome is not None
        assert decision.outcome["reasons"] == [auto_accept_lane.REASON_INTERRUPTED]
        assert attempt.state == auto_accept_lane.STATE_FAILED
    else:
        assert decision.outcome is None
        assert attempt.state == auto_accept_lane.STATE_RUNNING
    assert lane.accept_calls == 0


def test_claimed_but_not_yet_submitted_chain_defers_for_its_own_runner(
    tmp_path, monkeypatch, lanes
) -> None:
    lane = _Lane(tmp_path, monkeypatch, enabled=True, accept_fn=_accept_ok)
    lanes.append(lane)
    assert _settle_direct(lane).action == auto_accept_lane.ACTION_SUBMIT
    # Between the claim's commit and submit: tracked, no result -> defer.
    assert _settle_direct(lane).action == auto_accept_lane.ACTION_DEFER
    assert lane.runner.tracking(7) and lane.runner.result(7) is None
    attempt = _attempt(lane)
    assert attempt is not None and attempt.state == auto_accept_lane.STATE_RUNNING
    assert lane.events(auto_accept_lane.EVENT_DECLINED) == []
    assert lane.published == []
    kwargs = {"repo": tmp_path, "task_id": "TARGET", "request_id": "target-request"}
    # The reserved attempt starts exactly once; an unreserved id never starts.
    assert lane.runner.submit(7, lane.manager, **kwargs) is True
    assert lane.runner.submit(7, lane.manager, **kwargs) is False
    lane.runner.reserve(9)
    lane.runner.release(9)
    assert lane.runner.tracking(9) is False
    assert lane.runner.submit(9, lane.manager, **kwargs) is False
    assert lane.settle() == 1
    assert lane.accept_calls == 1
    assert len(lane.events(auto_accept_lane.EVENT_AUTO_ACCEPTED)) == 1
