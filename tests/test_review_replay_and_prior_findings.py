"""Re-review of work already reviewed: what the packet carries forward, and
what never has to run twice.

Measured 2026-09-08 over this repository's own ledger: reviewer runs are 26% of
all provider input tokens; 916 of 1,141 reviewer launches target a SUCCESSOR
candidate; 970 of 2,803 changed paths in those successors are byte-identical to
the predecessor; and 55 successor candidate sets are identical as a whole and
were re-reviewed from scratch by every lens.

Two mechanisms answer that, and both are tested here against the properties
that outrank the saving:

* ``prior_review`` in the packet -- earlier reviewer findings, labelled as
  prior reviewer output, with a MECHANICAL sha256-derived line status; and
* hash-keyed replay -- an already-ingested lens report carried forward when the
  candidate bytes AND the contract are identical, keeping the original
  reviewer's receipt identity and never minting a new one.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    process_launcher,
    quality_evidence,
    quality_reviewer,
    review_lifecycle,
    review_orchestrator,
    task_store,
)

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
ROUTE = {"runner": "codex56_reviewer", "adapter_id": "codex_cli", "model": "gpt-5.6-sol"}
CANDIDATE = "b" * 64
MANIFEST_1 = "a" * 64
MANIFEST_2 = "c" * 64


def _route(_repo: Path, _task_id: str, _lens: str) -> dict[str, str]:
    return dict(ROUTE)


class _Manager:
    """One manager whose target card is readable for EVERY request episode.

    The chain-level replay decision needs the contract of the target it is
    registering, so a manager that can only answer for one request id could
    never produce a second chain with a matching contract identity -- which is
    exactly the shape a rework successor has.
    """

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.launches: list[dict] = []
        self.accepts: list[tuple[str, str]] = []
        self.archives: list[str] = []
        self.events: list[dict] = []
        self.targets: dict[str, dict] = {}
        self.status_results: dict[str, dict] = {}
        self.status_result: dict = {"ok": True, "state": "starting"}

    def launch_quality_reviewer(self, **kwargs):
        self.launches.append(kwargs)
        return {
            "ok": True,
            "request_id": "review-request-" + kwargs["lens"],
            "task_id": kwargs["reviewer_task_id"],
            "state": "starting",
        }

    def status(self, request_id):
        if request_id in self.targets:
            return dict(self.targets[request_id])
        return {
            "request_id": request_id,
            **self.status_results.get(request_id, self.status_result),
        }

    def accept_review(self, request_id, task_id, **kwargs):
        self.accepts.append((request_id, task_id))
        return {"ok": True, "request_id": request_id, "task_id": task_id, **kwargs}

    def _append_event(self, event):
        self.events.append(event)


def _target_status(
    request_id: str,
    *,
    packet_sha256: str,
    candidate_sha256: str = CANDIDATE,
    objective: str = "make the thing correct",
    lenses: tuple[str, ...] = ("correctness",),
) -> dict:
    return {
        "ok": True,
        "state": "review_ready",
        "task_card": {
            "task_id": "TARGET",
            "request_id": request_id,
            "claim_epoch": "1",
            "packet_sha256": packet_sha256,
            "candidate_sha256": candidate_sha256,
            "workspace_identity": "workspace-candidate-a",
            "objective": objective,
            "acceptance": ["the test passes"],
            "required_outputs": ["src/module.py"],
            "validation": ["pytest -q"],
            "evidence": {"source_graph_partition_readiness": {"target": True}},
            # The tier's own lens plan, read from where the finalizer writes
            # it, so a chain here plans the lenses this tier requires instead
            # of falling back to all three.
            "terminal_review": {
                "evidence": {
                    "quality_gate": {
                        "review_risk_profile": {
                            "effective_tier": "medium",
                            "required_reviewer_lenses": list(lenses),
                        }
                    }
                }
            },
        },
    }


def _review_packet_sha256(request_id: str, lens: str) -> str:
    return hashlib.sha256(f"review-packet:{request_id}:{lens}".encode()).hexdigest()


def _review_status(
    *,
    lens: str = "correctness",
    target_request_id: str,
    reviewer_task_id: str,
    findings: list[dict] | None = None,
    provider: str = "codex_cli",
) -> dict:
    reviewer_request = "review-request-" + lens
    packet_sha256 = _review_packet_sha256(target_request_id, lens)
    receipt = {
        "schema_id": "aiworkhub.quality_review_receipt.v1",
        "packet_sha256": packet_sha256,
        "target": {
            "request_id": target_request_id,
            "task_id": "TARGET",
            "claim_epoch": 1,
        },
        "reviewer": {
            "request_id": reviewer_request,
            "task_id": reviewer_task_id,
            "provider": provider,
        },
        "report": {
            "lens": lens,
            "provider": provider,
            "read_only": True,
            "can_mutate_repo": False,
            "findings": list(findings or []),
        },
        "authority": {
            "process_identity_verified": True,
            "audit_verified": True,
            "terminal_state": "review_ready",
        },
        "submission_id": hashlib.sha256(
            f"submission:{target_request_id}:{lens}".encode()
        ).hexdigest(),
        "physical_submission_count": 1,
        "logical_submission_count": 1,
    }
    return {
        "ok": True,
        "state": "review_ready",
        "adapter_id": provider,
        "latest_event": {"quality_review_receipt": receipt},
        "task_card": {
            "terminal_review": {
                "evidence": {
                    "quality_review": {
                        "lens": lens,
                        "packet_sha256": packet_sha256,
                        "target_request_id": target_request_id,
                        "target_task_id": "TARGET",
                        "target_claim_epoch": 1,
                    },
                    "quality_review_receipt": receipt,
                }
            }
        },
    }


def _first_chain(tmp_path: Path, manager: _Manager, monkeypatch) -> tuple:
    """Drive one whole correctness chain to an INGESTED, archived report."""
    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "archive_task",
        lambda repo, task_id, **kwargs: manager.archives.append(task_id)
        or {"ok": True},
    )
    db = tmp_path / "review.sqlite"
    manager.targets["target-request"] = _target_status(
        "target-request", packet_sha256=MANIFEST_1
    )
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db, route_selector=_route
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET",
        target_request_id="target-request",
        claim_epoch=1,
        packet_sha256=MANIFEST_1,
        candidate_sha256=CANDIDATE,
        now=NOW,
    )
    assert driver.drain(max_actions=1, now=NOW).completed == 1
    reviewer_task = review_orchestrator.ReviewOrchestrator._reviewer_task_id(
        chain.chain_identity, "correctness"
    )
    manager.status_results["review-request-correctness"] = _review_status(
        target_request_id="target-request", reviewer_task_id=reviewer_task
    )
    assert driver.drain(max_actions=1, now=NOW).completed == 1  # accept
    assert driver.drain(max_actions=1, now=NOW).completed == 1  # archive
    # Walk the rest of this chain out of the way: the lenses the tier did not
    # require complete as obsolete, and ``target_accept`` fails closed on
    # AUTOMATIC_TARGET_ACCEPT_ENABLED. A successor's actions are only reached
    # once nothing earlier is still reservable.
    driver.drain(max_actions=12, now=NOW)
    return driver, chain, db


def _successor(
    driver,
    manager: _Manager,
    *,
    candidate_sha256: str = CANDIDATE,
    objective: str = "make the thing correct",
    lenses: tuple[str, ...] = ("correctness",),
):
    manager.targets["target-request-2"] = _target_status(
        "target-request-2",
        packet_sha256=MANIFEST_2,
        candidate_sha256=candidate_sha256,
        objective=objective,
        lenses=lenses,
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET",
        target_request_id="target-request-2",
        claim_epoch=1,
        packet_sha256=MANIFEST_2,
        candidate_sha256=candidate_sha256,
        now=NOW,
    )
    return chain


# --- hash-keyed replay ----------------------------------------------------


def test_an_all_unchanged_successor_replays_and_launches_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _Manager(tmp_path)
    driver, first, _db = _first_chain(tmp_path, manager, monkeypatch)
    launched_before = len(manager.launches)
    accepted_before = len(manager.accepts)

    second = _successor(driver, manager)
    result = driver.drain(max_actions=3, now=NOW)

    assert result.completed == 3
    # Not one provider token: no launch, and no second acceptance of one report.
    assert len(manager.launches) == launched_before
    assert len(manager.accepts) == accepted_before
    receipts = review_lifecycle.completed_receipts_for_chain(
        driver.db_path, second.chain_id
    )
    launch = next(r for r in receipts if r["action_type"] == "launch")
    assert launch["replayed_from_chain"] == first.chain_id
    assert launch["result"]["state"] == "replayed"
    # BOTH packet digests, and they are genuinely different objects.
    assert launch["replay"]["packet_sha256"] == MANIFEST_2
    assert launch["replay"]["source_packet_sha256"] == MANIFEST_1
    assert launch["replay"]["candidate_sha256"] == CANDIDATE


def test_a_successor_with_one_changed_path_launches_that_lens(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _Manager(tmp_path)
    driver, _first, _db = _first_chain(tmp_path, manager, monkeypatch)
    launched_before = len(manager.launches)

    _successor(driver, manager, candidate_sha256="d" * 64)
    driver.drain(max_actions=1, now=NOW)

    assert len(manager.launches) == launched_before + 1
    assert manager.launches[-1]["lens"] == "correctness"


def test_a_contract_identity_change_refuses_to_replay(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _Manager(tmp_path)
    driver, _first, _db = _first_chain(tmp_path, manager, monkeypatch)
    launched_before = len(manager.launches)

    # Same bytes, different objective: the same diff can satisfy one contract
    # and fail the one that replaced it, so the report is not transferable.
    _successor(driver, manager, objective="make the thing FAST instead")
    driver.drain(max_actions=1, now=NOW)

    assert len(manager.launches) == launched_before + 1


def test_an_unknown_contract_never_matches_and_never_replays(tmp_path: Path) -> None:
    db = tmp_path / "review.sqlite"
    review_lifecycle.create_or_replay_chain(
        db,
        target_task_id="TARGET",
        target_request_id="req-1",
        claim_epoch="1",
        packet_sha256=MANIFEST_1,
        candidate_sha256=CANDIDATE,
        now=NOW,
        contract_identity_sha256="",
    )
    assert (
        review_lifecycle.replay_sources(
            db,
            target_task_id="TARGET",
            candidate_sha256=CANDIDATE,
            contract_identity_sha256="",
            exclude_chain_id=0,
        )
        == {}
    )


def test_a_launched_but_never_ingested_report_is_not_replayable(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _Manager(tmp_path)
    db = tmp_path / "review.sqlite"
    manager.targets["target-request"] = _target_status(
        "target-request", packet_sha256=MANIFEST_1
    )
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db, route_selector=_route
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET",
        target_request_id="target-request",
        claim_epoch=1,
        packet_sha256=MANIFEST_1,
        candidate_sha256=CANDIDATE,
        now=NOW,
    )
    assert driver.drain(max_actions=1, now=NOW).completed == 1  # launch only

    second = _successor(driver, manager)

    # The accept action never completed, so nothing was ever ingested and no
    # lens on the successor is offered a replay.
    assert driver._replay_plan(second.chain_id, "correctness") == {}


def test_a_replayed_report_keeps_its_original_receipt_identity(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _Manager(tmp_path)
    driver, first, _db = _first_chain(tmp_path, manager, monkeypatch)
    accepted_before = len(manager.accepts)
    archived_before = len(manager.archives)

    second = _successor(driver, manager)
    driver.drain(max_actions=3, now=NOW)

    receipts = review_lifecycle.completed_receipts_for_chain(
        driver.db_path, second.chain_id
    )
    accept = next(r for r in receipts if r["action_type"] == "accept")
    # The ORIGINAL reviewer, and it is named as a replay in three places.
    assert accept["reviewer_request_id"] == "review-request-correctness"
    assert accept["reviewer_provider"] == "codex_cli"
    assert accept["replayed_from_chain"] == first.chain_id
    assert accept["result"]["state"] == "replayed"
    # Nothing here mints a new acceptance or a second archive of one card.
    assert len(manager.accepts) == accepted_before
    assert len(manager.archives) == archived_before
    # And the replayed report still resolved through the SAME verifier: its
    # report is bound to the source chain's request, not this one.
    assert accept["replay"]["source_target_request_id"] == "target-request"


def test_a_replayed_report_that_fails_the_real_verifier_is_never_completed(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _Manager(tmp_path)
    driver, _first, _db = _first_chain(tmp_path, manager, monkeypatch)
    accepted_before = len(manager.accepts)
    _successor(driver, manager)
    driver.drain(max_actions=1, now=NOW)  # replayed launch

    # Tamper with the original reviewer's card the way a swapped report would.
    tampered = _review_status(
        target_request_id="target-request",
        reviewer_task_id="SOME-OTHER-REVIEWER",
    )
    manager.status_results["review-request-correctness"] = tampered

    result = driver.drain(max_actions=1, now=NOW)

    assert result.failed == 1
    assert len(manager.accepts) == accepted_before


def test_a_replayed_actionable_finding_still_fails_the_chain(
    tmp_path: Path, monkeypatch
) -> None:
    """A replay carries the judgment forward -- including a blocking one."""
    manager = _Manager(tmp_path)
    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "archive_task",
        lambda repo, task_id, **kwargs: {"ok": True},
    )
    db = tmp_path / "review.sqlite"
    manager.targets["target-request"] = _target_status(
        "target-request", packet_sha256=MANIFEST_1
    )
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=db, route_selector=_route
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256=MANIFEST_1, candidate_sha256=CANDIDATE, now=NOW,
    )
    driver.drain(max_actions=1, now=NOW)
    reviewer_task = review_orchestrator.ReviewOrchestrator._reviewer_task_id(
        chain.chain_identity, "correctness"
    )
    clean = _review_status(
        target_request_id="target-request", reviewer_task_id=reviewer_task
    )
    manager.status_results["review-request-correctness"] = clean
    driver.drain(max_actions=2, now=NOW)

    driver.drain(max_actions=12, now=NOW)
    accepted_before = len(manager.accepts)
    _successor(driver, manager)
    driver.drain(max_actions=1, now=NOW)
    manager.status_results["review-request-correctness"] = _review_status(
        target_request_id="target-request",
        reviewer_task_id=reviewer_task,
        findings=[{"disposition": "defect", "actionable": True}],
    )

    assert driver.drain(max_actions=1, now=NOW).failed == 1
    # The blocking judgment was carried forward, not re-derived, and it still
    # stopped the chain -- and no second acceptance was minted on the way.
    assert len(manager.accepts) == accepted_before


def test_replay_never_enables_the_gated_automatic_target_accept(
    tmp_path: Path, monkeypatch
) -> None:
    assert review_orchestrator.AUTOMATIC_TARGET_ACCEPT_ENABLED is False
    manager = _Manager(tmp_path)
    driver, _first, _db = _first_chain(tmp_path, manager, monkeypatch)
    accepted_before = len(manager.accepts)
    second = _successor(driver, manager)

    driver.drain(max_actions=12, now=NOW)

    rows = [
        row
        for row in review_lifecycle.rows_for_test(driver.db_path)
        if row["chain_id"] == second.chain_id
        and row["action_type"] == "target_accept"
    ]
    assert rows and rows[0]["state"] == "failed"
    assert "target_accept_requires_verified_manager" in rows[0]["failure_reason"]
    # The target is never accepted by any path this feature opened.
    assert [pair for pair in manager.accepts[accepted_before:] if pair[1] == "TARGET"] == []


# --- required_reviewer_missing is never weakened --------------------------


def _profile(lenses: tuple[str, ...]) -> dict:
    return {
        "effective_tier": "medium",
        "required_reviewer_lenses": list(lenses),
        "cross_provider_required": False,
    }


def _report(lens: str, *, replay: dict | None = None) -> dict:
    report = {
        "lens": lens,
        "provider": "codex_cli",
        "read_only": True,
        "can_mutate_repo": False,
        "findings": [],
    }
    if replay is not None:
        report["replay"] = replay
    return report


def test_a_replayed_report_counts_for_its_lens_only_on_both_hashes() -> None:
    binding = {"candidate_sha256": CANDIDATE, "contract_identity_sha256": "e" * 64}
    verdict = quality_evidence.fold_quality_verdict(
        [],
        risk_profile=_profile(("correctness",)),
        reviewer_reports=[_report("correctness", replay=dict(binding))],
        worker_provider="claude_cli",
        replay_binding=dict(binding),
    )
    assert not any(
        str(blocker).startswith("required_reviewer_missing")
        for blocker in verdict["blocking_evidence"]
    )


@pytest.mark.parametrize(
    "replay",
    [
        {"candidate_sha256": "d" * 64, "contract_identity_sha256": "e" * 64},
        {"candidate_sha256": CANDIDATE, "contract_identity_sha256": "f" * 64},
        {"candidate_sha256": CANDIDATE},
        {},
    ],
)
def test_a_replay_that_does_not_match_both_hashes_never_covers_a_lens(
    replay: dict,
) -> None:
    binding = {"candidate_sha256": CANDIDATE, "contract_identity_sha256": "e" * 64}
    verdict = quality_evidence.fold_quality_verdict(
        [],
        risk_profile=_profile(("correctness",)),
        reviewer_reports=[_report("correctness", replay=replay)],
        worker_provider="claude_cli",
        replay_binding=dict(binding),
    )
    assert "required_reviewer_missing:correctness" in verdict["blocking_evidence"]
    assert "replayed_reviewer_binding_mismatch:correctness" in verdict["blocking_evidence"]
    assert verdict["passed"] is False


def test_a_replay_without_a_target_binding_is_refused_not_trusted() -> None:
    verdict = quality_evidence.fold_quality_verdict(
        [],
        risk_profile=_profile(("correctness",)),
        reviewer_reports=[
            _report(
                "correctness",
                replay={
                    "candidate_sha256": CANDIDATE,
                    "contract_identity_sha256": "e" * 64,
                },
            )
        ],
        worker_provider="claude_cli",
    )
    assert "required_reviewer_missing:correctness" in verdict["blocking_evidence"]


def test_a_fresh_report_is_untouched_by_the_replay_gate() -> None:
    verdict = quality_evidence.fold_quality_verdict(
        [],
        risk_profile=_profile(("correctness",)),
        reviewer_reports=[_report("correctness")],
        worker_provider="claude_cli",
        replay_binding={"candidate_sha256": CANDIDATE},
    )
    assert not any(
        str(blocker).startswith(("required_reviewer_missing", "replayed_reviewer"))
        for blocker in verdict["blocking_evidence"]
    )


# --- prior findings in the packet ----------------------------------------


def _manager_stub(tmp_path: Path):
    stub = process_launcher.ProcessManager.__new__(process_launcher.ProcessManager)
    stub.repo = tmp_path
    return stub


def _prior_report(
    *,
    lens: str = "correctness",
    request_id: str = "req-prev",
    findings: list[dict],
) -> dict:
    return {
        "lens": lens,
        "reviewer_task_id": "REVIEWER-1",
        "reviewer_request_id": "review-request-prev",
        "reviewer_provider": "codex_cli",
        "packet_sha256": "1" * 64,
        "target_request_id": request_id,
        "findings": findings,
    }


def _finding(**overrides) -> dict:
    finding = {
        "id": "F1",
        "severity": "high",
        "disposition": "defect",
        "actionable": True,
        "summary": "off-by-one in the loop bound",
        "path": "src/module.py",
        "line_start": 10,
        "line_end": 12,
    }
    finding.update(overrides)
    return finding


def test_a_prior_finding_on_unchanged_bytes_keeps_its_exact_line_map(
    tmp_path: Path,
) -> None:
    stub = _manager_stub(tmp_path)
    digest = "1" * 64
    stub._prior_reviewer_receipts = lambda *_args: (  # type: ignore[assignment]
        [_prior_report(findings=[_finding()])],
        {"req-prev": {"src/module.py": digest}},
    )
    record = stub._quality_review_prior_findings(
        target_task_id="TARGET",
        target_request_id="req-now",
        current_hashes={"src/module.py": digest},
        source_evidence={
            "src/module.py": {
                "segments": [
                    {"changed_start_line": 8, "changed_end_line": 14, "kind": "replace"}
                ]
            }
        },
        predecessor_request_id="req-prev",
    )

    row = record["lenses"]["correctness"]["findings"][0]
    assert row["status"] == "lines_unchanged"
    assert row["line_mapping"] == "identity"
    assert (row["line_start"], row["line_end"]) == (10, 12)
    assert row["overlaps_current_hunk"] is True


def test_a_prior_finding_on_changed_bytes_carries_no_line_numbers(
    tmp_path: Path,
) -> None:
    stub = _manager_stub(tmp_path)
    stub._prior_reviewer_receipts = lambda *_args: (  # type: ignore[assignment]
        [_prior_report(findings=[_finding()])],
        {"req-prev": {"src/module.py": "1" * 64}},
    )
    record = stub._quality_review_prior_findings(
        target_task_id="TARGET",
        target_request_id="req-now",
        current_hashes={"src/module.py": "2" * 64},
        source_evidence={},
        predecessor_request_id="req-prev",
    )

    row = record["lenses"]["correctness"]["findings"][0]
    assert row["status"] == "lines_changed"
    assert row["line_mapping"] == "unavailable"
    assert row["line_start"] is None and row["line_end"] is None
    assert row["overlaps_current_hunk"] is None
    # The judgment still reaches the reviewer; only the line map is withheld.
    assert row["summary"] == "off-by-one in the loop bound"


def test_the_status_vocabulary_admits_no_value_meaning_fixed() -> None:
    assert quality_reviewer.PRIOR_FINDING_LINE_STATUSES == {
        "lines_unchanged",
        "lines_changed",
    }


def test_prior_findings_are_labelled_as_prior_reviewer_output(
    tmp_path: Path,
) -> None:
    stub = _manager_stub(tmp_path)
    digest = "1" * 64
    stub._prior_reviewer_receipts = lambda *_args: (  # type: ignore[assignment]
        [_prior_report(findings=[_finding()])],
        {"req-prev": {"src/module.py": digest}},
    )
    record = stub._quality_review_prior_findings(
        target_task_id="TARGET",
        target_request_id="req-now",
        current_hashes={"src/module.py": digest},
        source_evidence={},
        predecessor_request_id="req-prev",
    )
    packet = quality_reviewer.build_review_packet(
        request_id="req-now",
        task_id="TARGET",
        claim_epoch=1,
        worker_provider="claude_cli",
        changed_path_hashes={"src/module.py": digest},
        objective="make the thing correct",
        prior_findings=record,
    )

    section = packet["prior_review"]
    assert section["schema_id"] == quality_reviewer.PRIOR_FINDINGS_SCHEMA_ID
    assert "PRIOR REVIEWER OUTPUT" in section["notice"]
    # It is NOT under ``candidate``: it is not this candidate's own evidence.
    assert "prior_findings" not in packet["candidate"]
    for row in section["lenses"]["correctness"]["findings"]:
        assert row["source"] == "prior_reviewer_report"
        assert row["reviewer_request_id"] == "review-request-prev"
    for row in section["lenses"]["correctness"]["reports"]:
        assert row["source"] == "prior_reviewer_report"
        assert row["verification"] == "receipt_identity_only"


def test_a_prior_clean_report_says_so_and_suppresses_nothing(
    tmp_path: Path,
) -> None:
    stub = _manager_stub(tmp_path)
    digest = "1" * 64
    stub._prior_reviewer_receipts = lambda *_args: (  # type: ignore[assignment]
        [_prior_report(lens="security", findings=[])],
        {"req-prev": {"src/module.py": digest}},
    )
    record = stub._quality_review_prior_findings(
        target_task_id="TARGET",
        target_request_id="req-now",
        current_hashes={"src/module.py": "2" * 64},
        source_evidence={},
        predecessor_request_id="req-prev",
    )
    packet = quality_reviewer.build_review_packet(
        request_id="req-now",
        task_id="TARGET",
        claim_epoch=1,
        worker_provider="claude_cli",
        changed_path_hashes={"src/module.py": "2" * 64},
        source_evidence={
            "src/module.py": {
                "candidate_sha256": "2" * 64,
                "excerpt": "@@ changed @@\n+new line\n",
                "excerpt_bytes": 26,
                "source_bytes": 26,
                "truncated": False,
                "diff_complete": True,
                "segments": [
                    {
                        "kind": "replace",
                        "candidate_start_line": 1,
                        "candidate_end_line": 2,
                        "changed_start_line": 1,
                        "changed_end_line": 2,
                        "baseline_start_line": 1,
                        "baseline_end_line": 2,
                        "excerpt_bytes": 26,
                        "truncated": False,
                    }
                ],
            }
        },
        prior_findings=record,
    )

    report = packet["prior_review"]["lenses"]["security"]["reports"][0]
    assert report["clean"] is True
    assert "is NOT a reason to skip this lens" in packet["prior_review"]["notice"]
    # The security lens's own evidence is untouched: every changed hunk is
    # still in the packet for it to look at.
    assert packet["candidate"]["source_evidence"][0]["segments"]
    lens_packet = quality_reviewer.build_lens_packet(packet, lens="security")
    assert lens_packet["candidate"]["source_evidence"][0]["segments"]


def test_a_clean_prior_report_never_removes_a_required_lens_from_the_plan(
    tmp_path: Path, monkeypatch
) -> None:
    """The lens plan is bound from the TIER and never reads prior findings."""
    manager = _Manager(tmp_path)
    driver, _first, _db = _first_chain(tmp_path, manager, monkeypatch)
    second = _successor(
        driver, manager, candidate_sha256="d" * 64,
        lenses=("correctness", "security"),
    )

    assert set(review_orchestrator.required_lenses(driver.db_path, second.chain_id)) == {
        "correctness",
        "security",
    }
    # Different bytes, so neither lens is offered a replay: a prior clean
    # report has no bearing on a candidate it was not written about.
    assert driver._replay_plan(second.chain_id, "correctness") == {}
    assert driver._replay_plan(second.chain_id, "security") == {}
    launched_before = len(manager.launches)
    driver.drain(max_actions=1, now=NOW)
    assert manager.launches[launched_before:][0]["lens"] == "correctness"


def test_the_lens_slice_keeps_only_this_lenss_prior_review(tmp_path: Path) -> None:
    digest = "1" * 64
    record = {
        "predecessor_request_id": "req-prev",
        "omitted": 0,
        "lenses": {
            lens: {
                "reports": [
                    {
                        "reviewer_request_id": f"review-request-{lens}",
                        "reviewer_task_id": "R",
                        "reviewer_provider": "codex_cli",
                        "target_request_id": "req-prev",
                        "packet_sha256": None,
                        "finding_count": 0,
                    }
                ],
                "findings": [],
            }
            for lens in ("correctness", "security", "code_quality")
        },
    }
    packet = quality_reviewer.build_review_packet(
        request_id="req-now",
        task_id="TARGET",
        claim_epoch=1,
        worker_provider="claude_cli",
        changed_path_hashes={"src/module.py": digest},
        prior_findings=record,
    )

    sliced = quality_reviewer.build_lens_packet(packet, lens="correctness")

    assert set(sliced["prior_review"]["lenses"]) == {"correctness"}
    body = {k: v for k, v in sliced.items() if k != "packet_sha256"}
    assert quality_reviewer._canonical_digest(body) == sliced["packet_sha256"]


def test_a_prior_finding_must_be_attributable_to_a_listed_report() -> None:
    with pytest.raises(quality_reviewer.ReviewerEvidenceError) as excinfo:
        quality_reviewer.build_review_packet(
            request_id="req-now",
            task_id="TARGET",
            claim_epoch=1,
            worker_provider="claude_cli",
            changed_path_hashes={"src/module.py": "1" * 64},
            prior_findings={
                "predecessor_request_id": "req-prev",
                "omitted": 0,
                "lenses": {
                    "correctness": {
                        "reports": [],
                        "findings": [
                            {
                                "reviewer_request_id": "ghost",
                                "finding_id": "F1",
                                "severity": "high",
                                "disposition": "defect",
                                "actionable": True,
                                "summary": "x",
                                "path": None,
                                "line_start": None,
                                "line_end": None,
                                "status": "lines_changed",
                                "line_mapping": "unavailable",
                                "path_in_candidate": False,
                                "overlaps_current_hunk": None,
                            }
                        ],
                    }
                },
            },
        )
    assert "prior_finding_unattributed" in str(excinfo.value)


def test_an_unchanged_status_may_never_claim_an_unavailable_line_map() -> None:
    with pytest.raises(quality_reviewer.ReviewerEvidenceError):
        quality_reviewer._prior_findings_rows(
            {
                "predecessor_request_id": "req-prev",
                "omitted": 0,
                "lenses": {
                    "correctness": {
                        "reports": [
                            {
                                "reviewer_request_id": "rev",
                                "finding_count": 1,
                            }
                        ],
                        "findings": [
                            {
                                "reviewer_request_id": "rev",
                                "finding_id": "F1",
                                "severity": "low",
                                "disposition": "observation",
                                "actionable": False,
                                "summary": "x",
                                "path": None,
                                "status": "lines_unchanged",
                                "line_mapping": "unavailable",
                                "path_in_candidate": False,
                            }
                        ],
                    }
                },
            },
            changed_paths={"src/module.py"},
        )


# --- the read that finds prior receipts -----------------------------------


def _seed_store(db: Path, *, cards: list[tuple[str, dict]], events: list[dict]) -> None:
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "CREATE TABLE tasks (task_id TEXT, runner TEXT, topic TEXT, "
            "card_json TEXT, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE task_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "task_id TEXT, event TEXT, payload_json TEXT)"
        )
        for task_id, card in cards:
            conn.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?)",
                (task_id, "codex_cli", "quality_review", json.dumps(card), "2026-09-08"),
            )
        for payload in events:
            conn.execute(
                "INSERT INTO task_events (task_id, event, payload_json) VALUES (?,?,?)",
                ("TARGET", "terminal_review", json.dumps(payload)),
            )
    conn.close()


def _reviewer_card(*, target_request_id: str, lens: str, findings: list[dict]) -> dict:
    return {
        "terminal_review": {
            "evidence": {
                "quality_review": {
                    "lens": lens,
                    "packet_sha256": "1" * 64,
                    "target_request_id": target_request_id,
                    "target_task_id": "TARGET",
                },
                "quality_review_receipt": {
                    "target": {"task_id": "TARGET", "request_id": target_request_id},
                    "reviewer": {"request_id": "review-request-prev"},
                    "report": {
                        "lens": lens,
                        "provider": "codex_cli",
                        "findings": findings,
                    },
                },
            }
        }
    }


def test_the_prior_receipt_read_binds_task_lens_and_a_different_request(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "task_queue.sqlite"
    _seed_store(
        db,
        cards=[
            ("REV-A", _reviewer_card(
                target_request_id="req-prev", lens="correctness",
                findings=[_finding()],
            )),
            # Same task, but THIS request: it is the run being packaged now.
            ("REV-B", _reviewer_card(
                target_request_id="req-now", lens="security", findings=[],
            )),
            # Another task entirely.
            ("REV-C", {
                "terminal_review": {"evidence": {
                    "quality_review": {
                        "lens": "correctness", "target_task_id": "OTHER",
                        "target_request_id": "req-prev", "packet_sha256": "1" * 64,
                    },
                    "quality_review_receipt": {
                        "target": {"task_id": "OTHER", "request_id": "req-prev"},
                        "reviewer": {"request_id": "rev-c"},
                        "report": {"lens": "correctness", "provider": "x",
                                   "findings": []},
                    },
                }}
            }),
        ],
        events=[
            {"evidence": {
                "request_id": "req-prev",
                "changed_path_hashes": {"src/module.py": "1" * 64},
            }}
        ],
    )
    monkeypatch.setattr(task_store, "canonical_db_path", lambda _repo: db)
    stub = _manager_stub(tmp_path)

    reports, hashes = stub._prior_reviewer_receipts("TARGET", "req-now")

    assert [row["reviewer_task_id"] for row in reports] == ["REV-A"]
    assert reports[0]["lens"] == "correctness"
    assert hashes["req-prev"] == {"src/module.py": "1" * 64}


def test_an_unreadable_store_yields_no_prior_findings_and_never_raises(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        task_store, "canonical_db_path", lambda _repo: tmp_path / "absent.sqlite"
    )
    stub = _manager_stub(tmp_path)
    assert stub._prior_reviewer_receipts("TARGET", "req-now") == ([], {})
    assert (
        stub._quality_review_prior_findings(
            target_task_id="TARGET",
            target_request_id="req-now",
            current_hashes={"src/module.py": "1" * 64},
            source_evidence={},
            predecessor_request_id="",
        )
        is None
    )
