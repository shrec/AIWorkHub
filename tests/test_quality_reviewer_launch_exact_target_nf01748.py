"""NF-2026-01748: a reviewer launch names one exact target request or nothing.

``launch_quality_reviewer`` used to reserve a reviewer attempt before anything
confirmed that ``target_request_id`` was a ledger request of
``target_task_id``; the exact lookup ran only later, on the background packet
build. A prefix of a real request id was therefore accepted at launch. The
gate now runs synchronously before the reservation and never resolves a
prefix to a request.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiworkhub import process_launcher

RUNNER = "codex"
ADAPTER = "codex_cli"
TARGET_TASK = "TARGET_TASK"
REAL_REQUEST = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


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


def _seed(manager: process_launcher.ProcessManager, request_id: str, task_id: str) -> None:
    manager._append_event({
        "request_id": request_id, "task_id": task_id, "runner": RUNNER,
        "adapter_id": ADAPTER, "state": "review_ready",
    })


def _spied(
    manager: process_launcher.ProcessManager, monkeypatch: pytest.MonkeyPatch
) -> dict[str, list]:
    """Make the target launchable and record every downstream side effect."""
    calls: dict[str, list] = {
        "reserve": [], "bind": [], "launch": [], "create": [],
    }

    def show(task_id: str) -> dict[str, object]:
        return {
            "returncode": 0,
            "stdout": json.dumps({
                "task_id": task_id,
                "terminal_review": {"substatus": "review_ready"},
            }),
            "stderr": "",
        }

    monkeypatch.setattr(manager, "_show_task", show)
    monkeypatch.setattr(
        manager, "_live_reviewer_receipt", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        process_launcher.quality_review,
        "assess_reviewer_launch_target",
        lambda **_kwargs: {"can_launch": True},
    )

    def reserve(**kwargs: object) -> dict[str, object]:
        calls["reserve"].append(kwargs)
        return {"ok": True, "request_id": "reviewer-request"}

    def bind(**kwargs: object) -> dict[str, object]:
        calls["bind"].append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(manager, "_reserve_quality_reviewer_attempt", reserve)
    monkeypatch.setattr(manager, "_ensure_quality_reviewer_card_bound", bind)
    monkeypatch.setattr(
        manager,
        "_launch_reserved_quality_reviewer",
        lambda **kwargs: calls["launch"].append(kwargs),
    )
    monkeypatch.setattr(
        process_launcher.core,
        "create_task",
        lambda **kwargs: calls["create"].append(kwargs) or {"ok": True},
    )
    return calls


def _launch(
    manager: process_launcher.ProcessManager, target_request_id: str
) -> dict[str, object]:
    return manager.launch_quality_reviewer(
        target_request_id=target_request_id,
        target_task_id=TARGET_TASK,
        reviewer_task_id="QR_NF01748",
        runner=RUNNER,
        adapter_id=ADAPTER,
        lens="correctness",
    )


def _assert_nothing_started(
    manager: process_launcher.ProcessManager, calls: dict[str, list]
) -> None:
    assert calls == {"reserve": [], "bind": [], "launch": [], "create": []}
    assert "reviewer-request" not in manager._latest_by_request()


def test_prefix_of_a_real_request_is_refused_before_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    _seed(manager, REAL_REQUEST, TARGET_TASK)
    calls = _spied(manager, monkeypatch)

    receipt = _launch(manager, REAL_REQUEST[:8])

    assert receipt["ok"] is False
    assert receipt["error"] == "quality_review_target_request_not_exact"
    assert receipt["candidate_request_ids"] == [REAL_REQUEST]
    assert receipt["candidate_count"] == 1
    # Named back, never resolved to: the receipt binds no request at all.
    assert "request_id" not in receipt
    _assert_nothing_started(manager, calls)


def test_prefix_candidates_are_bounded_and_scoped_to_the_target_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    own = [f"feed{index:02d}" for index in range(7)]
    for request_id in own:
        _seed(manager, request_id, TARGET_TASK)
    _seed(manager, "feed99", "OTHER_TASK")
    calls = _spied(manager, monkeypatch)

    receipt = _launch(manager, "feed")

    assert receipt["error"] == "quality_review_target_request_not_exact"
    limit = process_launcher.QUALITY_REVIEW_TARGET_CANDIDATE_LIMIT
    assert limit == 5
    assert receipt["candidate_request_ids"] == own[:limit]
    assert receipt["candidate_count"] == len(own)
    assert "feed99" not in receipt["candidate_request_ids"]
    _assert_nothing_started(manager, calls)


def test_unknown_request_is_refused_as_not_found_before_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    _seed(manager, REAL_REQUEST, TARGET_TASK)
    calls = _spied(manager, monkeypatch)

    receipt = _launch(manager, "0" * 32)

    assert receipt["ok"] is False
    assert receipt["error"] == "quality_review_target_request_not_found"
    assert "candidate_request_ids" not in receipt
    _assert_nothing_started(manager, calls)


def test_prefix_of_another_tasks_request_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    _seed(manager, REAL_REQUEST, "OTHER_TASK")
    calls = _spied(manager, monkeypatch)

    receipt = _launch(manager, REAL_REQUEST[:8])

    assert receipt["error"] == "quality_review_target_request_not_found"
    _assert_nothing_started(manager, calls)


def test_empty_request_id_is_not_found_and_matches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    _seed(manager, REAL_REQUEST, TARGET_TASK)
    calls = _spied(manager, monkeypatch)

    receipt = _launch(manager, "")

    assert receipt["error"] == "quality_review_target_request_not_found"
    _assert_nothing_started(manager, calls)


def test_exact_request_of_another_task_is_a_task_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    _seed(manager, REAL_REQUEST, "OTHER_TASK")
    calls = _spied(manager, monkeypatch)

    receipt = _launch(manager, REAL_REQUEST)

    assert receipt["ok"] is False
    assert receipt["error"] == "quality_review_target_request_task_mismatch"
    assert receipt["request_task_id"] == "OTHER_TASK"
    _assert_nothing_started(manager, calls)


def test_exact_matching_request_launches_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    _seed(manager, REAL_REQUEST, TARGET_TASK)
    # A longer id sharing the exact one as prefix must not divert the launch.
    _seed(manager, REAL_REQUEST + "-sibling", TARGET_TASK)
    calls = _spied(manager, monkeypatch)

    receipt = _launch(manager, REAL_REQUEST)

    assert receipt["ok"] is True
    assert receipt["request_id"] == "reviewer-request"
    assert len(calls["reserve"]) == 1
    assert calls["reserve"][0]["target_request_id"] == REAL_REQUEST
    assert calls["reserve"][0]["target_task_id"] == TARGET_TASK
    assert len(calls["bind"]) == 1
    assert calls["bind"][0]["target_request_id"] == REAL_REQUEST
