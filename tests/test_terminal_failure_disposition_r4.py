"""R4/NF-2026-00646: a transient provider hiccup and a real code defect must not
kill a card the same way.

Measured on this repository before any of this existed: 40 of 147 blocked cards
over 30 days died on the single string
``worker_failed:supervisor_state=exited:exit_code=1``, which says nothing about
whether the provider was at capacity, a credential expired, or the work was
wrong. This file pins the taxonomy that tells those apart, the evidence each
class is allowed to rest on, and -- just as importantly -- what it must REFUSE
to place.

Every provider payload below is a byte-faithful reproduction of a shape found in
``.aiworkhub/runtime/process_logs/processes`` for one of those 40 cards.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import (
    core,
    process_launcher,
    runtime_adapters,
    task_store,
    terminal_failure_classification as tfc,
)

TRANSIENT = tfc.FAILURE_CLASS_TRANSIENT
CREDENTIAL = tfc.FAILURE_CLASS_CREDENTIAL
DEFECT = tfc.FAILURE_CLASS_DEFECT
UNKNOWN = tfc.FAILURE_CLASS_UNKNOWN


# --- real provider shapes, reproduced exactly ------------------------------ #

# Codex CLI, ``codex_cli``/``gpt-5.4``: 13 byte-identical 725-byte logs. The
# upstream provider's error object arrives as a QUOTED JSON STRING inside
# ``message``; the outer line carries no status at all.
CODEX_MODEL_REJECTION = "\n".join([
    '{"type":"thread.started","thread_id":"01a06e80-ee3c-7a82-afac-ade065f0d8df"}',
    '{"type":"turn.started"}',
    '{"type":"error","message":"{\\"type\\":\\"error\\",\\"status\\":400,'
    '\\"error\\":{\\"type\\":\\"invalid_request_error\\",\\"message\\":'
    '\\"The \'gpt-5.4\' model is not supported when using Codex with a '
    'ChatGPT account.\\"}}"}',
    '{"type":"turn.failed","error":{"message":"{\\"type\\":\\"error\\",'
    '\\"status\\":400,\\"error\\":{\\"type\\":\\"invalid_request_error\\",'
    '\\"message\\":\\"The \'gpt-5.4\' model is not supported when using Codex '
    'with a ChatGPT account.\\"}}"}}',
])

# Kilo/xAI, ``grok_kilo_cli``: the OAuth token endpoint's own RFC 6749 error
# object, nested inside the CLI's error envelope.
GROK_TOKEN_REFRESH = (
    '{"type":"error","timestamp":1788485437318,"sessionID":"ses_f95f60e12ffeJui5",'
    '"error":{"name":"UnknownError","data":{"message":"xAI token refresh failed '
    '(400): {\\"error\\":\\"invalid_grant\\",\\"error_description\\":\\"Invalid '
    'or unknown refresh token\\"}"}}}'
)

# Claude CLI, ``claude_cli``/``claude-haiku-4.5``: a synthetic assistant line
# carrying the machine code, then the terminal result envelope with the status.
CLAUDE_MODEL_NOT_FOUND = "\n".join([
    '{"type":"assistant","message":{"model":"<synthetic>","role":"assistant"},'
    '"error":"model_not_found","request_id":"req_011Ceit55zzTkXL6VSFdRcti"}',
    '{"type":"result","subtype":"success","is_error":true,"api_error_status":404,'
    '"terminal_reason":"api_error","result":"There is an issue with the selected '
    'model (claude-haiku-4.5)."}',
])

# Codex CLI, ``gpt-5.5``: the audit called this transient, and the provider
# asserted nothing but prose. It must stay unplaced.
CODEX_AT_CAPACITY = "\n".join([
    '{"type":"error","message":"Selected model is at capacity. Please try a '
    'different model."}',
    '{"type":"turn.failed","error":{"message":"Selected model is at capacity. '
    'Please try a different model."}}',
])

CLAUDE_RATE_LIMITED = (
    '{"type":"result","subtype":"success","is_error":true,"api_error_status":429,'
    '"terminal_reason":"api_error"}'
)
CLAUDE_BARE_401 = (
    '{"type":"result","subtype":"success","is_error":true,"api_error_status":401,'
    '"terminal_reason":"api_error"}'
)


# --- the taxonomy is closed, and cannot grow a silent hole ----------------- #


def test_every_reason_constant_is_placed_or_disclaimed():
    """A new reason constant must not be able to arrive unclassified.

    Same gate ``dependency_autolaunch.unclassified_denial_reasons`` provides
    for launch denials: the residue is empty BY CONSTRUCTION, so adding a
    constant to any vocabulary without deciding its class fails here.
    """
    assert tfc.unclassified_reason_constants() == ()


def test_refusal_kind_vocabulary_matches_runtime_adapters():
    """The classifier and the module that mints refusal kinds cannot drift."""
    minted = {
        runtime_adapters.REFUSAL_SESSION_LIMIT,
        runtime_adapters.REFUSAL_QUOTA_EXHAUSTED,
        runtime_adapters.REFUSAL_BALANCE_EXHAUSTED,
        runtime_adapters.REFUSAL_RATE_LIMITED,
        runtime_adapters.REFUSAL_CREDENTIAL_REJECTED,
        runtime_adapters.REFUSAL_PROVIDER_UNAVAILABLE,
        runtime_adapters.REFUSAL_CAUSE_NOT_DISTINGUISHED,
    }
    assert minted <= set(tfc.PROVIDER_REFUSAL_KINDS)
    assert set(tfc.PROVIDER_REFUSAL_KINDS) == set(tfc.REFUSAL_KIND_DISPOSITION)
    assert set(tfc.REFUSAL_KIND_DISPOSITION.values()) <= tfc.FAILURE_CLASSES


def test_recoverable_refusals_are_exactly_the_transient_ones():
    """``runtime_adapters`` already decided which refusals a wait clears."""
    recoverable = {
        kind for kind, placed in tfc.REFUSAL_KIND_DISPOSITION.items()
        if placed == TRANSIENT
    }
    # ``provider_unavailable`` (a 5xx outage) and ``model_not_found`` (a route
    # this account cannot use) are transient for the CARD without being in
    # runtime_adapters' reset-window set, which is about a reported window.
    assert recoverable == {
        runtime_adapters.REFUSAL_SESSION_LIMIT,
        runtime_adapters.REFUSAL_QUOTA_EXHAUSTED,
        runtime_adapters.REFUSAL_RATE_LIMITED,
        runtime_adapters.REFUSAL_PROVIDER_UNAVAILABLE,
        "model_not_found",
    }
    # A dead account is never a wait, whatever the wording of its refusal.
    assert tfc.REFUSAL_KIND_DISPOSITION[
        runtime_adapters.REFUSAL_BALANCE_EXHAUSTED
    ] == CREDENTIAL


# --- what each class rests on --------------------------------------------- #


def test_oauth_machine_code_places_credential():
    verdict = tfc.failure_disposition(stdout_tail=GROK_TOKEN_REFRESH)
    assert verdict["failure_class"] == CREDENTIAL
    assert verdict["evidence"] == "provider_code=invalid_grant"


def test_claude_machine_code_places_the_route_as_transient():
    verdict = tfc.failure_disposition(stdout_tail=CLAUDE_MODEL_NOT_FOUND)
    assert verdict["failure_class"] == TRANSIENT
    assert verdict["evidence"] == "provider_code=model_not_found"


def test_provider_asserted_429_places_transient():
    verdict = tfc.failure_disposition(stdout_tail=CLAUDE_RATE_LIMITED)
    assert verdict["failure_class"] == TRANSIENT
    assert verdict["evidence"] == "provider_status=429"


def test_our_own_validator_refusal_places_defect():
    """Only AIWorkHub's own finding about the work may say the work is wrong."""
    verdict = tfc.failure_disposition(
        reason='required_output_unchanged:{"unchanged_mandatory_outputs":["a.py"]}'
    )
    assert verdict["failure_class"] == DEFECT
    assert verdict["evidence"] == "control_plane_reason=required_output_unchanged"


def test_subscription_refresh_reason_places_credential():
    """The reason on the card that spent 190.8M tokens and $46.07."""
    verdict = tfc.failure_disposition(
        reason="claude_subscription_session_refresh_required"
    )
    assert verdict["failure_class"] == CREDENTIAL


def test_refusal_kind_outranks_the_log_tail():
    """The boundary detector held the whole response body; a tail did not."""
    verdict = tfc.failure_disposition(
        refusal_kind="credential_rejected", stdout_tail=CLAUDE_RATE_LIMITED
    )
    assert verdict["failure_class"] == CREDENTIAL
    assert verdict["evidence"] == "refusal_kind=credential_rejected"


# --- what it must REFUSE to place ----------------------------------------- #


def test_prose_alone_never_places_a_card():
    """The trap this card exists to avoid.

    Every one of these says something a substring matcher would happily call
    transient or credential. None of them is a typed assertion, so none may
    move a card.
    """
    for prose in (
        CODEX_AT_CAPACITY,
        '{"type":"result","is_error":true,"result":"rate limit exceeded, please '
        'retry"}',
        "Error: 429 Too Many Requests",
        '{"type":"assistant","message":{"content":[{"type":"text","text":'
        '"the api returned invalid_api_key so I stopped"}]}}',
        "Traceback (most recent call last): RuntimeError: quota exhausted",
    ):
        verdict = tfc.failure_disposition(stdout_tail=prose)
        assert verdict["failure_class"] == UNKNOWN, prose[:60]


def test_a_bare_401_stays_unplaced():
    """NF-2026-00326: a dead key, an expired token and a rate condition are
    indistinguishable from the status alone, so none of them is claimed."""
    verdict = tfc.failure_disposition(stdout_tail=CLAUDE_BARE_401)
    assert verdict["failure_class"] == UNKNOWN
    assert verdict["evidence"].startswith("envelope_names_no_class")


def test_bare_supervisor_reason_stays_unplaced():
    """The exact string 40 cards died on names no class, and must not."""
    verdict = tfc.failure_disposition(
        reason="worker_failed:supervisor_state=exited:exit_code=1"
    )
    assert verdict["failure_class"] == UNKNOWN
    assert verdict["evidence"] == "no_provider_terminal_envelope"


def test_no_evidence_at_all_is_unknown_not_defect():
    assert tfc.failure_disposition()["failure_class"] == UNKNOWN


def test_a_cancelled_outcome_carries_no_disposition(tmp_path):
    verdict = tfc.failure_disposition_from_paths(state="cancelled", cancelled=True)
    assert verdict["failure_class"] == UNKNOWN
    assert verdict["evidence"] == "no_failure_verdict"


# --- the no-copy invariant holds for dispositions too ---------------------- #


def test_no_provider_byte_reaches_the_returned_evidence():
    secret = "sk-live-AKIAIOSFODNN7EXAMPLE"
    payload = (
        '{"type":"result","is_error":true,"api_error_status":429,'
        f'"terminal_reason":"api_error","result":"{secret}"}}'
    )
    verdict = tfc.failure_disposition(stdout_tail=payload)
    assert verdict["failure_class"] == TRANSIENT
    assert secret not in json.dumps(verdict)
    # Every string in the verdict is a module constant or a bounded int.
    assert verdict["provider_code"] in {"", *tfc.PROVIDER_CODE_DISPOSITION}
    assert verdict["envelope"] in {"", *tfc._PROVIDER_TERMINAL_ENVELOPES}
    assert isinstance(verdict["provider_status"], int)


def test_an_unrecognised_machine_code_is_not_carried_through():
    payload = (
        '{"type":"error","status":400,"error":{"code":"totally_made_up_code",'
        '"message":"x"}}'
    )
    verdict = tfc.failure_disposition(stdout_tail=payload)
    assert verdict["provider_code"] == ""
    assert "totally_made_up_code" not in json.dumps(verdict)


# --- the nested envelope: R1's seal could not see the real bytes ----------- #


def test_nested_provider_envelope_is_unwrapped():
    signal = tfc.provider_terminal_signal(CODEX_MODEL_REJECTION)
    assert signal["status"] == 400
    assert signal["envelope"] in tfc._PROVIDER_TERMINAL_ENVELOPES


def test_route_seal_fires_on_the_real_nested_codex_shape(tmp_path):
    """Replayed against the exact 725-byte shape of the 13 gpt-5.4 failures.

    Before this, ``_provider_model_rejection_from_output`` required a top-level
    ``status`` and a dict ``error``; the Codex CLI supplies neither, so the seal
    matched nothing on any of the 13 real logs.
    """
    log = tmp_path / "out.log"
    log.write_text(CODEX_MODEL_REJECTION, encoding="utf-8")
    sealed = process_launcher._provider_model_rejection_from_output(log, "gpt-5.4")
    assert sealed is not None
    assert sealed["refusal_kind"] == "model_not_found"
    assert sealed["http_status"] == 400


def test_route_seal_still_requires_the_message_to_name_this_launchs_model():
    """The anti-forgery anchor survives the unwrapping."""
    log = Path(__file__).parent / "__r4_anchor.log"
    try:
        log.write_text(CODEX_MODEL_REJECTION, encoding="utf-8")
        assert process_launcher._provider_model_rejection_from_output(
            log, "gpt-5.6-terra"
        ) is None
    finally:
        log.unlink(missing_ok=True)


def test_capacity_failure_is_not_a_route_seal(tmp_path):
    log = tmp_path / "out.log"
    log.write_text(CODEX_AT_CAPACITY, encoding="utf-8")
    assert process_launcher._provider_model_rejection_from_output(log, "gpt-5.5") is None


# --- credential-at-end-of-run must not discard finished work --------------- #


def test_a_credential_failure_never_sweeps_the_workspace():
    """The one rule that stops the $46.07 case from repeating."""
    assert tfc.terminal_workspace_cleanup_allowed(
        terminal_state="launch_failed", failure_class=CREDENTIAL
    ) is False
    # and nothing else changes
    assert tfc.terminal_workspace_cleanup_allowed(
        terminal_state="launch_failed", failure_class=UNKNOWN
    ) is True
    assert tfc.terminal_workspace_cleanup_allowed(
        terminal_state="launch_failed", failure_class=TRANSIENT
    ) is True
    for state in ("worker_failed", "timed_out", "validation_failed", "exited"):
        assert tfc.terminal_workspace_cleanup_allowed(
            terminal_state=state, failure_class=UNKNOWN
        ) is False


# --- the transient transition ---------------------------------------------- #

RUNNER = "codex_cli"
TOPIC = "tasking_system"
NOW = "2026-09-07T00:00:00+00:00"


def _repo(tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    return repo


def _insert_processing(repo: Path, task_id: str, *, request_id: str, attempts: int = 0):
    readiness = task_store.storage_readiness(repo)
    card = {
        "task_id": task_id,
        "runner": RUNNER,
        "topic": TOPIC,
        "status": "processing",
        "worker_status": "claimed",
        "claimed_by": RUNNER,
        "launch_request_id": request_id,
        "claim_epoch": 1,
        "transient_retry_attempts": attempts,
        "read_only": True,
        "allowed_writes": [],
        "required_outputs": [],
    }
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at, claimed_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, RUNNER, TOPIC, "solo", "processing", "claimed", "normal",
             "objective", json.dumps(card, ensure_ascii=False, sort_keys=True),
             NOW, NOW, RUNNER),
        )
        conn.commit()
    finally:
        conn.close()


def test_transient_retry_returns_the_card_to_pending(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _insert_processing(repo, "T1", request_id="req-1")

    ok, state = task_store.mark_transient_retry(
        repo, "T1", runner=RUNNER, reason="worker_failed:rate_limited:exit_code=1",
        request_id="req-1",
    )
    assert (ok, state) == (True, "pending")

    card = task_store.get_task(repo, "T1") or {}
    assert card["status"] == "pending"
    assert card["worker_status"] == "unclaimed"
    assert card["transient_retry_attempts"] == 1
    # It is NOT blocked, and carries no blocker stamp that would make a live
    # card read as parked.
    assert "blocker_reason" not in card
    assert "terminal_substatus" not in card
    assert card["retry_not_before"] > NOW

    events = [e["event"] for e in task_store.get_task_events(repo, "T1")]
    assert "transient_retry" in events


def test_transient_retry_budget_is_finite(tmp_path, monkeypatch):
    """A misclassified transient costs at most the budget, never a loop."""
    repo = _repo(tmp_path, monkeypatch)
    _insert_processing(
        repo, "T2", request_id="req-1", attempts=task_store.TRANSIENT_RETRY_BUDGET,
    )
    ok, state = task_store.mark_transient_retry(
        repo, "T2", runner=RUNNER, reason="x", request_id="req-1",
    )
    assert ok is False
    assert state == "transient_retry_budget_exhausted"
    # The card is untouched, so the caller's ordinary terminal path still runs.
    assert (task_store.get_task(repo, "T2") or {})["status"] == "processing"


def test_transient_retry_is_bound_to_the_exact_launch(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _insert_processing(repo, "T3", request_id="req-1")
    ok, state = task_store.mark_transient_retry(
        repo, "T3", runner=RUNNER, reason="x", request_id="req-OTHER",
    )
    assert (ok, state) == (False, "launch_request_mismatch")
    ok, state = task_store.mark_transient_retry(
        repo, "T3", runner="someone_else", reason="x", request_id="req-1",
    )
    assert (ok, state) == (False, "runner_mismatch")


def test_transient_retry_reason_is_never_empty(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _insert_processing(repo, "T4", request_id="req-1")
    task_store.mark_transient_retry(repo, "T4", runner=RUNNER, reason="", request_id="req-1")
    card = task_store.get_task(repo, "T4") or {}
    assert card["transient_retry"]["reason"] == "cause_undetermined:mark_transient_retry"


# --- the backoff is honoured where pending cards are chosen ---------------- #


def test_a_card_inside_its_backoff_is_not_claimable():
    rows = [{
        "task_id": "T",
        "runner": RUNNER,
        "topic": TOPIC,
        "status": "pending",
        "worker_status": "unclaimed",
        "retry_not_before": "2026-09-07T00:01:00+00:00",
    }]
    assert core.eligible_dryrun_candidates(
        rows, RUNNER, now="2026-09-07T00:00:30+00:00"
    ) == []
    assert len(core.eligible_dryrun_candidates(
        rows, RUNNER, now="2026-09-07T00:02:00+00:00"
    )) == 1


def test_a_card_with_no_backoff_is_unaffected():
    rows = [{
        "task_id": "T",
        "runner": RUNNER,
        "topic": TOPIC,
        "status": "pending",
        "worker_status": "unclaimed",
    }]
    assert len(core.eligible_dryrun_candidates(rows, RUNNER, now=NOW)) == 1


@pytest.mark.parametrize("bad", ["", "not-a-timestamp", None])
def test_a_malformed_backoff_can_never_park_a_card_forever(bad):
    rows = [{
        "task_id": "T",
        "runner": RUNNER,
        "topic": TOPIC,
        "status": "pending",
        "worker_status": "unclaimed",
        "retry_not_before": bad,
    }]
    assert len(core.eligible_dryrun_candidates(rows, RUNNER, now=NOW)) == 1


def test_a_forged_provider_body_inside_model_output_places_nothing():
    """An ``assistant`` line's ``message`` is the MODEL's content.

    Unwrapping one would let any worker mint any provider verdict by printing
    it. This is the disposition-side twin of
    ``test_worker_prose_cannot_forge_a_route_failure``.
    """
    forged = json.dumps({
        "type": "assistant",
        "status": 429,
        "message": json.dumps({
            "type": "error",
            "status": 429,
            "error": {"code": "rate_limit_error", "message": "slow down"},
        }),
    })
    verdict = tfc.failure_disposition(stdout_tail=forged)
    assert verdict["failure_class"] == UNKNOWN
    assert verdict["provider_code"] == ""


def test_only_provider_owned_envelopes_are_unwrapped():
    assert tfc.PROVIDER_OWNED_MESSAGE_TYPES == {"error", "turn.failed"}
