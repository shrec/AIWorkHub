"""NF-2026-01925: a ready-to-run decision section, and batch packet reads.

The review packet already folds every fact a manager decides on. These tests
pin the two additions: a ``decision`` section whose prefilled argument dicts
are exactly callable against the real decision tools (and never choose a
disposition or a verdict), and ``request_ids`` batching in which every member
is the single-call packet built with the split byte budget.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import completion_inbox, core, server  # noqa: E402


def _card(task_id: str = "T-1", request_id: str = "R-1", *, tier: str = "high") -> dict:
    return {
        "task_id": task_id,
        "runner": "claude_coding",
        "topic": "coding",
        "status": "review",
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {"request_id": request_id, "task_id": task_id},
                "changed_paths": [f"src/aiworkhub/mod_{i}.py" for i in range(40)],
                "changed_path_hashes": {"src/aiworkhub/mod_0.py": "a" * 64},
                "validation": [
                    {"declared_command": "pytest tests/test_mod.py",
                     "returncode": 0, "stdout_tail": "x" * 1900},
                ],
                "quality_gate": {
                    "passed": True,
                    "blocking_checks": [],
                    "review_risk_profile": {
                        "effective_tier": tier,
                        "required_reviewer_lenses": ["correctness"],
                    },
                    "checks": [
                        {"check_id": f"c{i}", "kind": "lint", "status": "passed",
                         "command": "ruff", "summary": "ok"}
                        for i in range(20)
                    ],
                    "quality_verdict": {},
                },
            },
        },
    }


def _reports() -> list[dict]:
    return [
        {
            "lens": "correctness",
            "reviewer_request_id": "RV-1",
            "findings": [
                {"id": "F-1", "severity": "high", "disposition": "defect",
                 "category": "general", "summary": "off by one",
                 "evidence": "src/aiworkhub/mod_0.py:42 loop bound"},
                {"id": "F-3", "severity": "low", "disposition": "defect",
                 "category": "general", "summary": "unchecked none",
                 "evidence": "src/aiworkhub/mod_1.py:7"},
                {"id": "O-1", "severity": "low", "disposition": "observation",
                 "category": "excess_scope", "summary": "extra helper",
                 "evidence": "src/aiworkhub/mod_2.py:80"},
            ],
        }
    ]


def _preview(*, dispositions: list[dict] | None = None) -> dict:
    preview = {
        "evaluated": True,
        "blocked": True,
        "blockers": [
            {"kind": "refinement_required",
             "error": "refinement_required:reviewer:correctness:F-1"},
            {"kind": "explicit_human_approval_missing",
             "error": "explicit_human_approval_missing"},
            {"kind": "required_reviewer_missing",
             "error": "required_reviewer_missing:security"},
        ],
        "reviewer_request_ids": ["RV-1"],
        "reviewer_request_id_source": "bound_children",
        "per_lens": [{"lens": "correctness", "task_id": "TR-1", "request_id": "RV-1",
                      "state": "review_ready", "usable": True, "required": True,
                      "selected": True}],
    }
    if dispositions is not None:
        preview["finding_dispositions"] = dispositions
    return preview


def _packet(**kwargs) -> dict:
    tier = kwargs.pop("tier", "high")
    preview = kwargs.pop("preview", None) or _preview()
    return completion_inbox.review_packet(
        "R-1", card=_card(tier=tier), task_id="T-1",
        accept_preview=preview, reviewer_reports=_reports(), **kwargs,
    )


def _assert_callable(tool, arguments: dict) -> None:
    params = inspect.signature(tool).parameters
    assert set(arguments) <= set(params), set(arguments) - set(params)
    required = {
        name for name, p in params.items() if p.default is inspect.Parameter.empty
    }
    assert required <= set(arguments), required - set(arguments)


def test_decision_argument_keys_match_the_real_tool_signatures():
    decision = _packet()["decision"]
    accept = decision["accept_call"]
    assert accept["tool"] == "aiworkhub_agent_accept_review"
    _assert_callable(server.aiworkhub_agent_accept_review, accept["arguments"])
    assert accept["arguments"]["request_id"] == "R-1"
    assert accept["arguments"]["task_id"] == "T-1"
    assert accept["arguments"]["no_new_lesson_reason"] == ""
    assert decision["dispose_calls"]
    for call in decision["dispose_calls"]:
        assert call["tool"] == "aiworkhub_manager_review_finding_dispose"
        _assert_callable(server.aiworkhub_manager_review_finding_dispose, call["arguments"])
    reject = decision["reject_call"]
    assert reject["tool"] == "aiworkhub_task_reject_review"
    _assert_callable(server.aiworkhub_task_reject_review, reject["arguments"])
    assert reject["arguments"] == {
        "task_id": "T-1", "predecessor_request_id": "R-1", "reason": "",
    }


def test_one_dispose_call_per_undisposed_finding_with_full_ids():
    calls = _packet()["decision"]["dispose_calls"]
    assert [c["arguments"]["finding_id"] for c in calls] == [
        "reviewer:correctness:F-1", "reviewer:correctness:F-3",
    ]
    for call in calls:
        assert call["arguments"]["task_id"] == "T-1"
        assert call["arguments"]["request_id"] == "R-1"
        assert call["arguments"]["reviewer_request_id"] == "RV-1"
        assert call["arguments"]["disposition"] == ""
    assert [c["severity"] for c in calls] == ["high", "low"]


def _preview_without_approval_blocker() -> dict:
    preview = _preview()
    preview["blockers"] = [
        row for row in preview["blockers"]
        if row["kind"] != "explicit_human_approval_missing"
    ]
    return preview


def test_confirm_high_risk_only_for_the_high_tier():
    assert _packet(tier="high")["decision"]["accept_call"]["arguments"][
        "confirm_high_risk"
    ] is True
    for tier in ("medium", "low", "critical", ""):
        decision = _packet(
            tier=tier, preview=_preview_without_approval_blocker()
        )["decision"]
        assert "confirm_high_risk" not in decision["accept_call"]["arguments"], tier
        assert decision["high_risk_approval_satisfied_by"] == "", tier


def test_live_approval_blocker_forces_confirm_high_risk_whatever_the_tier():
    # The preview owes explicit approval even when the recorded tier is
    # medium, empty, critical or stale: the accept call must carry it.
    for tier in ("medium", "", "critical"):
        decision = _packet(tier=tier)["decision"]
        args = decision["accept_call"]["arguments"]
        assert args["confirm_high_risk"] is True, tier
        assert args["no_new_lesson_reason"] == "", tier
        assert decision["high_risk_approval_satisfied_by"] == (
            "accept_call.arguments.confirm_high_risk"
        ), tier
        _assert_callable(server.aiworkhub_agent_accept_review, args)


def test_a_disposed_finding_produces_no_dispose_call():
    preview = _preview(dispositions=[{
        "finding_id": "reviewer:correctness:F-1", "reviewer_request_id": "RV-1",
        "disposition": "confirmed", "receipt_sha256": "b" * 64,
    }])
    decision = _packet(preview=preview)["decision"]
    assert [c["arguments"]["finding_id"] for c in decision["dispose_calls"]] == [
        "reviewer:correctness:F-3",
    ]
    assert decision["already_disposed_finding_count"] == 1
    # Already disposed, still blocking: no further disposition answers it.
    assert {
        "kind": "refinement_required",
        "error": "refinement_required:reviewer:correctness:F-1",
    } in decision["blockers_after_dispositions"]


def test_blockers_after_dispositions_keep_the_approval_blocker_separate():
    decision = _packet()["decision"]
    assert decision["blockers_after_dispositions"] == [
        {"kind": "required_reviewer_missing",
         "error": "required_reviewer_missing:security"},
    ]
    assert decision["blockers_after_dispositions_evaluated"] is True
    assert [r["kind"] for r in decision["finding_blockers_pending_disposition"]] == [
        "refinement_required",
    ]
    assert decision["high_risk_approval_blockers"] == [
        {"kind": "explicit_human_approval_missing",
         "error": "explicit_human_approval_missing"},
    ]
    assert decision["high_risk_approval_satisfied_by"] == (
        "accept_call.arguments.confirm_high_risk"
    )


def test_the_packet_never_chooses_a_disposition_or_a_verdict():
    packet = _packet()
    decision = packet["decision"]
    assert decision["disposition_chosen"] is False
    assert decision["verdict_chosen"] is False
    assert all(c["arguments"]["disposition"] == "" for c in decision["dispose_calls"])
    assert decision["reject_call"]["arguments"]["reason"] == ""
    assert packet["readonly"] is True
    assert set(packet["mutation"].values()) == {False}


def test_an_unevaluated_preview_is_named_not_read_as_clear():
    packet = completion_inbox.review_packet(
        "R-1", card=_card(), task_id="T-1", reviewer_reports=_reports(),
    )
    decision = packet["decision"]
    assert decision["blockers_after_dispositions"] == []
    assert decision["blockers_after_dispositions_evaluated"] is False


def test_decision_survives_a_tight_bound_and_evidence_is_dropped_first():
    full = _packet()
    assert full["truncation"]["dropped_sections"] == []
    tight = _packet(max_bytes=2_000)
    dropped = tight["truncation"]["dropped_sections"]
    assert dropped, tight["truncation"]
    assert "decision" not in completion_inbox._PACKET_DROP_ORDER
    assert dropped == [s for s in completion_inbox._PACKET_DROP_ORDER if s in dropped]
    assert dropped[0] == "validation_output_tails"
    assert "reviewer_findings" in dropped
    # The evidence went; the decision built from it did not.
    assert tight["review"]["findings"] == []
    assert tight["decision"] == full["decision"]


# ---------------------------------------------------------------------------
# request_ids batching through the MCP tool
# ---------------------------------------------------------------------------

_TASK_FOR = {"R-A": "T-A", "R-B": "T-B"}


class _Manager:
    def status(self, request_id):
        task_id = _TASK_FOR.get(request_id)
        if task_id is None:
            return {"ok": False, "error": "unknown_request"}
        return {"ok": True, "task_id": task_id}

    def accept_preview(self, request_id, task_id):
        if request_id == "R-BOOM":
            raise RuntimeError("boom")
        if not task_id:
            return {"evaluated": False, "error": "task_id_unresolved"}
        return {"evaluated": True, "blocked": False, "blockers": []}


def _fake_show(task_id: str):
    request_id = {v: k for k, v in _TASK_FOR.items()}.get(task_id, "R-?")
    return core.TaskCtlResult(
        command=["show", task_id],
        returncode=0,
        stdout=json.dumps(_card(task_id=task_id, request_id=request_id)),
        stderr="",
    )


def _patch(monkeypatch) -> None:
    monkeypatch.setattr(server.process_launcher, "default_manager", lambda: _Manager())
    monkeypatch.setattr(server.core, "show_task", _fake_show)
    # base_ci is a live `gh` call: two packets built seconds apart can differ
    # (one ok, one gh_timeout), so the batch-vs-single equality must not see it.
    monkeypatch.setattr(
        completion_inbox.ci_status,
        "base_ci_section",
        lambda *_a, **_k: {"state": "unavailable", "reason": "stubbed", "warnings": []},
    )


def test_two_request_ids_return_two_single_call_packets_with_split_budget(monkeypatch):
    _patch(monkeypatch)
    batch = server.aiworkhub_review_packet(request_ids=["R-A", "R-B"], max_bytes=20_000)
    assert batch["ok"] is True
    assert batch["per_packet_max_bytes"] == 10_000
    assert batch["member_error_count"] == 0
    assert len(batch["packets"]) == 2
    for rid, packet in zip(["R-A", "R-B"], batch["packets"]):
        single = server.aiworkhub_review_packet(rid, max_bytes=10_000)
        assert packet == single
        assert packet["decision"]["accept_call"]["arguments"]["request_id"] == rid
        assert packet["truncation"]["max_bytes"] == 10_000


def test_an_unknown_id_is_a_member_error_and_the_other_packet_is_returned(monkeypatch):
    _patch(monkeypatch)
    batch = server.aiworkhub_review_packet(request_ids=["R-A", "R-NOPE", "R-BOOM"])
    assert batch["ok"] is True
    good, unknown, boom = batch["packets"]
    assert good["ok"] is True and good["task_id"] == "T-A"
    assert unknown["ok"] is False and unknown["request_id"] == "R-NOPE"
    assert unknown["error"]
    assert boom == {
        "schema_id": completion_inbox.REVIEW_PACKET_SCHEMA_ID,
        "request_id": "R-BOOM",
        "ok": False,
        "readonly": True,
        "error": "review_packet_member_failed",
        "exception_type": "RuntimeError",
    }
    assert batch["member_error_count"] == 2


def test_request_ids_bounds_are_refused_by_name(monkeypatch):
    _patch(monkeypatch)
    too_many = server.aiworkhub_review_packet(request_ids=[f"R-{i}" for i in range(9)])
    assert too_many["ok"] is False
    assert too_many["error"] == "request_ids_limit_exceeded"
    assert too_many["request_ids_limit"] == 8
    assert server.aiworkhub_review_packet(request_ids=[])["error"] == "request_ids_empty"
    both = server.aiworkhub_review_packet("R-A", request_ids=["R-B"])
    assert both["error"] == "request_ids_exclusive_with_request_id_and_task_id"
    assert server.aiworkhub_review_packet()["error"] == (
        "request_id_or_request_ids_required"
    )


def test_request_ids_is_documented_in_the_tool_args_block():
    doc = inspect.getdoc(server.aiworkhub_review_packet) or ""
    args_block = doc.split("Args:", 1)[1]
    assert "request_ids:" in args_block
    assert "request_ids" in inspect.signature(server.aiworkhub_review_packet).parameters


def test_merged_multi_lens_finding_gets_one_dispose_call_per_reviewer():
    # The dedupe key is (path, line, check id), so the same check reported by
    # two lenses at one location merges into one kept finding; each lens's
    # reviewer still owns its own reviewer:<lens>:<id> finding to dispose.
    shared = {"id": "F-1", "severity": "high", "disposition": "defect",
              "category": "general", "summary": "off by one",
              "evidence": "src/aiworkhub/mod_0.py:42 loop bound"}
    reports = [
        {"lens": "correctness", "reviewer_request_id": "RV-1", "findings": [dict(shared)]},
        {"lens": "security", "reviewer_request_id": "RV-2", "findings": [dict(shared)]},
    ]
    preview = {
        "evaluated": True,
        "blocked": True,
        "blockers": [
            {"kind": "refinement_required",
             "error": "refinement_required:reviewer:correctness:F-1"},
            {"kind": "refinement_required",
             "error": "refinement_required:reviewer:security:F-1"},
        ],
    }
    packet = completion_inbox.review_packet(
        "R-1", card=_card(tier="medium"), task_id="T-1",
        accept_preview=preview, reviewer_reports=reports,
    )
    [kept] = packet["review"]["findings"]
    assert kept["lenses"] == ["correctness", "security"]
    assert kept["merged_dispose_targets"] == [
        {"reviewer_request_id": "RV-2", "dispose_finding_id": "reviewer:security:F-1"},
    ]
    decision = packet["decision"]
    calls = [c["arguments"] for c in decision["dispose_calls"]]
    assert [(a["finding_id"], a["reviewer_request_id"]) for a in calls] == [
        ("reviewer:correctness:F-1", "RV-1"),
        ("reviewer:security:F-1", "RV-2"),
    ]
    for args in calls:
        assert args["disposition"] == ""
        _assert_callable(server.aiworkhub_manager_review_finding_dispose, args)
    assert [r["error"] for r in decision["finding_blockers_pending_disposition"]] == [
        "refinement_required:reviewer:correctness:F-1",
        "refinement_required:reviewer:security:F-1",
    ]
    assert decision["blockers_after_dispositions"] == []
    assert "confirm_high_risk" not in decision["accept_call"]["arguments"]
    assert decision["high_risk_approval_satisfied_by"] == ""

    # A merged target already disposed under its own reviewer gets no call.
    preview["finding_dispositions"] = [{
        "finding_id": "reviewer:security:F-1", "reviewer_request_id": "RV-2",
        "disposition": "confirmed", "receipt_sha256": "c" * 64,
    }]
    decision = completion_inbox.review_packet(
        "R-1", card=_card(tier="medium"), task_id="T-1",
        accept_preview=preview, reviewer_reports=reports,
    )["decision"]
    assert [c["arguments"]["finding_id"] for c in decision["dispose_calls"]] == [
        "reviewer:correctness:F-1",
    ]
    assert decision["already_disposed_finding_count"] == 1
