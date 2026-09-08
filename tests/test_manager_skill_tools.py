"""Tests for the manager-bound skill registry lifecycle driver.

Covers the acceptance contract: the three manager operations drive the exact
``skill_registry`` lifecycle under a verified manager identity and persist only
through the store's public API; a proposal is stored and readable by a later
call; a duplicate proposal is refused without overwriting; one accepted evidence
entry does not activate while two from distinct actors do; and activation below
the threshold fails closed with the registry's own reason, leaving the stored
record unchanged.
"""

from __future__ import annotations

import inspect
import json
import sqlite3

import pytest

import aiworkhub.core as core
import aiworkhub.skill_registry as sr
from aiworkhub import learning_commit_store
from aiworkhub import manager_skill_tools as mst
from aiworkhub import skill_registry_store as store
from aiworkhub import task_store

BASE = {
    "identity": "commit-msg-check",
    "version": "1.0.0",
    "scope": "repository",
    "task_family": "commit",
    "path_or_symbol": "src/aiworkhub/skill_registry.py",
    "risk": "medium",
    "stage": "post-edit",
    "triggers": ["commit"],
    "confidence": 0.9,
}


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """Install a verified manager identity rooted at an isolated repo."""
    route = {
        "role": "manager",
        "provider": "claude",
        "repo": str(tmp_path),
        "manager_route": {"thread_id": "sess-123", "provider": "claude"},
    }
    monkeypatch.setattr(core, "manager_bootstrap", lambda: route)
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    return tmp_path


def _propose(**overrides):
    data = dict(BASE)
    data.update(overrides)
    return mst.propose(**data)


def _accept(actor_id, **overrides):
    args = {
        "identity": BASE["identity"],
        "version": BASE["version"],
        "source": f"src-{actor_id}",
        "outcome": "accepted",
        "actor_id": actor_id,
    }
    args.update(overrides)
    return mst.add_evidence(**args)


def test_propose_stores_a_record_readable_by_a_second_call(manager):
    result = _propose()
    assert result["ok"] is True
    assert result["identity"] == "commit-msg-check"
    assert result["version"] == "1.0.0"
    assert result["lifecycle_state"] == "proposed"
    assert result["digest"]

    # A later, independent call reads the same persisted record back.
    stored = store.get_record(manager, "commit-msg-check", "1.0.0")
    assert stored is not None
    assert stored.lifecycle_state is sr.LifecycleState.PROPOSED
    assert sr.skill_digest(stored) == result["digest"]
    # And a second lifecycle tool call loads it and advances it.
    assert _accept("actor-a")["ok"] is True


def test_propose_never_generates_lifecycle_or_evidence(manager):
    result = _propose()
    stored = store.get_record(manager, "commit-msg-check", "1.0.0")
    # The proposal is evidence-free with zero counters: nothing is inferred.
    assert result["lifecycle_state"] == "proposed"
    assert stored.evidence == ()
    assert stored.accepted_count == 0
    assert stored.negative_count == 0


def test_duplicate_propose_is_refused_without_overwriting(manager):
    assert _propose(confidence=0.9)["ok"] is True
    # A second proposal on the same identity/version is refused with the
    # registry's own immutability reason.
    duplicate = _propose(confidence=0.5)
    assert duplicate["ok"] is False
    assert duplicate["reason_code"].startswith("skill_registry.immutable")
    # The stored record is the untouched original, not the rejected 0.5 payload.
    stored = store.get_record(manager, "commit-msg-check", "1.0.0")
    assert stored.confidence == 0.9


def test_one_accepted_evidence_does_not_activate(manager):
    _propose()
    assert _accept("actor-a")["ok"] is True

    result = mst.activate(identity="commit-msg-check", version="1.0.0")
    assert result["ok"] is False
    assert result["reason_code"] == "skill_registry.insufficient_evidence"
    # Fail-closed: the stored record is left as a proposal.
    stored = store.get_record(manager, "commit-msg-check", "1.0.0")
    assert stored.lifecycle_state is sr.LifecycleState.PROPOSED


def test_two_accepted_from_same_actor_do_not_activate(manager):
    _propose()
    assert _accept("actor-a", source="first")["ok"] is True
    assert _accept("actor-a", source="second")["ok"] is True

    result = mst.activate(identity="commit-msg-check", version="1.0.0")
    assert result["ok"] is False
    assert result["reason_code"] == "skill_registry.insufficient_evidence"
    stored = store.get_record(manager, "commit-msg-check", "1.0.0")
    assert stored.lifecycle_state is sr.LifecycleState.PROPOSED


def test_two_accepted_from_different_actors_activate(manager):
    _propose()
    assert _accept("actor-a")["ok"] is True
    assert _accept("actor-b")["ok"] is True

    result = mst.activate(identity="commit-msg-check", version="1.0.0")
    assert result["ok"] is True
    assert result["lifecycle_state"] == "active"

    stored = store.get_record(manager, "commit-msg-check", "1.0.0")
    assert stored.lifecycle_state is sr.LifecycleState.ACTIVE
    assert stored.accepted_count == 2
    assert len(stored.evidence) == 2


def test_requires_verified_manager_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(
        core, "manager_bootstrap", lambda: {"role": "worker_or_unverified_client"}
    )
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    result = _propose()
    assert result["ok"] is False
    assert result["error"] == "verified_manager_identity_required"


def test_write_gate_closed_blocks_persistence(tmp_path, monkeypatch):
    route = {
        "role": "manager",
        "provider": "claude",
        "repo": str(tmp_path),
        "manager_route": {"thread_id": "sess-123"},
    }
    monkeypatch.setattr(core, "manager_bootstrap", lambda: route)
    monkeypatch.setattr(core, "writes_allowed", lambda: False)
    result = _propose()
    assert result["ok"] is False
    assert result["error"] == "write_gate_closed"
    # Nothing was persisted.
    assert store.get_record(tmp_path, "commit-msg-check", "1.0.0") is None


def test_persists_only_through_public_store_and_registry_api():
    source = inspect.getsource(mst)
    # No private store internals and no raw connection: persistence is through
    # put_record / advance_record / load_registry only.
    assert "_db_path" not in source
    assert "sqlite3.connect" not in source
    # No private registry state is touched.
    assert "_entries" not in source
    assert "_digest_index" not in source


# ---------------------------------------------------------------------------
# The second evidence source: an actor identity READ from a finished card
# ---------------------------------------------------------------------------


def _seed_card(
    root,
    task_id,
    *,
    runner="claude_sonnet-5",
    topic="code",
    completed_at="2026-01-01T00:10:00+00:00",
):
    """Write one card into the repository's REAL canonical task store."""
    task_store.initialize_repository(root)
    db = root / ".aiworkhub" / "tasking" / "task_queue.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id,runner,topic,mode,status,worker_status,priority,"
            "objective,card_json,created_at,updated_at,claimed_by,claimed_at,started_at,"
            "completed_at,origin_thread_id,archived_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                runner,
                topic,
                "",
                "done",
                "done",
                "normal",
                "objective",
                json.dumps({"task_id": task_id, "runner": runner, "topic": topic}),
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:10:00+00:00",
                runner,
                "2026-01-01T00:01:00+00:00",
                "2026-01-01T00:01:00+00:00",
                completed_at,
                "thread-1",
                "",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _loaded(root, identity="commit-msg-check", version="1.0.0"):
    return store.load_registry(root).get(identity, version)


def test_task_evidence_derives_its_actor_from_the_card_not_the_caller(manager):
    _propose()
    _seed_card(manager, "AIWORKHUB_01085_V1", runner="claude_sonnet-5")
    result = mst.add_task_evidence(
        identity=BASE["identity"],
        version=BASE["version"],
        task_id="AIWORKHUB_01085_V1",
        outcome="accepted",
        note="fixed the ledger attribution",
    )
    assert result["ok"] is True

    record = _loaded(manager)
    entry = record.evidence[-1]
    # The identity was read from the card's runner, never supplied by the caller,
    # and the card id is recorded as the verifiable provenance anchor.
    assert entry.actor_id == "worker.claude.sonnet.5"
    assert entry.authority is sr.AuthorityRole.WORKER
    assert entry.source == "AIWORKHUB_01085_V1"


def test_a_quality_review_card_yields_a_reviewer_actor(manager):
    _propose()
    _seed_card(manager, "QR_1", runner="codex_cli", topic="quality_review")
    assert mst.add_task_evidence(
        identity=BASE["identity"], version=BASE["version"], task_id="QR_1", outcome="accepted"
    )["ok"] is True
    assert _loaded(manager).evidence[-1].actor_id == "reviewer.codex.cli"


def test_a_card_derived_actor_can_never_collide_with_the_manager(manager):
    # The role token is emitted first by canonical_actor_id, so a card-derived
    # identity is structurally distinct from the manager's own -- even when the
    # runner is literally named after a manager.
    _propose()
    _seed_card(manager, "T_ODD", runner="claude_manager_7e6e8a47")
    assert mst.add_task_evidence(
        identity=BASE["identity"], version=BASE["version"], task_id="T_ODD", outcome="accepted"
    )["ok"] is True
    actor = _loaded(manager).evidence[-1].actor_id
    assert actor == "worker.claude.7e6e8a47"
    assert sr.canonical_actor_id(actor) != sr.canonical_actor_id("manager.claude.7e6e8a47")


def test_two_cards_run_by_one_runner_are_one_actor(manager):
    # The runner is the actor; the card is only the provenance anchor. Filing
    # two cards from one runner must not manufacture independence.
    _propose()
    _seed_card(manager, "T_A", runner="claude_sonnet-5")
    _seed_card(manager, "T_B", runner="claude_sonnet-5")
    for task_id in ("T_A", "T_B"):
        assert mst.add_task_evidence(
            identity=BASE["identity"],
            version=BASE["version"],
            task_id=task_id,
            outcome="accepted",
        )["ok"] is True

    record = _loaded(manager)
    assert record.accepted_count == 2
    assert sr.independent_accepted_evidence_count(record) == 1
    assert mst.activate(identity=BASE["identity"], version=BASE["version"])["ok"] is False


def test_activation_is_reachable_through_the_new_source(manager):
    # The ordering that makes the fix safe: a second legitimate evidence source
    # exists, so canonicalizing provenance narrows what counts as independent
    # without making activation unsatisfiable.
    _propose()
    # One manager entry -- and a second manager entry under a DIFFERENT spelling
    # of the same identity, which no longer buys independence.
    _accept("manager.claude.7e6e8a47")
    _accept("claude_manager_7e6e8a47")
    assert sr.independent_accepted_evidence_count(_loaded(manager)) == 1
    denied = mst.activate(identity=BASE["identity"], version=BASE["version"])
    assert denied["ok"] is False
    assert denied["reason_code"] == "skill_registry.insufficient_evidence"

    # A card-derived worker is a genuinely distinct actor, and the gate opens.
    _seed_card(manager, "T_REAL", runner="claude_sonnet-5")
    assert mst.add_task_evidence(
        identity=BASE["identity"], version=BASE["version"], task_id="T_REAL", outcome="accepted"
    )["ok"] is True
    assert sr.independent_accepted_evidence_count(_loaded(manager)) == 2

    activated = mst.activate(identity=BASE["identity"], version=BASE["version"])
    assert activated["ok"] is True
    assert activated["lifecycle_state"] == "active"
    # And the activation survives a reload, so it was not a demoted read.
    assert _loaded(manager).lifecycle_state is sr.LifecycleState.ACTIVE


@pytest.mark.parametrize(
    "task_id,seed",
    [
        ("MISSING", None),
        ("UNFINISHED", {"completed_at": ""}),
        ("NO_RUNNER", {"runner": ""}),
    ],
)
def test_task_evidence_fails_closed_without_a_verified_actor(manager, task_id, seed):
    _propose()
    task_store.initialize_repository(manager)
    if seed is not None:
        _seed_card(manager, task_id, **seed)
    result = mst.add_task_evidence(
        identity=BASE["identity"], version=BASE["version"], task_id=task_id, outcome="accepted"
    )
    assert result["ok"] is False
    assert result["reason_code"] == "skill_registry.invalid_evidence"
    # Nothing was appended: no actor, no evidence.
    assert _loaded(manager).evidence == ()


def test_audit_reports_an_unverified_active_record_without_repairing_it(manager):
    _propose()
    _accept("manager.claude.7e6e8a47")
    _seed_card(manager, "T_REAL", runner="claude_sonnet-5")
    mst.add_task_evidence(
        identity=BASE["identity"], version=BASE["version"], task_id="T_REAL", outcome="accepted"
    )
    mst.activate(identity=BASE["identity"], version=BASE["version"])

    report = mst.audit()
    assert report["ok"] is True
    assert report["unverified_active"] == []
    entry = report["active_records"][0]
    assert entry["independent_accepted_actors"] == 2
    assert entry["actor_ids"] == ["manager.claude.7e6e8a47", "worker.claude.sonnet.5"]
    assert entry["verified"] is True


def test_task_evidence_uses_only_public_store_and_task_api():
    source = inspect.getsource(mst)
    assert "sqlite3.connect" not in source
    assert "_decode_task_card" not in source
    assert "task_store.get_task" in source


# ---------------------------------------------------------------------------
# A caller-typed actor may not impersonate a runner.
#
# Canonicalization closed the ACCIDENTAL half of self-certification: two
# spellings of one manager are one actor. This closes the DELIBERATE half. The
# derived namespace -- worker/reviewer/coordinator/agent/owner -- is read off a
# task card's own runner and can never be typed, so the free-text surface can
# name only a manager and every entry it produces canonicalizes to one actor.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "impersonation",
    [
        "worker.claude.sonnet.5",   # exactly what the derived path would produce
        "reviewer.codex.cli",
        "claude_worker_7e6e8a47",   # the same claim under a different spelling
        "coordinator.claude",
        "agent.gpt",
        "owner",
    ],
)
def test_a_typed_actor_may_not_claim_a_derived_role(manager, impersonation):
    _propose()
    result = _accept(impersonation)

    assert result["ok"] is False
    assert result["reason_code"] == "skill_registry.invalid_evidence"
    assert "reserved role token" in result["error"]
    # Nothing was appended: a refused impersonation leaves no trace of itself.
    assert _loaded(manager).evidence == ()


def test_a_typed_actor_cannot_impersonate_the_runner_of_a_real_card(manager):
    """The exact attack: type the identity the card would have produced.

    A manager who has already filed one manager entry needs exactly one more
    independent actor to reach the floor. The runner of a real card is the
    obvious string to type, and it is the one string that must not work.
    """
    _propose()
    _seed_card(manager, "T_REAL", runner="claude_sonnet-5")
    _accept("manager.claude.7e6e8a47")

    forged = _accept("worker.claude.sonnet.5", source="T_REAL")

    assert forged["ok"] is False
    record = _loaded(manager)
    assert sr.independent_accepted_evidence_count(record) == 1
    assert mst.activate(identity=BASE["identity"], version=BASE["version"])["ok"] is False

    # The same actor, DERIVED from the same card, is accepted -- so the rule
    # narrows who may assert an identity, never who may hold one.
    assert mst.add_task_evidence(
        identity=BASE["identity"], version=BASE["version"],
        task_id="T_REAL", outcome="accepted",
    )["ok"] is True
    assert sr.independent_accepted_evidence_count(_loaded(manager)) == 2


def test_the_manager_surface_still_takes_a_manager_identity(manager):
    """The reservation is on the derived roles only; nothing else is narrowed."""
    _propose()
    assert _accept("manager.claude.7e6e8a47")["ok"] is True
    assert _accept("actor-a", source="second")["ok"] is True
    assert len(_loaded(manager).evidence) == 2


def test_record_decision_evidence_takes_no_actor_argument():
    """The security property is structural: this surface cannot be TOLD who acted."""
    parameters = inspect.signature(mst.record_decision_evidence).parameters
    assert "actor_id" not in parameters
    assert "actor" not in parameters
    assert set(parameters) == {"repo_root", "task_id", "request_id", "outcome", "note"}
    with pytest.raises(TypeError):
        mst.record_decision_evidence(  # type: ignore[call-arg]
            ".", task_id="T", request_id="R", outcome="accepted",
            actor_id="worker.claude.sonnet.5",
        )


# ---------------------------------------------------------------------------
# The automatic evidence row: a decision produces evidence without being asked.
# ---------------------------------------------------------------------------


def _record_selection(root, task_id, request_id, *, identity=None, version=None):
    """Persist the receipt a launcher writes when it injects a packet."""
    return store.record_selection(
        root,
        task_id=task_id,
        request_id=request_id,
        packet={
            "version": "1",
            "skills": [{
                "identity": identity or BASE["identity"],
                "version": version or BASE["version"],
                "digest": "d",
                "procedure_steps": ["read the fact off the object"],
            }],
        },
    )


def _decide(root, task_id, request_id, outcome):
    return mst.record_decision_evidence(
        root, task_id=task_id, request_id=request_id, outcome=outcome
    )


def test_an_accept_decision_appends_one_derived_evidence_row_per_injected_skill(manager):
    _propose()
    _seed_card(manager, "T_DEC", runner="claude_sonnet-5")
    _record_selection(manager, "T_DEC", "req-1")

    result = _decide(manager, "T_DEC", "req-1", "accepted")

    assert result["ok"] is True
    assert result["actor_source"] == "task_card_runner"
    assert result["actor_id"] == "worker.claude.sonnet.5"
    assert [row["identity"] for row in result["recorded"]] == [BASE["identity"]]

    entry = _loaded(manager).evidence[-1]
    assert entry.actor_id == "worker.claude.sonnet.5"
    assert entry.outcome is sr.EvidenceOutcome.ACCEPTED
    assert entry.source == "req-1"


def test_a_reject_decision_records_negative_evidence(manager):
    """The card's OWN adjudicated decision is the sign of the evidence."""
    _propose()
    _seed_card(manager, "T_REJ", runner="claude_sonnet-5")
    _record_selection(manager, "T_REJ", "req-2")

    result = _decide(manager, "T_REJ", "req-2", "rejected")

    assert result["ok"] is True
    assert result["evidence_outcome"] == "negative"
    entry = _loaded(manager).evidence[-1]
    assert entry.outcome is sr.EvidenceOutcome.NEGATIVE
    # And that unresolved negative evidence blocks activation, as it must.
    assert mst.activate(identity=BASE["identity"], version=BASE["version"])["ok"] is False


def test_a_card_keyed_receipt_still_resolves_at_the_decision(manager):
    """The launcher mints the request id AFTER it builds the context bundle.

    ``process_launcher`` calls ``collect_project_context`` and only then does
    ``request_id = uuid.uuid4().hex``, so the selection site cannot key its
    receipt by request. A card-keyed receipt must therefore still be found by a
    decision that names the request that actually ran -- and the request id ends
    up where it matters, as the evidence row's own provenance anchor.
    """
    _propose()
    _seed_card(manager, "T_CARDKEY", runner="claude_sonnet-5")
    _record_selection(manager, "T_CARDKEY", "")

    result = _decide(manager, "T_CARDKEY", "0f3ab12c", "accepted")

    assert result["ok"] is True
    assert result["recorded"][0]["identity"] == BASE["identity"]
    assert _loaded(manager).evidence[-1].source == "0f3ab12c"
    assert _loaded(manager).evidence[-1].actor_id == "worker.claude.sonnet.5"


def test_a_decision_with_no_selection_receipt_records_nothing(manager):
    """A card that received no packet contributes no evidence, and says so."""
    _propose()
    _seed_card(manager, "T_NONE", runner="claude_sonnet-5")

    result = _decide(manager, "T_NONE", "req-3", "accepted")

    assert result["ok"] is True
    assert result["reason"] == "no_selection_receipt_for_this_card"
    assert result["recorded"] == []
    assert _loaded(manager).evidence == ()


def test_a_repeated_finalization_cannot_inflate_the_evidence_count(manager):
    _propose()
    _seed_card(manager, "T_IDEM", runner="claude_sonnet-5")
    _record_selection(manager, "T_IDEM", "req-4")

    first = _decide(manager, "T_IDEM", "req-4", "accepted")
    second = _decide(manager, "T_IDEM", "req-4", "accepted")

    assert first["recorded"][0]["idempotent"] is False
    assert second["recorded"][0]["idempotent"] is True
    assert len(_loaded(manager).evidence) == 1


def test_an_outcome_that_is_not_an_adjudication_is_refused(manager):
    _propose()
    _seed_card(manager, "T_BAD", runner="claude_sonnet-5")
    _record_selection(manager, "T_BAD", "req-5")

    result = _decide(manager, "T_BAD", "req-5", "inconclusive")

    assert result["ok"] is False
    assert result["reason"] == "decision_outcome_not_adjudicated"
    assert result["allowed_outcomes"] == ["accepted", "rejected"]
    assert _loaded(manager).evidence == ()


def test_the_loop_reaches_activation_from_two_cards_run_by_two_runners(manager):
    """End to end, with nothing typed: two decisions, two runners, one activation."""
    _propose()
    for task_id, runner in (("T_ONE", "claude_sonnet-5"), ("T_TWO", "codex_gpt-5.5")):
        _seed_card(manager, task_id, runner=runner)
        _record_selection(manager, task_id, f"req-{task_id}")
        assert _decide(manager, task_id, f"req-{task_id}", "accepted")["ok"] is True

    record = _loaded(manager)
    assert sr.independent_accepted_evidence_count(record) == 2
    assert mst.activate(identity=BASE["identity"], version=BASE["version"])["ok"] is True


def test_two_decisions_on_cards_run_by_one_runner_are_one_actor(manager):
    """The runner is the actor; two cards are two anchors, not two contributors."""
    _propose()
    for task_id in ("T_S1", "T_S2"):
        _seed_card(manager, task_id, runner="claude_sonnet-5")
        _record_selection(manager, task_id, f"req-{task_id}")
        _decide(manager, task_id, f"req-{task_id}", "accepted")

    record = _loaded(manager)
    assert record.accepted_count == 2
    assert sr.independent_accepted_evidence_count(record) == 1
    assert mst.activate(identity=BASE["identity"], version=BASE["version"])["ok"] is False


def test_a_decision_evidence_row_never_activates_a_skill(manager):
    """Nothing on this path may transition a lifecycle. Activation stays manual."""
    _propose()
    for task_id, runner in (("T_A1", "claude_sonnet-5"), ("T_A2", "codex_gpt-5.5")):
        _seed_card(manager, task_id, runner=runner)
        _record_selection(manager, task_id, f"req-{task_id}")
        _decide(manager, task_id, f"req-{task_id}", "accepted")

    assert _loaded(manager).lifecycle_state is sr.LifecycleState.PROPOSED


# ---------------------------------------------------------------------------
# Usage statistics: what the owner asked for.
# ---------------------------------------------------------------------------


def test_usage_reports_the_exact_reason_a_skill_is_not_injectable(manager):
    _propose()
    _seed_card(manager, "T_U1", runner="claude_sonnet-5")
    _record_selection(manager, "T_U1", "req-u1")
    _decide(manager, "T_U1", "req-u1", "accepted")

    report = mst.usage()
    entry = report["skills"][0]

    assert report["ok"] is True
    assert entry["identity"] == BASE["identity"]
    assert entry["proposals"] == 1
    assert entry["evidence_by_outcome"] == {"accepted": 1, "negative": 0}
    assert entry["distinct_actors"] == 1
    assert entry["actor_ids"] == ["worker.claude.sonnet.5"]
    assert entry["injectable"] is False
    assert entry["injectable_reason"] == "activation_evidence_below_two_distinct_actors"
    assert entry["injected_cards"] == 1
    assert report["totals"]["selection_receipts"] == 1


def test_usage_does_not_call_a_proposed_record_injectable(manager):
    """select() serves ACTIVE only; a proposal a worker can never receive is not injectable."""
    _propose()

    entry = mst.usage()["skills"][0]

    assert entry["stored_lifecycle_state"] == "proposed"
    assert entry["injectable"] is False
    assert entry["injectable_reason"] == "activation_evidence_below_two_distinct_actors"


def test_usage_names_unresolved_negative_evidence_as_the_reason(manager):
    _propose()
    for task_id, runner in (("T_N1", "claude_sonnet-5"), ("T_N2", "codex_gpt-5.5")):
        _seed_card(manager, task_id, runner=runner)
        _record_selection(manager, task_id, f"req-{task_id}")
        _decide(manager, task_id, f"req-{task_id}", "accepted")
    _seed_card(manager, "T_N3", runner="grok_4.6")
    _record_selection(manager, "T_N3", "req-T_N3")
    _decide(manager, "T_N3", "req-T_N3", "rejected")

    entry = mst.usage()["skills"][0]

    assert entry["evidence_by_outcome"] == {"accepted": 2, "negative": 1}
    assert entry["unresolved_negative_evidence"] == 1
    assert entry["injectable"] is False
    assert entry["injectable_reason"] == "unresolved_negative_evidence"


def test_usage_reports_an_activated_skill_as_injectable(manager):
    _propose()
    for task_id, runner in (("T_G1", "claude_sonnet-5"), ("T_G2", "codex_gpt-5.5")):
        _seed_card(manager, task_id, runner=runner)
        _record_selection(manager, task_id, f"req-{task_id}")
        _decide(manager, task_id, f"req-{task_id}", "accepted")
    assert mst.activate(identity=BASE["identity"], version=BASE["version"])["ok"] is True

    entry = mst.usage()["skills"][0]

    assert entry["injectable"] is True
    assert entry["injectable_reason"] == ""
    assert entry["distinct_actors"] == 2
    assert entry["injected_cards"] == 2
    assert mst.usage()["totals"]["injectable"] == 1


def test_usage_never_writes(manager):
    _propose()
    before = store.get_record(manager, BASE["identity"], BASE["version"])
    mst.usage()
    after = store.get_record(manager, BASE["identity"], BASE["version"])
    assert after == before
    assert mst.usage()["authority"]["writes"] == "none"


# ---------------------------------------------------------------------------
# propose(candidate_id=...): the mechanical dimensions are copied, not retyped.
# ---------------------------------------------------------------------------


def _mineable_corpus(root):
    """Three cards, three runners, three files, one recurring rule."""
    rule = (
        "the candidate must enumerate every emitting branch before the field "
        "guarantee is treated as delivered to a reader"
    )
    for index in range(3):
        task_id = f"mined{index}"
        card = {
            "task_id": task_id,
            "runner": f"worker{index}",
            "allowed_writes": [f"src/aiworkhub/mod{index}.py"],
            "review_feedback": {
                "schema_id": "aiworkhub.rework_feedback_delta.v1",
                "instruction": f"{rule} observed in round {index}",
                "predecessor_request_id": f"req-{task_id}",
            },
        }
        _seed_card(root, task_id, runner=f"worker{index}")
        conn = sqlite3.connect(str(root / ".aiworkhub" / "tasking" / "task_queue.sqlite"))
        try:
            # The correction-record reader joins the ledger, so the ledger table
            # must exist. Use the store's own schema rather than restating it.
            conn.executescript(learning_commit_store._SCHEMA)
            conn.execute(
                "UPDATE tasks SET card_json=? WHERE task_id=?",
                (json.dumps(card), task_id),
            )
            conn.commit()
        finally:
            conn.close()


def test_propose_copies_the_mechanical_dimensions_from_a_mined_candidate(manager):
    _mineable_corpus(manager)
    candidate = mst.mine()["candidates"][0]
    draft = candidate["proposal_draft"]

    result = mst.propose(
        candidate_id=candidate["candidate_id"],
        task_family="bugfix",
        triggers=["clean_verdict_without_input_check"],
        applicability=["quality_gate"],
        confidence=0.6,
        procedure_steps=["resolve every row before contradicting a reviewer"],
    )

    assert result["ok"] is True, result
    assert result["identity"] == draft["identity"] == candidate["candidate_id"]
    assert result["version"] == draft["version"]

    stored = store.get_record(manager, result["identity"], result["version"])
    assert stored.scope.value == draft["scope"]
    assert stored.risk.value == draft["risk"]
    assert stored.stage == draft["stage"]
    assert stored.path_or_symbol == draft["path_or_symbol"]
    # Only the judgement half came from the caller.
    assert stored.task_family == "bugfix"
    assert stored.triggers == ("clean_verdict_without_input_check",)
    assert stored.confidence == 0.6


def test_a_mechanical_field_that_disagrees_with_the_candidate_is_refused(manager):
    _mineable_corpus(manager)
    candidate_id = mst.mine()["candidates"][0]["candidate_id"]

    result = mst.propose(
        candidate_id=candidate_id,
        identity="something.else",
        task_family="bugfix",
        triggers=["clean_verdict_without_input_check"],
        confidence=0.6,
    )

    assert result["ok"] is False
    assert "derived from candidate" in result["error"]
    assert store.get_record(manager, "something.else", "0.1.0") is None


def test_an_unknown_candidate_id_is_refused_and_names_what_was_mined(manager):
    _mineable_corpus(manager)

    result = mst.propose(
        candidate_id="mined.not.a.real.candidate",
        task_family="bugfix",
        triggers=["clean_verdict_without_input_check"],
        confidence=0.6,
    )

    assert result["ok"] is False
    assert "unknown_candidate_id" in result["error"]


def test_the_full_form_still_works_without_a_candidate(manager):
    """The compatibility path: every field supplied, nothing resolved."""
    assert _propose()["ok"] is True
    assert store.get_record(manager, BASE["identity"], BASE["version"]) is not None


# ---------------------------------------------------------------------------
# The tool DESCRIPTION carries the closed vocabularies, so a guess is refused
# before a turn is spent on it -- and cannot drift away from the real sets.
# ---------------------------------------------------------------------------


def _propose_description() -> str:
    from aiworkhub import server

    return server.aiworkhub_manager_skill_propose.__doc__ or ""


@pytest.mark.parametrize(
    "field,tokens",
    [
        ("task_family", sr.SKILL_TASK_FAMILIES),
        ("stage", sr.SKILL_STAGES),
        ("triggers", sr.SKILL_TRIGGERS),
        ("applicability", sr.SKILL_APPLICABILITY),
    ],
)
def test_every_closed_vocabulary_token_is_named_in_the_tool_description(field, tokens):
    description = _propose_description()
    missing = sorted(token for token in tokens if token not in description)
    assert not missing, f"{field} tokens absent from the tool description: {missing}"


def test_the_enum_vocabularies_are_named_in_the_tool_description():
    description = _propose_description()
    for enum in (sr.SkillScope, sr.RiskLevel):
        for member in enum:
            assert member.value in description, member.value


def test_the_evidence_outcome_vocabulary_is_named_in_its_own_tool_description():
    from aiworkhub import server

    description = server.aiworkhub_manager_skill_add_evidence.__doc__ or ""
    for member in sr.EvidenceOutcome:
        assert member.value in description, member.value
    # And the reserved role tokens, so an impersonation is refused before a call.
    for token in sr.ACTOR_ROLE_TOKENS - {"manager"}:
        assert token in description, token


def test_the_semver_and_identity_shapes_are_named_in_the_tool_description():
    description = _propose_description()
    assert "MAJOR.MINOR.PATCH" in description
    assert sr._IDENTITY_RE.pattern in description


def test_the_usage_tool_names_every_reason_it_can_return():
    from aiworkhub import server

    description = server.aiworkhub_manager_skill_usage.__doc__ or ""
    for reason in (
        "lifecycle_state_is_retired",
        "unresolved_negative_evidence",
        "activation_evidence_below_two_distinct_actors",
        "lifecycle_state_is_proposed_not_active",
    ):
        assert reason in description, reason
