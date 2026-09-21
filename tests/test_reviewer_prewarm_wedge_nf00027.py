"""NF-2026-00027: parallel reviewer prewarms must never wedge the launch queue.

A reviewer reservation reads as a live Source Graph prewarm while its latest
phase is ``reviewer_source_graph_prewarm_started`` and its owner process -- the
long-lived MCP server itself -- is alive.  Nothing refreshes that row while the
build runs, so a prewarm that hung, or whose launcher died without retiring the
phase, stayed "live" for the whole server lifetime: its bounded launch owner
waited forever and every such row held a launch slot past its reservation
deadline.  Three of them filled the default queue until an MCP restart changed
the owner pid.  These tests pin that bound, the launch-side prewarm capacity
(bounded queue, then a named refusal) and the exception-path phase retirement.
"""

from __future__ import annotations

import os
import sys
import threading
import time

from aiworkhub import process_launcher, reviewer_prewarm_capacity, worker_ai_tools_mcp

from test_process_launcher import _card, _manager, _reviewer_launch_setup, _show

_PREWARM_STARTED = "reviewer_source_graph_prewarm_started"


def _plain_manager(tmp_path):
    return _manager(
        tmp_path, show_task=_show(lambda: _card()), argv=[sys.executable, "-c", "pass"]
    )


def _reviewer_row(request_id: str, *, expires_at: float) -> dict:
    return {
        "request_id": request_id,
        "task_id": f"REVIEWER_{request_id}",
        "runner": "claude_worker_reviewer",
        "topic": "quality_review",
        "adapter_id": "claude_cli",
        "state": "starting",
        "reservation_expires_at_epoch": expires_at,
        "owner_pid": os.getpid(),
        "owner_pid_start_ticks": process_launcher._pid_start_ticks(os.getpid()),
    }


def test_three_stale_prewarm_rows_do_not_hold_the_launch_queue(tmp_path, monkeypatch):
    # Three reviewer prewarms started an hour ago and never retired their phase
    # (hung build, or a launcher that died without publishing a terminal phase).
    # Their owner is this very process, so owner identity alone keeps them live.
    monkeypatch.setenv(process_launcher.MAX_PROCESSES_ENV, "3")
    manager = _plain_manager(tmp_path)
    hour_ago = time.time() - 3600
    for index in range(3):
        row = _reviewer_row(f"stale-prewarm-{index}", expires_at=hour_ago)
        row["preparation_phase"] = _PREWARM_STARTED
        row["preparation_heartbeat_epoch"] = hour_ago
        manager._append_event(row)

    assert not any(
        manager._reviewer_source_graph_prewarm_live(f"stale-prewarm-{index}")
        for index in range(3)
    )
    assert manager._active_count() == 0
    reserved = manager._reserve_quality_reviewer_attempt(
        reviewer_task_id="REVIEWER_TASK_4",
        runner="claude_worker_reviewer",
        adapter_id="claude_cli",
        target_request_id="target-req-1",
        target_task_id="TARGET_TASK_1",
        lens="correctness",
        model=None,
        timeout_seconds=1800,
    )
    assert reserved.get("ok") is True, reserved


def test_hung_parallel_prewarm_owners_are_bounded(tmp_path, monkeypatch):
    # Three launch owners wait on three prewarm builds that never return.  Each
    # owner must give up once its prewarm outlives the preparation stall
    # ceiling, instead of extending forever while the server pid is alive.
    manager = _plain_manager(tmp_path)
    monkeypatch.setattr(
        process_launcher.ProcessManager, "_QUALITY_REVIEW_LAUNCH_OWNER_SECONDS", 0.05
    )
    monkeypatch.setattr(process_launcher, "preparation_stall_seconds", lambda: 0.3)
    release = threading.Event()
    outcomes: dict[str, str] = {}
    owners = []
    try:
        for index in range(3):
            request_id = f"hung-prewarm-{index}"
            manager._append_event(
                _reviewer_row(request_id, expires_at=time.time() + 600)
            )
            manager._publish_reviewer_progress(request_id, _PREWARM_STARTED)
            launcher = threading.Thread(target=release.wait, daemon=True)
            launcher.start()
            owner = threading.Thread(
                target=lambda rid=request_id, thread=launcher: outcomes.__setitem__(
                    rid, manager._reviewer_launch_owner_join(thread, rid)
                ),
                daemon=True,
            )
            owner.start()
            owners.append(owner)
        for owner in owners:
            owner.join(timeout=10)
        assert not any(owner.is_alive() for owner in owners), (
            "launch owner still waiting on a hung prewarm"
        )
        assert set(outcomes.values()) == {"timeout"}
    finally:
        release.set()


def test_fresh_prewarm_still_extends_its_owner(tmp_path, monkeypatch):
    # The ceiling bounds a stuck prewarm; it must not cut a live one short.
    manager = _plain_manager(tmp_path)
    monkeypatch.setattr(
        process_launcher.ProcessManager, "_QUALITY_REVIEW_LAUNCH_OWNER_SECONDS", 0.05
    )
    request_id = "fresh-prewarm"
    manager._append_event(_reviewer_row(request_id, expires_at=time.time() + 600))
    manager._publish_reviewer_progress(request_id, _PREWARM_STARTED)
    launcher = threading.Thread(target=time.sleep, args=(0.3,))
    launcher.start()

    assert manager._reviewer_launch_owner_join(launcher, request_id) == "completed"


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_three_parallel_prewarms_queue_the_third_until_a_slot_frees(
    tmp_path, monkeypatch
):
    # The owner-reported shape: three lenses prewarm at once.  Two start, the
    # third queues (bounded) and starts as soon as one of the two completes.
    manager = _plain_manager(tmp_path)
    monkeypatch.setattr(reviewer_prewarm_capacity, "capacity", lambda: 2)
    ticks = process_launcher._pid_start_ticks(os.getpid())
    request_ids = [f"lens-{index}" for index in range(3)]
    for request_id in request_ids:
        manager._append_event(_reviewer_row(request_id, expires_at=time.time() + 600))
    outcomes: dict[str, str | None] = {}

    def gate(request_id: str) -> None:
        outcomes[request_id] = reviewer_prewarm_capacity.wait_for_slot(
            manager,
            request_id,
            lambda phase, detail=None: manager._publish_reviewer_progress(
                request_id, phase, detail
            ),
            ceiling=30.0,
            owner_ticks=ticks,
        )

    def phase(request_id: str) -> str | None:
        return (manager._latest_by_request().get(request_id) or {}).get(
            "preparation_phase"
        )

    threads = {rid: threading.Thread(target=gate, args=(rid,)) for rid in request_ids}
    for thread in threads.values():
        thread.start()
    assert _wait_until(lambda: len(outcomes) == 2 and any(
        phase(rid) == reviewer_prewarm_capacity.PREWARM_QUEUED for rid in request_ids
    ))
    queued = [rid for rid in request_ids if rid not in outcomes]
    assert len(queued) == 1 and threads[queued[0]].is_alive()
    assert phase(queued[0]) == reviewer_prewarm_capacity.PREWARM_QUEUED

    finished = next(iter(outcomes))
    manager._publish_reviewer_progress(finished, "reviewer_source_graph_prewarm_complete")
    threads[queued[0]].join(timeout=5)

    assert not threads[queued[0]].is_alive()
    assert outcomes == dict.fromkeys(request_ids)
    assert phase(queued[0]) == _PREWARM_STARTED


def test_saturated_prewarm_slots_fail_the_launch_with_a_named_reason(
    tmp_path, monkeypatch
):
    manager, binding = _reviewer_launch_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(reviewer_prewarm_capacity, "capacity", lambda: 2)
    monkeypatch.setattr(process_launcher, "preparation_stall_seconds", lambda: 0.3)
    for index in range(2):
        row = _reviewer_row(f"busy-prewarm-{index}", expires_at=time.time() + 600)
        row["preparation_phase"] = _PREWARM_STARTED
        row["preparation_heartbeat_epoch"] = time.time() + 3600  # never outlives
        manager._append_event(row)
    builds: list[object] = []
    phases: list[str] = []
    monkeypatch.setattr(
        worker_ai_tools_mcp, "verify_quality_review_prewarm_authority", lambda _repo: None
    )
    monkeypatch.setattr(
        worker_ai_tools_mcp,
        "prewarm_quality_review_source_graph",
        lambda *args, **_kwargs: builds.append(args),
    )

    started_at = time.monotonic()
    result = manager._launch_isolated(
        task_id="TASK_REVIEW_1",
        runner="claude_worker_reviewer",
        topic="quality_review",
        adapter_id="claude_cli",
        model=None,
        owner_prompt="",
        timeout_seconds=30,
        quality_review_binding=binding,
        prewarm_progress=lambda phase, detail=None: phases.append(phase),
    )

    assert time.monotonic() - started_at < 10
    assert result.get("ok") is False
    assert str(result.get("blocked_reason") or "").startswith(
        "reviewer_prewarm_capacity_exhausted:in_flight=2:capacity=2"
    )
    assert builds == []
    assert phases == [reviewer_prewarm_capacity.PREWARM_QUEUED]


def test_capacity_is_derived_from_cores_with_headroom(monkeypatch):
    for cores, expected in ((1, 1), (2, 1), (3, 2), (16, 2), (64, 2)):
        monkeypatch.setattr(
            reviewer_prewarm_capacity.parallelism, "get_cpu_capacity", lambda c=cores: c
        )
        assert reviewer_prewarm_capacity.capacity() == expected, cores


def test_unexpected_prewarm_exception_retires_started_phase(tmp_path, monkeypatch):
    # Only WorkerToolError used to publish a terminal prewarm phase; any other
    # exception left the durable row reading "prewarm started" with nothing
    # running behind it.
    manager, binding = _reviewer_launch_setup(tmp_path, monkeypatch)
    phases: list[str] = []

    def exploding_prewarm(*_args, **_kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(
        worker_ai_tools_mcp,
        "verify_quality_review_prewarm_authority",
        lambda _repo: None,
    )
    monkeypatch.setattr(
        worker_ai_tools_mcp, "prewarm_quality_review_source_graph", exploding_prewarm
    )
    result = manager._launch_isolated(
        task_id="TASK_REVIEW_1",
        runner="claude_worker_reviewer",
        topic="quality_review",
        adapter_id="claude_cli",
        model=None,
        owner_prompt="",
        timeout_seconds=30,
        quality_review_binding=binding,
        prewarm_progress=lambda phase, detail=None: phases.append(phase),
    )

    assert result.get("ok") is False
    assert phases[-2:] == [_PREWARM_STARTED, "reviewer_source_graph_prewarm_failed"]
