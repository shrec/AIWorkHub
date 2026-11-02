"""NF-2026-01971: a lane-measured validation_environment failure is recoverable.

A correct candidate whose card validation failed only because the contained
build lane itself measured an infrastructure failure (``build_lane._row`` sets
``failure_class=validation_environment``) used to leave the manager at a dead
end: no candidate-output signature matched, so no replay plan was placed, and
``mark_done`` refused with a bare ``done_terminal_review_not_acceptable``.

The lane's own class now counts as the ``lane_infrastructure`` signature --
server-measured, so ``candidate_forgeable=False`` -- and the mark_done refusal
names the recorded validation-only replay and returns its two tool calls. A
product-code failure, alone or mixed in, is refused exactly as before.
"""

from __future__ import annotations

from aiworkhub import build_lane, core
from aiworkhub import process_launcher_validation as validation
from aiworkhub import terminal_failure_classification as tfc
from aiworkhub import validation_runner

_TASK_ID = "TASK_NF01971"
_TOPIC = "validation"
_LANE_COMMAND = "lane:cmake-build"
_PYTEST_COMMAND = "python -m pytest -q tests/test_parse.py"
_OUTPUT = "src/parse.py"
_DIGEST = "file:644:" + "cd" * 32
_LANE = "lane_infrastructure"
_TYPEERROR = "TypeError: unsupported operand type(s) for +: 'int' and 'str'"


def _lane_row(command: str = _LANE_COMMAND) -> dict:
    """A failed row as ``build_lane._row`` writes it when the lane measured an
    infrastructure failure."""
    blocker = "docker_exit_125"
    stderr = f"{build_lane.VALIDATION_ENVIRONMENT_CLASS}:{blocker}\ncmake: not found\n"
    return {
        "command": command,
        "declared_command": command,
        "argv": ["docker", "run", "cmake-build"],
        "sandbox_backend": "docker",
        "execution_boundary": "docker_container_lane",
        "returncode": 125,
        "timed_out": False,
        "stdout_head": "",
        "stdout_tail": "",
        "stdout_truncated": False,
        "stderr_head": stderr,
        "stderr_tail": stderr,
        "stderr_truncated": False,
        "launch_error": build_lane.INFRASTRUCTURE_LAUNCH_ERROR,
        "infrastructure_blocker": blocker,
        "failure_class": build_lane.VALIDATION_ENVIRONMENT_CLASS,
    }


def _product_row() -> dict:
    """A plain product-code pytest failure: no lane class, no signature."""
    stdout = (
        "...F    [100%]\n"
        + "=" * 30 + " FAILURES " + "=" * 30 + "\n"
        + "_" * 20 + " test_parse " + "_" * 20 + "\n"
        + "tests/test_parse.py:9: in test_parse\n"
        + f"E   {_TYPEERROR}\n"
        + "=" * 20 + " short test summary info " + "=" * 20 + "\n"
        + f"FAILED tests/test_parse.py::test_parse - {_TYPEERROR}\n"
        + "1 failed, 3 passed in 0.42s\n"
    )
    return {
        "command": _PYTEST_COMMAND,
        "declared_command": _PYTEST_COMMAND,
        "sandbox_backend": "windows_appcontainer",
        "returncode": 1,
        "timed_out": False,
        "stdout_head": stdout,
        "stdout_tail": stdout,
        "stdout_truncated": False,
        "stderr_head": "",
        "stderr_tail": "",
        "stderr_truncated": False,
    }


def _records() -> list[dict]:
    return [
        {"pattern": _OUTPUT, "path": _OUTPUT, "bytes": 42, "sha256": _DIGEST,
         "unchanged_allowed": False}
    ]


def _disposition(rows, *, records=None):
    return tfc.validation_environment_disposition(
        terminal_state="validation_failed",
        validations=rows,
        required_outputs=[_OUTPUT],
        required_output_records=_records() if records is None else records,
        task_id=_TASK_ID,
    )


_PLAN = [
    {
        "tool": "aiworkhub_task_reject_review",
        "arguments": {
            "task_id": _TASK_ID,
            "reason": f"validation_environment_signature={_LANE}",
            "to": "blocked",
            "failure_category": "validation_environment",
        },
    },
    {
        "tool": "aiworkhub_task_recover_blocked_rework",
        "arguments": {"task_id": _TASK_ID, "validation_only_replay": True},
    },
]


# --- (a) lane-measured rows place the disposition ---------------------------- #


def test_lane_measured_rows_place_the_disposition_with_the_replay_plan():
    rows = [
        {"command": "ruff check src", "returncode": 0},
        _lane_row(),
        _lane_row("lane:cmake-test"),
    ]

    disposition = _disposition(rows)

    assert disposition is not None
    assert disposition["cause"] == tfc.CAUSE_VALIDATION_ENVIRONMENT
    assert tfc.failure_category_projection(disposition) == "validation_environment"
    assert disposition["evidence"] == f"validation_environment_signature={_LANE}"
    # Server-measured, never candidate output -- yet still only classified.
    assert disposition["evidence_source"] == "validation_lane_measurement"
    assert disposition["candidate_forgeable"] is False
    assert disposition["evidence_authority"] == tfc.EVIDENCE_AUTHORITY_NONE
    assert disposition["action"] == tfc.ACTION_MANAGER_JUDGMENT_UNKNOWN
    assert disposition["provider_launched"] is False

    recovery = disposition["validation_environment_recovery"]
    assert recovery["schema_id"] == tfc.VALIDATION_ENVIRONMENT_RECOVERY_SCHEMA_ID
    assert recovery["signature"] == _LANE
    assert recovery["command"] == _LANE_COMMAND
    assert recovery["sandbox_backend"] == "docker"
    assert recovery["evidence_source"] == "validation_lane_measurement"
    assert recovery["candidate_forgeable"] is False
    assert recovery["host_execution"] is False
    assert recovery["plan"] == _PLAN


def test_the_lane_entry_is_named_and_never_reached_by_the_candidate_text_scan():
    entry = next(
        signature
        for signature in validation_runner.VALIDATION_ENVIRONMENT_SIGNATURES
        if signature.name == validation_runner.LANE_INFRASTRUCTURE_SIGNATURE
    )
    assert entry.name == _LANE
    assert entry.candidate_forgeable is False
    assert entry.evidence_source == validation_runner.LANE_INFRASTRUCTURE_EVIDENCE_SOURCE
    assert all(
        signature.candidate_forgeable
        for signature in validation_runner.VALIDATION_ENVIRONMENT_SIGNATURES
        if signature is not entry
    )
    # Candidate output that merely prints the class names nothing: only the
    # lane-set ``failure_class`` field counts.
    forged = {k: v for k, v in _lane_row().items() if k != "failure_class"}
    assert validation_runner.validation_environment_signature(forged) is None
    assert _disposition([forged]) is None


def test_required_outputs_must_still_be_hash_recorded():
    assert _disposition([_lane_row()], records=[]) is None
    assert _disposition(
        [_lane_row()], records=[{**_records()[0], "sha256": None}]
    ) is None


def test_the_finalizer_evidence_carries_the_lane_recovery():
    metadata = {"task_id": _TASK_ID, "required_outputs": [_OUTPUT]}
    evidence = validation.validation_environment_evidence(
        "validation_failed", metadata, [_lane_row()], _records()
    )
    assert evidence["validation_environment_recovery"]["plan"] == _PLAN
    assert evidence["validation_environment_disposition"]["candidate_forgeable"] is False


# --- (b) a mix with a product-code failure stays plain ----------------------- #


def test_a_mix_with_a_product_code_failure_returns_none():
    assert validation_runner.validation_environment_signature(_product_row()) is None
    assert _disposition([_lane_row(), _product_row()]) is None
    assert _disposition([_product_row(), _lane_row()]) is None
    assert validation_runner.match_validation_environment_signature(
        [_lane_row(), _product_row()],
        required_outputs=[_OUTPUT],
        required_output_records=_records(),
    ) is None


# --- (c)/(d) mark_done names the recovery, or refuses as before -------------- #


def _mark_done(monkeypatch, substatus: str, evidence: dict) -> dict:
    card = {
        "task_id": _TASK_ID,
        "topic": _TOPIC,
        "status": "review",
        "worker_status": "review",
        "terminal_review": {"substatus": substatus, "evidence": evidence},
    }
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: core.CODEX_RUNNER)
    monkeypatch.setattr(core, "_live_card", lambda task_id: (card, None))
    return core.mark_done(_TASK_ID)


def _lane_evidence() -> dict:
    metadata = {"task_id": _TASK_ID, "required_outputs": [_OUTPUT]}
    return {
        "changed_paths": [_OUTPUT],
        **validation.validation_environment_evidence(
            "validation_failed", metadata, [_lane_row()], _records()
        ),
    }


def test_mark_done_on_a_validation_environment_card_names_the_replay(monkeypatch):
    result = _mark_done(monkeypatch, "validation_failed", _lane_evidence())

    assert result["ok"] is False
    assert result["stderr"] == (
        "done_terminal_review_not_acceptable:validation_failed"
        ";recovery=validation_only_replay"
    )
    assert result["recovery_plan"] == _PLAN
    assert [step["tool"] for step in result["recovery_plan"]] == [
        "aiworkhub_task_reject_review",
        "aiworkhub_task_recover_blocked_rework",
    ]


def test_a_plain_validation_failed_refusal_is_unchanged(monkeypatch):
    metadata = {"task_id": _TASK_ID, "required_outputs": [_OUTPUT]}
    plain = validation.validation_environment_evidence(
        "validation_failed", metadata, [_lane_row(), _product_row()], _records()
    )
    assert plain == {}

    result = _mark_done(monkeypatch, "validation_failed", {"changed_paths": [_OUTPUT]})

    assert result == core._lifecycle_error(
        "done_terminal_review_not_acceptable:validation_failed"
    )
    assert "recovery_plan" not in result


def test_only_a_validation_failed_terminal_ever_names_the_recovery(monkeypatch):
    result = _mark_done(monkeypatch, "finalize_failed", _lane_evidence())

    assert result == core._lifecycle_error(
        "done_terminal_review_not_acceptable:finalize_failed"
    )
