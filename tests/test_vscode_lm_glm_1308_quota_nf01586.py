"""GLM code 1308 usage-limit refusal through the vscode_lm bridge (NF-2026-01586).

The extension host answered with the provider's own ``Rate limit exceeded``
body carrying code ``1308`` and a naive reset stamp.  It used to land as
``worker_failed``/``unknown`` with ``no_provider_terminal_envelope``: no refusal
kind, no ``reset_at`` and no sealed error for the route circuit.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from aiworkhub import (
    process_launcher_provider_failures as launcher_failures,
    runtime_adapters,
    terminal_failure_classification,
    vscode_lm_worker,
    workforce_catalog,
)

HOST_ERROR = (
    'Rate limit exceeded {"code":"1308","message":"Usage limit reached for 5 '
    'hour. Your limit will reset at 2026-10-17 06:35:11"}'
)
BRIDGE_REASON = f"vscode_lm_request_failed:{HOST_ERROR}"
# 06:35:11 on a zone-less clock, read as UTC-12:00 (the latest instant it can denote).
RESET_AT = "2026-10-17T18:35:11+00:00"


def _epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp).astimezone(timezone.utc).timestamp()


def test_exact_bridge_reason_is_a_recoverable_quota_refusal_with_reset() -> None:
    outcome = runtime_adapters.classify_provider_outcome(
        exit_code=1, message=BRIDGE_REASON
    )
    assert outcome["outcome"] == runtime_adapters.OUTCOME_PROVIDER_REFUSED
    assert outcome["refusal_kind"] == runtime_adapters.REFUSAL_QUOTA_EXHAUSTED
    assert outcome["recoverable"] is True
    assert outcome["reset_reported"] is True
    assert outcome["reset_at"] == RESET_AT


def test_unparseable_reset_leaves_reset_at_unset() -> None:
    message = BRIDGE_REASON.replace("2026-10-17 06:35:11", "2026-13-45 99:99:99")
    outcome = runtime_adapters.classify_provider_outcome(exit_code=1, message=message)
    assert outcome["refusal_kind"] == runtime_adapters.REFUSAL_QUOTA_EXHAUSTED
    assert outcome["reset_at"] is None
    assert outcome["reset_reported"] is False


def test_free_prose_without_wrapper_or_provider_code_selects_no_refusal() -> None:
    prose = (
        "I could not finish; the earlier error said the timeout was reached "
        "and that the code 1308 appears in the changelog."
    )
    outcome = runtime_adapters.classify_provider_outcome(exit_code=1, message=prose)
    assert outcome["refusal_kind"] == ""
    assert outcome["outcome"] != runtime_adapters.OUTCOME_PROVIDER_REFUSED


def test_worker_seals_only_the_host_body_that_names_code_1308() -> None:
    sealed = vscode_lm_worker.provider_quota_error(HOST_ERROR)
    assert sealed is not None
    assert sealed["owner"] == "provider"
    assert sealed["sealed"] is True
    assert sealed["source"] == vscode_lm_worker.PROVIDER_ERROR_SOURCE
    assert sealed["code"] == "quota_exhausted"
    assert sealed["provider_code"] == "1308"
    assert sealed["http_status"] == 429
    assert sealed["reset_at"] == RESET_AT
    assert sealed["reset_timezone_assumed"] == "UTC-12:00"

    assert vscode_lm_worker.provider_quota_error("Rate limit exceeded, try later") is None
    assert vscode_lm_worker.provider_quota_error(
        HOST_ERROR.replace('"1308"', '"1234"')
    ) is None
    assert vscode_lm_worker.provider_quota_error(None) is None


def _stdout(tmp_path: Path, event: dict) -> Path:
    path = tmp_path / "worker.stdout.jsonl"
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    return path


def test_launcher_seals_the_wrapper_only_with_a_sealed_provider_error(
    tmp_path: Path,
) -> None:
    sealed = vscode_lm_worker.provider_quota_error(HOST_ERROR)
    envelope = {
        "type": "result",
        "subtype": "error",
        "is_error": True,
        "error": BRIDGE_REASON,
    }
    failure = launcher_failures._vscode_lm_balance_failure_from_output(
        _stdout(tmp_path, {**envelope, "provider_error": sealed})
    )
    assert failure is not None
    assert failure["refusal_kind"] == runtime_adapters.REFUSAL_QUOTA_EXHAUSTED
    assert failure["recoverable"] is True
    assert failure["provider_error"]["reset_at"] == RESET_AT
    assert failure["provider_error"]["code"] == "quota_exhausted"

    assert launcher_failures._vscode_lm_balance_failure_from_output(
        _stdout(tmp_path, envelope)
    ) is None


def test_terminal_disposition_is_no_longer_cause_not_established() -> None:
    disposition = terminal_failure_classification.failure_disposition_from_paths(
        state="worker_failed",
        error=BRIDGE_REASON,
        refusal_kind=runtime_adapters.REFUSAL_QUOTA_EXHAUSTED,
    )
    assert disposition["failure_class"] != terminal_failure_classification.FAILURE_CLASS_UNKNOWN
    assert disposition["evidence"] != "no_provider_terminal_envelope"
    assert disposition["cause"] != terminal_failure_classification.CAUSE_NOT_ESTABLISHED


def test_sealed_refusal_opens_the_route_circuit_until_reset_at(tmp_path: Path) -> None:
    sealed = vscode_lm_worker.provider_quota_error(HOST_ERROR)
    failed_at = "2026-10-17T10:00:00+00:00"
    observation = {
        "adapter_id": "vscode_lm",
        "model": "glm-5.1",
        "state": "worker_failed",
        "error": BRIDGE_REASON,
        "finished_at": failed_at,
        "provider_error": sealed,
    }

    def circuit(row: dict, *, hours_later: float) -> dict:
        return workforce_catalog.route_circuit_for(
            tmp_path,
            "vscode_lm",
            "glm-5.1",
            now_epoch=_epoch(failed_at) + hours_later * 3600,
            observations=[row],
        )

    # Two hours on is far past the ordinary cooldown, yet the reported reset
    # (18:35 UTC) has not arrived, so the route stays closed to new launches.
    held = circuit(observation, hours_later=2)
    assert held["state"] == "open"
    assert held["failure_kind"] == "quota"
    assert held["reset_at"] == RESET_AT

    unreset = {k: v for k, v in observation.items() if k != "provider_error"}
    assert circuit(unreset, hours_later=2)["state"] != "open"

    assert circuit(observation, hours_later=9)["state"] != "open"
