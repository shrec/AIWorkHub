"""NF-2026-02107: every required reviewer lens launches or names why it did not.

Measured: 15 of 140 reviewers since 2026-11-06 had to be launched by hand. A
launch that raised vanished, an expired editor-hosted credential retired the
only attempt instead of moving to another adapter, and a required lens could
be absent with nothing on the target saying why.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_SRC = _TESTS.parent / "src"
for _path in (_SRC, _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import pytest  # noqa: E402

import test_review_orchestrator as base  # noqa: E402
from aiworkhub import process_launcher_accept_review  # noqa: E402
from aiworkhub import review_orchestrator  # noqa: E402
from aiworkhub import reviewer_launch_refusal  # noqa: E402
from aiworkhub import terminal_failure_classification as tfc  # noqa: E402

NOW = base.NOW
# Well past the readiness window: the existing suites all drain at a fixed NOW.
LATER = NOW + timedelta(minutes=5)
MUCH_LATER = NOW + timedelta(minutes=10)
EXPIRED = "vscode_lm_request_failed:token expired or incorrect"
CREDENTIAL_CODE = "vscode_lm_credential_rejected"
LENSES = ("correctness", "security", "code_quality")


def _driver(manager, tmp_path: Path, name: str, selector):
    driver = review_orchestrator.ReviewOrchestrator(
        manager, db_path=tmp_path / f"{name}.sqlite", route_selector=selector,
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=NOW,
    )
    return driver, chain


def _first_route(*_args) -> dict[str, str]:
    return dict(base.FIRST_ROUTE)


def _blockers(manager) -> list[dict]:
    return [
        event["review_automation"]
        for event in manager.events
        if event.get("event_type") == "review_orchestrator_lens_blocked"
    ]


# --- (1) a launch that raises is recorded, and still retried ----------------


class _RaisingManager(base._FailoverManager):
    def __init__(self, repo: Path, message: str = "launcher pipe closed") -> None:
        super().__init__(repo)
        self.raises = 1
        self.message = message

    def launch_quality_reviewer(self, **kwargs):
        if self.raises:
            self.raises -= 1
            raise OSError(self.message)
        return super().launch_quality_reviewer(**kwargs)


def test_launch_exception_is_recorded_on_the_attempt_and_retry_is_kept(
    tmp_path: Path,
) -> None:
    manager = _RaisingManager(tmp_path)
    driver, chain = _driver(manager, tmp_path, "raises", _first_route)

    assert driver.drain(max_actions=1, now=NOW).pending == 1

    attempt = driver._route_attempts(chain.chain_id, "correctness")[-1]
    assert attempt["state"] == "planned"
    assert attempt["failure_reason"].startswith("launch_exception:OSError:")
    assert "launcher pipe closed" in attempt["failure_reason"]

    assert driver.drain(max_actions=1, now=NOW).completed == 1
    retried = driver._route_attempts(chain.chain_id, "correctness")
    assert len(retried) == 1, "the retry reconciles the same attempt"
    assert retried[-1]["state"] == "launched"
    assert retried[-1]["failure_reason"] == "", "a healed fault is not a current one"


def test_launch_exception_message_is_bounded_and_cannot_forge_a_hold(
    tmp_path: Path,
) -> None:
    manager = _RaisingManager(tmp_path, "boom:hold=credential_hold " + "x" * 5000)
    driver, chain = _driver(manager, tmp_path, "bounded", _first_route)

    driver.drain(max_actions=1, now=NOW)

    reason = driver._route_attempts(chain.chain_id, "correctness")[-1]["failure_reason"]
    assert len(reason) <= 200
    assert review_orchestrator.route_attempt_hold(reason) == ""


# --- (2) a credential failure indicts the adapter ---------------------------


@pytest.mark.parametrize(
    "error",
    [
        EXPIRED,
        "vscode_lm_request_failed:IDE token expired: unauthorized",
        "vscode_lm_request_failed:incorrect token",
        "vscode_lm_request_failed:Authentication failed for this session",
    ],
)
def test_envelope_credential_body_is_named_by_the_classifier(error: str) -> None:
    verdict = tfc.terminal_event_authority(
        state="worker_failed", exit_code=1, error=error,
    )

    assert CREDENTIAL_CODE in verdict["diagnostic"]
    assert "token expired" not in verdict["diagnostic"].lower()


@pytest.mark.parametrize(
    "error",
    [
        "vscode_lm_request_failed:the model is overloaded",
        # The bounded diagnostics blob carries model-adjacent text and is never
        # evidence about the credential.
        'vscode_lm_request_failed:overloaded:diagnostics={"protocol_preview":"token expired"}',
        'vscode_lm_request_failed:overloaded:diagnostics='
        '{"protocol_preview":"vscode_lm_request_failed:token expired"}',
        "token expired",
    ],
)
def test_text_outside_the_envelope_body_names_no_credential(error: str) -> None:
    verdict = tfc.terminal_event_authority(
        state="worker_failed", exit_code=1, error=error,
    )

    assert CREDENTIAL_CODE not in str(verdict["diagnostic"])


def _credential_terminal(diagnostic: str) -> dict:
    return {
        "state": "worker_failed",
        "latest_event": {"failure_kind": "worker_failed", "diagnostic": diagnostic},
        "task_card": {
            "terminal_substatus": "worker_failed",
            "worker_status": "worker_failed",
        },
    }


CREDENTIAL_TERMINALS = [
    pytest.param(
        _credential_terminal(f"worker_failed:{CREDENTIAL_CODE}:exit_code=1"),
        id="classified-code",
    ),
    pytest.param(_credential_terminal(f"worker_failed:{EXPIRED}"), id="raw-envelope"),
    pytest.param(
        {"state": "launch_failed", "error_code": "credential_rejected"},
        id="typed-refusal",
    ),
]


@pytest.mark.parametrize("terminal", CREDENTIAL_TERMINALS)
def test_credential_failure_moves_to_one_successor_on_another_adapter(
    tmp_path: Path, terminal: dict,
) -> None:
    manager, _db, chain, first_task, seed = base._seed_terminal_launch(
        tmp_path, "credential", terminal,
    )

    assert seed.drain(max_actions=1, now=NOW).completed == 1

    assert len(manager.launches) == 2
    first, successor = seed._route_attempts(chain.chain_id, "correctness")
    assert first["state"] == "retired"
    assert first["reviewer_task_id"] == first_task
    assert review_orchestrator.route_attempt_hold(first["failure_reason"]) == ""
    assert successor["state"] == "launched"
    assert successor["adapter_id"] == base.SUCCESSOR_ROUTE["adapter_id"]
    _routes, adapters = review_orchestrator._route_attempt_exclusions([first])
    assert base.FIRST_ROUTE["adapter_id"] in adapters


def test_second_credential_failure_holds_instead_of_a_third_attempt(
    tmp_path: Path,
) -> None:
    terminal = _credential_terminal(f"worker_failed:{CREDENTIAL_CODE}:exit_code=1")
    manager, _db, chain, _first_task, seed = base._seed_terminal_launch(
        tmp_path, "second", terminal,
    )
    second_task = seed._reviewer_task_id(
        chain.chain_identity, "correctness", attempt_index=2,
    )
    manager.terminal_launch_results[second_task] = {"ok": False, **terminal}

    assert seed.drain(max_actions=1, now=NOW).completed == 0
    assert seed.drain(max_actions=1, now=NOW).completed == 0

    assert len(manager.launches) == 2, "exactly one relaunch"
    attempts = seed._route_attempts(chain.chain_id, "correctness")
    assert len(attempts) == 2
    assert review_orchestrator.route_attempt_hold(
        attempts[-1]["failure_reason"]
    ) == "credential_hold"


@pytest.mark.parametrize("terminal", CREDENTIAL_TERMINALS)
def test_credential_failure_with_no_other_adapter_records_a_reasoned_hold(
    tmp_path: Path, terminal: dict,
) -> None:
    manager, _db, chain, _first_task, seed = base._seed_terminal_launch(
        tmp_path, "single-adapter", terminal,
    )
    seed.route_selector = _first_route

    assert seed.drain(max_actions=1, now=NOW).completed == 0
    assert seed.drain(max_actions=1, now=NOW).completed == 0

    assert len(manager.launches) == 1, "no second launch on the indicted adapter"
    attempts = seed._route_attempts(chain.chain_id, "correctness")
    assert len(attempts) == 1
    assert attempts[0]["state"] == "retired"
    assert review_orchestrator.route_attempt_hold(
        attempts[0]["failure_reason"]
    ) == "credential_hold"
    assert "adapter_credential" in attempts[0]["failure_reason"]


def test_reviewer_that_ran_and_lost_its_credential_fails_over_on_reconcile(
    tmp_path: Path,
) -> None:
    manager = base._FailoverManager(tmp_path)
    driver, chain = _driver(manager, tmp_path, "midrun", _first_route)
    first_task = driver._reviewer_task_id(chain.chain_identity, "correctness")
    driver.route_selector = base._two_route_selector(first_task)
    assert driver.drain(max_actions=1, now=NOW).completed == 1
    first_request = manager.requests_by_task[first_task]
    manager.status_results[first_request] = {
        "ok": True,
        "request_id": first_request,
        "task_id": first_task,
        **base.FIRST_ROUTE,
        **_credential_terminal(f"worker_failed:{CREDENTIAL_CODE}:exit_code=1"),
    }

    driver.drain(max_actions=1, now=NOW)

    first, successor = driver._route_attempts(chain.chain_id, "correctness")
    assert first["state"] == "retired"
    assert "adapter_credential" in first["failure_reason"]
    assert successor["adapter_id"] == base.SUCCESSOR_ROUTE["adapter_id"]
    assert len(manager.provider_launches) == 2


@pytest.mark.parametrize("kind", ["rate_limited", "session_limit", "quota_exhausted"])
def test_account_wide_limit_keeps_todays_behaviour(tmp_path: Path, kind: str) -> None:
    manager, _db, chain, _first_task, seed = base._seed_terminal_launch(
        tmp_path, kind, {"state": "launch_failed", "error_code": kind},
    )

    assert seed.drain(max_actions=1, now=NOW).completed == 0

    assert len(manager.launches) == 1, "a limit is not failed over in this pass"
    attempts = seed._route_attempts(chain.chain_id, "correctness")
    assert "adapter_credential" not in attempts[0]["failure_reason"]
    dispatch = review_orchestrator.reviewer_recovery_dispatch(
        {"state": "launch_failed", "error_code": kind}, attempts_made=1,
    )
    assert dispatch["action"] == "capacity_hold"
    assert dispatch["relaunch_distinct_route"] is False


def test_exhausted_balance_stays_a_hold(tmp_path: Path) -> None:
    """Only added credit clears a balance, and no adapter switch adds any."""
    manager, _db, chain, _first_task, seed = base._seed_terminal_launch(
        tmp_path, "balance",
        {"state": "launch_failed", "error_code": "balance_exhausted"},
    )

    assert seed.drain(max_actions=1, now=NOW).completed == 0

    assert len(manager.launches) == 1
    attempts = seed._route_attempts(chain.chain_id, "correctness")
    assert review_orchestrator.route_attempt_hold(
        attempts[0]["failure_reason"]
    ) == "credential_hold"
    assert "adapter_credential" not in attempts[0]["failure_reason"]


# --- (3) a required lens is never silently absent ---------------------------


class _DeadFirstLensManager(base._FailoverManager):
    """The correctness launch fails closed, so its chain's later lenses retire."""

    def launch_quality_reviewer(self, **kwargs):
        if kwargs["lens"] == "correctness":
            self.launches.append(kwargs)
            return {"ok": True, "request_id": "", "task_id": kwargs["reviewer_task_id"]}
        return super().launch_quality_reviewer(**kwargs)


def test_lens_retired_with_its_chain_relaunches_exactly_once(tmp_path: Path) -> None:
    manager = _DeadFirstLensManager(tmp_path)
    driver, chain = _driver(manager, tmp_path, "dead-chain", _first_route)

    assert driver.drain(max_actions=1, now=NOW).failed == 1
    # The chain failed inside the window and nobody has judged it silent yet.
    assert len(manager.provider_launches) == 0

    driver.drain(max_actions=2, now=LATER)

    launched = [call["lens"] for call in manager.provider_launches]
    assert sorted(launched) == ["code_quality", "security"]
    for lens in ("security", "code_quality"):
        attempt = driver._route_attempts(chain.chain_id, lens)[-1]
        assert attempt["state"] == "launched"

    driver.drain(max_actions=1, now=MUCH_LATER)
    assert len(manager.provider_launches) == 2, "relaunch is once, not per pass"


def test_lens_that_cannot_relaunch_records_the_reason_once(tmp_path: Path) -> None:
    manager = _DeadFirstLensManager(tmp_path)

    def selector(_repo, _task, lens):
        if lens == "correctness":
            return dict(base.FIRST_ROUTE)
        raise review_orchestrator.ReviewRouteUnavailable(lens)

    driver, _chain = _driver(manager, tmp_path, "no-route", selector)
    driver.drain(max_actions=1, now=NOW)

    driver.drain(max_actions=1, now=LATER)
    driver.drain(max_actions=1, now=MUCH_LATER)

    reasons = {row["lens"]: row["reason"] for row in _blockers(manager)}
    assert reasons["security"].startswith("launch_exception:")
    assert reasons["security"].endswith("review_route_unavailable security")
    assert reasons["code_quality"].endswith("review_route_unavailable code_quality")
    assert len(_blockers(manager)) == len(LENSES), "one record per lens, not per pass"
    assert manager.provider_launches == []


def test_every_required_lens_has_a_reviewer_or_a_reason_after_a_pass(
    tmp_path: Path,
) -> None:
    manager = _DeadFirstLensManager(tmp_path)
    driver, chain = _driver(manager, tmp_path, "coverage", _first_route)
    driver.drain(max_actions=1, now=NOW)

    driver.drain(max_actions=3, now=LATER)

    blocked = {row["lens"] for row in _blockers(manager)}
    for lens in LENSES:
        attempts = driver._route_attempts(chain.chain_id, lens)
        assert lens in blocked or any(
            row["state"] == "launched" for row in attempts
        ), f"{lens} is absent with no reason"


def test_held_lens_and_the_lens_behind_it_carry_reasons(tmp_path: Path) -> None:
    manager, _db, _chain, _first_task, seed = base._seed_terminal_launch(
        tmp_path, "held", {"state": "launch_failed", "error_code": "runtime_error"},
    )
    seed.drain(max_actions=1, now=NOW)

    seed.drain(max_actions=1, now=LATER)
    seed.drain(max_actions=1, now=MUCH_LATER)

    reasons = {row["lens"]: row["reason"] for row in _blockers(manager)}
    assert reasons["correctness"].endswith(":hold=manager_judgment_unknown")
    assert reasons["security"] == "waiting_on_lens:correctness"
    assert reasons["code_quality"] == "waiting_on_lens:correctness"
    assert len(_blockers(manager)) == len(LENSES)


def test_lens_waiting_behind_a_live_reviewer_is_not_called_blocked(
    tmp_path: Path,
) -> None:
    manager = base._FailoverManager(tmp_path)
    driver, _chain = _driver(manager, tmp_path, "live", _first_route)
    driver.drain(max_actions=1, now=NOW)

    driver.drain(max_actions=1, now=LATER)

    assert _blockers(manager) == []
    assert len(manager.provider_launches) == 1


def test_a_target_that_is_not_ready_is_left_alone(tmp_path: Path) -> None:
    manager = _DeadFirstLensManager(tmp_path)
    driver, _chain = _driver(manager, tmp_path, "not-ready", _first_route)
    driver.drain(max_actions=1, now=NOW)
    manager.target_status = base._target_status(state="processing")

    driver.drain(max_actions=1, now=LATER)

    assert _blockers(manager) == []
    assert manager.provider_launches == []


def test_a_superseded_target_is_settled_and_never_relaunched(tmp_path: Path) -> None:
    manager = _DeadFirstLensManager(tmp_path)
    driver, _chain = _driver(manager, tmp_path, "superseded", _first_route)
    driver.drain(max_actions=1, now=NOW)
    manager.target_status = base._target_status(request_id="newer-request")

    driver.drain(max_actions=1, now=LATER)
    calls_after_settling = len(manager.launches)
    manager.target_status = base._target_status()
    driver.drain(max_actions=1, now=MUCH_LATER)

    assert _blockers(manager) == []
    assert manager.provider_launches == []
    assert len(manager.launches) == calls_after_settling


# --- settlement: what it may spend, what it may say -------------------------


SECRET = "sk-" + "a1B2c3D4e5F6"


def _settled_dead_chain(tmp_path: Path, name: str, selector=_first_route):
    manager = _DeadFirstLensManager(tmp_path)
    driver, chain = _driver(manager, tmp_path, name, selector)
    driver.drain(max_actions=1, now=NOW)
    return manager, driver, chain


def _launched_lenses(manager) -> list[str]:
    return sorted(call["lens"] for call in manager.provider_launches)


def _reviewer_died(manager, attempt: dict, **overrides) -> None:
    request_id = attempt["reviewer_request_id"]
    manager.status_results[request_id] = {
        "ok": True,
        "request_id": request_id,
        "task_id": attempt["reviewer_task_id"],
        **base.FIRST_ROUTE,
        "state": "launch_failed",
        "error_code": "runtime_error",
        **overrides,
    }


def _no_route_past_correctness(_repo, _task, lens):
    if lens == "correctness":
        return dict(base.FIRST_ROUTE)
    raise review_orchestrator.ReviewRouteUnavailable(lens)


def test_waiting_record_does_not_spend_the_lens_relaunch(tmp_path: Path) -> None:
    manager, driver, chain = _settled_dead_chain(tmp_path, "waiting")
    driver._store_lens_blocker(
        chain.chain_id, "security", "waiting_on_lens:correctness", NOW,
    )

    driver.drain(max_actions=3, now=LATER)
    driver.drain(max_actions=3, now=MUCH_LATER)

    assert _launched_lenses(manager).count("security") == 1


def test_settlement_launched_reviewer_that_dies_is_reported(tmp_path: Path) -> None:
    manager, driver, chain = _settled_dead_chain(tmp_path, "died")
    driver.drain(max_actions=3, now=LATER)
    assert len(manager.provider_launches) == 2
    _reviewer_died(manager, driver._route_attempts(chain.chain_id, "security")[-1])

    driver.drain(max_actions=3, now=MUCH_LATER)

    attempt = driver._route_attempts(chain.chain_id, "security")[-1]
    assert attempt["state"] == "retired"
    reasons = {row["lens"]: row["reason"] for row in _blockers(manager)}
    assert reasons["security"] == attempt["failure_reason"] != ""
    assert len(manager.provider_launches) == 2, "reported, not relaunched again"


def test_invalid_route_binding_is_that_lens_blocker(tmp_path: Path) -> None:
    manager, driver, chain = _settled_dead_chain(tmp_path, "binding")
    driver.drain(max_actions=3, now=LATER)
    _reviewer_died(
        manager, driver._route_attempts(chain.chain_id, "security")[-1],
        runner="foreign-runner", adapter_id="foreign-adapter", model="foreign-model",
    )

    driver.drain(max_actions=3, now=MUCH_LATER)

    reasons = {row["lens"]: row["reason"] for row in _blockers(manager)}
    assert reasons["security"] == "reviewer_terminal_route_binding_invalid"
    assert "code_quality" not in reasons, "the rest of the chain was still settled"
    assert "_fault" not in driver._lens_blocker_reasons(chain.chain_id)


@pytest.mark.parametrize(
    ("substatus", "relaunched"),
    [("", False), ("review_ready", False), ("accepted", False), ("worker_failed", True)],
)
def test_relaunch_defers_to_a_reviewer_already_bound_to_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, substatus: str, relaunched: bool,
) -> None:
    manager, driver, _chain = _settled_dead_chain(tmp_path, "bound")
    asked: list[tuple] = []

    def bound(*args):
        asked.append(args)
        return [{"task_id": "by-hand", "lens": "security", "terminal_substatus": substatus}]

    monkeypatch.setattr(process_launcher_accept_review, "bound_reviewer_rows", bound)

    driver.drain(max_actions=3, now=LATER)

    assert ("security" in _launched_lenses(manager)) is relaunched
    assert "code_quality" in _launched_lenses(manager)
    assert "security" not in {row["lens"] for row in _blockers(manager)}
    assert asked[0] == (manager.repo, "TARGET", "target-request")


def test_settlement_relaunch_spends_the_drain_bound(tmp_path: Path) -> None:
    manager, driver, chain = _settled_dead_chain(tmp_path, "budget")

    starved = driver.drain(max_actions=0, now=LATER)

    assert starved.attempted == 0
    assert manager.provider_launches == []
    recorded = driver._lens_blocker_reasons(chain.chain_id)
    assert "security" not in recorded
    assert "code_quality" not in recorded

    spent = driver.drain(max_actions=1, now=MUCH_LATER)

    assert (spent.attempted, spent.completed, spent.failed) == (1, 1, 0)
    assert len(manager.provider_launches) == 1


def test_settlement_relaunch_that_cannot_launch_counts_as_failed(tmp_path: Path) -> None:
    _manager, driver, _chain = _settled_dead_chain(
        tmp_path, "budget-failed", _no_route_past_correctness,
    )

    result = driver.drain(max_actions=1, now=LATER)

    assert (result.attempted, result.completed, result.failed) == (1, 0, 1)


def _always_fault(visited: list[int], text: str = "settlement read refused"):
    def fault(chain_id, _now, _budget):
        visited.append(chain_id)
        raise ValueError(text)

    return fault


def test_chain_fault_reaches_the_target_once_per_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = base._FailoverManager(tmp_path)
    driver, chain = _driver(manager, tmp_path, "fault-once", _first_route)
    monkeypatch.setattr(driver, "_settle_chain_lenses", _always_fault([]))

    driver.drain(max_actions=0, now=LATER)
    driver.drain(max_actions=0, now=MUCH_LATER)

    rows = [row for row in _blockers(manager) if row["lens"] == "_fault"]
    assert len(rows) == 1
    assert rows[0]["reason"] == driver._lens_blocker_reasons(chain.chain_id)["_fault"]
    assert rows[0]["reason"].startswith("launch_exception:ValueError:")


class _RefusingEventsManager(base._FailoverManager):
    refusing = True

    def _append_event(self, event):
        if self.refusing:
            raise OSError("event ledger full")
        super()._append_event(event)


def test_refused_fault_event_cannot_stop_the_next_chain_and_is_said_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _RefusingEventsManager(tmp_path)
    driver, first = _driver(manager, tmp_path, "fault-refused", _first_route)
    second = driver.ensure_chain(
        target_task_id="OTHER", target_request_id="other-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="c" * 64, now=NOW,
    )
    visited: list[int] = []
    monkeypatch.setattr(driver, "_settle_chain_lenses", _always_fault(visited))

    driver.drain(max_actions=0, now=LATER)

    assert visited == [first.chain_id, second.chain_id]
    assert "_fault" in driver._lens_blocker_reasons(first.chain_id)
    assert _blockers(manager) == []

    manager.refusing = False
    driver.drain(max_actions=0, now=MUCH_LATER)

    assert len(_blockers(manager)) == 2, "the unreported reason is said once it can be"


def test_relaunch_exception_text_cannot_carry_a_token_to_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, driver, chain = _settled_dead_chain(tmp_path, "token-relaunch")

    def refuse(_action):
        raise RuntimeError(f"launcher said {SECRET}")

    monkeypatch.setattr(driver, "_plan_route_attempt", refuse)

    driver.drain(max_actions=3, now=LATER)

    reasons = {row["lens"]: row["reason"] for row in _blockers(manager)}
    assert reasons["security"].startswith("launch_exception:RuntimeError:")
    assert SECRET not in str(reasons)
    assert SECRET not in str(driver._lens_blocker_reasons(chain.chain_id))


def test_chain_fault_text_cannot_carry_a_token_to_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = base._FailoverManager(tmp_path)
    driver, chain = _driver(manager, tmp_path, "token-fault", _first_route)
    monkeypatch.setattr(
        driver, "_settle_chain_lenses", _always_fault([], f"store said {SECRET}"),
    )

    driver.drain(max_actions=0, now=LATER)

    assert _blockers(manager), "the fault was reported"
    assert SECRET not in str(_blockers(manager))
    assert SECRET not in str(driver._lens_blocker_reasons(chain.chain_id))


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        (f"provider rejected {SECRET}", SECRET),
        ("header Bearer abc.def-123 refused", "abc.def-123"),
        ("argv was --token hunter2value here", "hunter2value"),
    ],
)
def test_launch_exception_reason_removes_recognized_secrets(
    message: str, secret: str,
) -> None:
    reason = reviewer_launch_refusal.launch_exception_reason(ValueError(message))

    assert secret not in reason
    assert reason.startswith("launch_exception:ValueError:")


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (f"worker_failed:overloaded:diagnostics=x:{CREDENTIAL_CODE}:y", ""),
        (f"vscode_lm_request_failed:overloaded:diagnostics={EXPIRED}", ""),
        (f"worker_failed:{CREDENTIAL_CODE}:diagnostics=x", CREDENTIAL_CODE),
        (f"{EXPIRED}:diagnostics=x", CREDENTIAL_CODE),
    ],
)
def test_credential_code_ignores_the_diagnostics_tail(reason: str, expected: str) -> None:
    assert tfc.reviewer_route_credential_code(reason) == expected
