"""NF-2026-02085: ANY adapter's HTTP 402 / balance-exhausted terminal output.

NF-2026-01601 sealed only opencode's ``provider.quota`` envelope. A reviewer
routed to deepseek_v4-pro answered ``[402] You have run out of balance`` and the
attempt was classed ``unknown`` with the provider left in rotation. These tests
pin that the launcher seals the line, the classifier places it, and the same
workforce circuit as NF-01601 opens for that adapter and provider prefix.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import (
    process_launcher_provider_failures as plf,
    terminal_failure_classification as tfc,
    workforce_catalog,
)

_NOW = 1_800_000_000.0
_ADAPTER = "kilo_cli"
_MODEL = "deepseek/deepseek-v4-pro"
_SIBLING = "deepseek/deepseek-v4-flash"

_DEEPSEEK_LINE = {
    "type": "error",
    "error": {
        "name": "APIError",
        "data": {
            "message": "[402] You have run out of balance",
            "statusCode": 402,
            "isRetryable": False,
        },
    },
}

# DeepSeek's own 402 body carries the unplaced ``invalid_request_error`` code.
_DEEPSEEK_BODY_LINE = {
    "type": "error",
    "error": {
        "name": "APIError",
        "data": {
            "message": "Insufficient Balance",
            "statusCode": 402,
            "responseBody": json.dumps(
                {
                    "error": {
                        "message": "Insufficient Balance",
                        "type": "unknown_error",
                        "param": None,
                        "code": "invalid_request_error",
                    }
                }
            ),
        },
    },
}

_BALANCE_ENVELOPES = {
    "deepseek_status_and_phrase": _DEEPSEEK_LINE,
    "deepseek_response_body": _DEEPSEEK_BODY_LINE,
    "phrase_only": {
        "type": "error",
        "error": {"name": "UnknownError", "data": {"message": "You have run out of balance"}},
    },
    "insufficient_credit_turn_failed": {
        "type": "turn.failed",
        "error": {"message": "Insufficient credit on this account"},
    },
    "payment_required_nested_json": {
        "type": "error",
        "message": json.dumps({"error": {"message": "Payment Required"}, "status": 402}),
    },
    "failed_result_402": {
        "type": "result",
        "is_error": True,
        "subtype": "error",
        "error_status": 402,
        "result": "request failed",
    },
}


def _write(tmp_path: Path, stdout: str = "", stderr: str = "") -> tuple[Path, Path]:
    out = tmp_path / "stdout.log"
    err = tmp_path / "stderr.log"
    out.write_text(stdout, encoding="utf-8")
    err.write_text(stderr, encoding="utf-8")
    return out, err


def _jsonl(*events: dict[str, Any]) -> str:
    return "\n".join(json.dumps(event, separators=(",", ":")) for event in events) + "\n"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


@pytest.mark.parametrize("name", sorted(_BALANCE_ENVELOPES))
def test_launcher_seals_any_adapters_balance_envelope(tmp_path: Path, name: str) -> None:
    stdout, _ = _write(tmp_path, _jsonl(_BALANCE_ENVELOPES[name]))
    failure = plf._provider_auth_failure_from_output(stdout)
    assert failure is not None
    assert failure["reason"] == "provider_refused_balance_exhausted"
    assert failure["refusal_kind"] == "balance_exhausted"
    assert failure["http_status"] == 402
    sealed = failure["provider_error"]
    assert sealed["owner"] == "provider"
    assert sealed["sealed"] is True
    assert sealed["http_status"] == 402
    assert sealed["code"] in workforce_catalog._ROUTE_QUOTA_ERROR_CODES


def test_deepseek_402_line_is_classified_not_unknown(tmp_path: Path) -> None:
    stdout, stderr = _write(tmp_path, _jsonl(_DEEPSEEK_LINE))
    disposition = tfc.failure_disposition_from_paths(
        state="worker_failed", error="exited", stdout_path=stdout, stderr_path=stderr,
    )
    assert disposition["failure_class"] == tfc.FAILURE_CLASS_CREDENTIAL
    assert disposition["provider_code"] == "insufficient_balance"
    assert disposition["cause"] == tfc.CAUSE_PROVIDER_CREDENTIAL
    assert disposition["action"] == tfc.ACTION_CREDENTIAL_HOLD
    assert disposition["evidence_authority"] == tfc.EVIDENCE_AUTHORITY_PROVIDER_ENVELOPE


@pytest.mark.parametrize("name", sorted(_BALANCE_ENVELOPES))
def test_every_balance_envelope_places_the_card(tmp_path: Path, name: str) -> None:
    stdout, stderr = _write(tmp_path, _jsonl(_BALANCE_ENVELOPES[name]))
    signal = tfc.provider_terminal_signal(stdout.read_text(encoding="utf-8"))
    if name == "failed_result_402":
        # A failed ``result`` without ``api_error`` names no terminal envelope; the
        # launcher seal above is what places it.
        assert signal["envelope"] == ""
        return
    assert signal["code"] == "insufficient_balance"
    disposition = tfc.failure_disposition_from_paths(
        state="worker_failed", error="exited", stdout_path=stdout, stderr_path=stderr,
    )
    assert disposition["failure_class"] == tfc.FAILURE_CLASS_CREDENTIAL


def test_launcher_terminal_mirror_classes_refusal_with_credential_cause(tmp_path: Path) -> None:
    stdout, stderr = _write(tmp_path, _jsonl(_DEEPSEEK_LINE))
    failure = plf._provider_auth_failure_from_output(stdout)
    assert failure is not None
    disposition = tfc.failure_disposition_from_paths(
        state="launch_failed",
        error=str(failure["reason"]),
        refusal_kind=str(failure["refusal_kind"]),
        stdout_path=stdout,
        stderr_path=stderr,
    )
    assert disposition["failure_class"] == tfc.FAILURE_CLASS_CREDENTIAL
    assert disposition["evidence"] == "refusal_kind=balance_exhausted"
    assert tfc.disposition_for_reason(failure["reason"]) == tfc.FAILURE_CLASS_CREDENTIAL


def test_plain_text_balance_line_on_stderr_is_placed(tmp_path: Path) -> None:
    stdout, stderr = _write(
        tmp_path, stderr="2026-11-05 ERROR provider: [402] You have run out of balance\n"
    )
    disposition = tfc.failure_disposition_from_paths(
        state="worker_failed", error="exited", stdout_path=stdout, stderr_path=stderr,
    )
    assert disposition["failure_class"] == tfc.FAILURE_CLASS_CREDENTIAL
    assert disposition["provider_code"] == "insufficient_balance"


def test_model_prose_cannot_forge_a_balance_seal(tmp_path: Path) -> None:
    prose = "[402] You have run out of balance"
    stdout, stderr = _write(
        tmp_path,
        _jsonl(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": prose}]}},
            {"type": "text", "part": {"type": "text", "text": prose}},
            {"type": "assistant", "message": prose},
        )
        + prose
        + "\n",
    )
    assert plf._provider_auth_failure_from_output(stdout) is None
    disposition = tfc.failure_disposition_from_paths(
        state="worker_failed", error="exited", stdout_path=stdout, stderr_path=stderr,
    )
    assert disposition["failure_class"] == tfc.FAILURE_CLASS_UNKNOWN
    assert disposition["provider_code"] == ""


def test_unrelated_provider_error_is_not_a_balance_refusal(tmp_path: Path) -> None:
    stdout, _ = _write(
        tmp_path,
        _jsonl({"type": "error", "error": {"name": "APIError", "data": {"message": "bad request", "statusCode": 400}}}),
    )
    assert plf._provider_auth_failure_from_output(stdout) is None


def _failed_row(adapter: str, model: str, sealed: dict[str, Any], *, age: float = 10.0) -> dict[str, Any]:
    return {
        "request_id": f"failed-{adapter}-{model.rsplit('/', 1)[-1]}",
        "task_id": "task-nf02085",
        "adapter_id": adapter,
        "model": model,
        "state": "launch_failed",
        "error": "provider_refused_balance_exhausted",
        "finished_at": _iso(_NOW - age),
        "provider_error": {**sealed, "model": model},
    }


def _sealed_from_launcher(tmp_path: Path) -> dict[str, Any]:
    stdout, _ = _write(tmp_path, _jsonl(_DEEPSEEK_LINE))
    failure = plf._provider_auth_failure_from_output(stdout)
    assert failure is not None
    return dict(failure["provider_error"])


def test_workforce_circuit_opens_for_adapter_and_provider_prefix(tmp_path: Path) -> None:
    row = _failed_row(_ADAPTER, _MODEL, _sealed_from_launcher(tmp_path))
    assert workforce_catalog._route_failure_kind(row) == "quota"
    assert workforce_catalog._is_account_balance_seal(row) is True

    exact = workforce_catalog._route_circuit([row], now_epoch=_NOW)
    assert exact["state"] == "open"
    assert exact["failure_kind"] == "quota"

    # A sibling model on the same exhausted account is held by the provider-prefix circuit.
    sibling = workforce_catalog._provider_quota_circuit(
        [row], adapter_ids={_ADAPTER}, model=_SIBLING, now_epoch=_NOW,
    )
    assert sibling is not None
    assert sibling["state"] == "open"
    assert sibling["scope"] == "adapter_and_provider_prefix"
    assert sibling["provider_prefix"] == "deepseek"


def test_workforce_circuit_is_scoped_to_the_failing_adapter_and_prefix(tmp_path: Path) -> None:
    row = _failed_row(_ADAPTER, _MODEL, _sealed_from_launcher(tmp_path))
    other_adapter = workforce_catalog._provider_quota_circuit(
        [row], adapter_ids={"codex_cli"}, model=_SIBLING, now_epoch=_NOW,
    )
    other_prefix = workforce_catalog._provider_quota_circuit(
        [row], adapter_ids={_ADAPTER}, model="glm/glm-5.2", now_epoch=_NOW,
    )
    assert other_adapter is None
    assert other_prefix is None


def test_circuit_closes_after_a_later_success(tmp_path: Path) -> None:
    failed = _failed_row(_ADAPTER, _MODEL, _sealed_from_launcher(tmp_path), age=60.0)
    healthy = {
        "request_id": "ok-1",
        "task_id": "task-ok",
        "adapter_id": _ADAPTER,
        "model": _MODEL,
        "state": "accepted",
        "error": "",
        "finished_at": _iso(_NOW - 5.0),
    }
    circuit = workforce_catalog._provider_quota_circuit(
        [failed, healthy], adapter_ids={_ADAPTER}, model=_SIBLING, now_epoch=_NOW,
    )
    assert circuit is None


def _failed_result(text: str) -> dict[str, Any]:
    return {"type": "result", "is_error": True, "result": text}


def test_failed_result_prose_is_not_balance() -> None:
    event = _failed_result("I ran out of credits, stopping")
    assert tfc.provider_event_names_balance_exhaustion(event) is False


def test_failed_result_cli_api_error_prefix_is_balance() -> None:
    event = _failed_result("API Error: 402 You have run out of balance")
    assert tfc.provider_event_names_balance_exhaustion(event) is True


def test_stderr_bare_balance_phrase_is_not_classified(tmp_path: Path) -> None:
    stdout, stderr = _write(tmp_path, stderr="insufficient funds in wallet\n")
    disposition = tfc.failure_disposition_from_paths(
        state="worker_failed", error="exited", stdout_path=stdout, stderr_path=stderr,
    )
    assert disposition["failure_class"] == tfc.FAILURE_CLASS_UNKNOWN
    assert disposition["provider_code"] == ""


def test_stderr_402_marker_line_is_classified(tmp_path: Path) -> None:
    stdout, stderr = _write(tmp_path, stderr="[402] You have run out of balance\n")
    disposition = tfc.failure_disposition_from_paths(
        state="worker_failed", error="exited", stdout_path=stdout, stderr_path=stderr,
    )
    assert disposition["failure_class"] == tfc.FAILURE_CLASS_CREDENTIAL
    assert disposition["provider_code"] == "insufficient_balance"
