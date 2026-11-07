"""NF-2026-02151: review_ready callback members carry a compact review digest.

The digest is built by the review packet's own section and decision code
(``completion_inbox._review_packet_sections`` + ``_packet_decision``), so its
verdict, blocker codes and validation counts are the packet's for the same
request. It is bounded to 700 bytes, read-only, and a builder failure is the
member's ``review_digest_error`` only -- the batch is still returned and acked.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import (  # noqa: E402
    callback_store,
    completion_inbox,
    core,
    manager_callback_wait,
    task_store,
)

REQUEST_ID = "R-DIGEST-1"
TASK_ID = "T-DIGEST-1"


def _card(*, task_id: str = TASK_ID, request_id: str = REQUEST_ID, tier: str = "high") -> dict:
    return {
        "task_id": task_id,
        "runner": "claude_coding",
        "topic": "coding",
        "status": "review",
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {"request_id": request_id, "task_id": task_id},
                "changed_paths": [f"src/aiworkhub/mod_{i}.py" for i in range(12)],
                "validation": [
                    {"declared_command": "pytest a", "returncode": 0, "stdout_tail": "ok"},
                    {"declared_command": "pytest b", "returncode": 1, "stdout_tail": "F"},
                    {"declared_command": "pytest c", "stdout_tail": "?"},
                ],
                "quality_gate": {
                    "passed": True,
                    "blocking_checks": [],
                    "review_risk_profile": {
                        "effective_tier": tier,
                        "required_reviewer_lenses": ["correctness"],
                    },
                    "quality_verdict": {},
                },
            },
        },
    }


def _reports(count: int = 2) -> list[dict]:
    return [
        {
            "lens": "correctness",
            "reviewer_request_id": "RV-1",
            "findings": [
                {"id": f"F-{i}", "severity": "high", "disposition": "defect",
                 "category": "general", "summary": f"defect {i}",
                 "evidence": f"src/aiworkhub/mod_{i}.py:{i + 1}"}
                for i in range(count)
            ],
        }
    ]


def _preview(*, blockers: list[dict] | None = None, blocked: bool = True) -> dict:
    return {
        "evaluated": True,
        "blocked": blocked,
        "blockers": blockers
        if blockers is not None
        else [
            {"kind": "refinement_required",
             "error": "refinement_required:reviewer:correctness:F-0"},
            {"kind": "refinement_required",
             "error": "refinement_required:reviewer:correctness:F-1"},
            {"kind": "explicit_human_approval_missing",
             "error": "explicit_human_approval_missing"},
        ],
        "reviewer_request_ids": ["RV-1"],
        "per_lens": [{"lens": "correctness", "request_id": "RV-1", "state": "review_ready",
                      "usable": True, "required": True, "selected": True}],
        # review_packet would otherwise ask ci_status; the digest never does.
        "base_ci": {"state": "unavailable", "reason": "test", "warnings": []},
    }


def _size(digest: dict) -> int:
    return len(json.dumps(digest).encode("utf-8"))


def test_digest_verdict_blockers_and_validation_equal_the_review_packet():
    kwargs = {"card": _card(), "task_id": TASK_ID, "accept_preview": _preview(),
              "reviewer_reports": _reports()}
    packet = completion_inbox.review_packet(REQUEST_ID, max_bytes=10_000_000, **kwargs)
    digest = completion_inbox.review_digest(REQUEST_ID, **kwargs)

    assert digest["ok"] is True
    assert digest["schema_id"] == completion_inbox.REVIEW_DIGEST_SCHEMA_ID
    assert packet["accept_preview"]["blocked"] is True
    assert digest["verdict"] == "blocked"
    kinds: list[str] = []
    for row in packet["accept_preview"]["blockers"]:
        if row["kind"] not in kinds:
            kinds.append(row["kind"])
    assert digest["blocker_codes"] == kinds
    assert digest["blocker_count"] == len(packet["accept_preview"]["blockers"])
    returncodes = [row["returncode"] for row in packet["gates"]["validation"]]
    assert digest["validation"] == {
        "total": len(returncodes),
        "passed": returncodes.count(0),
        "failed": sum(1 for rc in returncodes if isinstance(rc, int) and rc != 0),
        "unknown": returncodes.count(None),
    }
    assert digest["validation"] == {"total": 3, "passed": 1, "failed": 1, "unknown": 1}
    open_ids = [call["arguments"]["finding_id"] for call in packet["decision"]["dispose_calls"]]
    assert open_ids and digest["open_finding_ids"] == open_ids
    assert digest["open_finding_count"] == len(open_ids)
    assert digest["risk_tier"] == packet["decision"]["effective_tier"] == "high"
    assert digest["changed_file_count"] == packet["diff"]["changed_path_count"] == 12
    assert digest["quality_gate_passed"] is True
    assert digest["hint"] == "aiworkhub_review_packet for detail"
    assert "unevaluated_reason" not in digest
    assert _size(digest) <= 700


def test_clear_preview_is_clear_and_absent_preview_is_unevaluated_never_clear():
    clear = completion_inbox.review_digest(
        REQUEST_ID, card=_card(), task_id=TASK_ID,
        accept_preview=_preview(blockers=[], blocked=False), reviewer_reports=[],
    )
    assert clear["verdict"] == "clear"
    assert clear["blocker_codes"] == [] and clear["blocker_count"] == 0

    stored = completion_inbox.review_digest(
        REQUEST_ID, card=_card(), task_id=TASK_ID, unevaluated_reason="not_run_here",
    )
    packet = completion_inbox.review_packet(
        REQUEST_ID, card=_card(), task_id=TASK_ID,
        accept_preview={"base_ci": {"state": "unavailable", "warnings": []}},
    )
    assert packet["accept_preview"]["evaluated"] is False
    assert stored["verdict"] == "unevaluated"
    assert stored["unevaluated_reason"] == "not_run_here"
    assert stored["blocker_codes"] == []


def test_digest_is_capped_at_700_bytes_and_keeps_counts():
    blockers = [{"kind": f"blocker_kind_{i}_" + "k" * 70, "error": "e" * 400} for i in range(40)]
    digest = completion_inbox.review_digest(
        REQUEST_ID, card=_card(), task_id=TASK_ID,
        accept_preview=_preview(blockers=blockers), reviewer_reports=_reports(60),
    )
    assert digest["ok"] is True
    assert _size(digest) <= completion_inbox.REVIEW_DIGEST_MAX_BYTES == 700
    assert digest["lists_truncated"] is True
    assert digest["blocker_count"] == 40
    assert digest["open_finding_count"] == 60
    assert len(digest["open_finding_ids"]) < 60
    assert len(digest["blocker_codes"]) < 40


def test_unreadable_card_says_so_instead_of_computing():
    digest = completion_inbox.review_digest(
        REQUEST_ID, task_id=TASK_ID,
        show_fn=lambda _task_id: {"returncode": 0, "stdout": f"Task not found: {TASK_ID}"},
    )
    assert digest["ok"] is False
    assert digest["error"] == "card_unreadable"
    assert digest["read_error_kind"] == "not_found"
    assert _size(digest) <= 700


def test_only_review_ready_members_change_and_a_raising_builder_is_member_local(monkeypatch):
    members = [
        {"task_id": "A", "state": "review_ready", "event_id": "e1", "request_id": "RA"},
        {"task_id": "B", "state": "worker_failed", "event_id": "e2", "request_id": "RB"},
        {"task_id": "C", "state": "review_ready", "event_id": "e3", "request_id": "RC"},
    ]
    other_before = json.dumps(members[1], sort_keys=True).encode()

    def builder(entry):
        if entry["task_id"] == "A":
            raise RuntimeError("boom")
        return {"schema_id": "x", "ok": True}

    monkeypatch.setattr(manager_callback_wait, "_member_review_digest", builder)
    manager_callback_wait._attach_review_digests(members)
    assert members[0]["review_digest_error"] == {"error_kind": "RuntimeError", "message": "boom"}
    assert "review_digest" not in members[0]
    assert json.dumps(members[1], sort_keys=True).encode() == other_before
    assert members[2]["review_digest"] == {"schema_id": "x", "ok": True}
    assert "review_digest_error" not in members[2]


def _seed_review_ready(tmp_path, monkeypatch, *, request_id: str) -> tuple[dict, dict]:
    root = tmp_path / "repo_digest"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    identity = {
        "provider": "claude",
        "session_id": "5be44029-03da-4683-aae3-c68ecb07b1a4",
        "window_id": "claude_vscode_1234",
    }
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: identity)
    card = {
        **_card(task_id="CLAUDE_DIGEST_CANARY", request_id=request_id),
        "runner": "claude_digest_canary",
        "worker_status": "review",
        "origin_thread_id": identity["session_id"],
        "claim_epoch": 0,
    }
    conn = callback_store.open_db(Path(task_store.storage_readiness(root).canonical_db))
    try:
        now = callback_store.utc_now()
        conn.execute(
            "INSERT INTO tasks(task_id,runner,status,worker_status,objective,card_json,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (card["task_id"], card["runner"], "review", "review", "canary",
             json.dumps(card), now, now),
        )
        conn.commit()
        callback_store.enqueue_callback(
            conn, card["task_id"], request_id, "review_ready", provider="claude"
        )
    finally:
        conn.close()
    assert core.dispatcher_ensure_started()["status"] == "manager_inbox"
    return identity, card


def test_callback_wait_review_ready_member_carries_the_stored_evidence_digest(tmp_path, monkeypatch):
    request_id = "22222222-2222-4222-8222-222222222222"
    _identity, card = _seed_review_ready(tmp_path, monkeypatch, request_id=request_id)
    received = core.claude_callback_wait(timeout_seconds=1)
    assert received["status"] == "callback_ready"
    member = received["members"][0]
    assert member["state"] == "review_ready"
    digest = member["review_digest"]
    expected = completion_inbox.review_digest(
        request_id, card=card, task_id=card["task_id"],
        unevaluated_reason="accept_preview_not_run_in_callback_wait",
    )
    assert digest == expected
    assert digest["verdict"] == "unevaluated"
    assert digest["validation"] == {"total": 3, "passed": 1, "failed": 1, "unknown": 1}
    assert _size(digest) <= 700
    ack = core.claude_callback_ack(received["batch_id"], received["lease_id"])
    assert ack["status"] == "delivered"


def test_raising_digest_builder_never_blocks_the_batch_or_its_ack(tmp_path, monkeypatch):
    request_id = "33333333-3333-4333-8333-333333333333"
    _seed_review_ready(tmp_path, monkeypatch, request_id=request_id)

    def boom(*_args, **_kwargs):
        raise ValueError("digest exploded")

    monkeypatch.setattr(completion_inbox, "review_digest", boom)
    received = core.claude_callback_wait(timeout_seconds=1)
    assert received["status"] == "callback_ready"
    member = received["members"][0]
    assert "review_digest" not in member
    assert member["review_digest_error"]["error_kind"] == "ValueError"
    ack = core.claude_callback_ack(received["batch_id"], received["lease_id"])
    assert ack["status"] == "delivered"


def test_review_packet_output_is_unchanged_by_the_shared_sections_split():
    kwargs = {"card": _card(), "task_id": TASK_ID, "accept_preview": _preview(),
              "reviewer_reports": _reports()}
    packet = completion_inbox.review_packet(REQUEST_ID, **copy.deepcopy(kwargs))
    sections, preview = completion_inbox._review_packet_sections(
        REQUEST_ID, **copy.deepcopy(kwargs)
    )
    assert packet["decision"] == completion_inbox._packet_decision(sections, preview)
    assert packet["base_ci"] == _preview()["base_ci"]
    assert packet["truncation"]["within_bound"] is True


def test_top_level_terminal_substatus_is_review_ready_and_a_missing_one_is_absent():
    kwargs = {"task_id": TASK_ID, "accept_preview": _preview(), "reviewer_reports": _reports()}
    card = _card()
    del card["terminal_review"]["substatus"]
    card["terminal_substatus"] = "review_ready"
    digest = completion_inbox.review_digest(REQUEST_ID, card=copy.deepcopy(card), **kwargs)
    assert digest["ok"] is True and "card_substatus" not in digest
    packet = completion_inbox.review_packet(
        REQUEST_ID, card=copy.deepcopy(card), max_bytes=10_000_000, **kwargs
    )
    assert packet["status"]["terminal_substatus"] == "review_ready"

    bare = _card()
    del bare["terminal_review"]["substatus"]
    absent = completion_inbox.review_digest(REQUEST_ID, card=bare, **kwargs)
    assert absent["card_substatus"] == "absent"
