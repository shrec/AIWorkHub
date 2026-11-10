"""NF-2026-02143: an expired editor token takes the route out of ranking.

Measured on three real failures: the worker's last stdout line was a result
event whose error was ``vscode_lm_request_failed:token expired or incorrect``
with no ``provider_error``, and the launcher read sealed refusals only for
``adapter_id == 'vscode_lm'`` while the failing rows were ``glm_vscode_lm``.
Nothing sealed reached the event log, so the route stayed in ranking.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import (
    process_launcher,
    process_launcher_provider_failures as launcher_failures,
    review_orchestrator,
    runtime_adapters,
    vscode_lm_worker,
    workforce_catalog,
)
from aiworkhub import terminal_failure_classification as tfc

HOST_ERROR = "token expired or incorrect"
WRAPPED_ERROR = f"vscode_lm_request_failed:{HOST_ERROR}"
REASON = "provider_refused_credential_rejected_needs_new_credential"
LAUNCH_FAILURE_SCHEMA = "aiworkhub.provider_launch_failure.v1"
EDITOR_HOSTED_ADAPTERS = ("vscode_lm", "glm_vscode_lm", "deepseek_vscode_lm")
QUOTA_HOST_ERROR = (
    'Rate limit exceeded {"code":"1308","message":"Usage limit reached for 5 '
    'hour. Your limit will reset at 2026-10-17 06:35:11"}'
)
FAILED_AT = "2026-11-09T10:00:00+00:00"
SUCCEEDED_AT = "2026-11-09T10:02:00+00:00"


def _epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp).astimezone(timezone.utc).timestamp()


def _sealed() -> dict[str, Any]:
    sealed = vscode_lm_worker.provider_credential_error(HOST_ERROR)
    assert sealed is not None
    return sealed


def _envelope(**overrides: Any) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "error",
        "is_error": True,
        "error": WRAPPED_ERROR,
        "provider_error": _sealed(),
        **overrides,
    }


def _stdout(tmp_path: Path, event: dict[str, Any]) -> Path:
    path = tmp_path / "worker.stdout.jsonl"
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    return path


def _worker_events(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    error: str,
    text: str = "",
    diagnostics: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Run the real worker over a host response; return its stdout and last event."""
    workspace = tmp_path / "worktree"
    home = tmp_path / "home"
    workspace.mkdir(parents=True)
    home.mkdir(parents=True)
    request_id = "a" * 32
    response_path = home / "response.json"
    spec_path = home / "spec.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema_id": "aiworkhub.vscode_lm.worker_spec.v1",
                "request_id": request_id,
                "workspace_path": str(workspace),
                "response_path": str(response_path),
                "allowed_writes": ["out/*.txt"],
                "create_paths": [],
                "timeout_seconds": 30,
            }
        ),
        encoding="utf-8",
    )
    response: dict[str, Any] = {
        "schema_id": vscode_lm_worker.RESPONSE_SCHEMA_ID,
        "request_id": request_id,
        "error": error,
        "text": text,
    }
    if diagnostics is not None:
        response["diagnostics"] = diagnostics
    response_path.write_text(json.dumps(response), encoding="utf-8")

    capsys.readouterr()
    assert vscode_lm_worker.main(["--spec", str(spec_path)]) == 1
    output = capsys.readouterr().out
    stdout = tmp_path / "worker.stdout.jsonl"
    stdout.write_text(output, encoding="utf-8")
    return stdout, json.loads(output.strip().splitlines()[-1])


# --- the reproduction: one failure leaves ranking on every editor adapter ----


def test_sealed_credential_refusal_opens_the_route_circuit_on_every_editor_hosted_adapter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    stdout, terminal = _worker_events(tmp_path, capsys, error=HOST_ERROR)
    assert terminal["type"] == "result"
    assert terminal["is_error"] is True
    assert terminal["error"] == WRAPPED_ERROR
    assert terminal["provider_error"] == _sealed()

    for adapter_id in EDITOR_HOSTED_ADAPTERS:
        failure = launcher_failures._editor_hosted_route_failure_from_output(
            adapter_id, stdout
        )
        assert failure is not None, adapter_id
        assert failure["reason"] == REASON
    assert (
        launcher_failures._editor_hosted_route_failure_from_output("claude_cli", stdout)
        is None
    )

    failure = launcher_failures._editor_hosted_route_failure_from_output(
        "glm_vscode_lm", stdout
    )
    assert failure is not None
    row = {
        "adapter_id": "glm_vscode_lm",
        "model": "glm-5.3",
        "state": "worker_failed",
        "error": failure["reason"],
        "finished_at": FAILED_AT,
        "provider_error": failure["provider_error"],
    }

    def circuit(*rows: dict[str, Any]) -> dict[str, Any]:
        return workforce_catalog.route_circuit_for(
            tmp_path,
            "glm_vscode_lm",
            "glm-5.3",
            now_epoch=_epoch(FAILED_AT) + 240,
            observations=list(rows),
        )

    opened = circuit(row)
    assert opened["state"] == "open"
    assert opened["failure_kind"] == "auth"

    unsealed = {key: value for key, value in row.items() if key != "provider_error"}
    assert circuit(unsealed)["state"] != "open"

    success = {
        "adapter_id": "glm_vscode_lm",
        "model": "glm-5.3",
        "state": "accepted",
        "error": "",
        "finished_at": SUCCEEDED_AT,
    }
    assert circuit(row, success)["state"] not in {"open", "half_open"}


@pytest.mark.parametrize("adapter_id", ["", "no_such_adapter", "codex_cli"])
def test_route_family_gate_reads_nothing_for_other_adapters(
    tmp_path: Path, adapter_id: str,
) -> None:
    stdout = _stdout(tmp_path, _envelope())

    assert (
        launcher_failures._editor_hosted_route_failure_from_output(adapter_id, stdout)
        is None
    )


def test_launcher_reads_the_route_family_helper_once_and_keeps_the_old_name() -> None:
    assert (
        process_launcher._vscode_lm_balance_failure_from_output
        is launcher_failures._vscode_lm_balance_failure_from_output
    )
    source = Path(process_launcher.__file__).read_text(encoding="utf-8")
    assert source.count("_editor_hosted_route_failure_from_output(") == 1
    assert "_vscode_lm_balance_failure_from_output(" not in source


# --- the worker's seal: the host-owned field, anchored at both ends ----------


@pytest.mark.parametrize(
    "response_error",
    [HOST_ERROR, f"  {HOST_ERROR}\n", "Token Expired Or Incorrect"],
)
def test_worker_seals_the_whole_host_sentence(response_error: str) -> None:
    text = response_error.strip()

    assert vscode_lm_worker.provider_credential_error(response_error) == {
        "schema_id": vscode_lm_worker.PROVIDER_ROUTE_ERROR_SCHEMA_ID,
        "owner": "provider",
        "sealed": True,
        "source": vscode_lm_worker.PROVIDER_ERROR_SOURCE,
        "code": "authentication_failed",
        "detail": text[:300],
    }


@pytest.mark.parametrize(
    "response_error",
    [
        None,
        401,
        ["token expired or incorrect"],
        "",
        "token expired",
        "... please token expired or incorrect",
        "token expired or incorrect, please sign in again",
        "vscode_lm_request_failed:token expired or incorrect",
        "token expired or incorrect\ntoken expired or incorrect",
    ],
)
def test_worker_refuses_anything_but_the_whole_sentence(response_error: Any) -> None:
    assert vscode_lm_worker.provider_credential_error(response_error) is None


@pytest.mark.parametrize(
    ("error", "text", "diagnostics"),
    [
        pytest.param(
            f"... please {HOST_ERROR}", "", None, id="prose-around-the-sentence",
        ),
        pytest.param("host transport failed", HOST_ERROR, None, id="model-text"),
        pytest.param(
            "host transport failed",
            "",
            {"protocol_preview": HOST_ERROR, "turn_trace": [HOST_ERROR]},
            id="diagnostics",
        ),
        pytest.param("", HOST_ERROR, None, id="no-host-error-at-all"),
    ],
)
def test_worker_inspects_only_the_host_owned_error_field(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    error: str,
    text: str,
    diagnostics: dict[str, Any] | None,
) -> None:
    stdout, terminal = _worker_events(
        tmp_path, capsys, error=error, text=text, diagnostics=diagnostics,
    )

    assert "provider_error" not in terminal
    assert launcher_failures._vscode_lm_balance_failure_from_output(stdout) is None


# --- the launcher's seal: re-minted, and only from the sealed object ---------


def test_launcher_re_mints_the_exact_credential_failure(tmp_path: Path) -> None:
    long_detail = "x" * 900
    forged_body = {
        **_sealed(),
        "http_status": 200,
        "detail": long_detail,
        "reset_at": "2099-01-01T00:00:00+00:00",
        "provider_code": "1308",
        "extra": "dropped",
    }

    failure = launcher_failures._vscode_lm_balance_failure_from_output(
        _stdout(tmp_path, _envelope(provider_error=forged_body))
    )

    assert failure == {
        "schema_id": LAUNCH_FAILURE_SCHEMA,
        "reason": REASON,
        "refusal_kind": runtime_adapters.REFUSAL_CREDENTIAL_REJECTED,
        "recoverable": False,
        "http_status": 401,
        "error_code": "authentication_failed",
        "session_id": "",
        "provider_error": {
            "schema_id": "aiworkhub.provider_route_error.v1",
            "owner": "provider",
            "sealed": True,
            "source": "vscode_lm_extension_response",
            "code": "authentication_failed",
            "detail": long_detail[:300],
        },
    }


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"provider_error": None}, id="no-sealed-provider-error"),
        pytest.param({"provider_error": "sealed"}, id="provider-error-not-an-object"),
        pytest.param(
            {"provider_error": {**_sealed(), "source": "model_output"}},
            id="wrong-source",
        ),
        pytest.param(
            {"provider_error": {**_sealed(), "sealed": "true"}}, id="sealed-string",
        ),
        pytest.param(
            {"provider_error": {**_sealed(), "owner": "worker"}}, id="wrong-owner",
        ),
        pytest.param(
            {"provider_error": {**_sealed(), "code": "invalid_api_key"}},
            id="other-credential-code",
        ),
        pytest.param(
            {"provider_error": {**_sealed(), "code": "AUTHENTICATION_FAILED"}},
            id="code-not-exact",
        ),
        pytest.param(
            {"error": "vscode_lm_provider_balance_exhausted"},
            id="balance-constant-carrying-a-credential-code",
        ),
        pytest.param({"error": HOST_ERROR}, id="sentence-without-the-wrapper"),
        pytest.param({"type": "assistant"}, id="not-a-result-event"),
        pytest.param({"is_error": False}, id="not-an-error-result"),
        pytest.param({"subtype": "success"}, id="not-an-error-subtype"),
    ],
)
def test_forged_or_unsealed_credential_output_is_not_sealed(
    tmp_path: Path, overrides: dict[str, Any],
) -> None:
    stdout = _stdout(tmp_path, _envelope(**overrides))

    assert launcher_failures._vscode_lm_balance_failure_from_output(stdout) is None
    assert (
        launcher_failures._editor_hosted_route_failure_from_output(
            "glm_vscode_lm", stdout
        )
        is None
    )


def test_quota_1308_wrapper_is_unchanged(tmp_path: Path) -> None:
    sealed = vscode_lm_worker.provider_quota_error(QUOTA_HOST_ERROR)
    assert sealed is not None

    failure = launcher_failures._vscode_lm_balance_failure_from_output(
        _stdout(
            tmp_path,
            _envelope(
                error=f"vscode_lm_request_failed:{QUOTA_HOST_ERROR}",
                provider_error=sealed,
            ),
        )
    )

    assert failure is not None
    assert failure["reason"] == (
        "provider_refused_quota_exhausted_recoverable_after_reported_window"
    )
    assert failure["refusal_kind"] == runtime_adapters.REFUSAL_QUOTA_EXHAUSTED
    assert failure["recoverable"] is True
    assert failure["http_status"] == 429
    assert failure["error_code"] == "quota_exhausted"
    assert failure["provider_error"]["code"] == "quota_exhausted"
    assert failure["provider_error"]["provider_code"] == "1308"
    assert failure["provider_error"]["http_status"] == 429


def test_quota_code_without_1308_on_the_wrapper_still_seals_nothing(
    tmp_path: Path,
) -> None:
    body = {**_sealed(), "code": "quota_exhausted"}

    assert (
        launcher_failures._vscode_lm_balance_failure_from_output(
            _stdout(tmp_path, _envelope(provider_error=body))
        )
        is None
    )


def test_balance_constant_path_is_unchanged(tmp_path: Path) -> None:
    sealed = vscode_lm_worker.provider_balance_error(
        "You've reached your monthly credit limit. Upgrade to continue."
    )
    assert sealed is not None

    failure = launcher_failures._vscode_lm_balance_failure_from_output(
        _stdout(
            tmp_path,
            _envelope(
                error=vscode_lm_worker.VSCODE_LM_PROVIDER_BALANCE_EXHAUSTED,
                provider_error=sealed,
            ),
        )
    )

    assert failure is not None
    assert failure["reason"] == "provider_refused_balance_exhausted"
    assert failure["refusal_kind"] == "balance_exhausted"
    assert failure["recoverable"] is False
    assert failure["http_status"] == 402
    assert failure["error_code"] == "insufficient_balance"


# --- what the changed reason means downstream --------------------------------


def test_new_reason_classifies_as_credential_not_unknown() -> None:
    disposition = tfc.failure_disposition_from_paths(
        state="worker_failed",
        error=REASON,
        refusal_kind=runtime_adapters.REFUSAL_CREDENTIAL_REJECTED,
    )

    assert disposition["failure_class"] == tfc.FAILURE_CLASS_CREDENTIAL
    assert disposition["failure_class"] != tfc.FAILURE_CLASS_UNKNOWN


def _sealed_credential_fields() -> dict[str, Any]:
    return {
        "reason": REASON,
        "refusal_kind": runtime_adapters.REFUSAL_CREDENTIAL_REJECTED,
        "provider_error": {**_sealed(), "detail": HOST_ERROR},
    }


def _card() -> dict[str, Any]:
    return {"terminal_substatus": "worker_failed", "worker_status": "worker_failed"}


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(
            {
                "state": "worker_failed",
                "latest_event": {
                    "failure_kind": "worker_failed",
                    "diagnostic": f"worker_failed:{REASON}",
                    **_sealed_credential_fields(),
                },
                "task_card": _card(),
            },
            id="latest-event",
        ),
        pytest.param(
            {
                "state": "worker_failed",
                "latest_event": {"failure_kind": "worker_failed"},
                "task_card": {
                    **_card(),
                    "terminal_failure": {"evidence": _sealed_credential_fields()},
                },
            },
            id="terminal-failure-evidence",
        ),
    ],
)
def test_required_lens_failover_still_fires_on_the_changed_reason(
    status: dict[str, Any],
) -> None:
    disposition = review_orchestrator.reviewer_terminal_disposition(status)

    assert (
        tfc.adapter_credential_retry_allowed(disposition=disposition, attempts_made=0)
        is True
    )
