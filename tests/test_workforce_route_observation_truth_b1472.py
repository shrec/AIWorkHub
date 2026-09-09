"""Startability must never read as current round-trip observation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from aiworkhub import model_settings, provider_route_contracts, repo_policy, workforce_catalog


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True, exist_ok=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")
    if not model_settings.load(root)["configured"]:
        model_settings.update(
            root,
            provider="openai",
            adapter="codex_cli",
            model="gpt-5.5",
            enabled=True,
            expected_revision=0,
        )
    return root


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _codex_ready(*, access_observed: bool = True, status: str = "ready_unverified") -> dict:
    return {
        "providers": [{
            "adapter_id": "codex_cli",
            "launchable": True,
            "access_observed": access_observed,
            "observed_models": ["gpt-5.5"],
            "status": status,
        }]
    }


def _row(snapshot: dict, worker_id: str = "gpt-5.5") -> dict:
    return next(item for item in snapshot["workers"] if item["worker_id"] == worker_id)


def test_never_run_startable_route_has_no_observed_round_trip(tmp_path: Path) -> None:
    snapshot = workforce_catalog.build_catalog(
        _root(tmp_path),
        cards=[],
        process_rows=[],
        preflight=_codex_ready(),
        now_epoch=2_000_000_000.0,
    )
    worker = _row(snapshot)
    assert worker["launch_eligible"] is True
    assert worker["available"] is True
    assert worker["readiness_status"] == "ready_unverified"
    assert worker["readiness"]["proves_round_trip"] is False
    assert worker["availability_observed"] is False
    assert worker["availability_observation"]["proves_round_trip"] is False
    assert worker["round_trip_observed"] == "unknown"
    assert worker["route_observation"]["state"] == provider_route_contracts.CAPABILITY_UNKNOWN
    assert worker["route_observation"]["reason"] == repo_policy.ROUTE_OBSERVATION_NEVER_RECORDED
    assert worker["historical_route_observation"]["recorded"] is False


def test_historical_only_round_trip_stays_unknown_with_exact_reason(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    stale = now - workforce_catalog.ROUTE_CIRCUIT_LOOKBACK_SECONDS - 60
    snapshot = workforce_catalog.build_catalog(
        _root(tmp_path),
        cards=[],
        process_rows=[{
            "request_id": "old",
            "task_id": "task-old",
            "adapter_id": "codex_cli",
            "model": "gpt-5.5",
            "state": "accepted",
            "finished_at": _iso(stale),
        }],
        preflight=_codex_ready(status="ready"),
        now_epoch=now,
    )
    worker = _row(snapshot)
    assert worker["available"] is True
    assert worker["round_trip_observed"] == "unknown"
    assert worker["availability_observed"] is False
    assert worker["route_observation"]["state"] == provider_route_contracts.CAPABILITY_UNKNOWN
    assert worker["route_observation"]["reason"] == repo_policy.ROUTE_OBSERVATION_OUTSIDE_WINDOW
    history = worker["historical_route_observation"]
    assert history["recorded"] is True
    assert history["in_current_window"] is False
    assert history["reason"] == repo_policy.ROUTE_OBSERVATION_OUTSIDE_WINDOW
    assert history["count"] >= 1


def test_in_window_terminal_success_alone_sets_current_round_trip(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    startable = workforce_catalog.build_catalog(
        _root(tmp_path),
        cards=[],
        process_rows=[],
        preflight=_codex_ready(status="ready"),
        now_epoch=now,
    )
    quiet = _row(startable)
    assert quiet["available"] is True
    assert quiet["round_trip_observed"] == "unknown"
    assert quiet["availability_observed"] is False

    observed = workforce_catalog.build_catalog(
        _root(tmp_path),
        cards=[],
        process_rows=[{
            "request_id": "ok",
            "task_id": "task-ok",
            "adapter_id": "codex_cli",
            "model": "gpt-5.5",
            "state": "accepted",
            "finished_at": _iso(now - 30),
        }],
        preflight=_codex_ready(status="ready"),
        now_epoch=now,
    )
    worker = _row(observed)
    assert worker["round_trip_observed"] is True
    assert worker["availability_observed"] is True
    assert worker["route_observation"]["state"] == provider_route_contracts.CAPABILITY_SUPPORTED
    assert worker["route_observation"]["reason"] == repo_policy.ROUTE_OBSERVATION_IN_WINDOW
    assert worker["historical_route_observation"]["in_current_window"] is True


def test_in_window_terminal_failure_sets_current_observation(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    snapshot = workforce_catalog.build_catalog(
        _root(tmp_path),
        cards=[],
        process_rows=[{
            "request_id": "fail",
            "task_id": "task-fail",
            "adapter_id": "codex_cli",
            "model": "gpt-5.5",
            "state": "worker_failed",
            "finished_at": _iso(now - 15),
            "provider_error": {
                "owner": "provider",
                "sealed": True,
                "code": "insufficient_balance",
                "http_status": 402,
            },
        }],
        preflight=_codex_ready(status="ready"),
        now_epoch=now,
    )
    worker = _row(snapshot)
    assert worker["round_trip_observed"] is True
    assert worker["availability_observed"] is False
    assert worker["route_observation"]["state"] == provider_route_contracts.CAPABILITY_UNSUPPORTED
    assert worker["historical_route_observation"]["in_current_window"] is True
    assert worker["available"] is False
    assert worker["readiness_status"] == "route_circuit_open"


def test_disabled_and_uncredentialed_routes_do_not_invent_observation(
    tmp_path: Path,
) -> None:
    now = 2_000_000_000.0
    disabled = workforce_catalog.build_catalog(
        _root(tmp_path),
        cards=[],
        process_rows=[],
        preflight={"providers": [{
            "adapter_id": "codex_cli",
            "launchable": False,
            "access_observed": False,
            "status": "missing_credential",
        }]},
        now_epoch=now,
    )
    worker = _row(disabled)
    assert worker["available"] is False
    assert worker["launch_eligible"] is False
    assert worker["round_trip_observed"] == "unknown"
    assert worker["availability_observed"] is False
    assert worker["route_observation"]["reason"] == repo_policy.ROUTE_OBSERVATION_NEVER_RECORDED
    assert worker["route_observation"]["state"] == provider_route_contracts.CAPABILITY_UNKNOWN


def test_reviewer_submit_comes_from_route_contract_not_installation(
    tmp_path: Path,
) -> None:
    installed = workforce_catalog.build_catalog(
        _root(tmp_path),
        cards=[],
        process_rows=[],
        preflight={
            "providers": [
                {
                    "adapter_id": "codex_cli",
                    "launchable": True,
                    "access_observed": True,
                    "status": "ready",
                },
                {
                    "adapter_id": "glm_vscode_lm",
                    "launchable": True,
                    "access_observed": True,
                    "status": "ready",
                },
            ]
        },
    )
    missing = workforce_catalog.build_catalog(
        _root(tmp_path),
        cards=[],
        process_rows=[],
        preflight={
            "providers": [
                {
                    "adapter_id": "codex_cli",
                    "launchable": False,
                    "access_observed": False,
                    "status": "missing_credential",
                },
                {
                    "adapter_id": "glm_vscode_lm",
                    "launchable": False,
                    "access_observed": False,
                    "status": "missing_credential",
                },
            ]
        },
    )
    for worker_id, expected_state in (
        ("gpt-5.5", provider_route_contracts.CAPABILITY_SUPPORTED),
        ("glm-5.2", provider_route_contracts.CAPABILITY_UNKNOWN),
    ):
        present_row = _row(installed, worker_id)
        present = present_row["reviewer_submit"]
        absent = _row(missing, worker_id)["reviewer_submit"]
        assert present["state"] == expected_state
        assert absent["state"] == expected_state
        assert present == absent
        assert present == provider_route_contracts.capability_record(
            present_row["route_family"],
            provider_route_contracts.CAPABILITY_REVIEWER_SUBMIT,
        ).as_dict()
