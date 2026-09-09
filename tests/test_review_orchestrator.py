from __future__ import annotations

import sys
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import review_lifecycle, review_orchestrator  # noqa: E402
import pytest  # noqa: E402


NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)
ROUTE = {"runner": "codex56_reviewer", "adapter_id": "codex_cli", "model": "gpt-5.6-sol"}


def _route(_repo: Path, _task_id: str, _lens: str) -> dict[str, str]:
    return dict(ROUTE)


class _Manager:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.launches: list[dict] = []
        self.accepts: list[tuple[str, str]] = []
        self.status_result: dict = {"ok": True, "state": "starting"}
        self.status_results: dict[str, dict] = {}
        self.events: list[dict] = []
        self.target_status = _target_status()

    def launch_quality_reviewer(self, **kwargs):
        self.launches.append(kwargs)
        return {
            "ok": True,
            "request_id": "review-request-" + kwargs["lens"],
            "task_id": kwargs["reviewer_task_id"],
            "state": "starting",
        }

    def status(self, request_id):
        if request_id == "target-request":
            return dict(self.target_status)
        return {
            "request_id": request_id,
            **self.status_results.get(request_id, self.status_result),
        }

    def accept_review(self, request_id, task_id, **kwargs):
        self.accepts.append((request_id, task_id))
        return {"ok": True, "request_id": request_id, "task_id": task_id, **kwargs}

    def _append_event(self, event):
        self.events.append(event)


def _target_status(*, state: str = "review_ready", **card_overrides: object) -> dict:
    card = {
        "task_id": "TARGET",
        "request_id": "target-request",
        "claim_epoch": "1",
        "packet_sha256": "a" * 64,
        "candidate_sha256": "b" * 64,
        "workspace_identity": "workspace-candidate-a",
        "evidence": {"source_graph_partition_readiness": {"target": True}},
    }
    card.update(card_overrides)
    return {"ok": True, "state": state, "task_card": card}


def test_driver_launches_one_action_and_persists_exact_identity(tmp_path: Path) -> None:
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET",
        target_request_id="target-request",
        claim_epoch=1,
        packet_sha256="a" * 64,
        candidate_sha256="b" * 64,
        now=NOW,
    )

    result = driver.drain(max_actions=1, now=NOW)

    assert result.completed == 1
    assert len(manager.launches) == 1
    assert {key: manager.launches[0][key] for key in ROUTE} == ROUTE
    rows = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
    assert rows[0]["state"] == "completed"
    assert rows[0]["chain_id"] == chain.chain_id


def test_launch_waits_until_target_is_ready_then_launches_once(tmp_path: Path) -> None:
    manager = _Manager(tmp_path)
    manager.target_status = _target_status(state="processing")
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )

    deferred = driver.drain(max_actions=2, now=NOW)

    assert deferred.attempted == 1
    assert deferred.pending == 1
    assert manager.launches == []
    assert manager.events[-1]["review_automation"]["reason"] == "target_not_review_ready"
    manager.target_status = _target_status()
    launched = driver.drain(max_actions=2, now=NOW)

    assert launched.completed == 1
    assert len(manager.launches) == 1


def test_launch_accepts_canonical_review_ready_card_when_process_state_is_absent(
    tmp_path: Path,
) -> None:
    manager = _Manager(tmp_path)
    manager.target_status = _target_status(
        status="review",
        worker_status="review",
        terminal_substatus="review_ready",
    )
    manager.target_status["state"] = None
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )

    launched = driver.drain(max_actions=1, now=NOW)

    assert launched.completed == 1
    assert len(manager.launches) == 1


def test_launch_rejects_identity_mismatch_and_empty_partition(tmp_path: Path) -> None:
    manager = _Manager(tmp_path)
    manager.target_status = _target_status(candidate_sha256="c" * 64)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )

    invalid = driver.drain(max_actions=2, now=NOW)

    assert invalid.failed == 1
    assert manager.launches == []
    rows = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
    assert "target_candidate_identity_invalid" in rows[0]["failure_reason"]

    manager = _Manager(tmp_path)
    manager.target_status = _target_status(
        evidence={"source_graph_partition_readiness": {}}
    )
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "empty.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )

    empty = driver.drain(max_actions=2, now=NOW)

    assert empty.pending == 1
    assert manager.launches == []
    assert manager.events[-1]["review_automation"]["reason"] == "source_graph_partition_empty"


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("task_id", "target_task_identity_invalid"),
        ("packet_sha256", "target_packet_identity_invalid"),
        ("candidate_sha256", "target_candidate_identity_invalid"),
    ],
)
def test_launch_identity_mismatches_are_terminal_before_not_ready(
    tmp_path: Path, field: str, reason: str
) -> None:
    manager = _Manager(tmp_path)
    replacement = "wrong" if field != "candidate_sha256" else "c" * 64
    manager.target_status = _target_status(state="processing", **{field: replacement})
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "identity.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )

    result = driver.drain(max_actions=1, now=NOW)

    assert result.failed == 1
    assert manager.launches == []
    assert reason in review_lifecycle.rows_for_test(tmp_path / "identity.sqlite")[0]["failure_reason"]


@pytest.mark.parametrize(
    ("field", "replacement", "reason"),
    [
        ("request_id", "newer-request", "target_request_superseded:newer-request"),
        ("claim_epoch", "9", "target_claim_epoch_superseded:9"),
    ],
)
def test_superseded_target_is_retired_as_obsolete_not_failed(
    tmp_path: Path, field: str, replacement: str, reason: str
) -> None:
    """A newer request/claim on the same task ends the chain, it does not park it.

    Measured over 627 real chains: 337 were bound to a request the card had
    already replaced, and every one of them FAILED its launch action, which
    parked its whole chain permanently. Nothing is left to review on those
    bytes -- that is a completed chain, not a broken one.
    """
    manager = _Manager(tmp_path)
    manager.target_status = _target_status(**{field: replacement})
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "superseded.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )

    result = driver.drain(max_actions=1, now=NOW)

    assert result.failed == 0
    assert result.completed == 1
    assert manager.launches == []
    row = review_lifecycle.rows_for_test(tmp_path / "superseded.sqlite")[0]
    assert row["state"] == "completed"
    assert json.loads(row["receipt_json"])["obsolete_reason"] == reason


def test_launch_binds_workspace_when_canonical_card_becomes_available(tmp_path: Path) -> None:
    manager = _Manager(tmp_path)
    manager.target_status = {"ok": True, "state": "processing"}
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "late-card.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    manager.target_status = _target_status()

    result = driver.drain(max_actions=1, now=NOW)

    assert result.completed == 1
    assert len(manager.launches) == 1


def test_launch_rejects_different_nonempty_workspace_identity(tmp_path: Path) -> None:
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "workspace.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    manager.target_status = _target_status(workspace_identity="workspace-candidate-b")

    result = driver.drain(max_actions=1, now=NOW)

    assert result.failed == 1
    assert manager.launches == []
    rows = review_lifecycle.rows_for_test(tmp_path / "workspace.sqlite")
    assert "target_workspace_identity_invalid" in rows[0]["failure_reason"]


def test_accept_waits_for_supervisor_terminal_receipt_without_busy_loop(
    tmp_path: Path,
) -> None:
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET",
        target_request_id="target-request",
        claim_epoch=1,
        packet_sha256="a" * 64,
        candidate_sha256="b" * 64,
        now=NOW,
    )
    driver.drain(max_actions=1, now=NOW)

    result = driver.drain(max_actions=1, now=NOW)

    assert result.attempted == 1
    assert result.pending == 1
    assert result.completed == 0
    rows = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
    assert rows[1]["state"] == "pending"
    assert rows[1]["lease_token"] == ""


def _review_packet_sha256(lens: str) -> str:
    """The REVIEW-PACKET digest, which is never the attempt-manifest digest.

    The chain identity's ``packet_sha256`` ("a" * 64 throughout this module) is
    the digest of the attempt-artifact MANIFEST; a reviewer receipt carries the
    digest of the review PACKET, recomputed by
    ``quality_reviewer.verify_reviewer_receipt`` over the packet body -- and the
    packet is per lens, so the three lenses do not even share one. Putting the
    same constant on both sides hid a comparison that could never hold in
    production: measured over the live store, 0 of 627 chain packet digests
    appear among the 958 distinct receipt digests. Two different realistic
    digests are what catches that.
    """
    return hashlib.sha256(f"review-packet:target-request:{lens}".encode()).hexdigest()


def _review_status(
    lens: str = "correctness", *, findings: list[dict] | None = None,
    binding_packet_sha256: str | None = None,
    candidate_sha256: str = "b" * 64,
) -> dict:
    reviewer_request = "review-request-" + lens
    packet_sha256 = _review_packet_sha256(lens)
    receipt = {
        "schema_id": "aiworkhub.quality_review_receipt.v1",
        "packet_sha256": packet_sha256,
        "target": {"request_id": "target-request", "task_id": "TARGET", "claim_epoch": 1},
        "reviewer": {
            "request_id": reviewer_request,
            "task_id": review_orchestrator.ReviewOrchestrator._reviewer_task_id(
                {
                    "schema_id": "aiworkhub.review_lifecycle.v1",
                    "target_task_id": "TARGET", "target_request_id": "target-request",
                    "claim_epoch": "1", "packet_sha256": "a" * 64,
                    "candidate_sha256": candidate_sha256,
                },
                lens,
            ),
            "provider": "codex_cli",
        },
        "report": {
            "lens": lens, "provider": "codex_cli", "read_only": True,
            "can_mutate_repo": False, "findings": list(findings or []),
        },
        "authority": {
            "process_identity_verified": True, "audit_verified": True,
            "terminal_state": "review_ready",
        },
        "submission_id": hashlib.sha256(b"submission").hexdigest(),
        "physical_submission_count": 1,
        "logical_submission_count": 1,
    }
    # The reviewer card's own record of the packet THIS repository sealed for
    # THIS reviewer -- the receipt digest's real counterpart. Measured on the
    # live store: it equals the receipt digest in 2,219 of 2,219 stored reviewer
    # cards, with the lens matching in all 2,219.
    binding = {
        "lens": lens,
        "packet_sha256": (
            packet_sha256 if binding_packet_sha256 is None else binding_packet_sha256
        ),
        "target_request_id": "target-request",
        "target_task_id": "TARGET",
        "target_claim_epoch": 1,
    }
    return {
        "ok": True, "state": "review_ready", "adapter_id": "codex_cli",
        "latest_event": {"quality_review_receipt": receipt},
        "task_card": {
            "terminal_review": {
                "evidence": {
                    "quality_review": binding,
                    "quality_review_receipt": receipt,
                }
            }
        },
    }


def test_actionable_finding_fails_chain_before_accept(tmp_path: Path) -> None:
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    assert driver.drain(max_actions=1, now=NOW).completed == 1
    manager.status_result = _review_status(
        findings=[{"disposition": "defect", "actionable": True}]
    )

    result = driver.drain(max_actions=1, now=NOW)

    assert result.failed == 1
    assert manager.accepts == []


def test_terminal_receipt_card_event_mismatch_fails_closed(tmp_path: Path) -> None:
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    status = _review_status()
    status["task_card"]["terminal_review"]["evidence"]["quality_review_receipt"] = {}
    with pytest.raises(RuntimeError, match="reviewer_terminal_receipt_mismatch"):
        driver._review_receipt(
            chain.actions[1], status, "review-request-correctness",
            review_orchestrator.ReviewOrchestrator._reviewer_task_id(
                chain.chain_identity, "correctness"
            ),
        )


def _receipt_chain(tmp_path: Path) -> tuple[object, object]:
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    return driver, chain


def _verify_receipt(driver, chain, status: dict) -> dict:
    return driver._review_receipt(
        chain.actions[1], status, "review-request-correctness",
        review_orchestrator.ReviewOrchestrator._reviewer_task_id(
            chain.chain_identity, "correctness"
        ),
    )


def test_reviewer_receipt_binds_to_the_review_packet_not_the_attempt_manifest(
    tmp_path: Path,
) -> None:
    """The two digests are of two different objects and must not be conflated.

    ``chain_identity["packet_sha256"]`` is the attempt-artifact MANIFEST digest;
    the receipt carries the review PACKET digest. Comparing them refused every
    real receipt -- measured over the live store, 0 of 627 chain digests appear
    among the 958 distinct receipt digests, and of the 103 receipts whose target
    request owns a chain, 0 matched and 103 differed. The check now compares the
    receipt against the reviewer card's own record of the packet this repository
    sealed for it, which is the digest it is actually a receipt for.
    """
    driver, chain = _receipt_chain(tmp_path)
    status = _review_status()
    packet_sha256 = _review_packet_sha256("correctness")

    assert packet_sha256 != chain.chain_identity["packet_sha256"]
    assert _verify_receipt(driver, chain, status)["packet_sha256"] == packet_sha256

    # The exact shape the old comparison demanded: a receipt claiming the
    # attempt-manifest digest, which no reviewer can ever produce.
    manifest_digest = _review_status()
    for holder in (
        manifest_digest["latest_event"]["quality_review_receipt"],
        manifest_digest["task_card"]["terminal_review"]["evidence"][
            "quality_review_receipt"
        ],
    ):
        holder["packet_sha256"] = chain.chain_identity["packet_sha256"]
    with pytest.raises(RuntimeError, match="reviewer_receipt_binding_invalid"):
        _verify_receipt(driver, chain, manifest_digest)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({}, "reviewer_packet_binding_missing"),
        ({"packet_sha256": "c" * 64}, "reviewer_receipt_binding_invalid"),
        ({"packet_sha256": "not-a-digest"}, "reviewer_packet_binding_invalid"),
        ({"lens": "security"}, "reviewer_packet_binding_invalid"),
        ({"target_request_id": "other-request"}, "reviewer_packet_binding_invalid"),
        ({"target_claim_epoch": 9}, "reviewer_packet_binding_invalid"),
    ],
)
def test_reviewer_packet_binding_must_name_this_chain_and_this_lens(
    tmp_path: Path, mutation: dict, reason: str
) -> None:
    """A receipt is evidence only for the packet it was written against."""
    driver, chain = _receipt_chain(tmp_path)
    status = _review_status()
    evidence = status["task_card"]["terminal_review"]["evidence"]
    if mutation:
        evidence["quality_review"].update(mutation)
    else:
        evidence.pop("quality_review")
    with pytest.raises(RuntimeError, match=reason):
        _verify_receipt(driver, chain, status)


def test_automatic_chain_stops_at_acceptance_and_never_accepts_the_target(
    monkeypatch, tmp_path: Path
) -> None:
    """Fixing the launch check must not hand acceptance to the orchestrator.

    Every chain used to die at its first launch action, which is the only
    reason ``target_accept`` (which calls ``manager.accept_review`` on the
    TARGET) had never run. That was an accident, not a control. The nine
    reviewer actions now complete automatically and the chain parks at
    acceptance with an explicit reason.
    """
    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "archive_task",
        lambda _repo, task_id, **_kwargs: {"ok": True, "task_id": task_id},
    )
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "gated.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    for lens in review_orchestrator.LENSES:
        manager.status_results["review-request-" + lens] = _review_status(lens)

    assert review_orchestrator.AUTOMATIC_TARGET_ACCEPT_ENABLED is False
    for _ in range(9):
        assert driver.drain(max_actions=1, now=NOW).completed == 1
    gated = driver.drain(max_actions=1, now=NOW)

    assert gated.failed == 1
    assert manager.accepts == [
        ("review-request-" + lens, task_id)
        for lens, task_id in zip(
            review_orchestrator.LENSES,
            [row["reviewer_task_id"] for row in manager.launches],
        )
    ]
    assert ("target-request", "TARGET") not in manager.accepts
    rows = review_lifecycle.rows_for_test(tmp_path / "gated.sqlite")
    assert rows[9]["action_type"] == "target_accept"
    assert rows[9]["state"] == "failed"
    assert "target_accept_requires_verified_manager" in rows[9]["failure_reason"]


def test_happy_path_is_exactly_ordered_and_closes_linked_needfix(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        review_orchestrator, "AUTOMATIC_TARGET_ACCEPT_ENABLED", True
    )
    manager = _Manager(tmp_path)
    archived: list[str] = []
    resolved: list[tuple[str, str]] = []
    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "archive_task",
        lambda _repo, task_id, **_kwargs: archived.append(task_id) or {"ok": True},
    )
    monkeypatch.setattr(
        review_orchestrator.needfix_store,
        "list_needfix",
        lambda _repo, *, status, **_kwargs: (
            [{"id": "NF-1", "status": status, "converted_task_id": "TARGET"}]
            if status == "task_created"
            else []
        ),
    )
    monkeypatch.setattr(
        review_orchestrator.needfix_store,
        "resolve_needfix",
        lambda _repo, needfix_id, *, resolution_note: (
            resolved.append((needfix_id, resolution_note))
            or {
                "id": needfix_id,
                "status": "resolved",
                "converted_task_id": "TARGET",
            }
        ),
    )
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    for lens in review_orchestrator.LENSES:
        manager.status_results["review-request-" + lens] = _review_status(lens)

    for _ in range(12):
        result = driver.drain(max_actions=1, now=NOW)
        rows_now = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
        failed = [row["failure_reason"] for row in rows_now if row["state"] == "failed"]
        assert result.completed == 1, failed
        assert result.failed == 0
    exhausted = driver.drain(max_actions=1, now=NOW)

    assert exhausted.attempted == 0
    assert [row["lens"] for row in manager.launches] == list(review_orchestrator.LENSES)
    assert [row["runner"] for row in manager.launches] == [ROUTE["runner"]] * 3
    assert manager.accepts[-1] == ("target-request", "TARGET")
    assert archived[-1] == "TARGET"
    assert resolved == [
        ("NF-1", "automatic review lifecycle accepted and archived task TARGET")
    ]
    rows = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
    assert [row["state"] for row in rows] == ["completed"] * 12
    assert rows[11]["action_type"] == "needfix_close"
    assert rows[11]["state"] == "completed"


def test_needfix_close_without_linked_findings_completes_exactly_once(
    monkeypatch, tmp_path: Path
) -> None:
    manager = _Manager(tmp_path)
    monkeypatch.setattr(
        review_orchestrator.needfix_store,
        "list_needfix",
        lambda _repo, **_kwargs: [],
    )
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    monkeypatch.setattr(
        driver,
        "_receipts",
        lambda _chain_id: [
            {
                "lens": lens,
                "action_type": "accept",
                "reviewer_request_id": "review-request-" + lens,
            }
            for lens in review_orchestrator.LENSES
        ],
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    action = chain.actions[11]

    receipt = driver._execute(action)

    assert receipt is not None
    assert receipt["needfix_ids"] == []
    assert receipt["needfix_newly_resolved"] == []
    assert receipt["needfix_closed_count"] == 0


_CATALOG = {"schema_id": "aiworkhub.workforce_catalog.v1", "workers": [{"worker_id": "gpt-5.5"}]}


def _ready_storage(monkeypatch) -> None:
    monkeypatch.setattr(
        review_orchestrator.task_store,
        "storage_readiness",
        lambda _repo: SimpleNamespace(
            ready=True, reason="ready", repo_id="repo-test", canonical_db="queue.sqlite"
        ),
    )


def _capture_rank(monkeypatch, captured: list) -> None:
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "rank_task",
        lambda _repo, task, *, catalog=None: (
            captured.append((task, catalog)) or {"launch_contract": dict(ROUTE)}
        ),
    )


def test_default_route_uses_canonical_workforce_contract(monkeypatch, tmp_path: Path) -> None:
    captured: list = []
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    _capture_rank(monkeypatch, captured)
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "build_routing_catalog",
        lambda _repo: dict(_CATALOG),
    )

    route = review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_EXACT", "security"
    )

    assert route == ROUTE
    task, catalog = captured[0]
    assert task.kinds == frozenset({"review"})
    assert task.risk == "critical"
    assert "session-manager" in task.tool_needs
    # The point of this wiring: ranking sees the evidenced catalog, and no
    # longer falls through to build_catalog's empty process/usage defaults.
    assert catalog == _CATALOG


def test_reviewer_route_still_selects_when_the_catalog_build_raises(
    monkeypatch, tmp_path: Path
) -> None:
    """A reviewer chosen on the prior beats a reviewer that never launches."""
    captured: list = []
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    _capture_rank(monkeypatch, captured)

    def _explode(_repo):
        raise OSError("process ledger unreadable")

    monkeypatch.setattr(
        review_orchestrator.workforce_catalog, "build_routing_catalog", _explode
    )

    route = review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_EXACT", "security"
    )

    assert route == ROUTE
    # catalog=None makes rank_task rebuild the bare catalog itself, which is
    # exactly the conservative-prior ranking used before this was wired.
    assert captured[0][1] is None


def test_a_catalog_build_that_raises_anything_never_stops_the_review(
    monkeypatch, tmp_path: Path
) -> None:
    """Every failure mode of the ledger chain degrades to the prior, not to no review."""
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    for failure in (
        OSError("io"),
        sqlite3.Error("db"),
        ValueError("malformed row"),
        ImportError("dashboard mid-edit"),
        SyntaxError("module being edited"),
        RuntimeError("anything at all"),
    ):
        captured: list = []
        _capture_rank(monkeypatch, captured)
        review_orchestrator.reset_routing_catalog_cache()

        def _explode(_repo, _exc=failure):
            raise _exc

        monkeypatch.setattr(
            review_orchestrator.workforce_catalog, "build_routing_catalog", _explode
        )

        route = review_orchestrator.select_reviewer_route(
            tmp_path, "QUALITY_REVIEW_EXACT", "security"
        )

        assert route == ROUTE, failure
        assert captured[0][1] is None, failure


@pytest.mark.parametrize(
    "unusable",
    [None, {}, [], "catalog", {"workers": []}, {"workers": None}, {"no_workers": 1}],
)
def test_an_unusable_catalog_never_reaches_rank_task(
    monkeypatch, tmp_path: Path, unusable
) -> None:
    """A truthy catalog with no workers ranks zero candidates and kills the launch."""
    captured: list = []
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    _capture_rank(monkeypatch, captured)
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "build_routing_catalog",
        lambda _repo: unusable,
    )

    route = review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_EXACT", "security"
    )

    assert route == ROUTE
    assert captured[0][1] is None


def test_an_unusable_catalog_is_never_cached_as_if_it_were_good(
    monkeypatch, tmp_path: Path
) -> None:
    """A failed build must not poison the memo for the rest of the pass."""
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    _capture_rank(monkeypatch, [])
    builds: list[int] = []

    def _fail_then_succeed(_repo):
        builds.append(1)
        if len(builds) == 1:
            raise OSError("transient")
        return dict(_CATALOG)

    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "build_routing_catalog",
        _fail_then_succeed,
    )

    assert review_orchestrator.routing_catalog(tmp_path) is None
    assert review_orchestrator.routing_catalog(tmp_path) == _CATALOG
    assert len(builds) == 2


def test_the_routing_catalog_is_built_once_per_pass_not_once_per_action(
    monkeypatch, tmp_path: Path
) -> None:
    """12 actions in a drain pass must not pay the measured +1.49s twelve times."""
    review_orchestrator.reset_routing_catalog_cache()
    _ready_storage(monkeypatch)
    _capture_rank(monkeypatch, [])
    builds: list[int] = []
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "build_routing_catalog",
        lambda _repo: builds.append(1) or dict(_CATALOG),
    )

    for _ in range(12):
        review_orchestrator.select_reviewer_route(tmp_path, "QUALITY_REVIEW", "security")

    assert len(builds) == 1


def test_each_drain_pass_starts_from_a_fresh_routing_catalog(tmp_path: Path) -> None:
    """Evidence may go stale within a pass, never across one."""
    review_orchestrator.reset_routing_catalog_cache()
    review_orchestrator._ROUTING_CATALOG_CACHE["primed"] = (0.0, dict(_CATALOG))
    driver = review_orchestrator.ReviewOrchestrator(
        _Manager(tmp_path), db_path=tmp_path / "review.sqlite", route_selector=_route
    )

    driver.drain(max_actions=1, now=NOW)

    assert review_orchestrator._ROUTING_CATALOG_CACHE == {}


def test_workspace_binding_commits_and_closes_its_connection(
    monkeypatch, tmp_path: Path
) -> None:
    """The two workspace-binding sites must close their connection at block exit
    while still committing the binding (the write site relied on ``__exit__``
    to commit, so the transaction boundary has to be preserved)."""
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "bindings.sqlite", route_selector=_route
    )

    real_connect = sqlite3.connect
    open_conns: list[sqlite3.Connection] = []

    class _Tracked(sqlite3.Connection):
        def close(self) -> None:
            if self in open_conns:
                open_conns.remove(self)
            super().close()

    def _tracking_connect(*args, **kwargs):
        kwargs["factory"] = _Tracked
        conn = real_connect(*args, **kwargs)
        open_conns.append(conn)
        return conn

    monkeypatch.setattr(review_orchestrator.sqlite3, "connect", _tracking_connect)

    driver._repair_expected_workspace(7, "workspace-xyz")
    assert open_conns == [], "write site left a sqlite connection open"

    # Commit was preserved: the just-written binding is durably readable.
    assert driver._expected_workspace_identity(7) == "workspace-xyz"
    assert open_conns == [], "read site left a sqlite connection open"


def test_archive_status_uses_canonical_task_envelope(monkeypatch, tmp_path: Path) -> None:
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "show_task",
        lambda _repo, task_id: {
            "returncode": 0,
            "stdout": '{"task_id":"' + task_id + '","status":"finished",'
            '"archived_at":"2026-08-29T00:00:00Z"}',
        },
    )

    assert driver._is_archived("REVIEWER") is True


def _show(status_by_task: dict) -> object:
    def show(_repo: Path, task_id: str) -> dict:
        if task_id not in status_by_task:
            return {"returncode": 1, "stdout": ""}
        return {"returncode": 0, "stdout": json.dumps(status_by_task[task_id])}

    return show


@pytest.mark.parametrize(
    "card",
    [
        {"task_id": "TARGET", "status": "finished", "archived_at": "2026-08-29T00:00:00Z"},
        {"task_id": "TARGET", "status": "superseded", "worker_status": "superseded"},
        {"task_id": "TARGET", "status": "blocked"},
        {"task_id": "TARGET", "status": "finished"},
    ],
)
def test_an_action_whose_target_left_review_retires_instead_of_failing(
    monkeypatch, tmp_path: Path, card: dict
) -> None:
    """A decided target cannot be driven through review -- that is not a failure.

    Failing it was: a failed action parks every later action in its chain, so
    one moot launch stranded eleven more. Measured on this repository: 129
    chains and 1,389 actions permanently unreservable, and every one of the 39
    chains still live targeted an already-decided card.
    """
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    monkeypatch.setattr(review_orchestrator.task_engine, "show_task", _show({"TARGET": card}))

    result = driver.drain(max_actions=1, now=NOW)

    assert result.completed == 1
    assert result.failed == 0
    assert manager.launches == [], "a decided target must not spawn a reviewer"
    rows = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
    assert rows[0]["state"] == "completed"
    receipt = json.loads(rows[0]["receipt_json"])
    assert receipt["obsolete_reason"].startswith("target_left_review:")
    assert receipt["result"]["state"] == "obsolete"


def test_a_reviewable_target_is_still_driven(monkeypatch, tmp_path: Path) -> None:
    """Retirement must not swallow live work."""
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    monkeypatch.setattr(
        review_orchestrator.task_engine, "show_task",
        _show({"TARGET": {"task_id": "TARGET", "status": "review"}}),
    )

    result = driver.drain(max_actions=1, now=NOW)

    assert result.completed == 1
    assert len(manager.launches) == 1, "a target still in review must be launched"


def test_an_unreadable_card_is_never_treated_as_decided(monkeypatch, tmp_path: Path) -> None:
    """Fail closed: not knowing is not the same as knowing it is over."""
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    monkeypatch.setattr(
        review_orchestrator.task_engine, "show_task",
        lambda _repo, _task_id: {"returncode": 1, "stdout": ""},
    )
    assert driver._target_left_review("TARGET") == ""

    monkeypatch.setattr(
        review_orchestrator.task_engine, "show_task",
        lambda _repo, _task_id: {"returncode": 0, "stdout": "not json"},
    )
    assert driver._target_left_review("TARGET") == ""


def test_needfix_close_is_not_retired_by_a_decided_target() -> None:
    """Bookkeeping outlives the review it followed.

    needfix_close resolves NeedFix rows linked to the target. That stays
    meaningful once the target is accepted or archived, so it is deliberately
    absent from the driving set.
    """
    assert "needfix_close" not in review_orchestrator.REVIEW_DRIVING_ACTIONS
    assert review_orchestrator.REVIEW_DRIVING_ACTIONS == {
        "launch", "accept", "archive", "target_accept", "target_archive",
    }


def test_needfix_close_survives_a_chain_whose_reviewers_were_retired(
    monkeypatch, tmp_path: Path
) -> None:
    """Bookkeeping must not die of a dependency it never had.

    reviewer_request_ids were hoisted above the action branch, so needfix_close
    -- which never reads them -- raised KeyError as soon as the reviewer
    actions ahead of it were retired as obsolete. Measured live the moment
    retirement landed.
    """
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    monkeypatch.setattr(
        review_orchestrator.task_engine, "show_task",
        _show({"TARGET": {"task_id": "TARGET", "status": "finished",
                          "archived_at": "2026-08-29T00:00:00Z"}}),
    )
    monkeypatch.setattr(
        review_orchestrator.needfix_store, "list_needfix",
        lambda _repo, **_kwargs: [],
    )

    # eleven retirements, then the bookkeeping action itself
    result = driver.drain(max_actions=12, now=NOW)

    assert result.failed == 0, "no action in a retired chain may fail"
    assert result.completed == 12
    rows = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
    assert [r["state"] for r in rows] == ["completed"] * 12
    last = json.loads(rows[-1]["receipt_json"])
    assert last["action_type"] == "needfix_close"
    assert "obsolete_reason" not in last, "bookkeeping ran, it was not retired"


# --- mechanical short-circuit ------------------------------------------------
#
# 92.3% of this repository's 2,470 reject_review events were mechanically
# decidable, yet every one spent a quality-reviewer launch (avg 453,650 input
# tokens) to discover. The deterministic verdict that decides them is already
# computed and already on the card; these tests pin that the queue reads it,
# and -- far more important -- that it reads it ONE-DIRECTIONALLY. A wrong
# short-circuit lets defective code through unreviewed, which is much worse
# than the token waste being fixed, so every ambiguous verdict must still
# launch the reviewer.


def _verdict(
    *,
    applicable: object = True,
    passed: object = False,
    claim_epoch: object = "1",
    nothing_measured: object = False,
    failed: object = 2,
    missing: object = 0,
) -> dict:
    """Build a deterministic_verification exactly as task_fsm emits one."""
    return {
        "applicable": applicable,
        "pass": passed,
        "substatus": "review_ready",
        "reason": "evidence_verdict_failed",
        "claim_epoch": claim_epoch,
        "evidence_verdict": {
            "passed": False,
            "nothing_measured": nothing_measured,
            "validation_count": 3,
            "failed_validation_count": failed,
            "required_output_count": 1,
            "missing_required_output_count": missing,
        },
    }


def _drive(tmp_path: Path, db: str, **card_overrides: object):
    manager = _Manager(tmp_path)
    manager.target_status = _target_status(**card_overrides)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / db, route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    return manager, driver, driver.drain(max_actions=1, now=NOW)


def test_mechanically_failing_candidate_never_spends_a_reviewer_launch(
    tmp_path: Path,
) -> None:
    manager, _driver, result = _drive(
        tmp_path, "mech.sqlite", deterministic_verification=_verdict()
    )

    assert manager.launches == [], "a measured red candidate must not be reviewed"
    assert result.failed == 1
    row = review_lifecycle.rows_for_test(tmp_path / "mech.sqlite")[0]
    assert "mechanically_failing_candidate" in row["failure_reason"]
    # The reason is a measurement, not a label: it names the counts it read.
    assert "failed_validation_count=2" in row["failure_reason"]

    # The target's own event stream explains the transition too.
    event = manager.events[-1]
    assert event["event_type"] == "review_orchestrator_mechanical_rework"
    assert event["task_id"] == "TARGET"
    automation = event["review_automation"]
    assert automation["state"] == "mechanical_rework"
    assert automation["reviewer_launched"] is False
    assert automation["disposition"] == "return_for_rework"
    assert "mechanically_failing_candidate" in automation["reason"]


def test_missing_required_output_alone_short_circuits(tmp_path: Path) -> None:
    """The 8.4% unwired/required-output-unchanged class is mechanical too."""
    manager, _driver, result = _drive(
        tmp_path, "missing.sqlite",
        deterministic_verification=_verdict(failed=0, missing=3),
    )

    assert manager.launches == []
    assert result.failed == 1
    reason = review_lifecycle.rows_for_test(tmp_path / "missing.sqlite")[0]["failure_reason"]
    assert "missing_required_output_count=3" in reason


def test_verdict_on_terminal_review_is_read_like_core_reads_it(tmp_path: Path) -> None:
    """core.py prefers terminal_review's copy; so must the queue."""
    manager, _driver, result = _drive(
        tmp_path, "nested.sqlite",
        terminal_review={"deterministic_verification": _verdict()},
    )

    assert manager.launches == []
    assert result.failed == 1


def test_absent_verdict_still_launches_the_reviewer(tmp_path: Path) -> None:
    """No verdict is not a failing verdict. It is no measurement at all."""
    manager, _driver, result = _drive(tmp_path, "absent.sqlite")

    assert len(manager.launches) == 1, "an unmeasured candidate must be reviewed"
    assert result.completed == 1


def test_nothing_measured_verdict_still_launches_the_reviewer(tmp_path: Path) -> None:
    """The exact inversion the fail-closed doctrine forbids.

    ``nothing_measured`` means no validation ran and no required output was
    checked. Short-circuiting on it would silently convert "we did not
    measure" into "it passed" -- and it is precisely the candidate that most
    needs a human-grade review.
    """
    manager, _driver, result = _drive(
        tmp_path, "vacuum.sqlite",
        deterministic_verification=_verdict(
            nothing_measured=True, failed=0, missing=0
        ),
    )

    assert len(manager.launches) == 1
    assert result.completed == 1


def test_nothing_measured_still_launches_even_with_a_nonzero_count(
    tmp_path: Path,
) -> None:
    """A self-contradicting verdict is unreadable, so it gets a reviewer."""
    manager, _driver, result = _drive(
        tmp_path, "contradiction.sqlite",
        deterministic_verification=_verdict(nothing_measured=True, failed=5),
    )

    assert len(manager.launches) == 1
    assert result.completed == 1


def test_stale_verdict_from_another_claim_still_launches_the_reviewer(
    tmp_path: Path,
) -> None:
    """A verdict about a PREVIOUS attempt must never decide this one.

    The chain is bound to claim_epoch 1; this verdict was recorded against
    claim_epoch 0, so it is evidence about code that has since been reworked.
    """
    manager, _driver, result = _drive(
        tmp_path, "stale.sqlite",
        deterministic_verification=_verdict(claim_epoch="0"),
    )

    assert len(manager.launches) == 1
    assert result.completed == 1


@pytest.mark.parametrize(
    ("label", "verdict"),
    [
        ("not_a_mapping", "evidence_verdict_failed"),
        ("empty", {}),
        ("applicable_missing", {"pass": False, "claim_epoch": "1"}),
        ("applicable_false", _verdict(applicable=False)),
        ("applicable_truthy_not_true", _verdict(applicable=1)),
        ("pass_missing", {"applicable": True, "claim_epoch": "1"}),
        ("pass_none", _verdict(passed=None)),
        ("pass_true", _verdict(passed=True)),
        ("evidence_verdict_missing", {
            "applicable": True, "pass": False, "claim_epoch": "1",
        }),
        ("evidence_verdict_not_a_mapping", {
            "applicable": True, "pass": False, "claim_epoch": "1",
            "evidence_verdict": [1, 2, 3],
        }),
        ("nothing_measured_missing", {
            "applicable": True, "pass": False, "claim_epoch": "1",
            "evidence_verdict": {"failed_validation_count": 4},
        }),
        ("counts_all_zero", _verdict(failed=0, missing=0)),
        ("count_is_a_bool", _verdict(failed=True, missing=False)),
        ("count_is_a_string", _verdict(failed="7", missing="0")),
        ("count_is_negative", _verdict(failed=-3, missing=0)),
        ("count_is_a_float", _verdict(failed=2.0, missing=0)),
    ],
)
def test_unreadable_or_unmeasured_verdicts_always_launch(
    tmp_path: Path, label: str, verdict: object
) -> None:
    """Every ambiguous shape falls through to a real review.

    None of these is a positive, explicit, measured mechanical failure, so
    none of them may spend a card's review. When in doubt, launch.
    """
    manager, _driver, result = _drive(
        tmp_path, f"fallthrough-{label}.sqlite",
        deterministic_verification=verdict,
    )

    assert len(manager.launches) == 1, f"{label} must still be reviewed"
    assert result.completed == 1


def test_mechanical_verdict_never_overrides_an_identity_mismatch(
    tmp_path: Path,
) -> None:
    """Identity is checked first; a verdict cannot mask a tampered card."""
    manager, _driver, result = _drive(
        tmp_path, "identity-first.sqlite",
        candidate_sha256="c" * 64,
        deterministic_verification=_verdict(),
    )

    assert manager.launches == []
    assert result.failed == 1
    reason = review_lifecycle.rows_for_test(
        tmp_path / "identity-first.sqlite"
    )[0]["failure_reason"]
    assert "target_candidate_identity_invalid" in reason
    assert "mechanically_failing_candidate" not in reason


def test_mechanical_verdict_is_not_read_before_the_card_is_review_ready(
    tmp_path: Path,
) -> None:
    """A still-running card's verdict describes an unfinished attempt."""
    manager, _driver, result = _drive(
        tmp_path, "not-ready.sqlite",
        deterministic_verification=_verdict(),
    )
    assert result.failed == 1  # sanity: review_ready by default

    manager2 = _Manager(tmp_path)
    manager2.target_status = _target_status(
        state="processing", deterministic_verification=_verdict()
    )
    driver2 = review_orchestrator.ReviewOrchestrator(
        manager2, db_path=tmp_path / "processing.sqlite", route_selector=_route
    )
    driver2.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    pending = driver2.drain(max_actions=1, now=NOW)

    assert pending.pending == 1, "not-ready must defer, not mechanically reject"
    assert manager2.launches == []
    assert manager2.events[-1]["review_automation"]["reason"] == "target_not_review_ready"


def test_mechanical_failure_reason_is_pure_and_total() -> None:
    """The predicate never raises and never mutates, on any input."""
    reason = review_orchestrator.mechanical_failure_reason
    for hostile in (None, "", 0, [], (), object(), {"terminal_review": 5}):
        assert reason(hostile, "1") == ""
    card = {"deterministic_verification": _verdict()}
    snapshot = json.dumps(card, sort_keys=True)
    assert reason(card, "1").startswith("mechanically_failing_candidate:")
    assert json.dumps(card, sort_keys=True) == snapshot, "input was mutated"
    # An empty bound epoch must never match a recorded one.
    assert reason(card, "") == ""


def test_unknown_readiness_outcome_never_falls_through_to_a_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed on a readiness vocabulary the launch branch cannot read."""
    manager = _Manager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "unknown.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    monkeypatch.setattr(
        review_orchestrator.ReviewOrchestrator,
        "_launch_readiness",
        lambda self, action: {"outcome": "a_new_idea", "reason": "whatever"},
    )

    result = driver.drain(max_actions=1, now=NOW)

    assert manager.launches == []
    assert result.failed == 1
    reason = review_lifecycle.rows_for_test(tmp_path / "unknown.sqlite")[0]["failure_reason"]
    assert "launch_readiness_outcome_unknown:a_new_idea" in reason


# --- returning the mechanically failing card for rework -----------------------
#
# Refusing to spend a reviewer is only half the fix: the card still has to
# LEAVE review, or the manager has to dispose of every one of them by hand.
# The orchestrator has no canonical write surface of its own -- importing core
# here would be circular -- so it asks the manager, which delegates to
# core.reject_review. Every test below is about the half that can go wrong: a
# rejection that is refused, that raises, or that the manager cannot perform at
# all. The one outcome none of them may produce is a card that left review with
# nothing saying why, or a card believed returned that was not.


class _RejectingManager(_Manager):
    """A manager whose ``reject_review`` really returns the card to pending."""

    def __init__(
        self, repo: Path, *, result: object = None, raises: bool = False
    ) -> None:
        super().__init__(repo)
        self.rejects: list[tuple] = []
        self.card = {"task_id": "TARGET", "status": "review", "worker_status": "review"}
        self._result = result
        self._raises = raises

    def reject_review(self, task_id, reason, *, to="pending"):
        self.rejects.append((task_id, reason, to))
        if self._raises:
            raise RuntimeError("canonical store is locked")
        if self._result is not None:
            return self._result
        self.card = {"task_id": task_id, "status": to, "worker_status": "unclaimed"}
        return {
            "ok": True,
            "returncode": 0,
            "stdout": json.dumps(self.card),
            "task_id": task_id,
            "to": to,
            "learning_commit_owed": {"outcome": "rejected"},
        }


def _drive_rejecting(tmp_path: Path, db: str, manager, monkeypatch, **card_overrides):
    """Drive one mechanically failing chain against a live-ish card reader."""
    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "show_task",
        lambda _repo, task_id: (
            {"returncode": 0, "stdout": json.dumps(manager.card)}
            if task_id == "TARGET"
            else {"returncode": 1, "stdout": ""}
        ),
    )
    manager.target_status = _target_status(
        deterministic_verification=_verdict(), **card_overrides
    )
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / db, route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    return driver


def _failure_reason(db_path: Path) -> str:
    return str(review_lifecycle.rows_for_test(db_path)[0]["failure_reason"] or "")


def test_a_mechanically_failing_candidate_is_returned_to_pending_for_rework(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card leaves review by itself, with the measurement as its reason."""
    manager = _RejectingManager(tmp_path)
    driver = _drive_rejecting(tmp_path, "returned.sqlite", manager, monkeypatch)

    result = driver.drain(max_actions=1, now=NOW)

    assert manager.launches == [], "a measured red candidate must not be reviewed"
    assert len(manager.rejects) == 1, "exactly one rejection, never a retry storm"
    task_id, reason, to = manager.rejects[0]
    assert task_id == "TARGET"
    assert to == "pending", "rework, not blocked -- blocked is not a quality signal"
    # The reason handed to the canonical store is the measurement itself.
    assert reason.startswith("mechanically_failing_candidate:")
    assert "failed_validation_count=2" in reason
    assert manager.card["status"] == "pending"

    # Both durable surfaces still explain the transition.
    assert result.failed == 1
    outbox = _failure_reason(tmp_path / "returned.sqlite")
    assert "failed_validation_count=2" in outbox
    assert "returned_for_rework=returned" in outbox
    automation = manager.events[-1]["review_automation"]
    assert automation["rework_return"] == {"state": "returned", "detail": "pending"}
    assert automation["disposition"] == "return_for_rework"


def test_a_refused_rejection_still_records_the_reason_and_keeps_the_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejection that did not happen must never be reported as one.

    core refuses any move whose row is not still ``worker_status='review'``.
    When it does, the card stays exactly where it was and the manager disposes
    of it -- which costs one reviewer launch. Believing a refused rejection
    instead would leave the card in review with the chain saying it left, and
    a reader would have no way to tell. The refusal detail is carried verbatim.
    """
    manager = _RejectingManager(
        tmp_path,
        result={
            "ok": False,
            "returncode": 1,
            "stderr": "reject_not_reviewable:task_id=TARGET",
            "error": "reject_not_reviewable:task_id=TARGET",
        },
    )
    driver = _drive_rejecting(tmp_path, "refused.sqlite", manager, monkeypatch)

    result = driver.drain(max_actions=1, now=NOW)

    assert len(manager.rejects) == 1
    assert manager.card["status"] == "review", "a refused rejection moves nothing"
    assert manager.launches == []
    assert result.failed == 1
    outbox = _failure_reason(tmp_path / "refused.sqlite")
    assert "failed_validation_count=2" in outbox, "the measurement is never lost"
    assert "returned_for_rework=refused" in outbox
    assert "reject_not_reviewable:task_id=TARGET" in outbox
    assert manager.events[-1]["review_automation"]["rework_return"]["state"] == "refused"


def test_a_raising_rejection_is_a_refusal_and_never_a_lost_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store fault must not replace the mechanical reason with a traceback."""
    manager = _RejectingManager(tmp_path, raises=True)
    driver = _drive_rejecting(tmp_path, "raising.sqlite", manager, monkeypatch)

    result = driver.drain(max_actions=1, now=NOW)

    assert manager.card["status"] == "review"
    assert manager.launches == []
    assert result.failed == 1
    outbox = _failure_reason(tmp_path / "raising.sqlite")
    assert "failed_validation_count=2" in outbox
    assert "returned_for_rework=error" in outbox
    assert "RuntimeError" in outbox
    assert manager.events[-1]["review_automation"]["rework_return"]["state"] == "error"


def test_a_manager_with_no_reject_surface_still_records_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The short-circuit predates the reject surface and must outlive its absence."""
    manager = _Manager(tmp_path)
    manager.card = {"task_id": "TARGET", "status": "review"}
    driver = _drive_rejecting(tmp_path, "no-surface.sqlite", manager, monkeypatch)
    assert not hasattr(manager, "reject_review")

    result = driver.drain(max_actions=1, now=NOW)

    assert manager.launches == []
    assert result.failed == 1
    outbox = _failure_reason(tmp_path / "no-surface.sqlite")
    assert "failed_validation_count=2" in outbox
    assert "returned_for_rework=unavailable" in outbox


def test_a_reviewable_candidate_is_never_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One-directional in both halves: no verdict means review, never reject.

    The rejection is reachable ONLY from a positive, measured, in-epoch
    mechanical failure. Every other card gets its reviewer, and none of them
    may be moved out of review by this queue.
    """
    manager = _RejectingManager(tmp_path)
    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "show_task",
        lambda _repo, _task_id: {"returncode": 0, "stdout": json.dumps(manager.card)},
    )
    manager.target_status = _target_status()  # no deterministic verdict at all
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "healthy.sqlite", route_selector=_route
    )
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )

    result = driver.drain(max_actions=1, now=NOW)

    assert result.completed == 1
    assert len(manager.launches) == 1
    assert manager.rejects == [], "an unmeasured candidate is reviewed, not rejected"
    assert manager.card["status"] == "review"


def test_a_raising_event_surface_never_replaces_the_mechanical_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outbox row must carry the measurement even if the event write fails.

    _record_mechanical_rework is documented best-effort, but it only handled a
    manager with NO event surface. One that raises let the append error escape
    and become the outbox failure_reason, erasing the very explanation the
    method exists to record.
    """
    manager = _RejectingManager(tmp_path)

    def _boom(_event):
        raise OSError("event log is full")

    monkeypatch.setattr(manager, "_append_event", _boom)
    driver = _drive_rejecting(tmp_path, "noisy-events.sqlite", manager, monkeypatch)

    result = driver.drain(max_actions=1, now=NOW)

    assert result.failed == 1
    outbox = _failure_reason(tmp_path / "noisy-events.sqlite")
    assert "failed_validation_count=2" in outbox
    assert "returned_for_rework=returned" in outbox
    assert "event log is full" not in outbox
    assert manager.card["status"] == "pending", "the rejection still happened"


def test_returning_a_target_to_pending_does_not_retire_the_rest_of_its_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured, not assumed: a returned card does NOT drain its own chain.

    ``reject_review(to="pending")`` moves the target out of ``review``, but
    ``pending`` is deliberately inside REVIEWABLE_TARGET_STATUSES -- it is a
    status a target can still be driven through review FROM -- so
    ``_target_left_review`` reads "" and cannot retire the queued actions. The
    launch action fails with the mechanical reason and the remaining eleven
    stay parked, exactly as they did before any rejection existed.

    Pinned here rather than "fixed", because forcing the retirement would
    carry the chain on to ``needfix_close``, which resolves every linked
    NeedFix row with "automatic review lifecycle accepted and archived task
    <id>". That sentence is false about a card that was REJECTED. Trading a
    parked chain for a fabricated acceptance record is not an improvement,
    and this repository's worst defect class is exactly the state transition
    whose record does not match what happened.
    """
    assert "pending" in review_orchestrator.REVIEWABLE_TARGET_STATUSES
    manager = _RejectingManager(tmp_path)
    driver = _drive_rejecting(tmp_path, "chain.sqlite", manager, monkeypatch)

    first = driver.drain(max_actions=12, now=NOW)

    assert manager.card["status"] == "pending", "the target did leave review"
    assert driver._target_left_review("TARGET") == "", (
        "pending is still a drivable status, so nothing is retired"
    )
    assert first.attempted == 1 and first.failed == 1
    rows = review_lifecycle.rows_for_test(tmp_path / "chain.sqlite")
    assert [row["state"] for row in rows] == ["failed"] + ["pending"] * 11

    # And the chain is genuinely parked: a second pass reserves nothing.
    assert driver.drain(max_actions=12, now=NOW).attempted == 0


# ---------------------------------------------------------------------------
# The identity read (audit 2026-09-08, problem 1).
#
# Every fixture above uses TOP-LEVEL card keys, which nothing in production
# writes: measured over the live store's 627 chains, 0 target cards carry a
# top-level ``request_id`` and 522 of the 581 failed launch actions died with
# ``target_request_identity_invalid``. These fixtures use the shape
# ``_finalize_isolated_request`` actually seals.
# ---------------------------------------------------------------------------


def _sealed_changed_path_hashes() -> dict[str, str]:
    return {"src/aiworkhub/service.py": "c" * 64}


_SEALED_CANDIDATE_SHA256 = review_orchestrator.candidate_digest(
    {"src/aiworkhub/service.py": "c" * 64}
)


def _stub_archive(monkeypatch) -> None:
    """Archiving a finished reviewer is a store write, not what is under test."""
    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "archive_task",
        lambda _repo, task_id, **_kwargs: {"ok": True, "task_id": task_id},
    )


def _sealed_target_status(
    *, state: str = "review_ready", evidence_overrides: dict | None = None,
    claim_epoch: str = "1",
) -> dict:
    """A card in the shape ``_finalize_isolated_request`` writes: no top-level
    ``request_id``/``packet_sha256``/``candidate_sha256``/``workspace_identity``
    anywhere, everything under ``terminal_review.evidence``."""
    workspace = {
        "request_id": "target-request",
        "path": "/candidate/worktree",
        "base_oid": "base-oid",
    }
    evidence = {
        "request_identity": {
            "request_id": "target-request",
            "task_id": "TARGET",
            "claim_epoch": claim_epoch,
        },
        "attempt_artifact_manifest": {"manifest_sha256": "a" * 64},
        "changed_path_hashes": _sealed_changed_path_hashes(),
        "workspace": workspace,
        "source_graph_partition_readiness": {"target": True},
    }
    evidence.update(evidence_overrides or {})
    return {
        "ok": True,
        "state": state,
        "task_card": {
            "task_id": "TARGET",
            "claim_epoch": claim_epoch,
            "terminal_review": {"substatus": "review_ready", "evidence": evidence},
            "evidence": {"source_graph_partition_readiness": {"target": True}},
        },
    }


def _sealed_chain(driver) -> None:
    driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64,
        candidate_sha256=review_orchestrator.candidate_digest(
            _sealed_changed_path_hashes()
        ),
        now=NOW,
    )


def test_identity_resolves_from_terminal_evidence_where_the_finalizer_writes_it(
    tmp_path: Path,
) -> None:
    """The read that made 504 of 563 outbox launch actions terminal-fail.

    Verified read-only against the live store: the old top-level read resolved
    0 of 627 chains; this read resolves 93 exactly, retires 332 as superseded
    and leaves 202 unresolvable (199 of whose targets have already left review
    and retire one check earlier).
    """
    manager = _Manager(tmp_path)
    manager.target_status = _sealed_target_status()
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "sealed.sqlite", route_selector=_route
    )
    _sealed_chain(driver)

    # The old read, on the same card, resolves nothing.
    card = manager.target_status["task_card"]
    assert review_orchestrator._legacy_card_identity(card) == {}

    resolved = review_orchestrator.resolve_target_identity(manager.target_status)
    assert resolved["identity_source"] == "terminal_review_evidence"
    assert resolved["identity"]["target_request_id"] == "target-request"
    assert resolved["identity"]["packet_sha256"] == "a" * 64
    assert resolved["workspace_identity"]

    result = driver.drain(max_actions=1, now=NOW)

    assert result.completed == 1 and result.failed == 0
    assert len(manager.launches) == 1
    receipt = json.loads(
        review_lifecycle.rows_for_test(tmp_path / "sealed.sqlite")[0]["receipt_json"]
    )
    assert receipt["target_readiness_receipt"]["identity_source"] == (
        "terminal_review_evidence"
    )


def test_registration_payload_answers_when_the_card_evidence_is_gone(
    tmp_path: Path,
) -> None:
    """The finalizer records the same five fields on the terminal event as
    ``review_automation.registration``; it is the only remaining statement of
    what a chain was bound to once the card's terminal evidence is replaced."""
    status = {
        "ok": True,
        "state": "review_ready",
        "task_card": {"task_id": "TARGET"},
        "latest_event": {
            "review_automation": {
                "registration": {
                    "target_task_id": "TARGET",
                    "target_request_id": "target-request",
                    "claim_epoch": "1",
                    "packet_sha256": "a" * 64,
                    "candidate_sha256": "b" * 64,
                }
            }
        },
    }
    resolved = review_orchestrator.resolve_target_identity(status)

    assert resolved["identity_source"] == "review_automation_registration"
    assert resolved["identity"]["candidate_sha256"] == "b" * 64
    assert resolved["conflict"] == ""


def test_two_durable_identities_that_disagree_fail_closed(tmp_path: Path) -> None:
    """A packet digest that differs between the card and the registration is
    two statements about which bytes are under review. Picking one silently is
    exactly what must not happen."""
    status = _sealed_target_status()
    status["latest_event"] = {
        "review_automation": {
            "registration": {
                "target_task_id": "TARGET",
                "target_request_id": "target-request",
                "claim_epoch": "1",
                "packet_sha256": "f" * 64,
                "candidate_sha256": review_orchestrator.candidate_digest(
                    _sealed_changed_path_hashes()
                ),
            }
        }
    }
    resolved = review_orchestrator.resolve_target_identity(status)
    assert resolved["conflict"] == "packet_sha256"

    manager = _Manager(tmp_path)
    manager.target_status = status
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "conflict.sqlite", route_selector=_route
    )
    _sealed_chain(driver)

    result = driver.drain(max_actions=1, now=NOW)

    assert result.failed == 1 and manager.launches == []
    assert "target_identity_conflict:packet_sha256" in (
        review_lifecycle.rows_for_test(tmp_path / "conflict.sqlite")[0]["failure_reason"]
    )


# ---------------------------------------------------------------------------
# The lens plan (audit 2026-09-08, problem 2).
#
# ``_RISK_PROFILES`` requires low () / medium (correctness) / high (correctness,
# security) / critical (all three), but the plan launched all three for every
# chain: 194 of 893 launches on accepted targets (21.7%) were of a lens the
# tier never required, at ~889K input tokens each.
# ---------------------------------------------------------------------------


def test_only_the_tier_required_lenses_are_launched(
    monkeypatch, tmp_path: Path
) -> None:
    _stub_archive(monkeypatch)
    manager = _Manager(tmp_path)
    manager.target_status = _sealed_target_status()
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "tier.sqlite", route_selector=_route
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64,
        candidate_sha256=review_orchestrator.candidate_digest(
            _sealed_changed_path_hashes()
        ),
        now=NOW, required_reviewer_lenses=["correctness"], effective_tier="medium",
    )
    manager.status_results["review-request-correctness"] = _review_status(
        "correctness", candidate_sha256=_SEALED_CANDIDATE_SHA256
    )

    assert review_orchestrator.required_lenses(
        tmp_path / "tier.sqlite", chain.chain_id
    ) == ("correctness",)

    # Nine reviewer actions: three do work, six retire without a reviewer.
    for _ in range(9):
        assert driver.drain(max_actions=1, now=NOW).completed == 1

    assert [row["lens"] for row in manager.launches] == ["correctness"]
    rows = review_lifecycle.rows_for_test(tmp_path / "tier.sqlite")
    obsolete = [
        json.loads(row["receipt_json"])["obsolete_reason"]
        for row in rows[:9]
        if row["state"] == "completed"
        and json.loads(row["receipt_json"]).get("obsolete_reason")
    ]
    assert obsolete == ["lens_not_required_by_tier:medium"] * 6


def test_an_unplanned_chain_still_gets_every_lens(tmp_path: Path) -> None:
    """Fail OPEN on the plan: a chain with no recorded tier reviews everything,
    which is what happened before the plan existed."""
    assert review_orchestrator.required_lenses(
        tmp_path / "absent.sqlite", 999
    ) == review_orchestrator.LENSES
    record = review_orchestrator.lens_plan_record(tmp_path / "absent.sqlite", 999)
    assert record["planned"] is False
    assert record["lenses"] == list(review_orchestrator.LENSES)


def test_manager_override_adds_a_lens_and_can_never_remove_one(tmp_path: Path) -> None:
    """The tier is a floor. A manager may ask for more review than it demands;
    asking for less would silently lower a bar ``accept_review`` still enforces.
    """
    db_path = tmp_path / "override.sqlite"
    review_orchestrator.bind_lens_plan(
        db_path, chain_id=7, lenses=["correctness"], effective_tier="medium"
    )
    assert review_orchestrator.add_required_lens(
        db_path, chain_id=7, lens="security"
    ) == ("correctness", "security")
    # Re-binding cannot shrink a bound plan.
    assert review_orchestrator.bind_lens_plan(
        db_path, chain_id=7, lenses=[], effective_tier="low"
    ) == ("correctness", "security")
    assert review_orchestrator.lens_plan_record(db_path, 7)["source"] == (
        "manager_override"
    )
    with pytest.raises(ValueError, match="unknown_review_lens"):
        review_orchestrator.add_required_lens(db_path, chain_id=7, lens="vibes")


def test_registration_carries_the_tier_from_the_finalizers_own_gate_record() -> None:
    registration = review_orchestrator.candidate_registration(
        metadata={"task_id": "TARGET", "request_id": "target-request", "claim_epoch": 1},
        artifact_receipt={"manifest_sha256": "a" * 64},
        changed_path_hashes=_sealed_changed_path_hashes(),
        quality_gate={
            "review_risk_profile": {
                "effective_tier": "high",
                "required_reviewer_lenses": ["correctness", "security"],
                "error": "",
            }
        },
    )
    assert registration["effective_tier"] == "high"
    assert registration["required_reviewer_lenses"] == ["correctness", "security"]

    # A gate whose observation failed plans nothing, so every lens runs.
    degraded = review_orchestrator.candidate_registration(
        metadata={"task_id": "TARGET", "request_id": "target-request", "claim_epoch": 1},
        artifact_receipt={"manifest_sha256": "a" * 64},
        changed_path_hashes=_sealed_changed_path_hashes(),
        quality_gate={"review_risk_profile": {"error": "ValueError:boom"}},
    )
    assert "required_reviewer_lenses" not in degraded


def test_drain_defaults_to_the_whole_pass_and_keeps_its_hard_bound(
    monkeypatch, tmp_path: Path
) -> None:
    """One action per reconcile pass could not work off 627 chains: measured, 30
    launches completed automatically while 570 were typed by hand. The BOUND is
    unchanged -- drain still clamps to DEFAULT_DRAIN_MAX_ACTIONS -- only the
    default request changed."""
    import inspect

    _stub_archive(monkeypatch)
    assert review_orchestrator.DEFAULT_DRAIN_MAX_ACTIONS == 12
    assert inspect.signature(
        review_orchestrator.ReviewOrchestrator.drain
    ).parameters["max_actions"].default == 12

    manager = _Manager(tmp_path)
    manager.target_status = _sealed_target_status()
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "drain.sqlite", route_selector=_route
    )
    _sealed_chain(driver)
    for lens in review_orchestrator.LENSES:
        manager.status_results["review-request-" + lens] = _review_status(
            lens, candidate_sha256=_SEALED_CANDIDATE_SHA256
        )

    result = driver.drain(now=NOW)

    # Nine reviewer actions in ONE pass; the chain then parks at target_accept.
    assert result.attempted <= review_orchestrator.DEFAULT_DRAIN_MAX_ACTIONS
    assert result.completed == 9
    assert len(manager.launches) == 3
    # A caller asking for more than the bound still gets the bound.
    assert driver.drain(max_actions=10_000, now=NOW).attempted <= (
        review_orchestrator.DEFAULT_DRAIN_MAX_ACTIONS
    )
