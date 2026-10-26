"""NF-2026-01601: an opencode HTTP 402 'Insufficient account funds' holds the whole provider."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import (
    process_launcher,
    task_store,
    terminal_failure_classification as tfc,
    workforce_catalog,
    workforce_router,
)

_ADAPTER = "opencode_cli"
_FAILED_MODEL = "opencode-go/gemini-4-argon-contributor"
_SIBLING_MODEL = "opencode-go/mimo-v2.6-pro"
_THIRD_MODEL = "opencode-go/kimi-k3"
_OTHER_PROVIDER_MODEL = "opencode/glm-4.5-free"
_MODELS = (_FAILED_MODEL, _SIBLING_MODEL, _THIRD_MODEL, _OTHER_PROVIDER_MODEL)
_NOW = 2_000_000_000.0
_MEASURED_LINE = (
    '{"type":"error","error":{"type":"provider.quota",'
    '"message":"Upstream request failed: Insufficient account funds","status":402}}'
)


def _compact(event: dict[str, Any]) -> str:
    return json.dumps(event, separators=(",", ":"))


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _streams(tmp_path: Path, stdout: str) -> tuple[Path, Path]:
    stdout_path = tmp_path / "stdout.log"
    stdout_path.write_text(stdout + "\n", encoding="utf-8")
    stderr_path = tmp_path / "stderr.log"
    stderr_path.write_text("", encoding="utf-8")
    return stdout_path, stderr_path


def _tail_disposition(tmp_path: Path, stdout: str) -> dict[str, Any]:
    stdout_path, stderr_path = _streams(tmp_path, stdout)
    return tfc.failure_disposition_from_paths(
        state="worker_failed",
        error=tfc.supervisor_failure_reason("exited", 1).render(),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


def _launcher_terminal(tmp_path: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    # Mirrors the launcher's worker_failed settlement: both detectors, then the disposition.
    stdout_path, stderr_path = _streams(tmp_path, _MEASURED_LINE)
    failure = process_launcher._provider_auth_failure_from_output(stdout_path)
    if failure is None:
        failure = process_launcher._provider_model_rejection_from_output(
            stdout_path, _FAILED_MODEL
        )
    state = "worker_failed"
    error = tfc.supervisor_failure_reason("exited", 1).render()
    if failure is not None:
        state = "launch_failed"
        error = str(failure["reason"])
    disposition = tfc.failure_disposition_from_paths(
        state=state,
        error=error,
        refusal_kind=str(failure.get("refusal_kind") or "") if failure else "",
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    return failure, disposition


def _sealed_402(model: str) -> dict[str, Any]:
    return {
        "schema_id": "aiworkhub.provider_route_error.v1",
        "owner": "provider",
        "sealed": True,
        "code": "insufficient_balance",
        "http_status": 402,
        "model": model,
        "detail": "Upstream request failed: Insufficient account funds",
    }


def _row(
    model: str,
    *,
    state: str,
    age: float,
    request_id: str,
    error: str = "",
    provider_error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "request_id": request_id,
        "task_id": f"task-{request_id}",
        "adapter_id": _ADAPTER,
        "model": model,
        "state": state,
        "error": error,
        "finished_at": _iso(_NOW - age),
    }
    if provider_error is not None:
        row["provider_error"] = provider_error
    return row


def _failed(
    model: str, provider_error: dict[str, Any] | None, *, age: float = 10.0
) -> dict[str, Any]:
    return _row(
        model,
        state="launch_failed",
        age=age,
        request_id=f"failed-{model.rsplit('/', 1)[-1]}",
        error="provider_balance_exhausted",
        provider_error=provider_error,
    )


def _accepted(model: str, *, age: float) -> dict[str, Any]:
    return _row(
        model, state="accepted", age=age, request_id=f"ok-{model.rsplit('/', 1)[-1]}"
    )


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")
    return root


def _catalog(root: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    preflight = {
        "providers": [
            {
                "adapter_id": _ADAPTER,
                "launchable": True,
                "status": "ready",
                "provider_observed_models": list(_MODELS),
                "observed_models": list(_MODELS),
            }
        ]
    }
    return workforce_catalog.build_catalog(
        root, cards=[], process_rows=rows, preflight=preflight, now_epoch=_NOW
    )


def _worker(snapshot: dict[str, Any], model: str) -> dict[str, Any]:
    return next(
        row
        for row in snapshot["workers"]
        if row["adapter_id"] == _ADAPTER and row["model"] == model
    )


def _rank(root: Path, snapshot: dict[str, Any], *models: str) -> dict[str, Any]:
    workers = [
        row
        for row in snapshot["workers"]
        if row["adapter_id"] == _ADAPTER and row["model"] in models
    ]
    task = workforce_router.TaskRequirements.build(
        task_id="nf-2026-01601", repo_id="repo", kinds=["mechanical"], risk="low"
    )
    return workforce_catalog.rank_task(
        root, task, catalog={**snapshot, "workers": workers}
    )


def test_fixture_is_the_exact_measured_stdout_line() -> None:
    event = json.loads(_MEASURED_LINE)
    assert event == {
        "type": "error",
        "error": {
            "type": "provider.quota",
            "message": "Upstream request failed: Insufficient account funds",
            "status": 402,
        },
    }
    assert _compact(event) == _MEASURED_LINE


def test_launcher_seals_the_measured_line_as_a_402_quota_provider_error(
    tmp_path: Path,
) -> None:
    failure, _ = _launcher_terminal(tmp_path)
    assert failure is not None
    assert int(failure["http_status"]) == 402
    assert failure["refusal_kind"] == "balance_exhausted"
    sealed = failure["provider_error"]
    assert sealed["owner"] == "provider"
    assert sealed["sealed"] is True
    assert sealed["http_status"] == 402
    assert sealed["code"] in workforce_catalog._ROUTE_QUOTA_ERROR_CODES


def test_provider_terminal_signal_reads_the_nested_402_of_the_error_envelope() -> None:
    signal = tfc.provider_terminal_signal(_MEASURED_LINE)
    assert signal["status"] == 402
    assert signal["envelope"] == "provider_error"


def test_measured_line_disposes_as_a_credential_balance_hold(tmp_path: Path) -> None:
    _, disposition = _launcher_terminal(tmp_path)
    assert disposition["failure_class"] == tfc.FAILURE_CLASS_CREDENTIAL
    assert disposition["cause"] == tfc.CAUSE_PROVIDER_CREDENTIAL
    assert disposition["cause_owner"] == tfc.CAUSE_OWNER_PROVIDER_ACCOUNT
    assert disposition["action"] == tfc.ACTION_CREDENTIAL_HOLD
    assert disposition["retry_scope"] == tfc.RETRY_SCOPE_HOLD_UNTIL_CREDENTIAL_CHANGE
    assert not tfc.distinct_route_retry_allowed(disposition=disposition, attempts_made=0)


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_a_nested_status_other_than_402_is_not_read(tmp_path: Path, status: int) -> None:
    event = json.loads(_MEASURED_LINE)
    event["error"]["status"] = status
    disposition = _tail_disposition(tmp_path, _compact(event))
    assert disposition["failure_class"] == tfc.FAILURE_CLASS_UNKNOWN


def test_model_text_echoing_the_line_cannot_forge_a_credential_hold(
    tmp_path: Path,
) -> None:
    echoed = _compact({"type": "text", "part": {"type": "text", "text": _MEASURED_LINE}})
    disposition = _tail_disposition(tmp_path, echoed)
    assert disposition["failure_class"] != tfc.FAILURE_CLASS_CREDENTIAL


def test_a_nested_402_outside_a_provider_owned_envelope_is_not_read(
    tmp_path: Path,
) -> None:
    forged = _compact(
        {"type": "assistant", "error": {"type": "provider.quota", "status": 402}}
    )
    genuine = _compact(
        {"type": "error", "error": {"type": "provider.crash", "message": "boom"}}
    )
    disposition = _tail_disposition(tmp_path, forged + "\n" + genuine)
    assert disposition["failure_class"] != tfc.FAILURE_CLASS_CREDENTIAL


def test_one_sealed_402_opens_the_failed_route_circuit(tmp_path: Path) -> None:
    circuit = workforce_catalog.route_circuit_for(
        tmp_path,
        _ADAPTER,
        _FAILED_MODEL,
        now_epoch=_NOW,
        observations=[_failed(_FAILED_MODEL, _sealed_402(_FAILED_MODEL))],
    )
    assert circuit["state"] == "open"
    assert circuit["failure_kind"] == "quota"
    assert circuit["threshold"] == 1
    assert circuit["consecutive_failures"] == 1


def test_catalog_marks_the_failed_route_unavailable_with_a_quota_reason(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    snapshot = _catalog(root, [_failed(_FAILED_MODEL, _sealed_402(_FAILED_MODEL))])
    failed = _worker(snapshot, _FAILED_MODEL)
    assert failed["available"] is False
    assert failed["readiness_status"] == "route_circuit_open"
    assert failed["route_health"]["state"] == "open"
    assert failed["route_health"]["failure_kind"] == "quota"
    assert "quota" in failed["availability_reason"]


def test_rank_excludes_the_unfunded_provider_instead_of_falling_to_its_sibling(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    clean = _rank(root, _catalog(root, []), _FAILED_MODEL, _SIBLING_MODEL)
    assert clean["selected_worker_id"]

    snapshot = _catalog(root, [_failed(_FAILED_MODEL, _sealed_402(_FAILED_MODEL))])
    decision = _rank(root, snapshot, _FAILED_MODEL, _SIBLING_MODEL)
    assert not decision["selected_worker_id"]
    assert len(decision["candidates"]) == 2
    for candidate in decision["candidates"]:
        assert candidate["excluded"] is True
        assert "worker_unavailable" in candidate["exclusion_reasons"]
        assert "quota" in candidate["availability_reason"]


def test_sibling_routes_of_the_same_provider_are_unavailable_after_one_402(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    snapshot = _catalog(root, [_failed(_FAILED_MODEL, _sealed_402(_FAILED_MODEL))])
    for model in (_SIBLING_MODEL, _THIRD_MODEL):
        sibling = _worker(snapshot, model)
        assert sibling["available"] is False
        assert sibling["readiness_status"] == "route_circuit_open"
        assert sibling["route_health"]["state"] == "open"
        assert sibling["route_health"]["failure_kind"] == "quota"
        assert sibling["route_health"]["provider_prefix"] == "opencode-go"
        assert "quota" in sibling["availability_reason"]


def test_a_402_never_holds_another_provider(tmp_path: Path) -> None:
    root = _root(tmp_path)
    snapshot = _catalog(root, [_failed(_FAILED_MODEL, _sealed_402(_FAILED_MODEL))])
    other = _worker(snapshot, _OTHER_PROVIDER_MODEL)
    assert other["route_health"]["state"] == "closed"
    assert other["available"] is True


@pytest.mark.parametrize("success_model", [_SIBLING_MODEL, _FAILED_MODEL, _THIRD_MODEL])
def test_a_later_round_trip_on_any_route_of_the_provider_frees_the_sibling(
    tmp_path: Path, success_model: str
) -> None:
    root = _root(tmp_path)
    failure = _failed(_FAILED_MODEL, _sealed_402(_FAILED_MODEL), age=60.0)
    assert _worker(_catalog(root, [failure]), _SIBLING_MODEL)["available"] is False

    freed = _catalog(root, [failure, _accepted(success_model, age=30.0)])
    sibling = _worker(freed, _SIBLING_MODEL)
    assert sibling["available"] is True
    assert sibling["route_health"]["state"] == "closed"


def test_a_round_trip_older_than_the_402_does_not_free_the_provider(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    rows = [
        _accepted(_SIBLING_MODEL, age=120.0),
        _failed(_FAILED_MODEL, _sealed_402(_FAILED_MODEL), age=60.0),
    ]
    assert _worker(_catalog(root, rows), _SIBLING_MODEL)["available"] is False


def test_the_provider_hold_ends_with_the_route_cooldown(tmp_path: Path) -> None:
    root = _root(tmp_path)
    cooldown = workforce_catalog.ROUTE_CIRCUIT_COOLDOWN_SECONDS
    sealed = _sealed_402(_FAILED_MODEL)

    inside = _catalog(root, [_failed(_FAILED_MODEL, sealed, age=cooldown - 5.0)])
    assert _worker(inside, _SIBLING_MODEL)["available"] is False

    past = _catalog(root, [_failed(_FAILED_MODEL, sealed, age=cooldown + 5.0)])
    for model in (_FAILED_MODEL, _SIBLING_MODEL):
        row = _worker(past, model)
        assert row["route_health"]["state"] != "open"
        assert row["available"] is True


@pytest.mark.parametrize(
    ("code", "http_status", "kind"),
    [("invalid_api_key", 401, "auth"), ("model_not_available", 0, "model_not_found")],
)
def test_auth_and_model_failures_stay_exact_route(
    tmp_path: Path, code: str, http_status: int, kind: str
) -> None:
    root = _root(tmp_path)
    sealed = {**_sealed_402(_FAILED_MODEL), "code": code, "http_status": http_status}
    snapshot = _catalog(root, [_failed(_FAILED_MODEL, sealed)])
    failed = _worker(snapshot, _FAILED_MODEL)
    assert failed["route_health"]["state"] == "open"
    assert failed["route_health"]["failure_kind"] == kind
    sibling = _worker(snapshot, _SIBLING_MODEL)
    assert sibling["route_health"]["state"] == "closed"
    assert sibling["available"] is True


@pytest.mark.parametrize(
    ("state", "provider_error"),
    [
        ("worker_failed", None),
        ("worker_failed", {**_sealed_402(_FAILED_MODEL), "owner": "worker"}),
        ("worker_failed", {**_sealed_402(_FAILED_MODEL), "sealed": False}),
        ("launch_failed", None),
    ],
)
def test_only_a_sealed_provider_owned_402_holds_the_provider(
    tmp_path: Path, state: str, provider_error: dict[str, Any] | None
) -> None:
    root = _root(tmp_path)
    forged = _row(
        _FAILED_MODEL,
        state=state,
        age=10.0,
        request_id="forged",
        error="provider.quota 402 Insufficient account funds",
        provider_error=provider_error,
    )
    snapshot = _catalog(root, [forged])
    for model in (_FAILED_MODEL, _SIBLING_MODEL):
        row = _worker(snapshot, model)
        assert row["route_health"]["state"] != "open"
        assert row["available"] is True


def test_a_per_route_quota_code_without_a_402_stays_exact_route(tmp_path: Path) -> None:
    assert "quota_exhausted" in workforce_catalog._ROUTE_QUOTA_ERROR_CODES
    root = _root(tmp_path)
    sealed = {**_sealed_402(_FAILED_MODEL), "code": "quota_exhausted", "http_status": 429}
    snapshot = _catalog(root, [_failed(_FAILED_MODEL, sealed)])
    failed = _worker(snapshot, _FAILED_MODEL)
    assert failed["route_health"]["state"] == "open"
    assert failed["route_health"]["failure_kind"] == "quota"
    sibling = _worker(snapshot, _SIBLING_MODEL)
    assert sibling["route_health"]["state"] == "closed"
    assert sibling["available"] is True


def test_launch_gate_refuses_a_sibling_route_after_one_sealed_402(tmp_path: Path) -> None:
    rows = [_failed(_FAILED_MODEL, _sealed_402(_FAILED_MODEL))]
    circuit = workforce_catalog.route_circuit_for(
        tmp_path, _ADAPTER, _SIBLING_MODEL, now_epoch=_NOW, observations=rows
    )
    assert circuit["state"] == "open"
    assert circuit["failure_kind"] == "quota"
    assert circuit["scope"] == "adapter_and_provider_prefix"
    assert circuit["provider_prefix"] == "opencode-go"
    assert circuit["adapter_id"] == _ADAPTER
    assert circuit["model"] == _SIBLING_MODEL


def test_launch_gate_frees_the_sibling_after_a_later_accepted_round_trip(
    tmp_path: Path,
) -> None:
    rows = [
        _failed(_FAILED_MODEL, _sealed_402(_FAILED_MODEL), age=60.0),
        _accepted(_THIRD_MODEL, age=30.0),
    ]
    circuit = workforce_catalog.route_circuit_for(
        tmp_path, _ADAPTER, _SIBLING_MODEL, now_epoch=_NOW, observations=rows
    )
    assert circuit["state"] != "open"


def test_launch_gate_does_not_hold_another_provider(tmp_path: Path) -> None:
    rows = [_failed(_FAILED_MODEL, _sealed_402(_FAILED_MODEL))]
    circuit = workforce_catalog.route_circuit_for(
        tmp_path, _ADAPTER, _OTHER_PROVIDER_MODEL, now_epoch=_NOW, observations=rows
    )
    assert circuit.get("scope") != "adapter_and_provider_prefix"


def _event_store(path: Path, events: list[tuple[str, str, str, dict[str, Any]]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE task_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT, runner TEXT, event TEXT, payload_json TEXT, created_at TEXT)"
    )
    for event, model, created_at, evidence in events:
        payload = {"evidence": {"adapter_id": _ADAPTER, "model": model, **evidence}}
        conn.execute(
            "INSERT INTO task_events (task_id, runner, event, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            ("t", "r", event, json.dumps(payload), created_at),
        )
    conn.commit()
    conn.close()


def test_route_terminal_observations_provider_prefix_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "events.sqlite"
    since = _iso(_NOW - 1000.0)
    inside = _iso(_NOW - 10.0)
    _event_store(
        db_path,
        [
            ("launch_failed", _SIBLING_MODEL, _iso(_NOW - 5000.0), {}),
            ("launch_failed", _FAILED_MODEL, inside, {"provider_error": _sealed_402(_FAILED_MODEL)}),
            ("terminal_review", _THIRD_MODEL, inside, {}),
            ("terminal_review", _OTHER_PROVIDER_MODEL, inside, {}),
        ],
    )

    def _connect(_path: Path, **_kw: Any) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(task_store, "_require_ready", lambda _root: (None, db_path))
    monkeypatch.setattr(task_store, "_connect", _connect)

    exact = task_store.route_terminal_observations(
        tmp_path, adapter_id=_ADAPTER, model=_SIBLING_MODEL, since_iso=since
    )
    assert exact == []

    provider = task_store.route_terminal_observations(
        tmp_path, adapter_id=_ADAPTER, model="", since_iso=since, provider_prefix="OpenCode-Go"
    )
    assert sorted(row["model"] for row in provider) == sorted([_FAILED_MODEL, _THIRD_MODEL])
    sealed = next(row for row in provider if row["model"] == _FAILED_MODEL)
    assert sealed["provider_error"]["http_status"] == 402


def test_route_circuit_for_reads_provider_wide_evidence_from_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def _fake(_root: Path, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs)
        if kwargs.get("provider_prefix"):
            return [_failed(_FAILED_MODEL, _sealed_402(_FAILED_MODEL))]
        return []

    monkeypatch.setattr(task_store, "route_terminal_observations", _fake)
    circuit = workforce_catalog.route_circuit_for(
        tmp_path, _ADAPTER, _SIBLING_MODEL, now_epoch=_NOW
    )
    assert circuit["state"] == "open"
    assert circuit["scope"] == "adapter_and_provider_prefix"
    assert [bool(call.get("provider_prefix")) for call in calls] == [False, True]
    assert calls[1]["provider_prefix"] == "opencode-go"
