from __future__ import annotations

import sys
import hashlib
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

    def launch_quality_reviewer(self, **kwargs):
        self.launches.append(kwargs)
        return {
            "ok": True,
            "request_id": "review-request-" + kwargs["lens"],
            "task_id": kwargs["reviewer_task_id"],
            "state": "starting",
        }

    def status(self, request_id):
        return {
            "request_id": request_id,
            **self.status_results.get(request_id, self.status_result),
        }

    def accept_review(self, request_id, task_id, **kwargs):
        self.accepts.append((request_id, task_id))
        return {"ok": True, "request_id": request_id, "task_id": task_id, **kwargs}


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


def test_happy_path_is_exactly_ordered_and_needfix_stays_pending(
    monkeypatch, tmp_path: Path
) -> None:
    manager = _Manager(tmp_path)
    archived: list[str] = []
    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "archive_task",
        lambda _repo, task_id, **_kwargs: archived.append(task_id) or {"ok": True},
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

    for _ in range(11):
        result = driver.drain(max_actions=1, now=NOW)
        rows_now = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
        failed = [row["failure_reason"] for row in rows_now if row["state"] == "failed"]
        assert result.completed == 1, failed
        assert result.failed == 0
    deferred = driver.drain(max_actions=1, now=NOW)

    assert deferred.pending == 1
    assert [row["lens"] for row in manager.launches] == list(review_orchestrator.LENSES)
    assert [row["runner"] for row in manager.launches] == [ROUTE["runner"]] * 3
    assert manager.accepts[-1] == ("target-request", "TARGET")
    assert archived[-1] == "TARGET"
    rows = review_lifecycle.rows_for_test(tmp_path / "review.sqlite")
    assert [row["state"] for row in rows[:11]] == ["completed"] * 11
    assert rows[11]["action_type"] == "needfix_close"
    assert rows[11]["state"] == "pending"


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
