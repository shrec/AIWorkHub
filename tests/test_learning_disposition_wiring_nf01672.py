"""NF-2026-01672: accept/reject close a decision's learning out, and a committed lesson records its own disposition."""

from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from _taskdb_compat import upsert_card
from aiworkhub import (
    core,
    evidence_levels,
    feature_settings,
    learning_commit,
    learning_commit_store,
    manager_ai_tools,
    process_launcher,
    server,
    task_store,
)

SESSION_ID = "019f5097-6dbe-7172-870a-945afc5f3bfa"
TASK_ID = "TASK-WIRING-1"
REQUEST_ID = "request-wiring-0001"
REASON = "the changed branch is never exercised by a test"
LESSON = "a decision that closes its learning out in the same call never owes one"
ACCEPTANCE_REFERENCE = (
    f"file:.aiworkhub/runtime/process_logs/attempt-artifacts/{REQUEST_ID}/manifest.json"
)
EXTRA_EVIDENCE = "file:tests/test_learning_disposition_wiring_nf01672.py"
PROMOTED = ["src/aiworkhub/skill_registry_store.py"]
REJECTED_PATHS = ["src/aiworkhub/skill_miner.py", "tests/test_skill_miner.py"]
NO_NEW_LESSON = learning_commit_store.DISPOSITION_NO_NEW_LESSON
TOOLS = ("accept", "reject")
ENGINE_ACCEPT_ARGS = {
    "request_id",
    "task_id",
    "confirm_destructive_change",
    "requested_risk_tier",
    "risk_signals",
    "reviewer_reports",
    "reviewer_request_ids",
    "confirm_high_risk",
    "manager_correction",
}
NEW_PARAMETERS = {
    "lesson_candidate": None,
    "promote_ai_memory": False,
    "extra_evidence_ids": None,
    "no_new_lesson_reason": None,
}


def _manager_route(root: Path) -> dict[str, Any]:
    return {
        "ok": True,
        "role": "manager",
        "provider": "codex",
        "repo": str(root),
        "manager_route": {
            "provider": "codex",
            "session_id": SESSION_ID,
            "thread_id": SESSION_ID,
        },
    }


def _review_card() -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "runner": "worker",
        "topic": "learning",
        "mode": "edit",
        "status": "review",
        "worker_status": "review",
    }


def _accepted_card(request_id: str = REQUEST_ID) -> dict[str, Any]:
    record = evidence_levels.EvidenceRecord(
        evidence_level=evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED,
        severity="NONE",
        confidence="HIGH",
        reference=ACCEPTANCE_REFERENCE,
        verified_by="codex",
        message="manager verified exact accepted outcome",
    ).to_dict()
    return {
        **_review_card(),
        "status": "finished",
        "worker_status": "done",
        "accepted_request_id": request_id,
        "accept_evidence": {
            "acceptance_evidence_record": record,
            "promoted_paths": list(PROMOTED),
        },
    }


def _rejected_card(*, identified: bool = True) -> dict[str, Any]:
    card = {**_review_card(), "status": "pending", "worker_status": "pending"}
    if identified:
        card["review_feedback"] = {
            "schema_id": "aiworkhub.rework_feedback_delta.v1",
            "instruction": "the reader must consult the fact on the object",
            "predecessor_request_id": REQUEST_ID,
            "predecessor_changed_paths": list(REJECTED_PATHS),
        }
    return card


def _seed(root: Path, card: dict[str, Any]) -> None:
    con = sqlite3.connect(str(task_store.canonical_db_path(root)))
    con.row_factory = sqlite3.Row
    try:
        upsert_card(con, card)
    finally:
        con.close()


def _rows(root: Path, table: str) -> list[dict[str, Any]]:
    con = sqlite3.connect(str(task_store.canonical_db_path(root)))
    con.row_factory = sqlite3.Row
    try:
        present = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if present is None:
            return []
        return [dict(row) for row in con.execute(f"SELECT * FROM {table}")]
    finally:
        con.close()


def _stored(root: Path, request_id: str = REQUEST_ID) -> dict[str, Any] | None:
    return learning_commit_store.read_disposition(
        root, task_id=TASK_ID, request_id=request_id
    )


def _assert_nothing_recorded(root: Path) -> None:
    assert _rows(root, "learning_dispositions") == []
    assert _rows(root, "learning_commits") == []


class _Decisions:
    """Stands in for both decision engines and leaves behind the card each would have written."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.accept_ok = True
        self.reject_ok = True
        self.owed = True
        self.identified = True
        self.decided: str | None = None
        self.accepts: list[dict[str, Any]] = []
        self.rejects: list[dict[str, Any]] = []
        self.cancellations: list[dict[str, Any]] = []
        self.accept_result: dict[str, Any] = {}

    def accept_review(
        self, request_id: str, task_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        self.accepts.append({"request_id": request_id, "task_id": task_id, **kwargs})
        if not self.accept_ok:
            return {"ok": False, "error": "accept_refused"}
        decided = self.decided or request_id
        _seed(self.root, _accepted_card(decided))
        result: dict[str, Any] = {
            "ok": True,
            "request_id": decided,
            "promoted_paths": list(PROMOTED),
        }
        if self.owed:
            result["learning_commit_owed"] = learning_commit.commit_owed(
                task_id=task_id,
                request_id=decided,
                outcome="accepted",
                changed_paths=PROMOTED,
                evidence_reference=ACCEPTANCE_REFERENCE,
            )
        self.accept_result = result
        return dict(result)

    def reject_review(self, **kwargs: Any) -> dict[str, Any]:
        self.rejects.append(kwargs)
        command = ["taskctl", "reject-review", kwargs["task_id"], "--reason", kwargs["reason"]]
        if not self.reject_ok:
            return {
                "ok": False,
                "returncode": 1,
                "command": command,
                "stdout": "",
                "stderr": "reject_refused",
            }
        _seed(self.root, _rejected_card(identified=self.identified))
        result: dict[str, Any] = {
            "ok": True,
            "returncode": 0,
            "command": command,
            "stdout": json.dumps(task_store.get_task(self.root, kwargs["task_id"])),
            "stderr": "",
            "reviewer_finalization": [],
        }
        if self.owed:
            result["learning_commit_owed"] = learning_commit.commit_owed(
                task_id=kwargs["task_id"],
                request_id=REQUEST_ID,
                outcome="rejected",
                changed_paths=REJECTED_PATHS,
            )
        return result

    def cancel_disposed_reviewer_processes(
        self, finalization: Any, *, rejected_request_id: str = ""
    ) -> dict[str, Any]:
        self.cancellations.append(
            {"finalization": finalization, "rejected_request_id": rejected_request_id}
        )
        return {"cancelled": [], "rejected_request_id": rejected_request_id}


@pytest.fixture
def decisions(tmp_path, monkeypatch) -> _Decisions:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    feature_settings.update(root, changes={"context_graph": True}, expected_revision=0)
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(root))
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    _seed(root, _review_card())
    fake = _Decisions(root)
    monkeypatch.setattr(process_launcher, "default_manager", lambda: fake)
    monkeypatch.setattr(core, "reject_review", fake.reject_review)
    return fake


def _decide(tool: str, **learning: Any) -> dict[str, Any]:
    if tool == "accept":
        return server.aiworkhub_agent_accept_review(
            request_id=REQUEST_ID, task_id=TASK_ID, **learning
        )
    return server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, **learning
    )


# --------------------------------------------------------------------------- #
# A lesson supplied with the decision is committed in the same call.
# --------------------------------------------------------------------------- #


def test_accept_with_a_lesson_accepts_and_commits_in_one_call(decisions):
    root = decisions.root

    receipt = server.aiworkhub_agent_accept_review(
        request_id=REQUEST_ID,
        task_id=TASK_ID,
        lesson_candidate=LESSON,
        promote_ai_memory=True,
        extra_evidence_ids=[EXTRA_EVIDENCE],
    )

    assert receipt["ok"] is True, receipt
    assert receipt["schema_id"] == "aiworkhub.accept_review_receipt.v1"
    assert len(decisions.accepts) == 1
    assert set(decisions.accepts[0]) == ENGINE_ACCEPT_ARGS, (
        "none of the learning parameters may reach the accept engine"
    )
    assert receipt["learning_commit_owed"]["request_id"] == REQUEST_ID
    assert "learning_disposition" not in receipt
    commit = receipt["learning_commit"]
    assert commit["ok"] is True, commit
    assert (commit["outcome"], commit["task_id"], commit["request_id"]) == (
        "accepted",
        TASK_ID,
        REQUEST_ID,
    )
    assert commit["projections"]["ai_memory"]["state"] == "applied"
    stored = _stored(root)
    assert stored["disposition"] == "lesson_committed"
    assert stored["commit_id"] == commit["commit_id"]
    assert stored["recorded_by"] == SESSION_ID
    assert commit["disposition"] == {
        "state": "applied",
        "disposition_id": stored["disposition_id"],
    }
    payload = json.loads(_rows(root, "learning_commits")[0]["payload_json"])
    assert payload["lesson_candidate"] == LESSON
    assert payload["promotion_eligible_ai_memory"] is True
    assert payload["evidence_ids"] == [ACCEPTANCE_REFERENCE, EXTRA_EVIDENCE]
    measured = learning_commit_store.coverage(root)
    assert measured["cards_with_disposition"] == 1
    assert measured["cards_with_lesson"] == 1
    assert measured["disposition_coverage_percent"] == 100.0
    assert measured["recent_without_disposition"] == []


def test_reject_with_a_lesson_rejects_and_commits_in_one_call(decisions):
    root = decisions.root

    receipt = server.aiworkhub_task_reject_review(
        task_id=TASK_ID,
        reason=REASON,
        lesson_candidate=LESSON,
        promote_ai_memory=True,
        extra_evidence_ids=[EXTRA_EVIDENCE],
    )

    assert receipt["ok"] is True, receipt
    assert receipt["schema_id"] == "aiworkhub.reject_review_receipt.v1"
    assert decisions.rejects == [{"task_id": TASK_ID, "reason": REASON, "to": "pending"}], (
        "none of the learning parameters may reach core.reject_review"
    )
    assert receipt["request_id"] == REQUEST_ID
    assert receipt["learning_commit_owed"]["outcome"] == "rejected"
    assert decisions.cancellations == [
        {"finalization": [], "rejected_request_id": REQUEST_ID}
    ]
    assert "reviewer_process_cancellation" in receipt
    assert "learning_disposition" not in receipt
    commit = receipt["learning_commit"]
    assert commit["ok"] is True, commit
    assert (commit["outcome"], commit["request_id"]) == ("rejected", REQUEST_ID)
    assert commit["projections"]["ai_memory"]["state"] == "applied"
    stored = _stored(root)
    assert stored["disposition"] == "lesson_committed"
    assert stored["commit_id"] == commit["commit_id"]
    assert stored["recorded_by"] == SESSION_ID
    assert commit["disposition"] == {
        "state": "applied",
        "disposition_id": stored["disposition_id"],
    }
    payload = json.loads(_rows(root, "learning_commits")[0]["payload_json"])
    assert payload["outcome"] == "rejected"
    assert payload["lesson_candidate"] == LESSON
    assert payload["evidence_ids"] == [EXTRA_EVIDENCE]


# --------------------------------------------------------------------------- #
# A no-new-lesson reason is recorded as that disposition, and nothing else.
# --------------------------------------------------------------------------- #


def test_accept_with_a_no_new_lesson_reason_records_that_disposition(decisions):
    root = decisions.root

    receipt = server.aiworkhub_agent_accept_review(
        request_id=REQUEST_ID,
        task_id=TASK_ID,
        no_new_lesson_reason="mechanical_change_only",
    )

    assert receipt["ok"] is True, receipt
    recorded = receipt["learning_disposition"]
    assert recorded["ok"] is True, recorded
    assert recorded["surface"] == "manager_mcp"
    assert (recorded["task_id"], recorded["request_id"]) == (TASK_ID, REQUEST_ID)
    assert recorded["disposition"] == "no_new_lesson"
    assert recorded["reason"] == "mechanical_change_only"
    assert recorded["commit_id"] == ""
    assert recorded["recorded_by"] == SESSION_ID
    assert recorded["idempotent"] is False
    assert "learning_commit" not in receipt
    assert receipt["learning_commit_owed"]["owed"] is True
    assert _rows(root, "learning_commits") == []
    assert [row["disposition"] for row in _rows(root, "learning_dispositions")] == [
        "no_new_lesson"
    ]
    measured = learning_commit_store.coverage(root)
    assert measured["cards_with_disposition"] == 1
    assert measured["cards_with_lesson"] == 0, "a no_new_lesson is not a written lesson"


def test_reject_with_a_no_new_lesson_reason_records_that_disposition(decisions):
    root = decisions.root

    receipt = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, no_new_lesson_reason="no_generalizable_cause"
    )

    assert receipt["ok"] is True, receipt
    recorded = receipt["learning_disposition"]
    assert recorded["ok"] is True, recorded
    assert recorded["disposition"] == "no_new_lesson"
    assert recorded["reason"] == "no_generalizable_cause"
    assert recorded["request_id"] == REQUEST_ID
    assert recorded["recorded_by"] == SESSION_ID
    assert "learning_commit" not in receipt
    assert receipt["learning_commit_owed"]["owed"] is True
    assert receipt["to"] == "pending"
    assert decisions.rejects == [{"task_id": TASK_ID, "reason": REASON, "to": "pending"}]
    assert _rows(root, "learning_commits") == []
    assert [row["disposition"] for row in _rows(root, "learning_dispositions")] == [
        "no_new_lesson"
    ]


def test_a_reject_closeout_falls_back_to_the_request_id_in_its_receipt(decisions):
    decisions.owed = False

    receipt = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, no_new_lesson_reason="evidence_insufficient"
    )

    assert "learning_commit_owed" not in receipt
    assert receipt["learning_disposition"]["request_id"] == REQUEST_ID
    assert _stored(decisions.root)["reason"] == "evidence_insufficient"


def test_an_accept_closeout_without_an_owed_record_uses_the_receipts_request_id(decisions):
    decisions.owed = False
    decisions.decided = "request-decided-0003"

    receipt = server.aiworkhub_agent_accept_review(
        request_id=REQUEST_ID, task_id=TASK_ID, no_new_lesson_reason="evidence_insufficient"
    )

    assert "learning_commit_owed" not in receipt
    assert receipt["learning_disposition"]["request_id"] == "request-decided-0003"
    assert _stored(decisions.root, "request-decided-0003")["reason"] == "evidence_insufficient"
    assert _stored(decisions.root) is None


def test_the_closeout_targets_the_request_the_decision_reports(decisions):
    decisions.decided = "request-decided-0002"

    receipt = server.aiworkhub_agent_accept_review(
        request_id=REQUEST_ID,
        task_id=TASK_ID,
        no_new_lesson_reason="mechanical_change_only",
    )

    assert receipt["learning_disposition"]["request_id"] == "request-decided-0002"
    assert _stored(decisions.root, "request-decided-0002")["reason"] == "mechanical_change_only"
    assert _stored(decisions.root) is None


# --------------------------------------------------------------------------- #
# Contradictory input is refused before the decision changes anything.
# --------------------------------------------------------------------------- #

REFUSALS = [
    pytest.param(
        {"lesson_candidate": LESSON, "no_new_lesson_reason": "mechanical_change_only"},
        "learning_inline_conflict",
        id="lesson-and-reason",
    ),
    pytest.param(
        {"lesson_candidate": LESSON, "no_new_lesson_reason": "not-a-token"},
        "learning_inline_conflict",
        id="conflict-outranks-unknown-reason",
    ),
    pytest.param(
        {"no_new_lesson_reason": "because it was a small change"},
        "learning_disposition_invalid:unknown_no_new_lesson_reason",
        id="free-form-reason",
    ),
    pytest.param(
        {"no_new_lesson_reason": "MECHANICAL_CHANGE_ONLY"},
        "learning_disposition_invalid:unknown_no_new_lesson_reason",
        id="reason-is-case-sensitive",
    ),
    pytest.param(
        {"promote_ai_memory": True},
        "learning_commit_memory_promotion_requires_lesson",
        id="promotion-without-lesson",
    ),
    pytest.param(
        {"promote_ai_memory": True, "no_new_lesson_reason": "mechanical_change_only"},
        "learning_commit_memory_promotion_requires_lesson",
        id="promotion-with-reason",
    ),
    pytest.param(
        {"promote_ai_memory": True, "lesson_candidate": "   "},
        "learning_commit_memory_promotion_requires_lesson",
        id="promotion-with-blank-lesson",
    ),
]


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("learning, error", REFUSALS)
def test_a_contradictory_closeout_is_refused_before_any_state_change(
    decisions, tool, learning, error
):
    before = task_store.get_task(decisions.root, TASK_ID)

    reply = _decide(tool, **learning)

    assert reply["ok"] is False
    assert reply["error"] == error
    assert decisions.accepts == [] and decisions.rejects == []
    assert task_store.get_task(decisions.root, TASK_ID) == before
    _assert_nothing_recorded(decisions.root)


@pytest.mark.parametrize("tool", TOOLS)
def test_an_unknown_reason_names_the_tokens_that_would_have_been_accepted(
    decisions, tool
):
    reply = _decide(tool, no_new_lesson_reason="because it was small")

    assert reply["ok"] is False
    assert reply["allowed"] == list(learning_commit_store.NO_NEW_LESSON_REASONS)
    assert reply["received"] == "because it was small"


@pytest.mark.parametrize("tool", TOOLS)
def test_a_bad_include_card_is_still_refused_first(decisions, tool):
    reply = _decide(
        tool,
        include_card="bogus",
        lesson_candidate=LESSON,
        no_new_lesson_reason="mechanical_change_only",
    )

    assert reply["ok"] is False
    assert reply["error"] == "invalid_include_card"
    assert decisions.accepts == [] and decisions.rejects == []
    _assert_nothing_recorded(decisions.root)


# --------------------------------------------------------------------------- #
# Without the new parameters nothing changes and nothing is recorded.
# --------------------------------------------------------------------------- #


def test_a_plain_accept_reply_is_exactly_what_it_was(decisions):
    root = decisions.root

    receipt = server.aiworkhub_agent_accept_review(request_id=REQUEST_ID, task_id=TASK_ID)

    card = task_store.get_task(root, TASK_ID)
    # The one addition is the auto-commit report; this root is no git work tree.
    commit = receipt["commit"]
    assert commit["state"] == "skipped", commit
    assert commit["reason"] in {"not_a_git_work_tree", "repo_root_not_git_toplevel"}, commit
    assert commit["paths"] == PROMOTED
    expected = {
        "schema_id": "aiworkhub.accept_review_receipt.v1",
        **decisions.accept_result,
        "status": card["status"],
        "worker_status": card["worker_status"],
        **server._card_identity(card),
        "include_card": "none",
        "commit": commit,
    }
    assert json.dumps(receipt, sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert receipt["status"] == "finished"
    _assert_nothing_recorded(root)
    measured = learning_commit_store.coverage(root)
    assert (measured["decided_cards"], measured["cards_with_disposition"]) == (1, 0), (
        "a decision nobody closed out stays unclosed: no disposition is ever defaulted"
    )


def test_a_plain_reject_reply_is_exactly_what_it_was(decisions):
    receipt = server.aiworkhub_task_reject_review(task_id=TASK_ID, reason=REASON)

    assert set(receipt) == {
        "schema_id",
        "ok",
        "task_id",
        "status",
        "worker_status",
        "card_sha256",
        "card_bytes",
        "reviewer_finalization",
        "learning_commit_owed",
        "reviewer_process_cancellation",
        "to",
        "reason_identity",
        "failure_category",
        "failure_category_source",
        "request_id",
        "claim_epoch",
        "prior_episode",
        "predecessor_changed_paths_count",
        "residual_identities_count",
        "include_card",
    }
    assert receipt["learning_commit_owed"]["request_id"] == REQUEST_ID
    assert decisions.rejects == [{"task_id": TASK_ID, "reason": REASON, "to": "pending"}]
    _assert_nothing_recorded(decisions.root)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    "learning",
    [
        pytest.param({"lesson_candidate": ""}, id="empty-lesson"),
        pytest.param({"lesson_candidate": "   "}, id="blank-lesson"),
        pytest.param({"no_new_lesson_reason": ""}, id="empty-reason"),
        pytest.param({"no_new_lesson_reason": "  "}, id="blank-reason"),
        pytest.param({"extra_evidence_ids": [EXTRA_EVIDENCE]}, id="evidence-only"),
        pytest.param(dict(NEW_PARAMETERS), id="explicit-defaults"),
    ],
)
def test_parameters_that_name_no_learning_record_nothing(decisions, tool, learning):
    reply = _decide(tool, **learning)

    assert reply["ok"] is True, reply
    assert "learning_commit" not in reply and "learning_disposition" not in reply
    assert reply["learning_commit_owed"]["owed"] is True
    _assert_nothing_recorded(decisions.root)


@pytest.mark.parametrize(
    "tool", [server.aiworkhub_agent_accept_review, server.aiworkhub_task_reject_review]
)
def test_the_new_parameters_are_optional_keywords_with_inert_defaults(tool):
    parameters = inspect.signature(tool).parameters

    assert {name: parameters[name].default for name in NEW_PARAMETERS} == NEW_PARAMETERS


@pytest.mark.parametrize("tool", TOOLS)
def test_the_closeout_rides_alongside_the_card_view(decisions, tool):
    reply = _decide(tool, include_card="full", lesson_candidate=LESSON)

    assert reply["ok"] is True, reply
    assert reply["include_card"] == "full"
    assert reply["card"]["task_id"] == TASK_ID
    assert reply["learning_commit"]["ok"] is True, reply["learning_commit"]


# --------------------------------------------------------------------------- #
# A failed close-out is reported under its own key and never touches the decision.
# --------------------------------------------------------------------------- #


def test_a_prior_no_new_lesson_does_not_fail_the_decision_or_the_commit(decisions):
    root = decisions.root
    learning_commit_store.record_disposition(
        root,
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        disposition=NO_NEW_LESSON,
        reason="mechanical_change_only",
    )

    receipt = server.aiworkhub_agent_accept_review(
        request_id=REQUEST_ID, task_id=TASK_ID, lesson_candidate=LESSON
    )

    assert receipt["ok"] is True, receipt
    assert receipt["learning_commit_owed"]["owed"] is True
    commit = receipt["learning_commit"]
    assert commit["ok"] is True, commit
    assert commit["failures"] == {}
    assert commit["disposition"]["state"] == "failed"
    assert commit["disposition"]["error"].startswith(
        "learning_disposition_conflict:already_recorded_as_no_new_lesson"
    )
    assert len(_rows(root, "learning_commits")) == 1
    stored = _stored(root)
    assert (stored["disposition"], stored["reason"]) == (
        "no_new_lesson",
        "mechanical_change_only",
    )
    assert len(_rows(root, "learning_dispositions")) == 1


def test_a_prior_lesson_blocks_a_no_new_lesson_without_undoing_the_decision(decisions):
    root = decisions.root
    first = server.aiworkhub_agent_accept_review(
        request_id=REQUEST_ID, task_id=TASK_ID, lesson_candidate=LESSON
    )
    assert first["learning_commit"]["ok"] is True, first["learning_commit"]

    second = server.aiworkhub_agent_accept_review(
        request_id=REQUEST_ID,
        task_id=TASK_ID,
        no_new_lesson_reason="mechanical_change_only",
    )

    assert second["ok"] is True, second
    assert second["learning_commit_owed"]["owed"] is True
    refused = second["learning_disposition"]
    assert refused["ok"] is False
    assert refused["error"].startswith(
        "learning_disposition_conflict:already_recorded_as_lesson_committed"
    )
    stored = _stored(root)
    assert stored["disposition"] == "lesson_committed"
    assert stored["commit_id"] == first["learning_commit"]["commit_id"]
    assert len(_rows(root, "learning_dispositions")) == 1


def _close_the_write_gate(monkeypatch) -> str:
    monkeypatch.setattr(core, "writes_allowed", lambda: False)
    return "write_gate_closed"


def _unverify_the_manager(monkeypatch) -> str:
    monkeypatch.setattr(core, "manager_bootstrap", lambda: {"ok": False, "role": "worker"})
    return "verified_manager_identity_required"


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    "learning, key",
    [
        pytest.param({"lesson_candidate": LESSON}, "learning_commit", id="lesson"),
        pytest.param(
            {"no_new_lesson_reason": "mechanical_change_only"},
            "learning_disposition",
            id="reason",
        ),
    ],
)
@pytest.mark.parametrize("break_it", [_close_the_write_gate, _unverify_the_manager])
def test_a_closeout_that_cannot_run_is_reported_and_the_decision_stands(
    decisions, monkeypatch, tool, learning, key, break_it
):
    error = break_it(monkeypatch)

    reply = _decide(tool, **learning)

    assert reply["ok"] is True, reply
    assert reply[key]["ok"] is False
    assert reply[key]["error"] == error
    assert reply["learning_commit_owed"]["owed"] is True
    other = "learning_disposition" if key == "learning_commit" else "learning_commit"
    assert other not in reply
    _assert_nothing_recorded(decisions.root)


@pytest.mark.parametrize("tool", TOOLS)
def test_a_raising_learning_commit_is_reported_and_the_decision_stands(
    decisions, monkeypatch, tool
):
    def boom(**_kwargs):
        raise RuntimeError("commit exploded")

    monkeypatch.setattr(server, "aiworkhub_manager_learning_commit", boom)

    reply = _decide(tool, lesson_candidate=LESSON)

    assert reply["ok"] is True, reply
    assert reply["learning_commit"] == {
        "ok": False,
        "error": "learning_closeout_failed:RuntimeError",
    }
    assert reply["learning_commit_owed"]["owed"] is True


@pytest.mark.parametrize("tool", TOOLS)
def test_a_raising_disposition_write_is_reported_and_the_decision_stands(
    decisions, monkeypatch, tool
):
    def boom(_call, **_kwargs):
        raise RuntimeError("disposition exploded")

    monkeypatch.setattr(manager_ai_tools, "_write_invoke", boom)

    reply = _decide(tool, no_new_lesson_reason="mechanical_change_only")

    assert reply["ok"] is True, reply
    assert reply["learning_disposition"] == {
        "ok": False,
        "error": "learning_closeout_failed:RuntimeError",
    }
    assert reply["learning_commit_owed"]["owed"] is True


@pytest.mark.parametrize(
    "learning, key",
    [
        pytest.param({"lesson_candidate": LESSON}, "learning_commit", id="lesson"),
        pytest.param(
            {"no_new_lesson_reason": "mechanical_change_only"},
            "learning_disposition",
            id="reason",
        ),
    ],
)
def test_a_reject_with_no_resolvable_request_identity_reports_it(
    decisions, learning, key
):
    decisions.owed = False
    decisions.identified = False

    reply = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, **learning
    )

    assert reply["ok"] is True, reply
    assert reply[key] == {
        "ok": False,
        "error": "learning_closeout_request_id_unresolved",
    }
    _assert_nothing_recorded(decisions.root)


def test_a_refused_accept_attempts_no_closeout(decisions):
    decisions.accept_ok = False

    reply = server.aiworkhub_agent_accept_review(
        request_id=REQUEST_ID, task_id=TASK_ID, lesson_candidate=LESSON
    )

    assert reply == {"ok": False, "error": "accept_refused"}
    _assert_nothing_recorded(decisions.root)


@pytest.mark.parametrize(
    "learning",
    [
        pytest.param({"lesson_candidate": LESSON}, id="lesson"),
        pytest.param({"no_new_lesson_reason": "mechanical_change_only"}, id="reason"),
    ],
)
def test_a_refused_reject_attempts_no_closeout(decisions, learning):
    decisions.reject_ok = False

    reply = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, **learning
    )

    assert reply["ok"] is False
    assert reply["stderr"] == "reject_refused"
    assert "command" not in reply
    assert "learning_commit" not in reply and "learning_disposition" not in reply
    assert decisions.cancellations == []
    _assert_nothing_recorded(decisions.root)


# --------------------------------------------------------------------------- #
# Replays, call shape, and the standalone commit path.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tool", TOOLS)
def test_the_commit_is_asked_for_the_decided_request_with_the_managers_inputs(
    decisions, monkeypatch, tool
):
    seen: list[dict[str, Any]] = []

    def spy(**kwargs):
        seen.append(kwargs)
        return {"ok": True, "spied": True}

    monkeypatch.setattr(server, "aiworkhub_manager_learning_commit", spy)

    reply = _decide(
        tool,
        lesson_candidate=LESSON,
        promote_ai_memory=True,
        extra_evidence_ids=[EXTRA_EVIDENCE],
    )

    assert seen == [
        {
            "task_id": TASK_ID,
            "request_id": REQUEST_ID,
            "lesson_candidate": LESSON,
            "promote_ai_memory": True,
            "extra_evidence_ids": [EXTRA_EVIDENCE],
        }
    ]
    assert reply["learning_commit"] == {"ok": True, "spied": True}
    assert reply["learning_commit_owed"]["owed"] is True


def test_replaying_a_lesson_closeout_is_idempotent(decisions):
    root = decisions.root

    first = server.aiworkhub_agent_accept_review(
        request_id=REQUEST_ID, task_id=TASK_ID, lesson_candidate=LESSON
    )
    second = server.aiworkhub_agent_accept_review(
        request_id=REQUEST_ID, task_id=TASK_ID, lesson_candidate=LESSON
    )

    assert first["learning_commit"]["disposition"]["state"] == "applied"
    assert second["learning_commit"]["idempotent"] is True
    assert second["learning_commit"]["disposition"] == {
        "state": "idempotent",
        "disposition_id": first["learning_commit"]["disposition"]["disposition_id"],
    }
    assert len(_rows(root, "learning_commits")) == 1
    assert len(_rows(root, "learning_dispositions")) == 1


def test_replaying_a_no_new_lesson_closeout_is_idempotent(decisions):
    root = decisions.root

    first = _decide("accept", no_new_lesson_reason="mechanical_change_only")
    second = _decide("accept", no_new_lesson_reason="mechanical_change_only")

    assert first["learning_disposition"]["idempotent"] is False
    assert second["learning_disposition"]["idempotent"] is True
    assert (
        second["learning_disposition"]["disposition_id"]
        == first["learning_disposition"]["disposition_id"]
    )
    assert len(_rows(root, "learning_dispositions")) == 1


def test_a_standalone_short_form_commit_closes_the_decision_out_and_is_counted(
    decisions,
):
    root = decisions.root
    _seed(root, _accepted_card())
    assert learning_commit_store.coverage(root)["cards_with_disposition"] == 0

    commit = server.aiworkhub_manager_learning_commit(
        task_id=TASK_ID, request_id=REQUEST_ID, lesson_candidate=LESSON
    )

    assert commit["ok"] is True, commit
    assert [
        (row["task_id"], row["request_id"], row["disposition"], row["commit_id"])
        for row in _rows(root, "learning_dispositions")
    ] == [(TASK_ID, REQUEST_ID, "lesson_committed", commit["commit_id"])]
    measured = learning_commit_store.coverage(root)
    assert measured["cards_with_disposition"] == 1
    assert measured["cards_without_disposition"] == 0


def test_a_standalone_commit_after_a_no_new_lesson_still_succeeds(decisions):
    root = decisions.root
    _seed(root, _accepted_card())
    learning_commit_store.record_disposition(
        root,
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        disposition=NO_NEW_LESSON,
        reason="no_generalizable_cause",
    )

    commit = server.aiworkhub_manager_learning_commit(
        task_id=TASK_ID, request_id=REQUEST_ID, lesson_candidate=LESSON
    )

    assert commit["ok"] is True, commit
    assert commit["disposition"]["state"] == "failed"
    assert "learning_disposition_conflict" in commit["disposition"]["error"]
    assert _stored(root)["reason"] == "no_generalizable_cause"
    assert len(_rows(root, "learning_commits")) == 1
