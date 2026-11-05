"""NF-2026-02095: a dispose-time reproduction is bounded and never serializes disposes.

Two parallel ``review_finding_dispose`` calls on one target once sent nothing
for 1800 s; one stored its disposition and the other did not. The reproduction
callback now waits at most :data:`finding_reproduction.DISPOSE_REPRODUCTION_DEADLINE_SECONDS`
for a run, feeds a progress sink while it waits, and past the deadline returns
a signed UNRUNNABLE receipt with candidate detail ``timeout`` that the dispose
stores. The slow reproducer here is a fake sandbox runner that blocks -- every
run behind one shared lock, the suspected serialization -- never a real
workspace build.
"""

from __future__ import annotations

import math
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import finding_reproduction as fr  # noqa: E402
from aiworkhub import quality_evidence as qe  # noqa: E402
from aiworkhub import review_orchestrator  # noqa: E402

KEY = b"k" * 32
BASE_OID = "a" * 40
CANDIDATE = review_orchestrator.candidate_digest({"src/calc.py": "a" * 64})
SPEC = "pytest:tests/test_calc.py::test_add"
DEADLINE = 0.5
# Generous slack over the deadline for a loaded CI host; still far below the
# blocked run's own 30 s, so a hang cannot pass.
BOUND = DEADLINE + 4.0


@pytest.fixture(autouse=True)
def _short_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fr, "DISPOSE_REPRODUCTION_DEADLINE_SECONDS", DEADLINE)
    monkeypatch.setattr(fr, "DISPOSE_PROGRESS_INTERVAL_SECONDS", 0.05)
    # The run registry is process-wide; each test gets a fresh one so an
    # earlier test's abandoned run never answers for this one.
    monkeypatch.setattr(fr, "_DISPOSE_RUNS", fr.DisposeRunRegistry())


class _SlowSandbox:
    """Fake sandbox: each run takes the one target-wide lock, then blocks.

    ``release`` lets every blocked run finish: the candidate side exits 1 and
    the base side exits 0, so a finished pair classifies REPRODUCED.
    """

    def __init__(self, *, blocked: bool = True) -> None:
        self.lock = threading.Lock()
        self.release = threading.Event()
        if not blocked:
            self.release.set()
        self.resolved = 0
        self.created: list[str] = []
        self.cleaned: list[str] = []
        self._guard = threading.Lock()

    def target(self, _spec: fr.ReproductionSpec) -> fr.ReproductionTarget:
        with self._guard:
            self.resolved += 1

        def creator(side: str):
            def create() -> str:
                with self._guard:
                    self.created.append(side)
                return side

            return create

        def cleanup(workspace: str) -> None:
            with self._guard:
                self.cleaned.append(workspace)

        def run(workspace: str, commands: list[str]) -> list[dict[str, Any]]:
            with self.lock:
                self.release.wait(30.0)
                returncode = 1 if workspace == "candidate" else 0
                return [{"command": commands[0], "returncode": returncode, "timed_out": False}]

        return fr.ReproductionTarget(
            base_oid=BASE_OID,
            create_candidate=creator("candidate"),
            create_base=creator("base"),
            cleanup=cleanup,
            run=run,
            sealed_manifest=lambda _also: {},
            canonical_materialized=lambda: [],
        )

    def drain(self) -> None:
        """Release abandoned runs and wait until every workspace is cleaned."""
        self.release.set()
        stop = time.monotonic() + 10.0
        while time.monotonic() < stop:
            with self._guard:
                if sorted(self.cleaned) == sorted(self.created):
                    return
            time.sleep(0.02)


def _finding(finding_id: str) -> dict[str, Any]:
    return {
        "id": finding_id,
        "severity": qe.SEVERITY_MEDIUM,
        "summary": "add subtracts",
        "evidence": "fixture:claimed-defect",
        "disposition": "defect",
        "actionable": True,
        "reproduction": SPEC,
    }


def _receipt() -> dict[str, Any]:
    return {
        "schema_id": "aiworkhub.quality_review_receipt.v1",
        "packet_sha256": "b" * 64,
        "target": {"request_id": "target-request", "task_id": "TARGET", "claim_epoch": 3},
        "reviewer": {
            "request_id": "review-req-1",
            "task_id": "QUALITY_REVIEW_TARGET",
            "provider": "reviewer-b",
        },
        "report": {
            "lens": qe.LENS_CORRECTNESS,
            "provider": "reviewer-b",
            "read_only": True,
            "can_mutate_repo": False,
            "findings": [_finding("finding-1"), _finding("finding-2")],
        },
        "submission_id": "c" * 64,
    }


def _driver(tmp_path: Path) -> review_orchestrator.ReviewOrchestrator:
    return review_orchestrator.ReviewOrchestrator(
        SimpleNamespace(repo=tmp_path), db_path=tmp_path / "review.sqlite3"
    )


def _dispose(driver, reproduce, finding: str, **overrides) -> dict[str, Any]:
    args = {
        "task_id": "TARGET",
        "request_id": "target-request",
        "reviewer_request_id": "review-req-1",
        "finding_id": f"reviewer:correctness:{finding}",
        "disposition": "confirmed",
        "counter_evidence": "",
        "reason": "",
        "actor": "claude:session-exact",
        "reviewer_receipt": _receipt(),
        "candidate_sha256": CANDIDATE,
        "reproduce": reproduce,
    }
    args.update(overrides)
    return driver.dispose_review_finding(**args)


def _stored(driver) -> dict[str, dict[str, Any]]:
    rows = driver.review_finding_dispositions(task_id="TARGET", request_id="target-request")
    return {row["finding_id"]: row for row in rows}


def _assert_deadline_receipt(receipt: dict[str, Any], finding: str) -> None:
    assert receipt["classification"] == fr.UNRUNNABLE
    assert receipt["candidate"]["detail"] == fr.TIMEOUT_DETAIL
    assert receipt["candidate"]["timed_out"] is True
    assert receipt["deadline_seconds"] == DEADLINE
    assert receipt["finding_id"] == f"reviewer:correctness:{finding}"
    assert fr.is_deadline_receipt(receipt)
    assert fr.verify_receipt(receipt, fr.receipt_key(KEY))


# --------------------------------------------------------------------------
# the reported scenario: two concurrent disposes on one target
# --------------------------------------------------------------------------
def test_two_concurrent_disposes_with_a_slow_reproduction_both_return_and_store(
    tmp_path: Path,
) -> None:
    sandbox = _SlowSandbox()
    reproduce = fr.dispose_reproducer(coordinator_key=lambda: KEY, resolve_target=sandbox.target)
    driver = _driver(tmp_path)
    results: dict[str, dict[str, Any]] = {}
    elapsed: dict[str, float] = {}
    start = threading.Barrier(2)

    def call(finding: str) -> None:
        start.wait()
        began = time.monotonic()
        results[finding] = _dispose(driver, reproduce, finding)
        elapsed[finding] = time.monotonic() - began

    threads = [threading.Thread(target=call, args=(f,)) for f in ("finding-1", "finding-2")]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(BOUND + 1.0)
        assert not any(thread.is_alive() for thread in threads), "a dispose hung"
        # Both returned within the bound although the second run never even
        # got the sandbox lock: neither waiter is serialized behind a run.
        assert set(elapsed) == {"finding-1", "finding-2"}
        assert all(seconds < BOUND for seconds in elapsed.values()), elapsed
        for finding, result in results.items():
            assert result["ok"] is True, result
            assert result["idempotent"] is False
            _assert_deadline_receipt(result["reproduction_receipt"], finding)
            assert "evidence_level" not in result
        stored = _stored(driver)
        assert set(stored) == {
            "reviewer:correctness:finding-1",
            "reviewer:correctness:finding-2",
        }
        for finding_id, row in stored.items():
            assert row["disposition"] == "confirmed"
            _assert_deadline_receipt(row["reproduction_receipt"], finding_id.rsplit(":", 1)[1])
    finally:
        sandbox.drain()
    # Abandoned runs still cleaned up their own workspaces; nothing was killed.
    assert sorted(sandbox.cleaned) == sorted(sandbox.created)
    # A late finish never rewrote the stored rows.
    assert {
        row["reproduction_receipt"]["classification"] for row in _stored(driver).values()
    } == {fr.UNRUNNABLE}


def test_progress_is_reported_while_a_reproduction_runs(tmp_path: Path) -> None:
    sandbox = _SlowSandbox()
    ticks: list[tuple[float, float]] = []
    reproduce = fr.dispose_reproducer(
        coordinator_key=lambda: KEY,
        resolve_target=sandbox.target,
        progress=lambda elapsed, total: ticks.append((elapsed, total)),
    )
    try:
        result = _dispose(_driver(tmp_path), reproduce, "finding-1")
    finally:
        sandbox.drain()
    assert result["ok"] is True
    _assert_deadline_receipt(result["reproduction_receipt"], "finding-1")
    assert len(ticks) >= 2
    assert all(total == DEADLINE for _elapsed, total in ticks)
    elapsed = [value for value, _total in ticks]
    assert elapsed == sorted(elapsed) and 0 < elapsed[0] <= DEADLINE


def test_a_failing_progress_sink_never_decides_the_outcome(tmp_path: Path) -> None:
    sandbox = _SlowSandbox(blocked=False)

    def broken(_elapsed: float, _total: float) -> None:
        raise OSError("client gone")

    reproduce = fr.dispose_reproducer(
        coordinator_key=lambda: KEY, resolve_target=sandbox.target, progress=broken
    )
    result = _dispose(_driver(tmp_path), reproduce, "finding-1")
    assert result["ok"] is True
    assert result["reproduction_receipt"]["classification"] == fr.REPRODUCED


def test_a_reproduction_inside_the_deadline_is_unchanged(tmp_path: Path) -> None:
    sandbox = _SlowSandbox(blocked=False)
    reproduce = fr.dispose_reproducer(coordinator_key=lambda: KEY, resolve_target=sandbox.target)
    result = _dispose(_driver(tmp_path), reproduce, "finding-1")
    receipt = result["reproduction_receipt"]
    assert result["ok"] is True and result["evidence_level"] == "reproduced"
    assert receipt["classification"] == fr.REPRODUCED
    assert "deadline_seconds" not in receipt and not fr.is_deadline_receipt(receipt)
    assert sandbox.cleaned == sandbox.created == ["candidate", "base"]


def test_an_error_inside_the_deadline_still_names_its_cause(tmp_path: Path) -> None:
    def unavailable(_spec: fr.ReproductionSpec) -> fr.ReproductionTarget:
        raise RuntimeError("finding_reproduction_candidate_unavailable:gone")

    reproduce = fr.dispose_reproducer(coordinator_key=lambda: KEY, resolve_target=unavailable)
    driver = _driver(tmp_path)
    result = _dispose(driver, reproduce, "finding-1")
    assert result["ok"] is False
    assert result["error"].startswith("review_finding_reproduction_failed:")
    assert "candidate_unavailable" in result["error"]
    assert _stored(driver) == {}


# --------------------------------------------------------------------------
# a stored deadline receipt is rechecked, and a replay stays idempotent
# --------------------------------------------------------------------------
def test_replay_after_a_timeout_rechecks_and_stays_idempotent(tmp_path: Path) -> None:
    sandbox = _SlowSandbox()
    reproduce = fr.dispose_reproducer(coordinator_key=lambda: KEY, resolve_target=sandbox.target)
    driver = _driver(tmp_path)
    try:
        first = _dispose(driver, reproduce, "finding-1")
        assert first["ok"] is True and first["idempotent"] is False
        replay = _dispose(driver, reproduce, "finding-1")
    finally:
        sandbox.drain()
    # Rechecked, not cached: the replay waited on the still-blocked run again
    # (without starting a second one), timed out again, and the stored receipt
    # came back byte-identical, so the replay is idempotent.
    assert sandbox.resolved == 1
    assert replay["ok"] is True and replay["idempotent"] is True
    assert replay["reproduction_receipt"] == first["reproduction_receipt"]

    # Once the sandbox answers, a re-disposition with a reason records the
    # real receipt of that same run and supersedes the deadline one.
    redisposed = _dispose(driver, reproduce, "finding-1", reason="sandbox recovered")
    assert sandbox.resolved == 1
    assert redisposed["ok"] is True
    assert redisposed["reproduction_receipt"]["classification"] == fr.REPRODUCED
    assert redisposed["supersedes_receipt_sha256"] == first["receipt_sha256"]


@pytest.mark.parametrize("deadline", [0, -1.0, math.nan, math.inf, True, "600"])
def test_an_invalid_deadline_is_refused_by_name_and_stores_nothing(
    tmp_path: Path, deadline: Any
) -> None:
    sandbox = _SlowSandbox(blocked=False)
    reproduce = fr.dispose_reproducer(
        coordinator_key=lambda: KEY, resolve_target=sandbox.target, deadline_seconds=deadline
    )
    driver = _driver(tmp_path)
    result = _dispose(driver, reproduce, "finding-1")
    assert result["ok"] is False
    assert "finding_reproduction_deadline_invalid" in result["error"]
    assert sandbox.resolved == 0 and _stored(driver) == {}


# --------------------------------------------------------------------------
# production wiring: a hung target resolution is bounded too
# --------------------------------------------------------------------------
def test_manager_reproducer_bounds_a_hung_target_resolution() -> None:
    release = threading.Event()
    ticks: list[tuple[float, float]] = []

    def hung_prepare(_request_id: str, _task_id: str) -> dict[str, Any]:
        release.wait(30.0)
        return {"ok": False, "error": "quality_review_target_invalid"}

    manager = SimpleNamespace(
        _terminal_authority_key=lambda: KEY, _prepared_quality_review=hung_prepare
    )
    reproduce = fr.manager_reproducer(
        manager,
        task_id="TARGET",
        request_id="target-request",
        card={},
        deadline_seconds=DEADLINE,
        progress=lambda elapsed, total: ticks.append((elapsed, total)),
    )
    began = time.monotonic()
    try:
        receipt = reproduce(
            finding_id="reviewer:correctness:finding-1",
            spec_text=SPEC,
            candidate_sha256=CANDIDATE,
            reviewer_receipt_sha256="r" * 64,
        )
    finally:
        release.set()
    assert time.monotonic() - began < BOUND
    _assert_deadline_receipt(receipt, "finding-1")
    assert receipt["base_oid"] == ""
    assert ticks and all(total == DEADLINE for _elapsed, total in ticks)


# --------------------------------------------------------------------------
# replays never pile up abandoned runs (rework of NF-2026-02095)
# --------------------------------------------------------------------------
def _count_runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every ``reproduce_finding`` call the dispose path makes."""
    calls: list[str] = []
    real = fr.reproduce_finding

    def counted(spec: fr.ReproductionSpec, **kwargs: Any) -> dict[str, Any]:
        calls.append(str(kwargs.get("finding_id")))
        return real(spec, **kwargs)

    monkeypatch.setattr(fr, "reproduce_finding", counted)
    return calls


def _wait_until_idle() -> None:
    stop = time.monotonic() + 10.0
    while fr._DISPOSE_RUNS.running_count() and time.monotonic() < stop:
        time.sleep(0.02)
    assert fr._DISPOSE_RUNS.running_count() == 0


def test_replays_with_a_stored_deadline_receipt_join_the_blocked_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_runs(monkeypatch)
    sandbox = _SlowSandbox()
    reproduce = fr.dispose_reproducer(coordinator_key=lambda: KEY, resolve_target=sandbox.target)
    driver = _driver(tmp_path)
    replays: list[dict[str, Any]] = []
    try:
        first = _dispose(driver, reproduce, "finding-1")
        assert first["ok"] is True
        _assert_deadline_receipt(first["reproduction_receipt"], "finding-1")
        assert fr._DISPOSE_RUNS.abandoned_count() == 1

        def replay() -> None:
            replays.append(_dispose(driver, reproduce, "finding-1"))

        threads = [threading.Thread(target=replay) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(BOUND + 1.0)
        assert not any(thread.is_alive() for thread in threads), "a replay hung"
        # Both replays waited on the one blocked run; neither started another.
        assert calls == ["reviewer:correctness:finding-1"]
        assert sandbox.resolved == 1 and fr._DISPOSE_RUNS.started == 1
        assert fr._DISPOSE_RUNS.abandoned_count() == 1
    finally:
        sandbox.drain()
    assert len(replays) == 2
    for result in replays:
        assert result["ok"] is True and result["idempotent"] is True
        assert result["reproduction_receipt"] == first["reproduction_receipt"]


def test_a_replay_after_the_abandoned_run_finished_returns_its_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_runs(monkeypatch)
    sandbox = _SlowSandbox()
    reproduce = fr.dispose_reproducer(coordinator_key=lambda: KEY, resolve_target=sandbox.target)
    driver = _driver(tmp_path)
    try:
        first = _dispose(driver, reproduce, "finding-1")
        _assert_deadline_receipt(first["reproduction_receipt"], "finding-1")
    finally:
        sandbox.drain()
    _wait_until_idle()
    began = time.monotonic()
    late = _dispose(driver, reproduce, "finding-1", reason="sandbox recovered")
    assert time.monotonic() - began < DEADLINE
    assert calls == ["reviewer:correctness:finding-1"]
    assert sandbox.resolved == 1 and fr._DISPOSE_RUNS.started == 1
    receipt = late["reproduction_receipt"]
    assert late["ok"] is True and late["evidence_level"] == "reproduced"
    assert receipt["classification"] == fr.REPRODUCED
    assert fr.verify_receipt(receipt, fr.receipt_key(KEY))
    assert late["supersedes_receipt_sha256"] == first["receipt_sha256"]


def test_a_late_receipt_under_another_key_is_dropped_and_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _count_runs(monkeypatch)
    sandbox = _SlowSandbox()
    keys = {"current": KEY}
    reproduce = fr.dispose_reproducer(
        coordinator_key=lambda: keys["current"], resolve_target=sandbox.target
    )
    args = {
        "finding_id": "reviewer:correctness:finding-1",
        "spec_text": SPEC,
        "candidate_sha256": CANDIDATE,
        "reviewer_receipt_sha256": "r" * 64,
    }
    try:
        _assert_deadline_receipt(reproduce(**args), "finding-1")
    finally:
        sandbox.drain()
    _wait_until_idle()
    keys["current"] = b"z" * 32
    receipt = reproduce(**args)
    # The kept receipt does not verify under the rotated key: never returned.
    assert len(calls) == 2
    assert receipt["classification"] == fr.REPRODUCED
    assert fr.verify_receipt(receipt, fr.receipt_key(b"z" * 32))


def _call(reproduce, finding: str) -> dict[str, Any]:
    return reproduce(
        finding_id=f"reviewer:correctness:{finding}",
        spec_text=SPEC,
        candidate_sha256=CANDIDATE,
        reviewer_receipt_sha256="r" * 64,
    )


def _wait_for(predicate) -> None:
    stop = time.monotonic() + 10.0
    while not predicate() and time.monotonic() < stop:
        time.sleep(0.01)
    assert predicate()


def _waiters() -> int:
    registry = fr._DISPOSE_RUNS
    with registry._lock:
        return sum(run.waiters for run in registry._running.values())


def test_past_the_run_cap_with_abandoned_runs_no_new_run_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fr, "DISPOSE_RUN_LIMIT", 2)
    calls = _count_runs(monkeypatch)
    sandbox = _SlowSandbox()
    reproduce = fr.dispose_reproducer(coordinator_key=lambda: KEY, resolve_target=sandbox.target)
    try:
        for finding in ("finding-1", "finding-2"):
            _assert_deadline_receipt(_call(reproduce, finding), finding)
        assert fr._DISPOSE_RUNS.abandoned_count() == 2
        threads_before = threading.active_count()
        began = time.monotonic()
        capped = _call(reproduce, "finding-3")
        # Answered at once, with no thread, no target resolution, no run.
        assert time.monotonic() - began < DEADLINE / 2
        _assert_deadline_receipt(capped, "finding-3")
        # An earlier test's finished worker may still exit; none may start.
        assert threading.active_count() <= threads_before
        assert fr._DISPOSE_RUNS.started == 2 and sandbox.resolved == 2
        assert calls == ["reviewer:correctness:finding-1", "reviewer:correctness:finding-2"]
    finally:
        sandbox.drain()
    _wait_until_idle()
    assert fr._DISPOSE_RUNS.abandoned_count() == 0


def test_the_run_cap_counts_awaited_runs_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fr, "DISPOSE_RUN_LIMIT", 1)
    calls = _count_runs(monkeypatch)
    sandbox = _SlowSandbox()
    patient = fr.dispose_reproducer(
        coordinator_key=lambda: KEY, resolve_target=sandbox.target, deadline_seconds=8.0
    )
    reproduce = fr.dispose_reproducer(coordinator_key=lambda: KEY, resolve_target=sandbox.target)
    awaited: list[dict[str, Any]] = []
    thread = threading.Thread(target=lambda: awaited.append(_call(patient, "finding-1")))
    try:
        thread.start()
        _wait_for(lambda: _waiters() == 1)
        # The one in-flight run is still awaited, not abandoned, yet it fills the cap.
        assert fr._DISPOSE_RUNS.abandoned_count() == 0
        began = time.monotonic()
        capped = _call(reproduce, "finding-2")
        assert time.monotonic() - began < DEADLINE / 2
        _assert_deadline_receipt(capped, "finding-2")
        assert fr._DISPOSE_RUNS.started == 1
    finally:
        sandbox.drain()
        thread.join(BOUND + 8.0)
    assert not thread.is_alive()
    assert calls == ["reviewer:correctness:finding-1"]
    assert awaited and awaited[0]["classification"] == fr.REPRODUCED


def test_a_run_is_abandoned_only_when_its_last_waiter_gives_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _count_runs(monkeypatch)
    sandbox = _SlowSandbox()
    patient = fr.dispose_reproducer(
        coordinator_key=lambda: KEY, resolve_target=sandbox.target, deadline_seconds=8.0
    )
    hasty = fr.dispose_reproducer(coordinator_key=lambda: KEY, resolve_target=sandbox.target)
    awaited: list[dict[str, Any]] = []
    thread = threading.Thread(target=lambda: awaited.append(_call(patient, "finding-1")))
    try:
        thread.start()
        _wait_for(lambda: _waiters() == 1)
        _assert_deadline_receipt(_call(hasty, "finding-1"), "finding-1")
        # One waiter gave up, the other still waits: the run is not abandoned.
        assert fr._DISPOSE_RUNS.running_count() == 1
        assert _waiters() == 1
        assert fr._DISPOSE_RUNS.abandoned_count() == 0
    finally:
        sandbox.drain()
        thread.join(BOUND + 8.0)
    assert not thread.is_alive()
    assert awaited and awaited[0]["classification"] == fr.REPRODUCED
    _wait_until_idle()
    # The caller that gave up gets that same run's late receipt on replay.
    replay = _call(hasty, "finding-1")
    assert replay == awaited[0]
    assert calls == ["reviewer:correctness:finding-1"] and fr._DISPOSE_RUNS.started == 1


def test_a_joiner_gets_the_joined_result_only_through_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _count_runs(monkeypatch)
    sandbox = _SlowSandbox()
    rotated = b"z" * 32
    starter = fr.dispose_reproducer(
        coordinator_key=lambda: KEY, resolve_target=sandbox.target, deadline_seconds=8.0
    )
    joiner = fr.dispose_reproducer(
        coordinator_key=lambda: rotated, resolve_target=sandbox.target, deadline_seconds=8.0
    )
    results: dict[str, dict[str, Any]] = {}
    threads = [
        threading.Thread(target=lambda: results.update(starter=_call(starter, "finding-1"))),
        threading.Thread(target=lambda: results.update(joiner=_call(joiner, "finding-1"))),
    ]
    try:
        threads[0].start()
        _wait_for(lambda: _waiters() == 1)
        threads[1].start()
        _wait_for(lambda: _waiters() == 2)
    finally:
        sandbox.drain()
        for thread in threads:
            thread.join(BOUND + 8.0)
    assert not any(thread.is_alive() for thread in threads)
    assert calls == ["reviewer:correctness:finding-1"] and fr._DISPOSE_RUNS.started == 1
    assert results["starter"]["classification"] == fr.REPRODUCED
    assert fr.verify_receipt(results["starter"], fr.receipt_key(KEY))
    # The joined run's receipt does not verify under the joiner's key: it is
    # never handed back; the joiner gets its own signed deadline receipt.
    joined = results["joiner"]
    assert not fr.verify_receipt(results["starter"], fr.receipt_key(rotated))
    assert fr.is_deadline_receipt(joined)
    assert fr.verify_receipt(joined, fr.receipt_key(rotated))


@pytest.mark.parametrize("name", ["DISPOSE_RUN_LIMIT", "DISPOSE_FINISHED_RUN_LIMIT"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_an_invalid_registry_limit_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch, name: str, value: Any
) -> None:
    monkeypatch.setattr(fr, name, value)
    sandbox = _SlowSandbox(blocked=False)
    reproduce = fr.dispose_reproducer(coordinator_key=lambda: KEY, resolve_target=sandbox.target)
    with pytest.raises(ValueError, match="_run_limit_invalid"):
        _call(reproduce, "finding-1")
    assert sandbox.resolved == 0 and fr._DISPOSE_RUNS.started == 0
