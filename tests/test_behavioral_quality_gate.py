from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from aiworkhub import process_launcher, quality_evidence, server


def _receipt(
    command: str,
    role: str,
    *,
    returncode: int = 0,
    stdout: str = "",
    truncated: bool = False,
) -> dict[str, object]:
    return {
        "command": command,
        "declared_command": command,
        "behavioral_role": role,
        "returncode": returncode,
        "timed_out": False,
        "stdout_head": stdout,
        "stdout_tail": stdout,
        "stdout_truncated": truncated,
    }


@pytest.mark.parametrize(
    ("work_kind", "roles"),
    [
        ("bugfix", ["reproduction", "regression"]),
        ("refactor", ["parity"]),
        ("security", ["negative_fixture"]),
        ("data_ml", ["schema", "distribution"]),
    ],
)
def test_specialized_behavioral_roles_pass_from_exact_validation_receipts(
    work_kind, roles
):
    commands = [f"python checks/{role}.py" for role in roles]
    gate = quality_evidence.evaluate_behavioral_gate(
        {
            "work_kind": work_kind,
            "validation": commands,
            "validation_roles": roles,
        },
        [_receipt(command, role) for command, role in zip(commands, roles)],
    )

    assert gate["applicable"] is True
    assert gate["passed"] is True
    assert gate["required_roles"] == roles


def test_specialized_contract_fails_before_execution_when_role_is_missing():
    with pytest.raises(
        ValueError,
        match="behavioral_validation_roles_missing:regression",
    ):
        quality_evidence.normalize_behavioral_contract(
            "bugfix",
            ["python checks/reproduce.py"],
            ["reproduction"],
        )


def test_behavioral_gate_rejects_command_or_role_identity_drift():
    gate = quality_evidence.evaluate_behavioral_gate(
        {
            "work_kind": "refactor",
            "validation": ["python checks/parity.py"],
            "validation_roles": ["parity"],
        },
        [_receipt("python checks/other.py", "parity")],
    )

    assert gate["passed"] is False
    assert gate["reason"] == "behavioral_evidence_failed:parity"


def test_performance_gate_computes_baseline_delta_threshold():
    baseline_command = "python bench.py --baseline"
    delta_command = "python bench.py --candidate"
    baseline = (
        'AIWORKHUB_METRIC: {"metric":"latency","unit":"ms","value":100}'
    )
    candidate = (
        'AIWORKHUB_METRIC: {"metric":"latency","unit":"ms","value":104,'
        '"direction":"lower","max_regression_percent":5}'
    )
    gate = quality_evidence.evaluate_behavioral_gate(
        {
            "work_kind": "performance",
            "validation": [baseline_command, delta_command],
            "validation_roles": ["baseline", "delta"],
        },
        [
            _receipt(baseline_command, "baseline", stdout=baseline),
            _receipt(delta_command, "delta", stdout=candidate),
        ],
    )

    assert gate["passed"] is True
    assert gate["measurements"] == {
        "metric": "latency",
        "unit": "ms",
        "baseline": 100.0,
        "candidate": 104.0,
        "direction": "lower",
        "max_regression_percent": 5.0,
        "threshold": 105.0,
    }


def test_performance_gate_rejects_regression_and_truncated_metric_evidence():
    authority = {
        "work_kind": "performance",
        "validation": ["baseline", "candidate"],
        "validation_roles": ["baseline", "delta"],
    }
    baseline = 'AIWORKHUB_METRIC: {"metric":"throughput","unit":"rps","value":100}'
    slower = (
        'AIWORKHUB_METRIC: {"metric":"throughput","unit":"rps","value":90,'
        '"direction":"higher","max_regression_percent":5}'
    )

    regression = quality_evidence.evaluate_behavioral_gate(
        authority,
        [
            _receipt("baseline", "baseline", stdout=baseline),
            _receipt("candidate", "delta", stdout=slower),
        ],
    )
    assert regression["passed"] is False
    assert regression["reason"] == "performance_regression_exceeds_threshold"

    truncated = quality_evidence.evaluate_behavioral_gate(
        authority,
        [
            _receipt("baseline", "baseline", stdout=baseline, truncated=True),
            _receipt("candidate", "delta", stdout=slower),
        ],
    )
    assert truncated["passed"] is False
    assert truncated["reason"] == "performance_metric_stdout_truncated"


def test_generic_tasks_remain_backward_compatible():
    gate = quality_evidence.evaluate_behavioral_gate(
        {"validation": ["python -m pytest -q"]},
        [_receipt("python -m pytest -q", "generic")],
    )

    assert gate["applicable"] is False
    assert gate["passed"] is None
    assert gate["reason"] == "generic_work_kind"


def test_declared_validation_runner_attaches_roles(monkeypatch, tmp_path):
    monkeypatch.setattr(
        process_launcher,
        "_validation_route_kwargs",
        lambda _metadata: {},
    )
    monkeypatch.setattr(
        process_launcher,
        "run_validations",
        lambda _workspace, commands, **_kwargs: [
            {
                "command": command,
                "declared_command": command,
                "returncode": 0,
            }
            for command in commands
        ],
    )
    authority = {
        "work_kind": "bugfix",
        "validation": ["reproduce", "regress"],
        "validation_roles": ["reproduction", "regression"],
    }

    results = process_launcher._run_declared_validations(
        SimpleNamespace(path=tmp_path, home=tmp_path),
        authority,
        {"adapter_id": "vscode_lm"},
    )

    assert [row["behavioral_role"] for row in results] == [
        "reproduction",
        "regression",
    ]


def test_runtime_enforcement_attaches_gate_and_fails_closed():
    authority = {
        "work_kind": "security",
        "validation": ["python checks/negative.py"],
        "validation_roles": ["negative_fixture"],
    }
    quality_gate: dict[str, object] = {}

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="behavioral_gate_failed:behavioral_evidence_failed:negative_fixture",
    ):
        process_launcher._enforce_behavioral_gate(
            authority,
            [
                _receipt(
                    "python checks/negative.py",
                    "negative_fixture",
                    returncode=1,
                )
            ],
            quality_gate,
        )

    assert quality_gate["behavioral_gate"]["passed"] is False


def test_public_task_create_exposes_behavioral_contract_fields():
    parameters = inspect.signature(server.aiworkhub_task_create).parameters

    assert parameters["work_kind"].default == "generic"
    assert parameters["validation_roles"].default is None
