"""NF-2026-02397: never replay a reviewer whose terminal receipt is gone.

Measured 2026-11-24: chain 1662 replayed reviewer lenses from chain 1636 (same
candidate_sha256), but the source reviewer card had been archived with
``terminal_review={}``. Every replayed accept failed closed on
``reviewer_terminal_receipt_missing`` and the lens stayed missing for ever --
no fresh reviewer was launched.

Two shapes are pinned here:

* the replay source is already unreadable when the launch runs: the replay is
  ignored and a fresh reviewer is launched for the lens; and
* a chain already holding a replayed launch whose source receipt vanished:
  the accept retires the replayed attempt and plans a fresh reviewer, which is
  then verified and accepted through the ordinary path.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import review_lifecycle, review_orchestrator  # noqa: E402

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)
ROUTE = {
    "runner": "copilot_gpt-5.6-sol",
    "adapter_id": "vscode_lm",
    "model": "gpt-5.6-sol",
}
ALT_ROUTE = {
    "runner": "copilot_claude-sonnet-4.5",
    "adapter_id": "vscode_lm",
    "model": "claude-sonnet-4.5",
}
CANDIDATE = "b" * 64
MANIFEST_1 = "a" * 64
MANIFEST_2 = "c" * 64
LENS = "correctness"


class _Selector:
    def __init__(self) -> None:
        self.route = dict(ROUTE)

    def __call__(self, _repo: Path, _task_id: str, _lens: str) -> dict[str, str]:
        return dict(self.route)


class _Manager:
    """Readable target cards for every request; one request id per reviewer."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.launches: list[dict] = []
        self.accepts: list[tuple[str, str]] = []
        self.archives: list[str] = []
        self.events: list[dict] = []
        self.targets: dict[str, dict] = {}
        self.status_results: dict[str, dict] = {}

    def launch_quality_reviewer(self, **kwargs):
        self.launches.append(kwargs)
        return {
            "ok": True,
            "request_id": _request_for(kwargs["reviewer_task_id"]),
            "task_id": kwargs["reviewer_task_id"],
            "state": "starting",
        }

    def status(self, request_id):
        if request_id in self.targets:
            return dict(self.targets[request_id])
        return {
            "request_id": request_id,
            **self.status_results.get(request_id, {"ok": True, "state": "starting"}),
        }

    def accept_review(self, request_id, task_id, **kwargs):
        self.accepts.append((request_id, task_id))
        return {"ok": True, "request_id": request_id, "task_id": task_id, **kwargs}

    def _append_event(self, event):
        self.events.append(event)


def _request_for(reviewer_task_id: str) -> str:
    return "review-request-" + reviewer_task_id


def _target_status(request_id: str, *, packet_sha256: str) -> dict:
    return {
        "ok": True,
        "state": "review_ready",
        "task_card": {
            "task_id": "TARGET",
            "request_id": request_id,
            "claim_epoch": "1",
            "packet_sha256": packet_sha256,
            "candidate_sha256": CANDIDATE,
            "workspace_identity": "workspace-candidate-a",
            "objective": "make the thing correct",
            "acceptance": ["the test passes"],
            "required_outputs": ["src/module.py"],
            "validation": ["pytest -q"],
            "evidence": {"source_graph_partition_readiness": {"target": True}},
            "terminal_review": {
                "evidence": {
                    "quality_gate": {
                        "review_risk_profile": {
                            "effective_tier": "medium",
                            "required_reviewer_lenses": [LENS],
                        }
                    }
                }
            },
        },
    }


def _review_status(*, target_request_id: str, reviewer_task_id: str) -> dict:
    provider = "codex_cli"
    packet_sha256 = hashlib.sha256(
        f"review-packet:{target_request_id}:{LENS}".encode()
    ).hexdigest()
    receipt = {
        "schema_id": "aiworkhub.quality_review_receipt.v1",
        "packet_sha256": packet_sha256,
        "target": {"request_id": target_request_id, "task_id": "TARGET", "claim_epoch": 1},
        "reviewer": {
            "request_id": _request_for(reviewer_task_id),
            "task_id": reviewer_task_id,
            "provider": provider,
        },
        "report": {
            "lens": LENS,
            "provider": provider,
            "read_only": True,
            "can_mutate_repo": False,
            "findings": [],
        },
        "authority": {
            "process_identity_verified": True,
            "audit_verified": True,
            "terminal_state": "review_ready",
        },
        "submission_id": hashlib.sha256(
            f"submission:{target_request_id}:{reviewer_task_id}".encode()
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
                        "lens": LENS,
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


def _archived_status() -> dict:
    """The measured shape: still review_ready, but terminal_review is empty."""
    return {
        "ok": True,
        "state": "review_ready",
        "adapter_id": "codex_cli",
        "latest_event": {},
        "task_card": {"terminal_review": {}},
    }


def _first_chain(tmp_path: Path, monkeypatch):
    manager = _Manager(tmp_path)
    selector = _Selector()
    monkeypatch.setattr(
        review_orchestrator.task_engine,
        "archive_task",
        lambda repo, task_id, **kwargs: manager.archives.append(task_id)
        or {"ok": True},
    )
    manager.targets["target-request"] = _target_status(
        "target-request", packet_sha256=MANIFEST_1
    )
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / "review.sqlite", route_selector=selector
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET",
        target_request_id="target-request",
        claim_epoch=1,
        packet_sha256=MANIFEST_1,
        candidate_sha256=CANDIDATE,
        now=NOW,
    )
    assert driver.drain(max_actions=1, now=NOW).completed == 1  # launch
    source_task = review_orchestrator.ReviewOrchestrator._reviewer_task_id(
        chain.chain_identity, LENS
    )
    manager.status_results[_request_for(source_task)] = _review_status(
        target_request_id="target-request", reviewer_task_id=source_task
    )
    assert driver.drain(max_actions=1, now=NOW).completed == 1  # accept
    assert driver.drain(max_actions=1, now=NOW).completed == 1  # archive
    driver.drain(max_actions=12, now=NOW)
    return manager, selector, driver, chain, source_task


def _successor(driver, manager):
    manager.targets["target-request-2"] = _target_status(
        "target-request-2", packet_sha256=MANIFEST_2
    )
    return driver.ensure_chain(
        target_task_id="TARGET",
        target_request_id="target-request-2",
        claim_epoch=1,
        packet_sha256=MANIFEST_2,
        candidate_sha256=CANDIDATE,
        now=NOW,
    )


def _receipt(driver, chain_id: int, action_type: str) -> dict:
    receipts = review_lifecycle.completed_receipts_for_chain(driver.db_path, chain_id)
    return next(r for r in receipts if r["action_type"] == action_type)


def test_a_replay_whose_source_receipt_is_gone_launches_a_fresh_reviewer(
    tmp_path: Path, monkeypatch
) -> None:
    manager, _selector, driver, _first, source_task = _first_chain(
        tmp_path, monkeypatch
    )
    # The source reviewer card is archived with terminal_review={}.
    manager.status_results[_request_for(source_task)] = _archived_status()
    launched_before = len(manager.launches)

    second = _successor(driver, manager)
    # The replay was bound at registration; it is the launch that must refuse it.
    assert driver._replay_plan(second.chain_id, LENS)
    result = driver.drain(max_actions=1, now=NOW)

    assert result.failed == 0
    assert len(manager.launches) == launched_before + 1
    assert manager.launches[-1]["lens"] == LENS
    launch = _receipt(driver, second.chain_id, "launch")
    assert not launch.get("replayed_from_chain")
    assert not launch.get("replay")
    assert launch["reviewer_task_id"] != source_task


def test_a_stuck_replayed_accept_plans_a_fresh_reviewer_and_recovers(
    tmp_path: Path, monkeypatch
) -> None:
    manager, selector, driver, first, source_task = _first_chain(
        tmp_path, monkeypatch
    )
    launched_before = len(manager.launches)
    accepted_before = len(manager.accepts)
    second = _successor(driver, manager)
    assert driver.drain(max_actions=1, now=NOW).completed == 1  # replayed launch
    launch = _receipt(driver, second.chain_id, "launch")
    assert launch["replayed_from_chain"] == first.chain_id
    assert len(manager.launches) == launched_before

    # The source card is archived AFTER the replay was launched: the state
    # chain 1662 was measured in.
    manager.status_results[_request_for(source_task)] = _archived_status()
    selector.route = dict(ALT_ROUTE)
    result = driver.drain(max_actions=1, now=NOW)

    assert result.failed == 0
    assert len(manager.launches) == launched_before + 1
    fresh = manager.launches[-1]
    assert fresh["lens"] == LENS
    fresh_task = str(fresh["reviewer_task_id"])
    assert fresh_task != source_task
    attempts = driver._route_attempts(second.chain_id, LENS)
    assert attempts[0]["state"] == "retired"
    assert attempts[0]["failure_reason"] == (
        review_orchestrator._REPLAY_SOURCE_RECEIPT_MISSING_RETIREMENT
    )
    assert attempts[-1]["reviewer_task_id"] == fresh_task

    # The fresh reviewer reports against THIS chain's target and is accepted
    # through the ordinary verifier -- not as a replay.
    manager.status_results[_request_for(fresh_task)] = _review_status(
        target_request_id="target-request-2", reviewer_task_id=fresh_task
    )
    driver.drain(max_actions=12, now=NOW)
    receipts = review_lifecycle.completed_receipts_for_chain(
        driver.db_path, second.chain_id
    )
    accept = next(
        r for r in receipts if r["action_type"] == "accept" and r["lens"] == LENS
    )
    assert accept["reviewer_task_id"] == fresh_task
    assert not accept.get("replayed_from_chain")
    assert manager.accepts[accepted_before:] == [(_request_for(fresh_task), fresh_task)]
