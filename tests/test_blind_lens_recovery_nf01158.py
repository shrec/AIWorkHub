"""NF-2026-01158: a process_limit-only reviewer lens must be RECOVERABLE.

The measured deadlock. A reviewer returns nothing but ``process_limit``
findings, so ``fold_quality_verdict`` marks its lens ``reviewer_could_not_inspect``
and ``accept_review`` refuses. Relaunching did not help: the reservation asked
``existing_lens_reviewer`` whether this lens already had a reviewer, that lookup
returned the SAME blind reviewer's sealed receipt, and the launch handed it back
with ``reused_existing_reviewer`` True without starting anything. The supplemental
round could not run either -- an accepted reviewer has no live workspace, so no
sealed packet is recoverable for it -- which left the chain with no exit but a
rerun of a candidate whose bytes were never in question.

The chosen fix is (a)-fresh-reviewer rather than (a)-retain-the-packet, because
the loop closes at the REUSE decision: retaining the packet would add durable
state and still leave every relaunch returning the blind reviewer. One
predicate, ``quality_evidence.reviewer_card_lens_verdict_is_blind``, disqualifies
a blind sealed reviewer from the reuse POOL inside
``review_orchestrator.existing_lens_reviewer`` -- the single reuse decision that
the launcher's reservation (``_reserve_quality_reviewer_attempt``), the launch
receipt path (``launch_quality_reviewer``) and the chain's adoption
(``ReviewOrchestrator``) all read their answer from. Filtering the pool rather
than annotating the result is deliberate: an eligibility rule applied to the
emitted flag instead of the candidate set is how the chosen row and the parallel
predicate came to disagree in NF-2026-01209.

Nothing here can weaken a gate. Refusing a reuse can only buy MORE review, and
a blind reviewer still never counts as a pass -- which
``tests/test_blind_reviewer_never_passes.py`` pins independently.
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import process_launcher
from aiworkhub import process_launcher_accept_review as par
from aiworkhub import quality_evidence as qe
from aiworkhub import review_orchestrator
from aiworkhub import task_store


RUNNER = "deepseek_v4-pro"
TOPIC = "quality_review"
ADAPTER = "vscode_lm"
PACKET = "c" * 64
EPOCH = "2"

_ATTEMPT = {
    "target_request_id": "target-req",
    "target_task_id": "TARGET_TASK",
    "lens": "correctness",
    "target_claim_epoch": EPOCH,
}
_LOOKUP = {
    "target_task_id": "TARGET_TASK",
    "target_request_id": "target-req",
    "claim_epoch": EPOCH,
    "lens": "correctness",
}


# ---------------------------------------------------------------------------
# Fixtures. Every reviewer report below is built the way the server seals one,
# because the point of the fix is that the REUSE decision now reads the same
# sealed report the accept fold judges -- a fixture that invented a shortcut
# field would be testing a path production does not have.
# ---------------------------------------------------------------------------
def _process_limit_finding(finding_id: str = "pl-1") -> dict[str, str]:
    """The reviewer's own statement that it was prevented from inspecting."""
    return {
        "id": finding_id,
        "severity": qe.SEVERITY_LOW,
        "disposition": qe.FINDING_DISPOSITION_PROCESS_LIMIT,
        "summary": "reviewer could not inspect the packet",
        "evidence": "no file-read tool was available for the packet path",
    }


def _report(
    lens: str = "correctness",
    *,
    blind: bool = False,
    provider: str = "reviewer-b",
    **extra: object,
) -> dict[str, object]:
    return {
        "lens": lens,
        "provider": provider,
        "read_only": True,
        "can_mutate_repo": False,
        "findings": [_process_limit_finding()] if blind else [],
        **extra,
    }


def _sealed_card(
    task_id: str,
    request_id: str,
    *,
    blind: bool,
    lens: str = "correctness",
    **card: object,
) -> dict[str, object]:
    """A reviewer card carrying a receipt ``_verified_lens_report`` accepts.

    Every field is one the server writes: the five-field target identity, the
    packet binding, the reviewer's own task binding, the read-only authority and
    the single-submission counters. The ONLY difference between the blind and
    the sighted card is the disposition of the findings in the sealed report.
    """
    return {
        "task_id": task_id,
        "terminal_review": {
            "evidence": {
                "quality_review": {
                    "lens": lens,
                    "packet_sha256": PACKET,
                    "target_request_id": "target-req",
                    "target_task_id": "TARGET_TASK",
                    "target_claim_epoch": EPOCH,
                },
                "quality_review_receipt": {
                    "packet_sha256": PACKET,
                    "target": {
                        "request_id": "target-req",
                        "task_id": "TARGET_TASK",
                        "claim_epoch": EPOCH,
                    },
                    "reviewer": {"request_id": request_id, "task_id": task_id},
                    "report": _report(lens, blind=blind),
                    "authority": {
                        "process_identity_verified": True,
                        "audit_verified": True,
                        "terminal_state": "review_ready",
                    },
                    "submission_id": "d" * 64,
                    "physical_submission_count": 1,
                    "logical_submission_count": 1,
                },
            },
        },
        **card,
    }


def _manager(tmp_path: Path) -> process_launcher.ProcessManager:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "proc" / "process_events.jsonl",
        process_dir=tmp_path / "proc",
        isolation_enabled=False,
        collision_guard=lambda **_kwargs: {"returncode": 0},
    )


def _seal_finished_reviewer(
    manager: process_launcher.ProcessManager, request_id: str, task_id: str
) -> None:
    """One reviewer that reserved, ran and reached ``review_ready``.

    Only the reservation row carries the sealed ``quality_review_attempt``
    (NF-2026-01131); the terminal row that replaces it in the latest-row
    projection does not, and the seal projection is what recovers it.
    """
    manager._append_event({
        "request_id": request_id, "task_id": task_id, "runner": RUNNER,
        "topic": TOPIC, "adapter_id": ADAPTER, "state": "starting",
        "reservation_expires_at_epoch": time.time() + 120.0,
        "quality_review_attempt": dict(_ATTEMPT),
    })
    manager._append_event({
        "request_id": request_id, "task_id": task_id, "runner": RUNNER,
        "topic": TOPIC, "adapter_id": ADAPTER, "state": "review_ready",
    })


def _cards(monkeypatch: pytest.MonkeyPatch, cards: dict[str, dict]) -> None:
    original = review_orchestrator.task_store.get_task
    monkeypatch.setattr(
        review_orchestrator.task_store, "get_task",
        lambda repo, task_id: (
            cards[task_id] if task_id in cards else original(repo, task_id)
        ),
    )


def _reserve(
    manager: process_launcher.ProcessManager, reviewer_task_id: str
) -> dict[str, object]:
    return manager._reserve_quality_reviewer_attempt(
        reviewer_task_id=reviewer_task_id,
        runner=RUNNER,
        adapter_id=ADAPTER,
        target_request_id="target-req",
        target_task_id="TARGET_TASK",
        lens="correctness",
        model=None,
        timeout_seconds=1800,
        target_claim_epoch=EPOCH,
    )


# ---------------------------------------------------------------------------
# The one shared predicate.
# ---------------------------------------------------------------------------
def test_nf01158_blind_reviewer_lenses_is_one_rule_over_whole_lenses() -> None:
    # A LENS is blind when nothing inspected it, which is what makes recovery
    # possible at all: one sighted read of the same packet answers the blind one.
    assert qe.blind_reviewer_lenses([_report(blind=True)]) == {qe.LENS_CORRECTNESS}
    assert qe.blind_reviewer_lenses(
        [_report(blind=True), _report(blind=False)]
    ) == set()
    assert qe.blind_reviewer_lenses([_report(blind=False)]) == set()
    # A lens outside the judgment set, a non-mapping row and an empty list are
    # each nothing to say -- never a refusal invented out of a malformed input.
    assert qe.blind_reviewer_lenses([_report("does_it_run", blind=True)]) == set()
    assert qe.blind_reviewer_lenses(["not-a-report", None, 7]) == set()
    assert qe.blind_reviewer_lenses([]) == set()


def test_nf01158_card_predicate_reads_only_the_sealed_receipt() -> None:
    blind = _sealed_card("QR-BLIND", "rq-blind", blind=True)
    sighted = _sealed_card("QR-SIGHTED", "rq-sighted", blind=False)

    assert qe.reviewer_card_lens_verdict_is_blind(blind, "correctness") is True
    assert qe.reviewer_card_lens_verdict_is_blind(sighted, "correctness") is False
    # Another lens's report is not this lens's verdict, and an absent, malformed
    # or not-yet-sealed receipt is UNKNOWN rather than blind: only "blind"
    # changes anything, and all it can do is refuse a reuse.
    assert qe.reviewer_card_lens_verdict_is_blind(blind, "security") is False
    assert qe.reviewer_card_lens_verdict_is_blind({}, "correctness") is False
    assert qe.reviewer_card_lens_verdict_is_blind(None, "correctness") is False
    assert qe.reviewer_card_lens_verdict_is_blind(blind, "") is False


def test_nf01158_no_supplied_verdict_can_flip_the_reuse_predicate() -> None:
    # The forgery this must refuse: a card or report decorated with the words a
    # model or a manager would use to claim the review succeeded. The predicate
    # reads dispositions off the sealed report and nothing else, so every one of
    # these is still blind.
    forged = _sealed_card("QR-FORGED", "rq-forged", blind=True)
    receipt = forged["terminal_review"]["evidence"]["quality_review_receipt"]
    receipt["report"].update(
        passed=True, verdict="pass", inspected=True,
        reviewer_could_not_inspect=False, usage={"usage_observed": True},
    )
    receipt.update(passed=True, verdict="pass")
    forged.update(passed=True, quality_gate={"passed": True})

    assert qe.reviewer_card_lens_verdict_is_blind(forged, "correctness") is True
    # And there is no parameter through which a report could be supplied: the
    # card is the only input, so a caller wanting another answer has to make a
    # reviewer produce one.
    assert list(
        inspect.signature(qe.reviewer_card_lens_verdict_is_blind).parameters
    ) == ["card", "lens"]


# ---------------------------------------------------------------------------
# Reuse site 1: the launcher's reservation (process_launcher.py).
# ---------------------------------------------------------------------------
def test_nf01158_relaunch_after_a_blind_lens_reserves_a_fresh_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    _seal_finished_reviewer(manager, "rq-blind", "QR_BLIND")
    _cards(monkeypatch, {"QR_BLIND": _sealed_card("QR_BLIND", "rq-blind", blind=True)})

    # The exact call the deadlock was measured on: the reservation asks whether
    # this lens already has a reviewer and must now answer no.
    assert manager.existing_lens_reviewer(**_LOOKUP) is None

    fresh = _reserve(manager, "QR_RELAUNCH")
    assert fresh["ok"] is True
    assert "reused_existing_reviewer" not in fresh
    assert fresh["request_id"] != "rq-blind"
    # The fresh reservation is sealed to the same target, lens and claim epoch,
    # so it is the same question being asked again -- not a different review.
    sealed = manager._latest_by_request()[fresh["request_id"]]
    assert sealed["quality_review_attempt"] == dict(_ATTEMPT)
    assert sealed["task_id"] == "QR_RELAUNCH"


def test_nf01158_a_sighted_sealed_reviewer_is_still_reused_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The invariant the fix must not spend: a reviewer that really did inspect
    # this lens is still reused, so no second provider is ever bought for a
    # verdict sealed minutes earlier (NF-2026-01064/NF-2026-01131).
    manager = _manager(tmp_path)
    _seal_finished_reviewer(manager, "rq-sighted", "QR_SIGHTED")
    _cards(
        monkeypatch,
        {"QR_SIGHTED": _sealed_card("QR_SIGHTED", "rq-sighted", blind=False)},
    )

    found = manager.existing_lens_reviewer(**_LOOKUP)
    assert found is not None
    assert (found["request_id"], found["state"]) == ("rq-sighted", "sealed")

    reused = _reserve(manager, "QR_SECOND")
    assert reused["ok"] is True
    assert reused["reused_existing_reviewer"] is True
    assert reused["request_id"] == "rq-sighted"
    # No provider was bought: the relaunched task never reserved a slot.
    assert [
        event for event in manager._events()
        if event.get("task_id") == "QR_SECOND" and event.get("state") == "starting"
    ] == []


# ---------------------------------------------------------------------------
# Reuse site 2: the launch receipt path (process_launcher.py, ~8815).
# ---------------------------------------------------------------------------
class _FakeWorkspace:
    def as_metadata(self) -> dict[str, str]:
        return {
            "path": "/tmp/reviewer-ws",
            "home": "/tmp/reviewer-home",
            "repo": "/tmp/reviewer-repo",
            "request_id": "target-req",
        }


def _launchable(
    manager: process_launcher.ProcessManager,
    monkeypatch: pytest.MonkeyPatch,
    reviewer_cards: dict[str, dict],
) -> list[dict]:
    """Make ``launch_quality_reviewer`` runnable with fakes, and spy the spawn.

    The target is ``review_ready``, card creation/claim succeed, packet
    preparation is a fixed result and the provider spawn is recorded instead of
    run. Nothing here decides reuse; that stays the reservation's answer.
    """
    created: dict[str, dict] = {}

    def show(task_id: str) -> dict[str, object]:
        card = reviewer_cards.get(task_id) or created.get(task_id)
        if card is not None:
            return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}
        return {
            "returncode": 0,
            "stdout": json.dumps({
                "task_id": task_id,
                "claim_epoch": int(EPOCH),
                "terminal_review": {"substatus": "review_ready"},
            }),
            "stderr": "",
        }

    # ``_show_task`` is bound in ``__init__``, so the live instance attribute is
    # the seam -- patching the class default after construction reaches nothing.
    monkeypatch.setattr(manager, "_show_task", show)
    monkeypatch.setattr(
        process_launcher.core, "create_task",
        lambda **kwargs: (
            created.update({
                str(kwargs["task_id"]): {
                    "task_id": str(kwargs["task_id"]),
                    "runner": kwargs["runner"], "topic": kwargs["topic"],
                    "read_only": kwargs.get("read_only") is True,
                    "allowed_writes": list(kwargs.get("allowed_writes") or []),
                    "status": "pending", "worker_status": "unclaimed",
                }
            }),
            {"ok": True, "created": True, "task_id": str(kwargs["task_id"])},
        )[1],
    )

    def claim(
        _repo: Path, task_id: str, runner: str, topic: str, *, request_id: str
    ) -> dict[str, object]:
        card = {
            "task_id": task_id, "runner": runner, "topic": topic,
            "launch_request_id": request_id, "claim_epoch": int(EPOCH),
            "status": "processing", "worker_status": "claimed",
            "claimed_by": runner, "read_only": True, "allowed_writes": [],
        }
        created[task_id] = card
        return {
            "ok": True, "returncode": 0, "stdout": json.dumps(card), "stderr": "",
        }

    monkeypatch.setattr(process_launcher.task_engine, "claim_start_exact", claim)
    monkeypatch.setattr(
        manager, "_prepared_quality_review",
        lambda *_args, **_kwargs: {
            "ok": True,
            "prepared": {
                "worker_adapter_id": "independent_adapter",
                "workspace": _FakeWorkspace(),
                "changed_hashes": {"candidate.py": "a" * 64},
                "packet": {
                    "packet_sha256": PACKET, "target": {"claim_epoch": int(EPOCH)},
                },
            },
        },
    )

    spawns: list[dict] = []

    def spy(**kwargs: object) -> dict[str, object]:
        request_id = str(kwargs["reserved_request_id"])
        manager._append_event({
            "request_id": request_id, "task_id": kwargs["task_id"],
            "runner": kwargs["runner"], "topic": TOPIC,
            "adapter_id": kwargs["adapter_id"], "state": "running",
            "pid": os.getpid(),
            "pid_start_ticks": process_launcher._pid_start_ticks(os.getpid()),
        })
        spawns.append(dict(kwargs))
        return {"ok": True, "request_id": request_id, "state": "running"}

    monkeypatch.setattr(manager, "_launch_isolated", spy)
    return spawns


def _launch(
    manager: process_launcher.ProcessManager, reviewer_task_id: str
) -> dict[str, object]:
    return manager.launch_quality_reviewer(
        target_request_id="target-req",
        target_task_id="TARGET_TASK",
        reviewer_task_id=reviewer_task_id,
        runner=RUNNER,
        adapter_id=ADAPTER,
        lens="correctness",
        target_claim_epoch=EPOCH,
    )


def test_nf01158_launch_starts_a_real_provider_for_a_blind_lens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End to end through the surface a manager actually calls: the receipt is a
    # fresh deferred launch, not the blind reviewer's receipt, and a provider is
    # genuinely started for it.
    manager = _manager(tmp_path)
    _seal_finished_reviewer(manager, "rq-blind", "QR_BLIND")
    blind = _sealed_card("QR_BLIND", "rq-blind", blind=True)
    _cards(monkeypatch, {"QR_BLIND": blind})
    spawns = _launchable(manager, monkeypatch, {"QR_BLIND": blind})

    receipt = _launch(manager, "QR_RELAUNCH")

    assert receipt["ok"] is True
    assert "reused_existing_reviewer" not in receipt
    assert receipt["deferred"] is True
    assert receipt["task_id"] == "QR_RELAUNCH"
    assert receipt["request_id"] != "rq-blind"

    deadline = time.time() + 10
    while not spawns and time.time() < deadline:
        time.sleep(0.01)
    assert len(spawns) == 1
    assert spawns[0]["reserved_request_id"] == receipt["request_id"]
    assert spawns[0]["quality_review_binding"]["lens"] == "correctness"


def test_nf01158_launch_still_hands_back_a_sighted_reviewer_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    _seal_finished_reviewer(manager, "rq-sighted", "QR_SIGHTED")
    sighted = _sealed_card("QR_SIGHTED", "rq-sighted", blind=False)
    _cards(monkeypatch, {"QR_SIGHTED": sighted})
    spawns = _launchable(manager, monkeypatch, {"QR_SIGHTED": sighted})

    receipt = _launch(manager, "QR_SECOND")

    assert receipt["ok"] is True
    assert receipt["reused_existing_reviewer"] is True
    assert receipt["request_id"] == "rq-sighted"
    time.sleep(0.2)
    assert spawns == []


# ---------------------------------------------------------------------------
# Reuse site 3: the chain's adoption (review_orchestrator.py, ~3037).
# ---------------------------------------------------------------------------
def test_nf01158_chain_adoption_reads_the_same_single_reuse_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The chain does not own a reuse rule: ``_existing_lens_reviewer`` forwards
    # to the manager's ``existing_lens_reviewer``, which is the one place the
    # predicate lives. So a blind sealed reviewer is never adopted and never
    # reaches the ``reused_existing_reviewer`` branch, for the same reason the
    # reservation never mints its receipt.
    manager = _manager(tmp_path)
    _seal_finished_reviewer(manager, "rq-blind", "QR_BLIND")
    _cards(monkeypatch, {"QR_BLIND": _sealed_card("QR_BLIND", "rq-blind", blind=True)})

    chain = object.__new__(review_orchestrator.ReviewOrchestrator)
    chain.manager = manager
    action = SimpleNamespace(
        chain_id=1,
        lens="correctness",
        descriptor={"chain_identity": {
            "target_task_id": "TARGET_TASK",
            "target_request_id": "target-req",
            "claim_epoch": EPOCH,
        }},
    )

    assert chain._existing_lens_reviewer(action) is None

    # Same chain, same lookup, a reviewer that actually inspected: adopted.
    _seal_finished_reviewer(manager, "rq-sighted", "QR_SIGHTED")
    _cards(
        monkeypatch,
        {
            "QR_BLIND": _sealed_card("QR_BLIND", "rq-blind", blind=True),
            "QR_SIGHTED": _sealed_card("QR_SIGHTED", "rq-sighted", blind=False),
        },
    )
    found = chain._existing_lens_reviewer(action)
    assert found is not None
    assert found["task_id"] == "QR_SIGHTED"


def test_nf01158_one_patched_predicate_moves_every_reuse_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared-helper claim, made mechanical.

    Patching ONE function changes the reservation and the chain's adoption
    together, in both directions. That is only possible because neither holds a
    copy of the rule: both read the reuse decision that consults this predicate.
    """
    assert review_orchestrator.quality_evidence is qe

    manager = _manager(tmp_path)
    _seal_finished_reviewer(manager, "rq-sighted", "QR_SIGHTED")
    _cards(
        monkeypatch,
        {"QR_SIGHTED": _sealed_card("QR_SIGHTED", "rq-sighted", blind=False)},
    )
    chain = object.__new__(review_orchestrator.ReviewOrchestrator)
    chain.manager = manager
    action = SimpleNamespace(
        chain_id=1,
        lens="correctness",
        descriptor={"chain_identity": {
            "target_task_id": "TARGET_TASK",
            "target_request_id": "target-req",
            "claim_epoch": EPOCH,
        }},
    )

    # Sighted, so both sites reuse it.
    assert _reserve(manager, "QR_A")["reused_existing_reviewer"] is True
    assert chain._existing_lens_reviewer(action) is not None

    # One patched predicate, and both stop -- with the reviewer row, the ledger
    # and the card all completely unchanged. The chain is asked FIRST: the
    # reservation below mints a NEW live reviewer for this lens, which the chain
    # may then legitimately adopt. What must never come back either way is the
    # sighted reviewer the predicate now calls blind.
    monkeypatch.setattr(
        qe, "reviewer_card_lens_verdict_is_blind", lambda _card, _lens: True
    )
    assert chain._existing_lens_reviewer(action) is None
    assert "reused_existing_reviewer" not in _reserve(manager, "QR_B")
    adopted = chain._existing_lens_reviewer(action)
    assert (adopted or {}).get("task_id") != "QR_SIGHTED"


# ---------------------------------------------------------------------------
# Recovery: the fresh reviewer's sighted pass clears the lens.
# ---------------------------------------------------------------------------
def _check(check_id: str = "tests") -> qe.EvidenceCheck:
    return qe.EvidenceCheck(
        check_id=check_id, kind="test", status=qe.STATUS_PASSED, summary=""
    )


def _fold(reports: list[dict[str, object]], tier: str = qe.RISK_MEDIUM) -> dict:
    return qe.fold_quality_verdict(
        [_check()],
        risk_profile=qe.resolve_risk_profile(tier),
        reviewer_reports=reports,
        combined_tree_checks=[_check("union")],
        worker_provider="worker-a",
        human_approval=True,
    )


def _lens_row(verdict: dict, lens: str) -> dict:
    return next(row for row in verdict["lenses"] if row["lens"] == lens)


def test_nf01158_a_subsequent_inspecting_pass_clears_the_blind_lens() -> None:
    # The whole point of buying the fresh reviewer: its sighted read of the same
    # lens is a real attributable review of these exact bytes, so the blocker
    # the blind one produced is gone and acceptance can proceed.
    blind_only = _fold([_report(blind=True)])
    assert blind_only["passed"] is False
    assert (
        f"reviewer_could_not_inspect:{qe.LENS_CORRECTNESS}"
        in blind_only["blocking_evidence"]
    )
    assert (
        _lens_row(blind_only, qe.LENS_CORRECTNESS)["status"]
        == qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    )

    recovered = _fold([_report(blind=True), _report(blind=False)])
    assert recovered["passed"] is True
    assert recovered["blocking_evidence"] == []
    assert _lens_row(recovered, qe.LENS_CORRECTNESS)["status"] == qe.STATUS_PASSED


# ---------------------------------------------------------------------------
# accept_preview predicts exactly the refusal accept_review makes.
# ---------------------------------------------------------------------------
def _preview_repo(tmp_path: Path, cards: list[dict]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    assert task_store.initialize_repository(repo)["ok"]
    conn = sqlite3.connect(task_store.storage_readiness(repo).canonical_db)
    try:
        for card in cards:
            conn.execute(
                "INSERT INTO tasks (task_id, runner, topic, mode, status, "
                "worker_status, priority, objective, card_json, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    card["task_id"], "reviewer", TOPIC, "auto", "review", "done",
                    5, "review", json.dumps(card),
                    "2026-09-30T00:00:00+00:00", "2026-09-30T00:00:00+00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return repo


def _bound_card(task_id: str, lens: str, *, blind: bool) -> dict[str, object]:
    """A reviewer child bound to this parent request, sealed for one lens."""
    card = _sealed_card(task_id, f"rq-{task_id}", blind=blind, lens=lens)
    card["topic"] = TOPIC
    terminal = card["terminal_review"]
    terminal["substatus"] = "review_ready"
    return card


def _preview(tmp_path: Path, cards: list[dict], **overrides: object) -> dict:
    """``accept_preview`` over a real manager, store and process ledger."""
    repo = _preview_repo(tmp_path, cards)
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "proc" / "process_events.jsonl",
        process_dir=tmp_path / "proc",
        isolation_enabled=False,
        collision_guard=lambda **_kwargs: {"returncode": 0},
    )
    for card in cards:
        manager._append_event({
            "request_id": f"rq-{card['task_id']}", "task_id": card["task_id"],
            "runner": RUNNER, "topic": TOPIC, "adapter_id": ADAPTER,
            "state": "review_ready",
            "finished_at": "2026-09-30T01:00:00+00:00",
        })
    target = {
        "task_id": "TARGET_TASK",
        "risk_tier": "high",
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {"changed_paths": ["src/aiworkhub/process_launcher.py"]},
        },
    }
    manager._show_task = lambda task_id: {
        "returncode": 0, "stdout": json.dumps(target), "stderr": "",
    }
    return par.accept_preview(
        manager, "target-req", "TARGET_TASK",
        confirm_high_risk=True, **overrides,
    )


def test_nf01158_accept_preview_predicts_reviewer_could_not_inspect(
    tmp_path: Path
) -> None:
    # High tier requires correctness and security. The correctness reviewer is
    # usable -- so no ``required_reviewer_missing`` -- and blind, which is
    # exactly the state that used to preview clean and then refuse after a
    # combined tree and two validation runs had been paid for.
    preview = _preview(
        tmp_path,
        [
            _bound_card("QR_C", "correctness", blind=True),
            _bound_card("QR_S", "security", blind=False),
        ],
    )

    assert preview["ok"] is True and preview["evaluated"] is True
    assert preview["blocked"] is True
    assert preview["risk_profile"]["required_reviewer_lenses"] == [
        "correctness", "security"
    ]
    assert [row["error"] for row in preview["blockers"]] == [
        "reviewer_could_not_inspect:correctness"
    ]
    blocker = preview["blockers"][0]
    assert blocker["kind"] == "reviewer_could_not_inspect"
    assert blocker["lens"] == "correctness"
    assert blocker["kind"] in par.ACCEPT_BLOCKER_KINDS
    # Preview and acceptance produce the SAME string from the SAME classifier
    # over the same reports -- which is what "never disagree" has to mean.
    verdict = _fold(
        [_report("correctness", blind=True), _report("security", blind=False)],
        tier=qe.RISK_HIGH,
    )
    assert blocker["error"] in verdict["blocking_evidence"]


def test_nf01158_accept_preview_is_silent_when_the_reviewers_inspected(
    tmp_path: Path
) -> None:
    # The other direction of "exactly when": two sighted reviewers, nothing
    # cheap refusing, and no invented blocker.
    preview = _preview(
        tmp_path,
        [
            _bound_card("QR_C", "correctness", blind=False),
            _bound_card("QR_S", "security", blind=False),
        ],
    )

    assert preview["blocked"] is False
    assert preview["blockers"] == []
    verdict = _fold(
        [_report("correctness"), _report("security")], tier=qe.RISK_HIGH
    )
    assert not [
        blocker for blocker in verdict["blocking_evidence"]
        if str(blocker).startswith("reviewer_could_not_inspect:")
    ]


def _reviewer_row(lens: str, *, blind: bool) -> dict[str, object]:
    """One ``reviewer_evidence`` row carrying the sealed receipt the fold reads."""
    return {
        "task_id": f"QR_{lens}", "lens": lens, "request_id": f"rq-{lens}",
        "state": "review_ready", "usable": True,
        "receipt": {"report": _report(lens, blind=blind)},
    }


def _blind_evidence(item: object) -> bool:
    return str(item).startswith("reviewer_could_not_inspect:")


def test_nf01158_the_cheap_fold_is_gated_exactly_as_the_real_fold_is() -> None:
    # ``fold_quality_verdict`` blocks on a blind lens only where the tier runs an
    # attributable review (``review_active = bool(required_lenses)``); at a tier
    # that requires none, an unsolicited blind report is non-blocking noise.
    # Predicting a blocker there would be a refusal the real path never makes, so
    # the cheap fold is gated on the same condition -- checked here over the same
    # report, at a tier on each side of the gate.
    blind = [_reviewer_row("correctness", blind=True)]

    low = par.fold_accept_blockers(
        reviewers=blind, reviewer_request_ids=None,
        risk_profile=qe.resolve_risk_profile(qe.RISK_LOW),
        terminal_substatus="review_ready",
    )
    assert low["required_reviewer_lenses"] == []
    assert low["blockers"] == []
    assert not [
        item
        for item in _fold([_report(blind=True)], tier=qe.RISK_LOW)["blocking_evidence"]
        if _blind_evidence(item)
    ]

    medium = par.fold_accept_blockers(
        reviewers=blind, reviewer_request_ids=None,
        risk_profile=qe.resolve_risk_profile(qe.RISK_MEDIUM),
        terminal_substatus="review_ready",
    )
    assert [row["error"] for row in medium["blockers"]] == [
        "reviewer_could_not_inspect:correctness"
    ]
    assert [
        item
        for item in _fold([_report(blind=True)], tier=qe.RISK_MEDIUM)[
            "blocking_evidence"
        ]
        if _blind_evidence(item)
    ] == ["reviewer_could_not_inspect:correctness"]


def test_nf01158_preview_ignores_a_reviewer_the_manager_excluded(
    tmp_path: Path
) -> None:
    # The blind claim is made over the reports acceptance would actually READ.
    # An explicitly empty reviewer list excludes every reviewer, so the blind
    # one is not evidence here -- and the missing required lenses are, exactly
    # as they were before this change.
    preview = _preview(
        tmp_path,
        [_bound_card("QR_C", "correctness", blind=True)],
        reviewer_request_ids=[],
    )

    kinds = [row["kind"] for row in preview["blockers"]]
    assert "reviewer_could_not_inspect" not in kinds
    assert kinds == ["required_reviewer_missing", "required_reviewer_missing"]
