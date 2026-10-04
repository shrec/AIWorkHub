"""NF-2026-01352: rework feedback is refused over its cap, never silently cut.

reject_review used to keep only the first 4 KiB of a reason and
recover_blocked_rework only the first 2000 characters of its feedback, so the
worker received an instruction whose tail was gone.  The cap is now one UTF-8
byte bound (``task_store.MAX_REWORK_FEEDBACK_BYTES``): a text within it is
stored whole, a text over it is refused before any state change.
"""
from __future__ import annotations

import json

import pytest

from aiworkhub import core, process_launcher, quality_reviewer, task_store
from aiworkhub.crash_retry_packet import MAX_CRASH_RETRY_PACKET_BYTES
from test_aiworkhub_coordinator_disposition_b961 import (  # noqa: F401
    _events,
    _insert,
    _row,
    coord,
)
from test_blocked_rework_recovery import (
    _get_card,
    _insert_blocked_task,
    _setup_repo,
)

CAP = task_store.MAX_REWORK_FEEDBACK_BYTES
# Exactly CAP bytes in 2668 characters: the bound is bytes, not characters.
_AT_CAP = "ქ" * 2666 + "ab"
_OVER_CAP = "ქ" * 2667  # CAP + 1 bytes
# The largest card contract measured in the field (2026-10-05, 1226 cards)
# without its review_feedback.
_MEASURED_MAX_CONTRACT_BYTES = 15_719


def test_nf01352_cap_is_one_shared_byte_bound() -> None:
    assert len(_AT_CAP.encode("utf-8")) == CAP
    assert len(_OVER_CAP.encode("utf-8")) == CAP + 1
    assert core._MAX_REWORK_FEEDBACK_BYTES == CAP
    # Raised from 4 KiB, but never past a consumer that would cut it again.
    assert 4 * 1024 < CAP <= quality_reviewer.MAX_MANAGER_AMENDMENT_CHARS


@pytest.mark.parametrize("to", ["pending", "blocked", "archived"])
@pytest.mark.parametrize("reason", ["x" * (CAP + 1), _OVER_CAP], ids=["ascii", "utf8"])
def test_nf01352_reject_review_over_cap_fails_closed_before_any_change(
    coord, to: str, reason: str
) -> None:
    _insert(coord, "T_OVER", card={"objective": "repair"})
    row_before, events_before = _row(coord, "T_OVER"), _events(coord, "T_OVER")

    res = core.reject_review("T_OVER", reason, to=to)

    assert res["ok"] is False, res
    assert res["stderr"] == f"reject_reason_too_large:{CAP + 1}>{CAP}"
    assert _row(coord, "T_OVER") == row_before
    assert row_before["status"] == "review"
    assert _events(coord, "T_OVER") == events_before


def test_nf01352_reject_review_stores_a_cap_sized_reason_whole(coord) -> None:
    _insert(coord, "T_AT_CAP")

    res = core.reject_review("T_AT_CAP", f"  {_AT_CAP}\n", to="pending")

    assert res["ok"] is True, res
    feedback = json.loads(_row(coord, "T_AT_CAP")["card_json"])["review_feedback"]
    assert feedback["instruction"] == _AT_CAP
    assert feedback["reason_identity"]["truncated"] is False
    assert feedback["reason_identity"]["bytes"] == CAP + 3


def test_nf01352_core_recover_refuses_over_cap_feedback_before_write_gate(
    monkeypatch,
) -> None:
    card = {"task_id": "T_REC", "topic": "blocked_rework"}
    forwarded: list[str] = []
    monkeypatch.setattr(core, "_live_card", lambda task_id: (card, None))
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **k: None)
    monkeypatch.setattr(core, "_reconcile_retained_workspaces", lambda result: result)
    monkeypatch.setattr(task_store, "get_task", lambda root, task_id: card)

    def recover(root, task_id, **kwargs):
        forwarded.append(kwargs["feedback_reason"])
        return True, "recovered"

    monkeypatch.setattr(task_store, "recover_blocked_rework", recover)

    refused = core.recover_blocked_rework("T_REC", feedback_reason=_OVER_CAP)
    accepted = core.recover_blocked_rework("T_REC", feedback_reason=_AT_CAP)

    assert refused["ok"] is False
    assert refused["stderr"] == f"feedback_reason_too_large:{CAP + 1}>{CAP}"
    assert accepted["ok"] is True, accepted
    assert forwarded == [_AT_CAP]


def test_nf01352_recover_stores_feedback_whole_and_refuses_over_cap(tmp_path) -> None:
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(repo, "T_WHOLE", reject_review_reason="residual")
    card_before = _get_card(repo, "T_WHOLE")
    events_before = task_store.get_task_events(repo, "T_WHOLE")

    assert task_store.recover_blocked_rework(
        repo, "T_WHOLE", actor="coordinator", feedback_reason=_OVER_CAP
    ) == (False, f"feedback_reason_too_large:{CAP + 1}>{CAP}")
    assert _get_card(repo, "T_WHOLE") == card_before
    assert task_store.get_task_events(repo, "T_WHOLE") == events_before

    assert len(_AT_CAP) > 2000
    assert task_store.recover_blocked_rework(
        repo, "T_WHOLE", actor="coordinator", feedback_reason=_AT_CAP
    ) == (True, "recovered")
    assert _get_card(repo, "T_WHOLE")["recovery_feedback"] == _AT_CAP
    recovery = next(
        event for event in task_store.get_task_events(repo, "T_WHOLE")
        if event["event"] == "blocked_rework_recovery"
    )
    assert json.loads(recovery["payload"])["feedback"] == _AT_CAP


def test_nf01352_stored_residual_reason_is_bounded_never_refused(tmp_path) -> None:
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(repo, "T_RESIDUAL", reject_review_reason="r" * (CAP + 500))

    assert task_store.recover_blocked_rework(
        repo, "T_RESIDUAL", actor="coordinator"
    ) == (True, "recovered")
    assert _get_card(repo, "T_RESIDUAL")["recovery_feedback"] == "r" * CAP


def test_nf01352_cap_sized_feedback_reaches_every_consumer_whole() -> None:
    # Quotes and newlines double under JSON escaping: the realistic worst case.
    instruction = '"\n' * (CAP // 2)
    feedback = {
        "schema_id": "aiworkhub.rework_feedback_delta.v1",
        "instruction": instruction,
        "reason_identity": {"bytes": CAP, "sha256": "f" * 64, "truncated": False},
        "predecessor_request_id": "r" * 32,
    }
    amendment = quality_reviewer._manager_amendment_section(feedback)
    assert amendment is not None and amendment["instruction"] == instruction
    # The crash retry packet carries the overlay digest, never feedback text,
    # so its own bound is spent independently; give it the whole bound here.
    crash_packet = {"pad": "p" * (MAX_CRASH_RETRY_PACKET_BYTES - 10)}
    assert len(
        json.dumps(crash_packet, separators=(",", ":")).encode("utf-8")
    ) == MAX_CRASH_RETRY_PACKET_BYTES

    prompt = process_launcher.build_worker_prompt(
        task_id="T_PROMPT",
        runner="codex_worker_test",
        topic="coding",
        card={
            "objective": "o" * _MEASURED_MAX_CONTRACT_BYTES,
            "review_feedback": feedback,
        },
        owner_prompt="w" * process_launcher.MAX_OWNER_PROMPT_BYTES,
        crash_retry_packet=crash_packet,
    )

    contract_json = prompt.split("TASK_CONTRACT_JSON:\n", 1)[1].split(
        "\nEND_TASK_CONTRACT_JSON", 1
    )[0]
    assert json.loads(contract_json)["review_feedback"]["instruction"] == instruction
    assert (
        len(contract_json.encode("utf-8"))
        <= process_launcher.MAX_REWORK_TASK_CONTRACT_BYTES
    )
    assert (
        len(prompt.encode("utf-8")) <= process_launcher.MAX_REWORK_WORKER_PROMPT_BYTES
    )
