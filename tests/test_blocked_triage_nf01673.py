from __future__ import annotations

import copy
import hashlib
import inspect
import json
import sqlite3

import pytest

from aiworkhub import (
    completion_inbox,
    core,
    launch_queue_persist,
    needfix_store,
    stale_recovery,
    task_fsm,
    task_store,
)

EARLY = "2026-01-01T00:00:00+00:00"
LATE = "2026-02-01T00:00:00+00:00"
LATER = "2026-03-01T00:00:00+00:00"
RANKED = ["archive", "supersede", "replay", "relaunch", "keep"]
AUTHORITY_FLAGS = {
    "runtime_authority": False,
    "support_authority": False,
    "queue_write": False,
    "audit_write": False,
    "process_launch": False,
    "agent_launch": False,
}
ROW_KEYS = {"task_id", "topic", "terminal_substatus", "failure_class", "recommendation", "reason"}
KEEP_SUBSTATUSES = sorted(
    task_fsm.KNOWN_TERMINAL_SUBSTATUSES - stale_recovery.RELAUNCH_SUBSTATUSES
) + ["never_heard_of_it"]


def _card(task_id, *, topic="alpha", status="blocked", created_at=EARLY, **extra):
    archived = status == "archived"
    state = "finished" if archived else status
    return {
        "task_id": task_id,
        "topic": topic,
        "status": state,
        "worker_status": state,
        "created_at": created_at,
        "archived_at": LATER if archived else "",
        **extra,
    }


def _triage(tmp_path, cards, needfix=None, **kwargs):
    return stale_recovery.build_blocked_triage(
        root=tmp_path,
        cards=cards,
        needfix_status=needfix if needfix is not None else (lambda needfix_id: None),
        **kwargs,
    )


def _rows(result):
    return [row for group in result["groups"].values() for row in group]


def _row(result, task_id):
    matches = [row for row in _rows(result) if row["task_id"] == task_id]
    assert len(matches) == 1, task_id
    return matches[0]


def _boom(*args, **kwargs):
    raise AssertionError("must not be called")


def _resolved(*needfix_ids):
    return lambda needfix_id: "resolved" if needfix_id in needfix_ids else None


def _delta(monkeypatch, verified):
    calls = []

    def has_verified_rework_delta(predecessor, *, authority_repo):
        calls.append((predecessor, authority_repo))
        return verified

    monkeypatch.setattr(
        "aiworkhub.worker_workspace.has_verified_rework_delta", has_verified_rework_delta
    )
    return calls


def _mixed_cards():
    return [
        _card("needfix-NF-2026-00010", topic="t1", terminal_substatus="worker_failed"),
        _card("D-old", topic="t2", terminal_substatus="timed_out"),
        _card("D-new", topic="t2", status="finished", created_at=LATE),
        _card("S-old", topic="t3", terminal_substatus="cancelled"),
        _card("S-new", topic="t3", status="pending", created_at=LATE),
        _card("R-1", topic="t4", terminal_substatus="worker_failed",
              rework_predecessor={"task_id": "P-1"}),
        _card("L-2", topic="t5", terminal_substatus="launch_failed"),
        _card("L-1", topic="t5", terminal_substatus="liveness_lost"),
        _card("K-1", topic="t6", terminal_substatus="exited"),
        _card("K-0", topic="t6"),
    ]


def _mixed_result(tmp_path, monkeypatch, cards=None, **kwargs):
    _delta(monkeypatch, True)
    return _triage(
        tmp_path,
        _mixed_cards() if cards is None else cards,
        needfix=_resolved("NF-2026-00010"),
        **kwargs,
    )


@pytest.fixture(scope="module")
def tool():
    from aiworkhub import server

    return server.aiworkhub_task_stale_recovery_recommend


@pytest.mark.parametrize("needfix_state", ["resolved", "archived"])
@pytest.mark.parametrize(
    ("task_id", "needfix_id"),
    [
        ("needfix-NF-2026-01673", "NF-2026-01673"),
        ("needfix-NF-2026-01673-r2", "NF-2026-01673"),
    ],
)
def test_archive_when_the_needfix_is_closed(tmp_path, task_id, needfix_id, needfix_state):
    asked = []

    def needfix(identifier):
        asked.append(identifier)
        return needfix_state

    cards = [_card(task_id, terminal_substatus="worker_failed")]
    row = _row(_triage(tmp_path, cards, needfix=needfix), task_id)

    assert row["recommendation"] == "archive"
    assert needfix_id in row["reason"] and needfix_state in row["reason"]
    assert "sibling_task_id" not in row
    assert asked == [needfix_id]


@pytest.mark.parametrize(
    "task_id",
    [
        "T-plain",
        "needfix-bogus",
        "needfix-NF-2026-01673-extra",
        "xneedfix-NF-2026-01673",
        "needfix-NF-26-1",
    ],
)
def test_other_card_ids_never_consult_the_needfix_store(tmp_path, task_id):
    cards = [_card(task_id, terminal_substatus="worker_failed")]
    result = _triage(tmp_path, cards, needfix=_boom)
    assert _row(result, task_id)["recommendation"] == "relaunch"


@pytest.mark.parametrize("state", ["captured", "task_created", None])
def test_open_or_missing_needfix_does_not_archive(tmp_path, state):
    cards = [_card("needfix-NF-2026-00005", terminal_substatus="timed_out")]
    result = _triage(tmp_path, cards, needfix=lambda needfix_id: state)
    assert _row(result, "needfix-NF-2026-00005")["recommendation"] == "relaunch"


@pytest.mark.parametrize("sibling_status", ["finished", "archived"])
def test_archive_when_a_later_same_topic_card_is_done(tmp_path, sibling_status):
    cards = [
        _card("T-old", terminal_substatus="worker_failed"),
        _card("T-new", status=sibling_status, created_at=LATE),
    ]
    result = _triage(tmp_path, cards)
    row = _row(result, "T-old")

    assert row["recommendation"] == "archive"
    assert row["sibling_task_id"] == "T-new"
    assert "T-new" in row["reason"] and sibling_status in row["reason"]
    assert result["counts"]["blocked"] == 1


@pytest.mark.parametrize(
    "sibling",
    [
        {"created_at": EARLY},
        {"created_at": "2025-12-01T00:00:00+00:00"},
        {"topic": "beta"},
        {"topic": ""},
        {"status": "superseded"},
        {"status": "blocked"},
        {"created_at": "not-a-timestamp"},
        {"created_at": "2026-02-01T00:00:00"},
    ],
    ids=[
        "same-instant", "older", "other-topic", "no-topic",
        "superseded", "also-blocked", "unparseable-time", "naive-vs-aware-time",
    ],
)
def test_siblings_that_do_not_prove_a_later_outcome_are_ignored(tmp_path, sibling):
    cards = [
        _card("T-old", terminal_substatus="worker_failed"),
        _card("T-other", **{"status": "finished", "created_at": LATE, **sibling}),
    ]
    assert _row(_triage(tmp_path, cards), "T-old")["recommendation"] == "relaunch"


@pytest.mark.parametrize("sibling_status", ["pending", "processing", "review"])
def test_supersede_when_a_later_same_topic_card_is_live(tmp_path, sibling_status):
    cards = [
        _card("T-old", terminal_substatus="worker_failed"),
        _card("T-new", status=sibling_status, created_at=LATE),
    ]
    row = _row(_triage(tmp_path, cards), "T-old")

    assert row["recommendation"] == "supersede"
    assert row["sibling_task_id"] == "T-new"
    assert "T-new" in row["reason"] and sibling_status in row["reason"]


def test_the_earliest_later_sibling_is_named_and_ties_break_on_task_id(tmp_path):
    cards = [
        _card("T-old", terminal_substatus="worker_failed"),
        _card("T-z", status="pending", created_at=LATE),
        _card("T-a", status="pending", created_at=LATER),
        _card("T-n", status="review", created_at=LATE),
    ]
    row = _row(_triage(tmp_path, cards), "T-old")
    assert (row["recommendation"], row["sibling_task_id"]) == ("supersede", "T-n")


def test_replay_when_the_rework_predecessor_has_a_verified_delta(tmp_path, monkeypatch):
    calls = _delta(monkeypatch, True)
    predecessor = {"task_id": "T-pred", "request_id": "req-1"}
    cards = [_card("T-rework", terminal_substatus="worker_failed", rework_predecessor=predecessor)]
    row = _row(_triage(tmp_path, cards), "T-rework")

    assert row["recommendation"] == "replay"
    assert "verified" in row["reason"]
    assert calls == [(predecessor, tmp_path)]


@pytest.mark.parametrize(
    ("substatus", "expected"),
    [("worker_failed", "relaunch"), ("exited", "keep"), (None, "keep")],
)
def test_unverified_rework_delta_falls_through(tmp_path, monkeypatch, substatus, expected):
    calls = _delta(monkeypatch, False)
    extra = {"terminal_substatus": substatus} if substatus else {}
    cards = [_card("T-rework", rework_predecessor={"task_id": "T-pred"}, **extra)]
    assert _row(_triage(tmp_path, cards), "T-rework")["recommendation"] == expected
    assert len(calls) == 1


def test_cards_without_a_rework_predecessor_never_ask_for_a_delta(tmp_path, monkeypatch):
    monkeypatch.setattr("aiworkhub.worker_workspace.has_verified_rework_delta", _boom)
    cards = [
        _card("T-1", terminal_substatus="worker_failed"),
        _card("T-2", terminal_substatus="worker_failed", rework_predecessor={}),
        _card("T-3", terminal_substatus="worker_failed", rework_predecessor=None),
    ]
    result = _triage(tmp_path, cards)
    assert result["counts"]["by_recommendation"]["relaunch"] == 3


@pytest.mark.parametrize("substatus", sorted(stale_recovery.RELAUNCH_SUBSTATUSES))
def test_relaunch_for_each_operational_substatus(tmp_path, substatus):
    row = _row(_triage(tmp_path, [_card("T1", terminal_substatus=substatus)]), "T1")

    assert row["recommendation"] == "relaunch"
    assert row["terminal_substatus"] == substatus
    assert substatus in row["reason"]


def test_failure_class_is_reported_but_never_decides(tmp_path):
    recorded = {"terminal_failure": {"evidence": {"failure_class": "provider_unavailable"}}}
    cards = [
        _card("T-relaunch", terminal_substatus="worker_failed", **recorded),
        _card("T-keep", terminal_substatus="exited", **recorded),
        _card("T-plain", terminal_substatus="worker_failed"),
        _card("T-odd", terminal_substatus="worker_failed", terminal_failure="boom"),
    ]
    result = _triage(tmp_path, cards)

    assert _row(result, "T-relaunch")["failure_class"] == "provider_unavailable"
    assert _row(result, "T-relaunch")["recommendation"] == "relaunch"
    assert _row(result, "T-keep")["failure_class"] == "provider_unavailable"
    assert _row(result, "T-keep")["recommendation"] == "keep"
    assert _row(result, "T-plain")["failure_class"] is None
    assert _row(result, "T-odd")["failure_class"] is None


@pytest.mark.parametrize("substatus", KEEP_SUBSTATUSES)
def test_keep_for_every_other_substatus(tmp_path, substatus):
    row = _row(_triage(tmp_path, [_card("T1", terminal_substatus=substatus)]), "T1")

    assert row["recommendation"] == "keep"
    assert row["terminal_substatus"] == substatus
    assert substatus in row["reason"]


@pytest.mark.parametrize("recorded", [None, "", "   "], ids=["absent", "empty", "blank"])
def test_keep_when_no_substatus_is_recorded(tmp_path, recorded):
    extra = {} if recorded is None else {"terminal_substatus": recorded}
    result = _triage(tmp_path, [_card("T1", **extra)])
    row = _row(result, "T1")

    assert row["recommendation"] == "keep"
    assert row["terminal_substatus"] is None
    assert result["counts"]["by_substatus"] == {"unrecorded": 1}


@pytest.mark.parametrize(
    ("needfix_state", "closed_sibling", "live_sibling", "verified", "substatus", "expected"),
    [
        ("resolved", "finished", "pending", True, "worker_failed", "archive"),
        ("resolved", None, "pending", True, "worker_failed", "archive"),
        (None, "archived", "processing", True, "worker_failed", "archive"),
        (None, None, "review", True, "worker_failed", "supersede"),
        (None, None, None, True, "worker_failed", "replay"),
        (None, None, None, False, "worker_failed", "relaunch"),
        (None, None, None, False, "exited", "keep"),
    ],
)
def test_first_rule_wins_when_several_apply(
    tmp_path, monkeypatch, needfix_state, closed_sibling, live_sibling, verified, substatus,
    expected,
):
    _delta(monkeypatch, verified)
    task_id = "needfix-NF-2026-00007"
    cards = [
        _card(task_id, terminal_substatus=substatus, rework_predecessor={"task_id": "T-pred"})
    ]
    if closed_sibling:
        cards.append(_card("T-closed", status=closed_sibling, created_at=LATER))
    if live_sibling:
        cards.append(_card("T-live", status=live_sibling, created_at=LATE))

    result = _triage(tmp_path, cards, needfix=lambda needfix_id: needfix_state)
    assert _row(result, task_id)["recommendation"] == expected


def test_needfix_evidence_is_named_before_sibling_evidence(tmp_path):
    cards = [
        _card("needfix-NF-2026-00008", terminal_substatus="worker_failed"),
        _card("T-done", status="finished", created_at=LATE),
    ]
    row = _row(_triage(tmp_path, cards, needfix=_resolved("NF-2026-00008")), cards[0]["task_id"])

    assert row["recommendation"] == "archive"
    assert "NF-2026-00008" in row["reason"]
    assert "sibling_task_id" not in row


def test_unreadable_needfix_keeps_the_card_with_a_named_reason(tmp_path):
    def locked(needfix_id):
        raise RuntimeError("database is locked")

    cards = [
        _card("needfix-NF-2026-00002", terminal_substatus="worker_failed"),
        _card("T-live", status="pending", created_at=LATE),
    ]
    row = _row(_triage(tmp_path, cards, needfix=locked), "needfix-NF-2026-00002")

    assert row["recommendation"] == "keep"
    assert "NeedFix" in row["reason"] and "RuntimeError" in row["reason"]


def test_a_done_sibling_still_archives_when_the_needfix_is_unreadable(tmp_path):
    def locked(needfix_id):
        raise RuntimeError("database is locked")

    cards = [
        _card("needfix-NF-2026-00002", terminal_substatus="worker_failed"),
        _card("T-done", status="finished", created_at=LATE),
    ]
    row = _row(_triage(tmp_path, cards, needfix=locked), "needfix-NF-2026-00002")

    assert (row["recommendation"], row["sibling_task_id"]) == ("archive", "T-done")


def test_unreadable_rework_delta_keeps_the_card_with_a_named_reason(tmp_path, monkeypatch):
    def broken(predecessor, *, authority_repo):
        raise OSError("workspace gone")

    monkeypatch.setattr("aiworkhub.worker_workspace.has_verified_rework_delta", broken)
    cards = [
        _card("T-rework", terminal_substatus="worker_failed", rework_predecessor={"task_id": "P"})
    ]
    row = _row(_triage(tmp_path, cards), "T-rework")

    assert row["recommendation"] == "keep"
    assert "OSError" in row["reason"]


@pytest.mark.parametrize(
    "scope", ["bogus", "Blocked", "PROCESSING", "", " blocked", "all"],
)
def test_unknown_scope_is_refused_without_reading_anything(tool, monkeypatch, scope):
    monkeypatch.setattr(stale_recovery, "build_recovery_actions", _boom)
    monkeypatch.setattr(stale_recovery, "build_blocked_triage", _boom)

    assert tool(scope=scope) == {
        "ok": False,
        "error": "unknown_scope",
        "allowed": ["processing", "blocked"],
    }


def test_tool_keeps_its_existing_arguments_first(tool):
    parameters = inspect.signature(tool).parameters
    assert [(name, parameter.default) for name, parameter in parameters.items()] == [
        ("topic", None),
        ("limit", 200),
        ("scope", "processing"),
        ("sample", 10),
    ]


def test_triage_signature_matches_the_contract():
    parameters = inspect.signature(stale_recovery.build_blocked_triage).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters.values())
    assert parameters["root"].default is inspect.Parameter.empty
    assert {
        name: parameter.default
        for name, parameter in parameters.items()
        if parameter.default is not inspect.Parameter.empty
    } == {"topic": None, "limit": 500, "sample": 10, "cards": None, "needfix_status": None}


def test_default_scope_is_unchanged_from_build_recovery_actions(tool, monkeypatch):
    inbox = {
        "stale_processing": [
            {"task_id": "T-zombie", "runner": "r1", "topic": "alpha", "zombie_hint": True, "pid": 7},
            {"task_id": "T-open", "claimed_by": "r2", "topic": "alpha"},
            {"task_id": "T-pass", "topic": "beta", "validation_status": "pass"},
            {"task_id": "T-none", "topic": "beta"},
        ]
    }
    launch_log = {
        "last_entries": [
            {"task_id": "T-open", "to_state": "queued"},
            {"task_id": "T-pass", "to_state": "queued", "decision": "rolled_back"},
        ]
    }
    calls = []

    def fake_inbox(**kwargs):
        calls.append(("inbox", kwargs))
        return inbox

    def fake_log(**kwargs):
        calls.append(("log", kwargs))
        return launch_log

    monkeypatch.setattr(completion_inbox, "build_completion_inbox", fake_inbox)
    monkeypatch.setattr(launch_queue_persist, "read_persisted_log", fake_log)

    direct = stale_recovery.build_recovery_actions(topic="alpha", limit=50)
    by_default = tool(topic="alpha", limit=50)
    explicit = tool(topic="alpha", limit=50, scope="processing", sample=3)

    assert json.dumps(by_default) == json.dumps(direct) == json.dumps(explicit)
    assert calls[:2] == [("inbox", {"topic": "alpha", "limit": 50}), ("log", {"max_entries": 50})]
    assert list(direct) == [
        "tool", "contract", "readonly", "topic", "recovery_actions", "counts", "authority_flags",
    ]
    assert direct["contract"] == "B287_v1_readonly_recommendations"
    assert [
        (a["task_id"], a["action_type"], a["confidence"], a["rank"])
        for a in direct["recovery_actions"]
    ] == [
        ("T-zombie", "kill_zombie", "medium", 0),
        ("T-open", "requeue", "medium", 1),
        ("T-pass", "mark_done", "medium", 2),
        ("T-none", "escalate", "low", 3),
    ]
    assert direct["counts"] == {
        "stale_processing": 4,
        "recovery_actions": 4,
        "by_action": {"kill_zombie": 1, "requeue": 1, "mark_done": 1, "escalate": 1},
    }
    assert direct["authority_flags"] == AUTHORITY_FLAGS
    assert list(direct["authority_flags"]) == list(AUTHORITY_FLAGS)


def test_blocked_scope_dispatches_to_the_triage(tool, tmp_path, monkeypatch):
    seen = {}

    def triage(**kwargs):
        seen.update(kwargs)
        return {"sentinel": "triage"}

    monkeypatch.setattr(stale_recovery, "build_blocked_triage", triage)
    monkeypatch.setattr(stale_recovery, "build_recovery_actions", _boom)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)

    assert tool(topic="alpha", limit=7, scope="blocked", sample=3) == {"sentinel": "triage"}
    assert seen == {"root": tmp_path, "topic": "alpha", "limit": 7, "sample": 3}


def test_processing_scope_ignores_the_triage_arguments(tool, monkeypatch):
    seen = {}

    def recovery(**kwargs):
        seen.update(kwargs)
        return {"sentinel": "recovery"}

    monkeypatch.setattr(stale_recovery, "build_recovery_actions", recovery)
    monkeypatch.setattr(stale_recovery, "build_blocked_triage", _boom)

    assert tool(topic="alpha", limit=7, sample=3) == {"sentinel": "recovery"}
    assert tool(topic="alpha", limit=7, scope="processing", sample=99) == {"sentinel": "recovery"}
    assert seen == {"topic": "alpha", "limit": 7}


def test_sample_caps_each_group_while_counts_stay_exact(tmp_path):
    cards = [_card(f"T-{index:02d}", terminal_substatus="worker_failed") for index in range(25)]
    cards += [_card(f"K-{index:02d}", terminal_substatus="exited") for index in range(4)]
    result = _triage(tmp_path, cards, sample=3)

    assert result["counts"]["blocked"] == 29
    assert result["counts"]["by_recommendation"] == {
        "archive": 0, "supersede": 0, "replay": 0, "relaunch": 25, "keep": 4,
    }
    assert result["counts"]["by_substatus"] == {"exited": 4, "worker_failed": 25}
    assert [row["task_id"] for row in result["groups"]["relaunch"]] == ["T-00", "T-01", "T-02"]
    assert [row["task_id"] for row in result["groups"]["keep"]] == ["K-00", "K-01", "K-02"]
    assert result["groups"]["archive"] == []
    assert result["bounds"]["sample"] == 3


def test_default_sample_is_ten_rows_per_group(tmp_path):
    cards = [_card(f"T-{index:02d}", terminal_substatus="worker_failed") for index in range(25)]
    result = _triage(tmp_path, cards)

    assert len(result["groups"]["relaunch"]) == 10
    assert result["counts"]["by_recommendation"]["relaunch"] == 25


@pytest.mark.parametrize("sample", [0, -1])
def test_sample_of_zero_lists_no_rows_but_counts_everything(tmp_path, sample):
    cards = [_card(f"T-{index}", terminal_substatus="worker_failed") for index in range(5)]
    result = _triage(tmp_path, cards, sample=sample)

    assert _rows(result) == []
    assert result["counts"]["blocked"] == 5
    assert result["counts"]["by_recommendation"]["relaunch"] == 5
    assert result["bounds"]["sample"] == 0


def test_limit_bounds_the_cards_read_and_truncation_is_reported(tmp_path):
    cards = [_card(f"T-{index:02d}", terminal_substatus="worker_failed") for index in range(12)]
    cut = _triage(tmp_path, cards, limit=5)

    assert cut["counts"]["blocked"] == 5
    assert [row["task_id"] for row in cut["groups"]["relaunch"]] == [
        f"T-{index:02d}" for index in range(5)
    ]
    assert cut["bounds"] == {
        "limit": 5, "sample": 10, "blocked_matching": 12, "truncated": True, "scan_capped": [],
    }

    whole = _triage(tmp_path, cards, limit=12)
    assert whole["counts"]["blocked"] == 12
    assert whole["bounds"]["truncated"] is False


@pytest.mark.parametrize(("asked", "applied"), [(0, 1), (-4, 1), (10**6, 5000)])
def test_limit_is_clamped(tmp_path, asked, applied):
    assert _triage(tmp_path, [], limit=asked)["bounds"]["limit"] == applied


def test_an_empty_queue_yields_zero_counts(tmp_path):
    result = _triage(tmp_path, [])

    assert result["counts"] == {
        "blocked": 0,
        "by_recommendation": {name: 0 for name in RANKED},
        "by_substatus": {},
    }
    assert result["groups"] == {name: [] for name in RANKED}


def test_topic_restricts_the_triage_to_that_topic(tmp_path):
    cards = [
        _card("A-1", topic="alpha", terminal_substatus="worker_failed"),
        _card("B-1", topic="beta", terminal_substatus="worker_failed"),
        _card("B-2", topic="beta", terminal_substatus="exited"),
    ]
    result = _triage(tmp_path, cards, topic="beta")

    assert result["topic"] == "beta"
    assert sorted(row["task_id"] for row in _rows(result)) == ["B-1", "B-2"]
    assert result["bounds"]["blocked_matching"] == 2


def test_each_card_gets_exactly_one_recommendation(tmp_path, monkeypatch):
    result = _mixed_result(tmp_path, monkeypatch, sample=100)

    assert {row["task_id"]: row["recommendation"] for row in _rows(result)} == {
        "needfix-NF-2026-00010": "archive",
        "D-old": "archive",
        "S-old": "supersede",
        "R-1": "replay",
        "L-1": "relaunch",
        "L-2": "relaunch",
        "K-0": "keep",
        "K-1": "keep",
    }
    assert len(_rows(result)) == result["counts"]["blocked"] == 8
    assert sum(result["counts"]["by_recommendation"].values()) == 8
    assert result["counts"]["by_substatus"] == {
        "cancelled": 1,
        "exited": 1,
        "launch_failed": 1,
        "liveness_lost": 1,
        "timed_out": 1,
        "unrecorded": 1,
        "worker_failed": 2,
    }


def test_output_is_deterministic_and_sorted_by_rank_then_task_id(tmp_path, monkeypatch):
    cards = _mixed_cards()
    forward = _mixed_result(tmp_path, monkeypatch, cards, sample=100)
    backward = _mixed_result(tmp_path, monkeypatch, list(reversed(cards)), sample=100)
    rotated = _mixed_result(tmp_path, monkeypatch, cards[3:] + cards[:3], sample=100)

    assert json.dumps(forward) == json.dumps(backward) == json.dumps(rotated)
    assert list(forward["groups"]) == RANKED
    assert list(forward["counts"]["by_recommendation"]) == RANKED
    assert [(row["recommendation"], row["task_id"]) for row in _rows(forward)] == [
        ("archive", "D-old"),
        ("archive", "needfix-NF-2026-00010"),
        ("supersede", "S-old"),
        ("replay", "R-1"),
        ("relaunch", "L-1"),
        ("relaunch", "L-2"),
        ("keep", "K-0"),
        ("keep", "K-1"),
    ]
    assert list(forward["counts"]["by_substatus"]) == sorted(forward["counts"]["by_substatus"])


def test_payload_shape_and_authority_flags(tmp_path, monkeypatch):
    result = _mixed_result(tmp_path, monkeypatch, sample=100)

    assert set(result) == {
        "tool", "contract", "readonly", "scope", "topic", "counts", "groups", "bounds",
        "authority_flags",
    }
    assert result["tool"] == "aiworkhub_task_stale_recovery_recommend"
    assert result["contract"] == "NF-2026-01673_blocked_triage_v1"
    assert result["readonly"] is True
    assert result["scope"] == "blocked"
    assert result["topic"] is None
    assert set(result["counts"]) == {"blocked", "by_recommendation", "by_substatus"}
    assert set(result["groups"]) == set(result["counts"]["by_recommendation"]) == set(RANKED)
    for row in _rows(result):
        assert set(row) - {"sibling_task_id"} == ROW_KEYS
        assert ("sibling_task_id" in row) == (row["task_id"] in {"D-old", "S-old"})
    assert _row(result, "D-old")["sibling_task_id"] == "D-new"
    assert _row(result, "S-old")["sibling_task_id"] == "S-new"


def test_authority_flags_match_the_processing_scope_and_are_never_shared(tmp_path):
    processing = stale_recovery.build_recovery_actions(inbox={}, launch_log={})
    blocked = _triage(tmp_path, [])

    assert blocked["authority_flags"] == processing["authority_flags"] == AUTHORITY_FLAGS
    assert list(blocked["authority_flags"]) == list(processing["authority_flags"])
    blocked["authority_flags"]["queue_write"] = True
    assert _triage(tmp_path, [])["authority_flags"] == AUTHORITY_FLAGS


def test_every_reason_is_one_short_sentence_naming_its_evidence(tmp_path, monkeypatch):
    result = _mixed_result(tmp_path, monkeypatch, sample=100)
    evidence = {
        "needfix-NF-2026-00010": "NF-2026-00010",
        "D-old": "D-new",
        "S-old": "S-new",
        "R-1": "verified",
        "L-1": "liveness_lost",
        "L-2": "launch_failed",
        "K-0": "terminal_substatus",
        "K-1": "exited",
    }

    for row in _rows(result):
        reason = row["reason"]
        assert reason == reason.strip() and reason.endswith(".")
        assert "\n" not in reason and len(reason) <= 120
        assert evidence[row["task_id"]] in reason


def test_input_cards_are_not_mutated(tmp_path, monkeypatch):
    cards = _mixed_cards()
    before = copy.deepcopy(cards)
    _mixed_result(tmp_path, monkeypatch, cards)
    assert cards == before


def test_relaunch_substatuses_are_a_named_subset_of_what_the_system_writes():
    relaunch = stale_recovery.RELAUNCH_SUBSTATUSES
    written = task_fsm.LAUNCHER_TERMINAL_SUBSTATUSES | task_fsm.MARK_TERMINAL_FAILURE_SUBSTATUSES

    assert relaunch == {"worker_failed", "liveness_lost", "launch_failed", "timed_out", "cancelled"}
    assert relaunch <= task_fsm.KNOWN_TERMINAL_SUBSTATUSES
    assert relaunch <= written


def test_closed_needfix_statuses_are_a_named_subset_of_the_store_vocabulary():
    assert stale_recovery.NEEDFIX_CLOSED_STATUSES == {"resolved", "archived"}
    assert stale_recovery.NEEDFIX_CLOSED_STATUSES <= needfix_store.NEEDFIX_TERMINAL_STATUSES


def test_recommendations_are_one_ranked_vocabulary():
    ranked = sorted(stale_recovery.RECOMMENDATION_RANK, key=stale_recovery.RECOMMENDATION_RANK.get)
    assert ranked == RANKED
    assert stale_recovery.SCOPES == ("processing", "blocked")


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = tmp_path / "tasks.db"
    connection = sqlite3.connect(db)
    connection.executescript(task_store.SCHEMA)
    connection.commit()
    connection.close()
    readiness = task_store.StorageReadiness(True, "ready", "repo", str(db))
    monkeypatch.setattr(task_store, "storage_readiness", lambda root: readiness)
    return db


def _insert(
    db, task_id, *, topic="alpha", status="blocked", created_at=EARLY, updated_at=None,
    archived_at="", **card,
):
    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO tasks(task_id, topic, status, worker_status, card_json, created_at, "
        "updated_at, archived_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id, topic, status, status, json.dumps({"task_id": task_id, **card}),
            created_at, updated_at or created_at, archived_at,
        ),
    )
    connection.commit()
    connection.close()


def _digest(db):
    connection = sqlite3.connect(db)
    try:
        return hashlib.sha256("\n".join(connection.iterdump()).encode("utf-8")).hexdigest()
    finally:
        connection.close()


def _seed_needfix(root, title, **kwargs):
    return needfix_store.add_needfix(
        root, title=title, description="Seeded by the blocked triage test.", **kwargs
    )["id"]


def test_store_reader_classifies_every_branch_without_writing(tmp_path, store, monkeypatch):
    resolved = _seed_needfix(tmp_path, "Resolved triage fixture", status="resolved")
    archived = _seed_needfix(tmp_path, "Archived triage fixture", status="archived")
    still_open = _seed_needfix(tmp_path, "Open triage fixture")
    _insert(store, f"needfix-{resolved}", topic="t1", terminal_substatus="worker_failed")
    _insert(store, f"needfix-{archived}-r1", topic="t1a", terminal_substatus="timed_out")
    _insert(store, f"needfix-{still_open}", topic="t1b", terminal_substatus="worker_failed")
    _insert(store, "D-old", topic="t2", terminal_substatus="timed_out")
    _insert(store, "D-new", topic="t2", status="finished", created_at=LATE)
    _insert(store, "A-old", topic="t2b", terminal_substatus="timed_out")
    _insert(store, "A-new", topic="t2b", status="finished", created_at=LATE, archived_at=LATER)
    _insert(store, "S-old", topic="t3", terminal_substatus="cancelled")
    _insert(store, "S-new", topic="t3", status="pending", created_at=LATE)
    _insert(store, "R-1", topic="t4", terminal_substatus="worker_failed",
            rework_predecessor={"task_id": "P-1"})
    _insert(store, "L-1", topic="t5", terminal_substatus="liveness_lost")
    _insert(store, "K-1", topic="t6", terminal_substatus="exited")
    _insert(store, "K-0", topic="t6")
    _insert(store, "N-1", topic="noise", status="finished", created_at=LATE)
    _delta(monkeypatch, True)
    needfix_db = tmp_path.joinpath(*needfix_store.NEEDFIX_DB_REL)
    before = (_digest(store), _digest(needfix_db))

    result = stale_recovery.build_blocked_triage(root=tmp_path)

    assert (_digest(store), _digest(needfix_db)) == before
    assert {row["task_id"]: row["recommendation"] for row in _rows(result)} == {
        f"needfix-{resolved}": "archive",
        f"needfix-{archived}-r1": "archive",
        f"needfix-{still_open}": "relaunch",
        "D-old": "archive",
        "A-old": "archive",
        "S-old": "supersede",
        "R-1": "replay",
        "L-1": "relaunch",
        "K-1": "keep",
        "K-0": "keep",
    }
    assert _row(result, "D-old")["sibling_task_id"] == "D-new"
    assert _row(result, "A-old")["sibling_task_id"] == "A-new"
    assert _row(result, "S-old")["sibling_task_id"] == "S-new"
    assert result["bounds"] == {
        "limit": 500, "sample": 10, "blocked_matching": 10, "truncated": False, "scan_capped": [],
    }


def test_store_reader_reports_a_recorded_failure_class(tmp_path, store):
    recorded = {"terminal_failure": {"evidence": {"failure_class": "credential_expired"}}}
    _insert(store, "T-1", terminal_substatus="worker_failed", **recorded)
    row = _row(stale_recovery.build_blocked_triage(root=tmp_path), "T-1")

    assert (row["recommendation"], row["failure_class"]) == ("relaunch", "credential_expired")


def test_missing_needfix_database_is_neither_created_nor_fatal(tmp_path, store):
    _insert(store, "needfix-NF-2026-09999", terminal_substatus="worker_failed")
    result = stale_recovery.build_blocked_triage(root=tmp_path)

    assert _row(result, "needfix-NF-2026-09999")["recommendation"] == "relaunch"
    assert not tmp_path.joinpath(needfix_store.HUB_DIRNAME).exists()


def test_each_needfix_is_read_once_per_triage(tmp_path, store, monkeypatch):
    shared = _seed_needfix(tmp_path, "Shared triage fixture", status="resolved")
    for suffix in ("", "-r1", "-r2"):
        _insert(store, f"needfix-{shared}{suffix}", terminal_substatus="worker_failed")
    real = needfix_store.get_needfix
    asked = []

    def counting(repo_root, needfix_id, *args, **kwargs):
        asked.append(needfix_id)
        return real(repo_root, needfix_id, *args, **kwargs)

    monkeypatch.setattr(needfix_store, "get_needfix", counting)
    result = stale_recovery.build_blocked_triage(root=tmp_path)

    assert asked == [shared]
    assert result["counts"]["by_recommendation"]["archive"] == 3


def test_unreadable_cards_are_kept_with_a_named_reason(tmp_path, store, monkeypatch):
    for task_id in ("T-gone", "T-broken", "T-fine"):
        _insert(store, task_id, terminal_substatus="worker_failed")
    real = task_store.get_task

    def flaky(root, task_id, **kwargs):
        if task_id == "T-gone":
            return None
        if task_id == "T-broken":
            raise sqlite3.DatabaseError("malformed")
        return real(root, task_id, **kwargs)

    monkeypatch.setattr(task_store, "get_task", flaky)
    result = stale_recovery.build_blocked_triage(root=tmp_path)
    gone, broken = _row(result, "T-gone"), _row(result, "T-broken")

    assert _row(result, "T-fine")["recommendation"] == "relaunch"
    assert gone["recommendation"] == broken["recommendation"] == "keep"
    assert "card_missing" in gone["reason"]
    assert "DatabaseError" in broken["reason"]
    assert gone["topic"] == "alpha" and gone["terminal_substatus"] is None


@pytest.mark.parametrize(
    "failure",
    [
        task_store.StorageNotReadyError("canonical_db_missing"),
        sqlite3.OperationalError("database is locked"),
        PermissionError("denied"),
    ],
    ids=lambda failure: type(failure).__name__,
)
def test_an_unreadable_queue_is_reported_instead_of_raised(tmp_path, store, monkeypatch, failure):
    def unreadable(*args, **kwargs):
        raise failure

    monkeypatch.setattr(task_store, "list_tasks", unreadable)
    result = stale_recovery.build_blocked_triage(root=tmp_path, topic="alpha")

    assert result["ok"] is False
    assert result["error"] == "blocked_queue_unreadable"
    assert type(failure).__name__ in result["detail"] and len(result["detail"]) <= 200
    assert result["scope"] == "blocked" and result["readonly"] is True
    assert result["topic"] == "alpha"
    assert result["authority_flags"] == AUTHORITY_FLAGS
    assert "counts" not in result


def test_a_store_that_is_not_ready_is_reported_instead_of_raised(tmp_path, monkeypatch):
    unready = task_store.StorageReadiness(False, "canonical_db_missing", "repo", "")
    monkeypatch.setattr(task_store, "storage_readiness", lambda root: unready)
    result = stale_recovery.build_blocked_triage(root=tmp_path)

    assert result["ok"] is False and result["error"] == "blocked_queue_unreadable"
    assert "canonical_db_missing" in result["detail"]


def test_scan_cap_is_reported_when_the_queue_exceeds_it(tmp_path, store):
    connection = sqlite3.connect(store)
    connection.executemany(
        "INSERT INTO tasks(task_id, topic, status, worker_status, card_json, created_at, "
        "updated_at) VALUES(?, 'alpha', 'blocked', 'blocked', '{}', ?, ?)",
        [(f"T-{index:05d}", EARLY, EARLY) for index in range(5001)],
    )
    connection.commit()
    connection.close()

    result = stale_recovery.build_blocked_triage(root=tmp_path, limit=5)

    assert result["counts"]["blocked"] == 5
    assert result["bounds"] == {
        "limit": 5, "sample": 10, "blocked_matching": 5000, "truncated": True,
        "scan_capped": ["blocked"],
    }


def test_limit_keeps_the_most_recently_updated_cards(tmp_path, store):
    for index in range(4):
        _insert(store, f"T-{index}", updated_at=f"2026-01-0{index + 1}T00:00:00+00:00")
    result = stale_recovery.build_blocked_triage(root=tmp_path, limit=2)

    assert sorted(row["task_id"] for row in _rows(result)) == ["T-2", "T-3"]
    assert result["bounds"]["blocked_matching"] == 4
    assert result["bounds"]["truncated"] is True


def test_topic_filter_is_applied_before_the_limit(tmp_path, store):
    for index in range(3):
        _insert(store, f"A-{index}", topic="alpha", updated_at=f"2026-02-0{index + 1}T00:00:00+00:00")
    _insert(store, "B-0", topic="beta", updated_at=EARLY)
    result = stale_recovery.build_blocked_triage(root=tmp_path, topic="beta", limit=1)

    assert [row["task_id"] for row in _rows(result)] == ["B-0"]
    assert result["bounds"]["blocked_matching"] == 1
    assert result["bounds"]["truncated"] is False
