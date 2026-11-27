"""NF-2026-02277 / NF-2026-02359: a required lens is covered only by a reviewer.

A) About ten review_ready targets a night read ``required_reviewer_missing:
correctness, reviewer_running: false`` and each needed a hand launch: the
recovery scan counted a reviewer child that was reserved or pending -- no live
process, no report -- as COVERED, so it skipped the lens as ``already_present``
and said nothing on the target.

B) During a provider account/session switch four auto reviewers went
terminal_failure and were never relaunched, while the workers on the same
adapter recovered through transient_retry: the reviewer path would only ever
buy a DISTINCT route, and there was none.

Fakes only: no provider CLI is started.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

_TESTS = Path(__file__).resolve().parent
_SRC = _TESTS.parent / "src"
for _path in (_SRC, _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import pytest  # noqa: E402

import test_review_orchestrator as base  # noqa: E402
from aiworkhub import review_orchestrator  # noqa: E402

LENSES = review_orchestrator.LENSES
GATE = {
    "review_risk_profile": {
        "effective_tier": "high",
        "required_reviewer_lenses": list(LENSES),
    }
}
TARGET, TARGET_REQUEST = "TARGET", "request-1"
CREDENTIAL_CODE = "vscode_lm_credential_rejected"
SECRET = "sk-live-must-not-reach-the-target"


def _iso(delta_seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


STALE = -10 * review_orchestrator._LENS_SETTLE_WINDOW_SECONDS
FRESH = -1


def _reviewer_task(lens: str, *, attempt: int = 1) -> str:
    return base._nf887_reviewer_task(TARGET, TARGET_REQUEST, lens, attempt=attempt)


def _reserved_row(lens: str, *, age: float, request_id: str = "", attempt: int = 1) -> dict:
    """A child the launcher reserved/queued that no worker ever took."""
    row = {
        "task_id": _reviewer_task(lens, attempt=attempt),
        "topic": "quality_review",
        "status": "pending",
        "worker_status": "reserved",
        "created_at": _iso(age),
        "updated_at": _iso(age),
    }
    if request_id:
        row["launch_request_id"] = request_id
    return row


def _dead_row(lens: str, *, attempt: int) -> dict:
    return {
        "task_id": _reviewer_task(lens, attempt=attempt),
        "topic": "quality_review",
        "status": "worker_failed",
        "worker_status": "",
    }


def _reported_siblings(skip: str = "correctness") -> dict[str, dict]:
    """Every lens but ``skip`` holds this claim's verified report."""
    rows = [
        base._reported_reviewer_row(TARGET, TARGET_REQUEST, lens, state="finished")
        for lens in LENSES
        if lens != skip
    ]
    return {row["task_id"]: row for row in rows}


class _FakeLauncher:
    """Stands in for the ensure that buys the lens: records, never spawns."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.launches: list[dict] = []
        self.error = error

    def __call__(self, _manager, *, db_path, registration):
        self.launches.append(dict(registration))
        if self.error is not None:
            raise self.error
        # A ReviewChain-shaped result: one durable launch action per lens the
        # registration asks for, exactly what ReviewOrchestrator.ensure_chain returns.
        lenses = tuple(registration.get("required_reviewer_lenses") or ())
        return SimpleNamespace(
            chain_id=1, chain_identity_sha256="", chain_identity={},
            actions=tuple(
                SimpleNamespace(action_type="launch", lens=lens, action_id=index + 1)
                for index, lens in enumerate(lenses)
            ),
        )


def _recover(
    monkeypatch, tmp_path: Path, rows: dict[str, dict], *,
    launcher=None, status_result: dict | None = None,
):
    base._stub_archive(monkeypatch)
    base._patch_nf887(
        monkeypatch, [base._nf887_queue_card(TARGET, TARGET_REQUEST, GATE)], rows,
    )
    launcher = launcher or _FakeLauncher()
    monkeypatch.setattr(review_orchestrator, "register_candidate", launcher)
    manager = base._Manager(tmp_path)
    if status_result is not None:
        manager.status_result = dict(status_result)
    manager.status_results[TARGET_REQUEST] = base._nf887_status(
        TARGET, TARGET_REQUEST, GATE,
    )
    result = review_orchestrator.recover_review_ready_targets(
        manager, db_path=tmp_path / "nf02277.sqlite",
    )
    return manager, launcher, result


def _coverage(manager, *, now: datetime | None = None) -> dict[str, str]:
    return review_orchestrator._reviewer_lens_coverage(
        manager.repo,
        base._nf887_identity(TARGET, TARGET_REQUEST),
        LENSES,
        status_of=manager.status,
        now=now or datetime.now(timezone.utc),
    )


def _blocked(manager) -> list[dict]:
    return [
        event["review_automation"]
        for event in manager.events
        if event.get("event_type") == "review_orchestrator_lens_blocked"
    ]


# --- A) a reserved/pending child with no live process is not coverage -------


@pytest.mark.parametrize("request_id", ["", "review-request-reserved"])
def test_reserved_reviewer_past_the_window_is_not_coverage_and_is_launched(
    monkeypatch, tmp_path: Path, request_id: str,
) -> None:
    rows = _reported_siblings()
    reserved = _reserved_row("correctness", age=STALE, request_id=request_id)
    rows[reserved["task_id"]] = reserved
    # When the ledger knows the request, it reports no process running for it.
    no_process = {"ok": True, "state": "missing"} if request_id else None

    manager, launcher, result = _recover(
        monkeypatch, tmp_path, rows, status_result=no_process,
    )

    assert _coverage(manager)["correctness"] == review_orchestrator._COVERAGE_UNUSABLE
    assert result["review_recovery_reasons"].get("already_present") is None
    assert result["review_recovery_reasons"]["unusable_reviewer"] == 1
    assert result["review_recovery_ensured"] == 1
    assert len(launcher.launches) == 1, "the recovery pass buys the lens once"
    assert launcher.launches[0]["target_task_id"] == TARGET


def test_reserved_reviewer_inside_the_window_is_still_waited_on(
    monkeypatch, tmp_path: Path,
) -> None:
    rows = _reported_siblings()
    reserved = _reserved_row("correctness", age=FRESH)
    rows[reserved["task_id"]] = reserved

    _manager, launcher, result = _recover(monkeypatch, tmp_path, rows)

    assert result["review_recovery_reasons"]["already_present"] == 1
    assert launcher.launches == []


def test_live_reviewer_is_coverage_and_is_never_duplicated(
    monkeypatch, tmp_path: Path,
) -> None:
    """A pending row whose process the ledger reports running is live."""
    rows = _reported_siblings()
    reserved = _reserved_row(
        "correctness", age=STALE, request_id="review-request-live",
    )
    rows[reserved["task_id"]] = reserved

    manager, launcher, result = _recover(monkeypatch, tmp_path, rows)

    assert manager.status("review-request-live")["state"] in (
        review_orchestrator._REVIEWER_RUNNING_STATES
    )
    assert result["review_recovery_reasons"]["already_present"] == 1
    assert result["review_recovery_ensured"] == 0
    assert launcher.launches == []


def test_claimed_reviewer_and_verified_reports_stay_coverage(
    monkeypatch, tmp_path: Path,
) -> None:
    rows = _reported_siblings()
    live = base._live_reviewer_row(TARGET, TARGET_REQUEST, "correctness")
    rows[live["task_id"]] = live

    manager, launcher, result = _recover(monkeypatch, tmp_path, rows)

    assert result["review_recovery_reasons"]["already_present"] == 1
    assert launcher.launches == []
    assert set(_coverage(manager).values()) == {review_orchestrator._COVERAGE_COVERED}


def test_claimed_reviewer_whose_process_is_terminal_is_not_coverage(
    monkeypatch, tmp_path: Path,
) -> None:
    rows = _reported_siblings()
    claimed = {
        **base._live_reviewer_row(TARGET, TARGET_REQUEST, "correctness"),
        "launch_request_id": "review-request-dead",
    }
    rows[claimed["task_id"]] = claimed
    base._patch_nf887(monkeypatch, [], rows)
    manager = base._Manager(tmp_path)
    manager.status_results["review-request-dead"] = {
        "ok": True, "state": "worker_failed",
    }

    assert _coverage(manager)["correctness"] == review_orchestrator._COVERAGE_UNUSABLE


# --- A) every skip names its reason on the target ----------------------------


def test_retry_exhausted_skip_is_stamped_on_the_target(
    monkeypatch, tmp_path: Path,
) -> None:
    rows = _reported_siblings()
    for attempt in range(1, review_orchestrator._MAX_ROUTE_ATTEMPTS + 1):
        dead = _dead_row("correctness", attempt=attempt)
        rows[dead["task_id"]] = dead

    manager, launcher, result = _recover(monkeypatch, tmp_path, rows)

    assert result["review_recovery_reasons"]["retry_exhausted"] == 1
    assert launcher.launches == []
    blocked = _blocked(manager)
    assert [row["lens"] for row in blocked] == ["correctness"]
    assert blocked[0]["reason"] == (
        "review_recovery_skipped:retry_exhausted:attempts="
        f"{review_orchestrator._MAX_ROUTE_ATTEMPTS}"
    )
    event = next(
        event for event in manager.events
        if event.get("event_type") == "review_orchestrator_lens_blocked"
    )
    assert (event["task_id"], event["request_id"]) == (TARGET, TARGET_REQUEST)


def test_repeated_recovery_pass_stamps_the_same_skip_once(
    monkeypatch, tmp_path: Path,
) -> None:
    # The reconciler re-runs recovery on every pass; an unchanged skip must
    # not append another identical event to the target's ledger each time.
    rows = _reported_siblings()
    for attempt in range(1, review_orchestrator._MAX_ROUTE_ATTEMPTS + 1):
        dead = _dead_row("correctness", attempt=attempt)
        rows[dead["task_id"]] = dead

    manager, _launcher, _result = _recover(monkeypatch, tmp_path, rows)
    for _pass in range(3):
        review_orchestrator.recover_review_ready_targets(
            manager, db_path=tmp_path / "nf02277.sqlite",
        )

    assert [row["lens"] for row in _blocked(manager)] == ["correctness"]


def test_failed_ensure_is_stamped_without_its_exception_text(
    monkeypatch, tmp_path: Path,
) -> None:
    rows = _reported_siblings()
    reserved = _reserved_row("correctness", age=STALE)
    rows[reserved["task_id"]] = reserved

    manager, launcher, result = _recover(
        monkeypatch, tmp_path, rows,
        launcher=_FakeLauncher(RuntimeError(f"route table said {SECRET}")),
    )

    assert result["review_recovery_reasons"]["ensure_failed"] == 1
    assert len(launcher.launches) == 1
    blocked = _blocked(manager)
    assert [row["reason"] for row in blocked] == [
        "review_recovery_skipped:ensure_failed:RuntimeError"
    ]
    assert SECRET not in str(manager.events)


# --- B) a transient/credential-reset reviewer death relaunches on its route --


def _credential_terminal() -> dict:
    return {
        "state": "worker_failed",
        "latest_event": {
            "failure_kind": "worker_failed",
            "diagnostic": f"worker_failed:{CREDENTIAL_CODE}:exit_code=1",
        },
        "task_card": {
            "terminal_substatus": "worker_failed",
            "worker_status": "worker_failed",
        },
    }


def _transient_terminal() -> dict:
    return {
        "state": "worker_failed",
        "error_code": "provider_unavailable",
        "task_card": {
            "terminal_substatus": "worker_failed",
            "worker_status": "worker_failed",
        },
    }


def _deterministic_terminal() -> dict:
    return {
        "state": "worker_failed",
        "error_code": "runtime_error",
        "task_card": {
            "terminal_substatus": "worker_failed",
            "worker_status": "worker_failed",
        },
    }


def _only_route(*_args) -> dict[str, str]:
    """One route on one adapter: the account/session switch case."""
    return dict(base.FIRST_ROUTE)


def _single_route_chain(tmp_path: Path, name: str):
    manager = base._FailoverManager(tmp_path)
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / f"{name}.sqlite", route_selector=_only_route,
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=base.NOW,
        **base._one_lens_plan(),
    )
    assert driver.drain(max_actions=1, now=base.NOW).completed == 1
    return manager, driver, chain


def _kill_latest(manager, driver, chain, terminal: dict) -> dict:
    attempt = driver._route_attempts(chain.chain_id, "correctness")[-1]
    request_id = str(attempt["reviewer_request_id"])
    manager.status_results[request_id] = {
        "ok": True,
        "request_id": request_id,
        "task_id": str(attempt["reviewer_task_id"]),
        **{key: str(attempt[key]) for key in base.ROUTE},
        **terminal,
    }
    return dict(attempt)


@pytest.mark.parametrize(
    "terminal", [_credential_terminal(), _transient_terminal()],
    ids=["credential_reset", "transient"],
)
def test_transient_or_credential_reset_reviewer_relaunches_once_on_its_route(
    tmp_path: Path, terminal: dict,
) -> None:
    manager, driver, chain = _single_route_chain(tmp_path, "same-route")
    first = _kill_latest(manager, driver, chain, terminal)

    driver.drain(max_actions=1, now=base.NOW)

    attempts = driver._route_attempts(chain.chain_id, "correctness")
    assert len(attempts) == 2
    retired, successor = attempts
    assert retired["state"] == "retired"
    assert review_orchestrator.reviewer_route_same_route_retry(
        retired["failure_reason"]
    )
    assert review_orchestrator.route_attempt_hold(retired["failure_reason"]) == ""
    assert successor["state"] == "launched"
    assert {key: successor[key] for key in base.ROUTE} == {
        key: first[key] for key in base.ROUTE
    }, "the same route"
    assert successor["reviewer_task_id"] != first["reviewer_task_id"]
    assert successor["reviewer_request_id"] != first["reviewer_request_id"], (
        "under a new request_id"
    )
    assert len(manager.provider_launches) == 2

    # Bounded by the existing retry ceiling: a second death buys nothing more.
    _kill_latest(manager, driver, chain, terminal)
    driver.drain(max_actions=1, now=base.NOW)
    base._drain_lens(driver, "correctness")

    assert len(manager.provider_launches) == 2, "relaunched once, not per death"
    assert len(driver._route_attempts(chain.chain_id, "correctness")) == 2


def test_deterministic_reviewer_failure_is_not_relaunched(tmp_path: Path) -> None:
    manager, driver, chain = _single_route_chain(tmp_path, "deterministic")
    _kill_latest(manager, driver, chain, _deterministic_terminal())

    driver.drain(max_actions=1, now=base.NOW)
    base._drain_lens(driver, "correctness")

    assert len(manager.provider_launches) == 1
    attempts = driver._route_attempts(chain.chain_id, "correctness")
    assert len(attempts) == 1
    assert not review_orchestrator.reviewer_route_same_route_retry(
        attempts[0]["failure_reason"]
    )


def test_same_route_classifier_is_the_canonical_disposition() -> None:
    """Placed by the typed disposition, never by a copy of its vocabulary."""
    allowed = review_orchestrator.reviewer_same_route_retry_allowed
    transient = review_orchestrator.reviewer_terminal_disposition(_transient_terminal())
    credential = review_orchestrator.reviewer_terminal_disposition(_credential_terminal())
    unknown = review_orchestrator.reviewer_terminal_disposition(_deterministic_terminal())
    capacity = review_orchestrator.reviewer_terminal_disposition(
        {"state": "launch_failed", "error_code": "rate_limited"}
    )

    assert allowed(transient, attempts_made=1) is True
    assert allowed(credential, attempts_made=1) is True
    assert allowed(unknown, attempts_made=1) is False
    assert allowed(capacity, attempts_made=1) is False, "a reset window owns capacity"
    assert allowed(transient, attempts_made=review_orchestrator._MAX_ROUTE_ATTEMPTS) is False
