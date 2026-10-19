from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import learning_commit  # noqa: E402
from aiworkhub import process_launcher as pl  # noqa: E402
from aiworkhub import process_launcher_validation as plv  # noqa: E402
from aiworkhub import terminal_failure_classification as tfc  # noqa: E402

PREFIX = "provider_no_effect"
WorkspaceError = pl.WorkspaceError

WRITABLE = {"required_outputs": ["out/report.json"]}
WRITABLE_WORKSPACE = SimpleNamespace(allowed_writes=["out/report.json"])
READONLY = {"read_only": True, "required_outputs": []}
READONLY_WORKSPACE = SimpleNamespace(allowed_writes=[])
REVIEW_ROUTED_STATES = frozenset(
    {"validation_failed", "scope_rejected", "promotion_conflict"}
)


def _prefix(error: BaseException) -> str:
    return str(error).partition(":")[0]


def _calls(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Name)
        and inner.func.id == name
        for inner in ast.walk(node)
    )


def _is_no_effect_raise(node: ast.AST) -> bool:
    if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
        return False
    call = node.exc
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "WorkspaceError"
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Attribute)
        and call.args[0].attr == "PROVIDER_NO_EFFECT"
    )


def _enclosing_block(root: ast.AST, target: ast.stmt) -> list[ast.stmt]:
    for node in ast.walk(root):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and any(item is target for item in block):
                return block
    raise AssertionError("statement has no enclosing block")


def _assign_index(block: list[ast.stmt], name: str) -> int:
    for index, stmt in enumerate(block):
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == name
        ):
            return index
    raise AssertionError(f"{name} is not assigned beside the guard")


def _run_guard(guard: ast.If, **scope: Any) -> WorkspaceError | None:
    module = ast.fix_missing_locations(ast.Module(body=[guard], type_ignores=[]))
    code = compile(module, pl.__file__, "exec")
    try:
        exec(code, {**vars(pl), **scope})
    except WorkspaceError as error:
        return error
    return None


@pytest.fixture(scope="module")
def finalizer() -> ast.AST:
    tree = ast.parse(Path(pl.__file__).read_text(encoding="utf-8"))
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_finalize_isolated_request"
    ]
    assert len(found) == 1
    return found[0]


@pytest.fixture(scope="module")
def guard(finalizer: ast.AST) -> ast.If:
    guards = [
        node
        for node in ast.walk(finalizer)
        if isinstance(node, ast.If)
        and len(node.body) == 1
        and _is_no_effect_raise(node.body[0])
    ]
    assert len(guards) == 1
    return guards[0]


def test_prefix_is_a_registered_launcher_minted_reason() -> None:
    assert tfc.PROVIDER_NO_EFFECT == PREFIX
    assert tfc.constant(PREFIX) == PREFIX
    assert tfc.unclassified_reason_constants() == ()


def test_prefix_is_outside_every_review_routed_family() -> None:
    assert not PREFIX.startswith(("validation_", "required_output"))
    base_state = plv.terminal_state_for_workspace_error(WorkspaceError(PREFIX))
    assert base_state not in REVIEW_ROUTED_STATES


@pytest.mark.parametrize("message", [PREFIX, f"{PREFIX}:detail"])
def test_launcher_maps_the_prefix_to_worker_failed(message: str) -> None:
    state = pl._terminal_state_for_workspace_error(WorkspaceError(message))

    assert state == "worker_failed"
    assert state in pl.TERMINAL_PROCESS_STATES
    assert state not in REVIEW_ROUTED_STATES


def test_review_routed_neighbours_keep_their_states() -> None:
    expected = {
        "required_output_mismatch:out/report.json": "validation_failed",
        "validation_required_aiworkhub_mcp_call_missing:x": "validation_failed",
        "no_effect": "finalize_failed",
        pl._REWORK_NO_DELTA: "worker_failed",
    }

    actual = {
        message: pl._terminal_state_for_workspace_error(WorkspaceError(message))
        for message in expected
    }

    assert actual == expected


@pytest.mark.parametrize("message", [PREFIX, f"{PREFIX}:claimed_by=worker_x"])
def test_typed_reason_carries_only_the_prefix(message: str) -> None:
    reason = tfc.workspace_error_reason(message)

    assert isinstance(reason, tfc.TerminalReason)
    assert reason.render() == PREFIX


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reason": PREFIX},
        {"reason": f"{PREFIX}:detail", "terminal_substatus": "worker_failed"},
    ],
)
def test_prefix_is_a_provider_runtime_disposition(kwargs: dict[str, str]) -> None:
    disposition = tfc.failure_disposition_from_substatus(**kwargs)

    assert disposition["cause"] == tfc.CAUSE_PROVIDER_RUNTIME_UNCLASSIFIED
    assert disposition["action"] == tfc.ACTION_MANAGER_JUDGMENT_UNKNOWN
    assert disposition["retry_scope"] == tfc.RETRY_SCOPE_NONE
    assert disposition["provider_launched"] is False
    category = tfc.failure_category_projection(disposition)
    assert category == tfc.FAILURE_CATEGORY_PROVIDER_RUNTIME
    assert category == "provider_runtime"


def test_learning_taxonomy_files_the_prefix_under_provider_runtime() -> None:
    state = pl._terminal_state_for_workspace_error(WorkspaceError(PREFIX))

    assert state in tfc.PROVIDER_RUNTIME_TERMINAL_SUBSTATUSES
    category = learning_commit.classify_failure_category(terminal_substatus=state)
    assert category is learning_commit.FailureCategory.PROVIDER_RUNTIME


def test_terminal_event_names_the_prefix() -> None:
    event = tfc.terminal_event_authority(
        state="worker_failed",
        exit_code=None,
        error=PREFIX,
        reason=tfc.workspace_error_reason(PREFIX),
    )

    assert event["failure_kind"] == "worker_failed"
    assert event["diagnostic"] == f"worker_failed:{PREFIX}"
    assert event["error"] == PREFIX


@pytest.mark.parametrize(
    ("metadata", "workspace"),
    [
        pytest.param(WRITABLE, WRITABLE_WORKSPACE, id="writable"),
        pytest.param(
            {"read_only": True, "required_outputs": ["out/report.json"]},
            READONLY_WORKSPACE,
            id="read-only-flag-with-required-output",
        ),
        pytest.param(
            {"read_only": True, "required_outputs": []},
            WRITABLE_WORKSPACE,
            id="read-only-flag-with-allowed-write",
        ),
        pytest.param(
            {"required_outputs": []},
            READONLY_WORKSPACE,
            id="no-read-only-flag",
        ),
    ],
)
def test_zero_change_writable_attempt_raises_the_prefix(
    guard: ast.If, metadata: dict[str, Any], workspace: SimpleNamespace
) -> None:
    error = _run_guard(
        guard,
        changed=[],
        validation_only_replay=False,
        metadata=metadata,
        workspace=workspace,
    )

    assert error is not None
    assert _prefix(error) == PREFIX


def test_raised_prefix_lands_on_a_non_review_terminal_state(guard: ast.If) -> None:
    error = _run_guard(
        guard,
        changed=[],
        validation_only_replay=False,
        metadata=WRITABLE,
        workspace=WRITABLE_WORKSPACE,
    )

    assert error is not None
    state = pl._terminal_state_for_workspace_error(error)
    assert state == "worker_failed"
    assert state not in REVIEW_ROUTED_STATES


@pytest.mark.parametrize(
    ("changed", "replay", "metadata", "workspace"),
    [
        pytest.param([], True, WRITABLE, WRITABLE_WORKSPACE, id="validation-only-replay"),
        pytest.param([], False, READONLY, READONLY_WORKSPACE, id="read-only-research"),
        pytest.param(
            ["out/report.json"], False, WRITABLE, WRITABLE_WORKSPACE, id="changed-paths"
        ),
    ],
)
def test_guard_leaves_replay_research_and_changed_attempts_alone(
    guard: ast.If,
    changed: list[str],
    replay: bool,
    metadata: dict[str, Any],
    workspace: SimpleNamespace,
) -> None:
    error = _run_guard(
        guard,
        changed=changed,
        validation_only_replay=replay,
        metadata=metadata,
        workspace=workspace,
    )

    assert error is None


def test_guard_precedes_the_output_and_validation_gates(
    finalizer: ast.AST, guard: ast.If
) -> None:
    block = _enclosing_block(finalizer, guard)
    position = block.index(guard)

    inputs = (
        _assign_index(block, "changed"),
        _assign_index(block, "validation_only_replay"),
    )
    gates = (
        _assign_index(block, "required_output_records"),
        _assign_index(block, "worker_mcp_gate"),
        _assign_index(block, "validations"),
    )
    assert max(inputs) < position < min(gates)


def test_later_no_effect_and_research_branches_survive(finalizer: ast.AST) -> None:
    no_effect_raises = [
        node
        for node in ast.walk(finalizer)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "WorkspaceError"
        and node.exc.args
        and isinstance(node.exc.args[0], ast.Constant)
        and node.exc.args[0].value == "no_effect"
    ]

    assert no_effect_raises
    assert _calls(finalizer, "_readonly_research_result_evidence")
    assert _calls(finalizer, "_rework_no_delta_refusal")
