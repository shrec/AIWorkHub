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
        ("request_id", "target_request_identity_invalid"),
        ("claim_epoch", "target_claim_identity_invalid"),
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


def _review_status(
    lens: str = "correctness", *, findings: list[dict] | None = None
) -> dict:
    reviewer_request = "review-request-" + lens
    receipt = {
        "schema_id": "aiworkhub.quality_review_receipt.v1",
        "packet_sha256": "a" * 64,
        "target": {"request_id": "target-request", "task_id": "TARGET", "claim_epoch": 1},
        "reviewer": {
            "request_id": reviewer_request,
            "task_id": review_orchestrator.ReviewOrchestrator._reviewer_task_id(
                {
                    "schema_id": "aiworkhub.review_lifecycle.v1",
                    "target_task_id": "TARGET", "target_request_id": "target-request",
                    "claim_epoch": "1", "packet_sha256": "a" * 64,
                    "candidate_sha256": "b" * 64,
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
    return {
        "ok": True, "state": "review_ready", "adapter_id": "codex_cli",
        "latest_event": {"quality_review_receipt": receipt},
        "task_card": {"terminal_review": {"evidence": {"quality_review_receipt": receipt}}},
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


def test_happy_path_is_exactly_ordered_and_closes_linked_needfix(
    monkeypatch, tmp_path: Path
) -> None:
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


def test_default_route_uses_canonical_workforce_contract(monkeypatch, tmp_path: Path) -> None:
    captured: list[object] = []
    monkeypatch.setattr(
        review_orchestrator.task_store,
        "storage_readiness",
        lambda _repo: SimpleNamespace(
            ready=True, reason="ready", repo_id="repo-test", canonical_db="queue.sqlite"
        ),
    )
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "rank_task",
        lambda _repo, task: captured.append(task) or {"launch_contract": dict(ROUTE)},
    )

    route = review_orchestrator.select_reviewer_route(
        tmp_path, "QUALITY_REVIEW_EXACT", "security"
    )

    assert route == ROUTE
    assert captured[0].kinds == frozenset({"review"})
    assert captured[0].risk == "critical"
    assert "session-manager" in captured[0].tool_needs


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
