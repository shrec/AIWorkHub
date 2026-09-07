"""NF-2026-00655 / A1-R1: launch-time route identity and failure-circuit gate.

The measured defect: 40 of 110 blocked cards were launched on a runner absent
from the workforce catalog, and ``codex_cli``/``gpt-5.4`` alone took 103
launches for 0 accepts and 0 rejects because a route with no catalog row
belonged to no failure circuit -- ``route_failure_circuit`` was only ever
computed inside ``build_catalog``, once per catalog ROW.

These tests hold the three facts that fix it: a route's circuit is computable
whether or not the catalog lists it, a sealed provider ``model_not_found``
extinguishes that route after ONE failure, and a working route that is merely
unregistered is never refused.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiworkhub import core, process_launcher, task_store, workforce_catalog


def _repo(tmp_path: Path) -> Path:
    """A repository whose workforce is the built-in default (a fresh install)."""
    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True)
    return root


def _ready_repo(tmp_path: Path) -> Path:
    """A fresh install whose canonical task store exists and is empty."""
    root = _repo(tmp_path)
    task_store.initialize_repository(root)
    return root


def _sealed_model_error(model: str) -> dict[str, object]:
    return {
        "schema_id": "aiworkhub.provider_route_error.v1",
        "owner": "provider",
        "sealed": True,
        "code": "model_not_supported",
        "http_status": 400,
        "model": model,
        "detail": f"The '{model}' model is not supported",
    }


def _observation(
    *, state: str, age_seconds: float, provider_error: dict | None = None
) -> dict[str, object]:
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    row: dict[str, object] = {
        "adapter_id": "codex_cli",
        "model": "gpt-5.4",
        "state": state,
        "error": "worker_failed:supervisor_state=exited:exit_code=1",
        "finished_at": stamp.isoformat(),
    }
    if provider_error is not None:
        row["provider_error"] = provider_error
    return row


# ---------------------------------------------------------------------------
# The circuit is a property of what a route DID, not of being listed.
# ---------------------------------------------------------------------------


def test_sealed_model_not_found_opens_the_circuit_after_one_failure(
    tmp_path: Path,
) -> None:
    circuit = workforce_catalog.route_circuit_for(
        _repo(tmp_path),
        "codex_cli",
        "gpt-5.4",
        observations=[
            _observation(
                state="worker_failed",
                age_seconds=10.0,
                provider_error=_sealed_model_error("gpt-5.4"),
            )
        ],
    )

    assert circuit["state"] == "open"
    assert circuit["failure_kind"] == "model_not_found"
    assert circuit["consecutive_failures"] == 1
    assert circuit["threshold"] == 1
    assert circuit["adapter_id"] == "codex_cli"
    assert circuit["model"] == "gpt-5.4"


def test_unsealed_worker_failures_do_not_open_the_circuit(tmp_path: Path) -> None:
    # The 31 retained ``gpt-5.4`` failures carried no sealed provider error, so
    # they are exit-code evidence about the WORKER.  Opening a route circuit on
    # them would extinguish routes for crashes the route did not cause.
    circuit = workforce_catalog.route_circuit_for(
        _repo(tmp_path),
        "codex_cli",
        "gpt-5.4",
        observations=[
            _observation(state="worker_failed", age_seconds=float(index * 10))
            for index in range(1, 6)
        ],
    )

    assert circuit["state"] == "closed"
    assert circuit["consecutive_failures"] == 0


def test_a_later_success_closes_a_model_not_found_circuit(tmp_path: Path) -> None:
    circuit = workforce_catalog.route_circuit_for(
        _repo(tmp_path),
        "codex_cli",
        "gpt-5.4",
        observations=[
            _observation(state="review_ready", age_seconds=5.0),
            _observation(
                state="worker_failed",
                age_seconds=60.0,
                provider_error=_sealed_model_error("gpt-5.4"),
            ),
        ],
    )

    assert circuit["state"] == "closed"


def test_model_not_found_outlives_the_transient_cooldown(tmp_path: Path) -> None:
    # Replayed against the real 2026-09-04 sequence, the 600s transient
    # cooldown re-admitted the dead ``gpt-5.4`` route 68 times in 16.3 hours.
    # A model this account does not have is not re-provisioned in ten minutes.
    circuit = workforce_catalog.route_circuit_for(
        _repo(tmp_path),
        "codex_cli",
        "gpt-5.4",
        observations=[
            _observation(
                state="worker_failed",
                age_seconds=workforce_catalog.ROUTE_CIRCUIT_COOLDOWN_SECONDS * 3,
                provider_error=_sealed_model_error("gpt-5.4"),
            )
        ],
    )

    assert circuit["state"] == "open"
    assert circuit["cooldown_seconds"] == workforce_catalog.ROUTE_CIRCUIT_LOOKBACK_SECONDS


def test_transient_failures_keep_the_short_cooldown(tmp_path: Path) -> None:
    # Only the model kind gets the long horizon; a transport blip still clears
    # in ten minutes, so a flaky bridge never becomes a day-long outage.
    circuit = workforce_catalog.route_circuit_for(
        _repo(tmp_path),
        "codex_cli",
        "gpt-5.4",
        observations=[
            {
                "adapter_id": "codex_cli",
                "model": "gpt-5.4",
                "state": "launch_failed",
                "error": "provider_timeout",
                "finished_at": (
                    datetime.now(timezone.utc)
                    - timedelta(
                        seconds=workforce_catalog.ROUTE_CIRCUIT_COOLDOWN_SECONDS * 2
                    )
                ).isoformat(),
            }
            for _ in range(3)
        ],
    )

    assert circuit["cooldown_seconds"] == workforce_catalog.ROUTE_CIRCUIT_COOLDOWN_SECONDS
    assert circuit["state"] == "half_open"


def test_one_probe_reaches_the_provider_then_the_route_is_shut(
    tmp_path: Path,
) -> None:
    """The measured burst, replayed: 105 launches become 1.

    Each launch the circuit admits reaches the provider, takes the measured
    400, and is recorded as a sealed ``launch_failed``.  Each launch it refuses
    never spawns.
    """
    root = _repo(tmp_path)
    start = datetime.now(timezone.utc) - timedelta(hours=16)
    observations: list[dict[str, object]] = []
    admitted = 0
    for index in range(105):
        at = start + timedelta(seconds=index * 560)
        circuit = workforce_catalog.route_circuit_for(
            root,
            "codex_cli",
            "gpt-5.4",
            now_epoch=at.timestamp(),
            observations=list(observations),
        )
        if circuit["state"] == "open":
            continue
        admitted += 1
        observations.append(
            {
                "adapter_id": "codex_cli",
                "model": "gpt-5.4",
                "state": "launch_failed",
                "error": "provider_route_model_unavailable:model=gpt-5.4",
                "finished_at": (at + timedelta(seconds=40)).isoformat(),
                "provider_error": _sealed_model_error("gpt-5.4"),
            }
        )

    assert admitted == 1


def test_circuit_reopens_for_a_route_the_catalog_never_declared(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    # ``gpt-5.4`` has no catalog row at all -- that is precisely the population
    # whose circuit nobody computed.
    assert workforce_catalog.catalog_declares_route(root, "codex_cli", "gpt-5.4") is False
    circuit = workforce_catalog.route_circuit_for(
        root,
        "codex_cli",
        "gpt-5.4",
        observations=[
            _observation(
                state="worker_failed",
                age_seconds=1.0,
                provider_error=_sealed_model_error("gpt-5.4"),
            )
        ],
    )
    assert circuit["state"] == "open"


def test_unreadable_route_evidence_is_unobserved_not_a_refusal(
    tmp_path: Path,
) -> None:
    # A repository with no initialized task store cannot answer the question.
    # Unmeasured must never be published as measured-negative, or the gate
    # refuses every route on a fresh install.
    circuit = workforce_catalog.route_circuit_for(
        tmp_path / "never-initialized", "codex_cli", "gpt-5.5"
    )

    assert circuit["state"] == "unobserved"
    assert circuit["consecutive_failures"] == 0
    assert str(circuit.get("reason") or "").startswith("route_evidence_unavailable:")


# ---------------------------------------------------------------------------
# Identity resolution: report exactly, or report nothing.
# ---------------------------------------------------------------------------


def test_variant_spelling_resolves_to_the_one_catalog_identity(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    verdict = workforce_catalog.resolve_launch_route(
        root, "deepseek_v4_pro", "deepseek_vscode_lm", "deepseek-v4-pro"
    )

    assert verdict["resolved_runner"] == "deepseek_v4-pro"
    assert verdict["identity_state"] == "resolved_variant_spelling"
    assert verdict["model_declared_by_catalog"] is True


def test_absent_runner_resolves_to_nothing_and_names_the_alternatives(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    verdict = workforce_catalog.resolve_launch_route(
        root, "codex_gpt-5.4", "codex_cli", "gpt-5.4"
    )

    assert verdict["resolved_runner"] == ""
    assert verdict["identity_state"] == "unknown_runner"
    assert verdict["model_declared_by_catalog"] is False
    # A bare refusal on a free-text field is not actionable; the caller is told
    # what this repository does declare.
    assert "codex_gpt-5.5" in verdict["catalog_runners"]


def test_a_near_miss_is_refused_rather_than_retargeted(tmp_path: Path) -> None:
    # ``codex_5.6`` looks like ``codex_gpt-5.6-sol``.  Mapping it there because
    # it looks close is worse than saying it resolved to nothing.
    root = _repo(tmp_path)
    verdict = workforce_catalog.resolve_launch_route(
        root, "codex_5.6", "codex_cli", "gpt-5.6"
    )

    assert verdict["resolved_runner"] == ""
    assert verdict["model_declared_by_catalog"] is False


def test_editor_discovered_family_model_counts_as_declared(tmp_path: Path) -> None:
    # ``glm-5.3`` served 6 accepted cards while being absent from every
    # configuration file: its model set is populated by the live editor, and
    # the ``glm-5.2`` seed is the declaration that it is.
    root = _repo(tmp_path)

    assert workforce_catalog.catalog_declares_route(root, "glm_vscode_lm", "glm-5.3") is True
    assert workforce_catalog.catalog_declares_route(root, "glm_vscode_lm", "gpt-5.4") is False


def test_catalog_identities_do_not_depend_on_the_running_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)

    def unavailable(*_args, **_kwargs):
        raise AssertionError("identity must not consult live host readiness")

    monkeypatch.setattr(workforce_catalog.repo_policy, "build_preflight", unavailable)
    identities = workforce_catalog.catalog_launch_identities(root)

    assert "claude_opus-5" in identities
    assert "codex_gpt-5.5" in identities


# ---------------------------------------------------------------------------
# The provider's own 400, read at the boundary where the body is in hand.
# ---------------------------------------------------------------------------


_MEASURED_BODY = json.dumps(
    {
        "type": "error",
        "status": 400,
        "error": {
            "type": "invalid_request_error",
            "message": (
                "The 'gpt-5.4' model is not supported when using Codex with a "
                "ChatGPT account."
            ),
        },
    }
)


def test_measured_400_envelope_is_sealed_as_a_route_failure(tmp_path: Path) -> None:
    path = tmp_path / "stdout.log"
    path.write_text(_MEASURED_BODY + "\n", encoding="utf-8")

    record = process_launcher._provider_model_rejection_from_output(path, "gpt-5.4")

    assert record is not None
    assert record["refusal_kind"] == "model_not_found"
    assert record["recoverable"] is False
    assert record["reason"] == "provider_route_model_unavailable:model=gpt-5.4"
    sealed = record["provider_error"]
    assert sealed["owner"] == "provider"
    assert sealed["sealed"] is True
    assert workforce_catalog._sealed_error_kind(sealed) == "model_not_found"


def test_a_400_naming_another_model_is_not_this_routes_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stdout.log"
    path.write_text(_MEASURED_BODY + "\n", encoding="utf-8")

    assert (
        process_launcher._provider_model_rejection_from_output(path, "gpt-5.5") is None
    )


def test_a_plain_400_is_not_a_route_failure(tmp_path: Path) -> None:
    path = tmp_path / "stdout.log"
    path.write_text(
        json.dumps(
            {
                "type": "error",
                "status": 400,
                "error": {"type": "overloaded_error", "message": "gpt-5.4 busy"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        process_launcher._provider_model_rejection_from_output(path, "gpt-5.4") is None
    )


def test_worker_prose_cannot_forge_a_route_failure(tmp_path: Path) -> None:
    path = tmp_path / "stdout.log"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": _MEASURED_BODY,
                "status": 400,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        process_launcher._provider_model_rejection_from_output(path, "gpt-5.4") is None
    )


# ---------------------------------------------------------------------------
# The launch gate itself.
# ---------------------------------------------------------------------------


def test_launch_identity_refuses_an_extinguished_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(
        workforce_catalog,
        "route_circuit_for",
        lambda *_a, **_k: {
            "schema_id": "aiworkhub.route_failure_circuit.v1",
            "state": "open",
            "failure_kind": "model_not_found",
            "consecutive_failures": 1,
            "threshold": 1,
            "latest_failure_age_seconds": 60.0,
            "cooldown_seconds": 600.0,
        },
    )

    with pytest.raises(process_launcher.LaunchRejected) as excinfo:
        process_launcher.validate_workforce_identity(
            "codex_gpt-5.4", "codex_cli", "gpt-5.4", repo=root
        )

    message = str(excinfo.value)
    assert message.startswith("route_failure_circuit_open:")
    # Names the runner, what it resolved to, the route, why, and the way out.
    assert "runner=codex_gpt-5.4" in message
    assert "resolved=nothing_in_this_repository_catalog" in message
    assert "model=gpt-5.4" in message
    assert "failure_kind=model_not_found" in message
    assert "cooldown_remaining_seconds=540" in message
    assert "catalog_runners=" in message
    assert "codex_gpt-5.5" in message


def test_launch_identity_allows_an_unregistered_route_with_a_closed_circuit(
    tmp_path: Path,
) -> None:
    # ``gpt-5.6-terra`` was measured at 250 claims and 30 accepted cards while
    # absent from the catalog.  Registration is not the gate; outcome is.
    root = _ready_repo(tmp_path)

    assert (
        process_launcher.validate_workforce_identity(
            "codex_gpt-5.6-terra", "codex_cli", "gpt-5.6-terra", repo=root
        )
        == "gpt-5.6-terra"
    )


def test_launch_identity_without_a_repo_consults_no_route_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args, **_kwargs):
        raise AssertionError("no repository was supplied; no evidence may be read")

    monkeypatch.setattr(workforce_catalog, "route_circuit_for", unavailable)

    assert (
        process_launcher.validate_workforce_identity(
            "codex_gpt-5.4", "codex_cli", "gpt-5.4"
        )
        == "gpt-5.4"
    )


# ---------------------------------------------------------------------------
# Retained evidence: the 90-day event log, not the short-horizon ledger.
# ---------------------------------------------------------------------------


def _append_terminal_event(
    root: Path, *, event: str, adapter_id: str, model: str, substatus: str
) -> None:
    db_path = task_store.canonical_db_path(root)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at)"
            " VALUES (?,?,?,?,?)",
            (
                "TASK_ROUTE_EVIDENCE",
                event,
                "codex_gpt-5.4",
                json.dumps(
                    {
                        "deterministic_verification": {"substatus": substatus},
                        "evidence": {
                            "adapter_id": adapter_id,
                            "model": model,
                            "error": "worker_failed",
                            "provider_error": _sealed_model_error(model),
                        },
                    }
                ),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_route_observations_are_scoped_to_one_exact_route(tmp_path: Path) -> None:
    root = _ready_repo(tmp_path)
    _append_terminal_event(
        root,
        event="terminal_failure",
        adapter_id="codex_cli",
        model="gpt-5.4",
        substatus="worker_failed",
    )
    _append_terminal_event(
        root,
        event="terminal_failure",
        adapter_id="codex_cli",
        model="gpt-5.5",
        substatus="worker_failed",
    )
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    rows = task_store.route_terminal_observations(
        root, adapter_id="codex_cli", model="gpt-5.4", since_iso=since
    )

    assert [row["model"] for row in rows] == ["gpt-5.4"]
    assert rows[0]["state"] == "worker_failed"
    assert rows[0]["provider_error"]["code"] == "model_not_supported"


def test_retained_evidence_alone_extinguishes_the_route(tmp_path: Path) -> None:
    root = _ready_repo(tmp_path)
    _append_terminal_event(
        root,
        event="terminal_failure",
        adapter_id="codex_cli",
        model="gpt-5.4",
        substatus="worker_failed",
    )

    circuit = workforce_catalog.route_circuit_for(root, "codex_cli", "gpt-5.4")

    assert circuit["state"] == "open"
    assert circuit["failure_kind"] == "model_not_found"

    with pytest.raises(
        process_launcher.LaunchRejected, match="route_failure_circuit_open"
    ):
        process_launcher.validate_workforce_identity(
            "codex_gpt-5.4", "codex_cli", "gpt-5.4", repo=root
        )


def test_observations_outside_the_window_are_not_read(tmp_path: Path) -> None:
    root = _ready_repo(tmp_path)
    _append_terminal_event(
        root,
        event="terminal_failure",
        adapter_id="codex_cli",
        model="gpt-5.4",
        substatus="worker_failed",
    )
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    assert (
        task_store.route_terminal_observations(
            root, adapter_id="codex_cli", model="gpt-5.4", since_iso=future
        )
        == []
    )


# ---------------------------------------------------------------------------
# Create-time normalization folds onto the catalog, and refuses nothing.
# ---------------------------------------------------------------------------


def test_create_time_runner_folds_onto_catalog_identities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(core, "repo_root", lambda: root)

    assert core.resolve_create_time_runner("deepseek_v4_pro") == ("deepseek_v4-pro", None)
    assert core.resolve_create_time_runner("glm-5.2") == ("glm_5.2", None)
    assert core.resolve_create_time_runner("claude_opus_5") == ("claude_opus-5", None)


def test_create_time_runner_never_refuses_a_per_card_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 804 of 2,660 measured claim_start events used a per-card reviewer
    # identity.  Refusing them would stop the review lane, not the leak.
    root = _repo(tmp_path)
    monkeypatch.setattr(core, "repo_root", lambda: root)

    for runner in ("codex_qr_nf492_correctness", "claude_worker", "codex_5_6_sol"):
        assert core.resolve_create_time_runner(runner) == (runner, None)
