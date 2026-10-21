from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import worker_workspace  # noqa: E402

REDACTED = "<redacted>"


def _receipt(tail: str) -> dict:
    receipt = worker_workspace._validation_failure_receipt(
        {"returncode": 1, "argv": ["pytest", "-q"], "stderr_tail": tail}
    )
    assert receipt is not None
    return receipt


@pytest.mark.parametrize(
    "identifier",
    [
        "validation_toolchain_authority_secret_unavailable",
        "tests/test_worker_workspace.py::test_some_very_long_function_name_here",
        r"C:\Users\someone\projects\AIWorkHub\src\aiworkhub\worker_workspace.py",
        "src/aiworkhub/worker_workspace.py:12138: in _validation_failure_receipt",
        "aiworkhub.worker_workspace._validation_failure_receipt_helper_name",
        "/home/ci/Work2/AIWorkHub/src/aiworkhub/worker_workspace.py",
        "https://github.com/Org/Repo2/blob/main/src/aiworkhub/worker_workspace.py",
    ],
)
def test_identifiers_paths_and_node_ids_survive(identifier: str) -> None:
    tail = _receipt(f"FAILED {identifier} - assert False")["diagnostic_tail"]
    assert tail == f"FAILED {identifier} - assert False"


@pytest.mark.parametrize(
    "secret",
    [
        "sk-proj-" + "Ab1_" * 5,
        "ghp_" + "a1b2c3d4e5" * 3,
        "github_pat_" + "a1b2c3d4e5" * 3,
        "xoxb-" + "1234567890-abcdef",
        "AKIA" + "ABCDEFGH12345678",
        "0123456789abcdef0123456789abcdef01234567",
        "Qw3rTyUiOp9AsDfGhJk1ZxCvBnM2Qw3rTyUiOp9A",
        "Qw3rTyUiOp9AsDfGhJk1-ZxCvBnM2Qw3rTyUiOp9AbC4",
        "Qw3rTyUiOp9AsDfGhJk1_ZxCvBnM2Qw3rTyUiOp9AbC4",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9."
        "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        "Ab1+Cd2/Ef3+Gh4/Ij5+Kl6/Mn7+Op8/Qr9==",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "s" * 80,
    ],
)
def test_secret_shaped_tokens_are_redacted(secret: str) -> None:
    tail = _receipt(f"token={secret} trailing")["diagnostic_tail"]
    assert tail == f"token={REDACTED} trailing"


def test_akia_key_glued_to_preceding_text_is_redacted() -> None:
    tail = _receipt("key%3DAKIA" + "ABCDEFGH12345678 trailing")["diagnostic_tail"]
    assert tail == f"key%3D{REDACTED} trailing"


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "Basic"])
def test_bearer_and_basic_credentials_are_redacted(scheme: str) -> None:
    tail = _receipt(f"Authorization: {scheme} abc.def-ghi_jkl123")["diagnostic_tail"]
    assert tail == f"Authorization: {REDACTED}"


def test_receipt_schema_and_keys_unchanged() -> None:
    reason = "validation_toolchain_authority_secret_unavailable"
    receipt = _receipt("word " * 1000 + reason)
    assert receipt["schema_id"] == "aiworkhub.validation_failure_receipt.v1"
    assert set(receipt) == {
        "schema_id",
        "failure_class",
        "argv",
        "returncode",
        "timed_out",
        "diagnostic_sha256",
        "command_sha256",
        "diagnostic_tail",
        "receipt_sha256",
    }
    assert len(receipt["diagnostic_tail"]) == 2048
    assert receipt["diagnostic_tail"].endswith(reason)
