"""Regression for NF-2026-01374: statusless provider.no-route seals and opens the route circuit.

Process 58dfd05ff348470cac6fd41e2c97378b (opencode_cli, model
opencode-go/muse-spark-1.3-contributor) wrote exactly one stdout line

    {"type":"error","error":{"type":"provider.no-route",
     "message":"Model unavailable: opencode-go/muse-spark-1.3-contributor"}}

and empty stderr, then ended as a generic worker_failed.  The model-rejection
seal required a status in {400, 404}, so the route stayed ready and the card
died.  ``_provider_model_rejection_from_output`` must now seal statusless
``provider.no-route`` envelopes when (and only when) the provider's message
names the exact model the launch pinned.
"""

from __future__ import annotations

import json
from pathlib import Path

from aiworkhub import process_launcher

_MODEL = "opencode-go/muse-spark-1.3-contributor"


def _write_stdout(tmp_path: Path, envelope: object) -> Path:
    path = tmp_path / "stdout.log"
    path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
    return path


def test_measured_statusless_no_route_line_seals_the_typed_refusal(
    tmp_path: Path,
) -> None:
    path = _write_stdout(
        tmp_path,
        {
            "type": "error",
            "error": {
                "type": "provider.no-route",
                "message": f"Model unavailable: {_MODEL}",
            },
        },
    )

    failure = process_launcher._provider_model_rejection_from_output(path, _MODEL)

    assert failure is not None
    assert failure["schema_id"] == "aiworkhub.provider_launch_failure.v1"
    assert failure["reason"] == f"provider_route_model_unavailable:model={_MODEL}"
    assert failure["refusal_kind"] == "model_not_found"
    assert failure["recoverable"] is False
    assert int(failure["http_status"]) == 0
    assert failure["error_code"] == "model_not_available"
    sealed = failure["provider_error"]
    assert isinstance(sealed, dict)
    assert sealed["schema_id"] == "aiworkhub.provider_route_error.v1"
    assert sealed["owner"] == "provider"
    assert sealed["sealed"] is True
    assert sealed["code"] == "model_not_available"
    assert sealed["http_status"] == 0
    assert sealed["model"] == _MODEL
    assert sealed["detail"] == f"Model unavailable: {_MODEL}"


def test_statusless_no_route_opens_the_route_circuit_after_one_failure(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timedelta, timezone

    from aiworkhub import workforce_catalog

    path = _write_stdout(
        tmp_path,
        {
            "type": "error",
            "error": {
                "type": "provider.no-route",
                "message": f"Model unavailable: {_MODEL}",
            },
        },
    )

    failure = process_launcher._provider_model_rejection_from_output(path, _MODEL)
    assert failure is not None
    assert int(failure["http_status"]) == 0

    repo = tmp_path / "repo"
    (repo / ".aiworkhub/config").mkdir(parents=True)
    finished_at = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    row = {
        "adapter_id": "opencode_cli",
        "model": _MODEL,
        "state": "launch_failed",
        "error": failure["reason"],
        "provider_error": failure["provider_error"],
        "finished_at": finished_at,
    }

    circuit = workforce_catalog.route_circuit_for(
        repo, "opencode_cli", _MODEL, observations=[row]
    )
    assert circuit["state"] == "open"
    assert circuit["failure_kind"] == "model_not_found"
    assert circuit["threshold"] == 1


def test_a_non_integer_status_string_is_not_erased_into_a_seal(
    tmp_path: Path,
) -> None:
    path = _write_stdout(
        tmp_path,
        {
            "type": "error",
            "status": "503",
            "error": {
                "type": "provider.no-route",
                "message": f"Model unavailable: {_MODEL}",
            },
        },
    )

    assert process_launcher._provider_model_rejection_from_output(path, _MODEL) is None


def test_a_statusless_no_route_naming_a_suffixed_preview_model_is_not_sealed(
    tmp_path: Path,
) -> None:
    path = _write_stdout(
        tmp_path,
        {
            "type": "error",
            "error": {
                "type": "provider.no-route",
                "message": f"Model unavailable: {_MODEL}-preview",
            },
        },
    )

    assert process_launcher._provider_model_rejection_from_output(path, _MODEL) is None


def test_a_404_no_route_naming_the_model_also_seals(
    tmp_path: Path,
) -> None:
    path = _write_stdout(
        tmp_path,
        {
            "type": "error",
            "status": 404,
            "error": {
                "type": "provider.no-route",
                "message": f"Model unavailable: {_MODEL}",
            },
        },
    )

    failure = process_launcher._provider_model_rejection_from_output(path, _MODEL)
    assert failure is not None
    assert failure["error_code"] == "model_not_available"
    assert failure["http_status"] == 404


def test_statusless_no_route_naming_another_model_is_not_sealed(
    tmp_path: Path,
) -> None:
    path = _write_stdout(
        tmp_path,
        {
            "type": "error",
            "error": {
                "type": "provider.no-route",
                "message": "Model unavailable: opencode-go/other-model",
            },
        },
    )

    assert process_launcher._provider_model_rejection_from_output(path, _MODEL) is None


def test_assistant_prose_echo_of_no_route_cannot_mint_the_seal(
    tmp_path: Path,
) -> None:
    path = _write_stdout(
        tmp_path,
        {
            "type": "assistant",
            "message": {
                "type": "error",
                "error": {
                    "type": "provider.no-route",
                    "message": f"Model unavailable: {_MODEL}",
                },
            },
        },
    )

    assert process_launcher._provider_model_rejection_from_output(path, _MODEL) is None


def test_another_statusless_error_type_is_still_not_sealed(
    tmp_path: Path,
) -> None:
    path = _write_stdout(
        tmp_path,
        {
            "type": "error",
            "error": {
                "type": "overloaded_error",
                "message": f"{_MODEL} busy",
            },
        },
    )

    assert process_launcher._provider_model_rejection_from_output(path, _MODEL) is None


def test_a_400_invalid_request_with_a_model_not_found_code_keeps_its_code(
    tmp_path: Path,
) -> None:
    path = _write_stdout(
        tmp_path,
        {
            "type": "error",
            "status": 400,
            "error": {
                "type": "invalid_request_error",
                "code": "model_not_found",
                "message": "no such model",
            },
        },
    )

    failure = process_launcher._provider_model_rejection_from_output(path, _MODEL)
    assert failure is not None
    assert failure["error_code"] == "model_not_found"


def test_a_404_invalid_request_without_a_code_keeps_model_not_supported(
    tmp_path: Path,
) -> None:
    path = _write_stdout(
        tmp_path,
        {
            "type": "error",
            "status": 404,
            "error": {
                "type": "invalid_request_error",
                "message": f"model {_MODEL} not found",
            },
        },
    )

    failure = process_launcher._provider_model_rejection_from_output(path, _MODEL)
    assert failure is not None
    assert failure["error_code"] == "model_not_supported"


def test_an_explicit_null_status_never_hides_a_carried_error_status(
    tmp_path: Path,
) -> None:
    path = _write_stdout(
        tmp_path,
        {
            "type": "error",
            "status": None,
            "error_status": 503,
            "error": {
                "type": "provider.no-route",
                "message": f"Model unavailable: {_MODEL}",
            },
        },
    )

    assert process_launcher._provider_model_rejection_from_output(path, _MODEL) is None


def test_a_model_code_never_waives_the_pinned_model_anchor_for_no_route(
    tmp_path: Path,
) -> None:
    path = _write_stdout(
        tmp_path,
        {
            "type": "error",
            "error": {
                "type": "provider.no-route",
                "code": "model_not_found",
                "message": "Model unavailable: opencode-go/other-model",
            },
        },
    )

    assert process_launcher._provider_model_rejection_from_output(path, _MODEL) is None
