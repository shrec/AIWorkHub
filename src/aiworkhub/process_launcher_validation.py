"""Pure and dependency-injected validation helpers for ``process_launcher``."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping

from . import quality_evidence
from . import worker_workspace as _worker_workspace
from .worker_workspace import (
    ValidationEnvironmentBlocked,
    ValidationRunError,
    WorkerWorkspace,
    WorkspaceError,
)

ValidationRunner = Callable[..., list[dict[str, Any]]]
WorkspaceCreator = Callable[..., WorkerWorkspace]
WorkspaceCleanup = Callable[..., None]
RouteResolver = Callable[[Mapping[str, Any]], dict[str, Any]]


def validation_route_kwargs(
    metadata: Mapping[str, Any],
    sandbox_backend_for_adapter: Callable[[str], str],
) -> dict[str, Any]:
    """Return the exact launch-bound validation route, failing on drift."""
    adapter_id = str(metadata.get("adapter_id") or "").strip()
    if not adapter_id:
        raise WorkspaceError("validation_route_adapter_missing")
    expected_backend = sandbox_backend_for_adapter(adapter_id)
    recorded_backend = str(metadata.get("sandbox_backend") or "").strip()
    execution_mode = str(metadata.get("execution_mode") or "").strip()
    route: dict[str, Any] = {"backend": expected_backend, "adapter_id": adapter_id}
    if execution_mode == "validation_only_replay":
        if recorded_backend and recorded_backend not in {
            expected_backend,
            "deterministic_validation",
        }:
            raise WorkspaceError(
                "validation_route_backend_mismatch:"
                f"expected={expected_backend}:recorded={recorded_backend}"
            )
        route["outer_validation_authority"] = True
        return route
    if recorded_backend and recorded_backend != expected_backend:
        raise WorkspaceError(
            "validation_route_backend_mismatch:"
            f"expected={expected_backend}:recorded={recorded_backend}"
        )
    return route


def declared_validation_commands(authority: Mapping[str, Any]) -> list[str]:
    """Return the exact non-empty validation contract from card/metadata."""
    raw = authority.get("validation")
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise WorkspaceError("validation_commands_invalid")
    commands: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise WorkspaceError("validation_command_invalid")
        commands.append(value)
    return commands


def requires_bridge_cancellation(metadata: Mapping[str, Any]) -> bool:
    """Return whether finalization must publish a provider bridge decision."""
    return not (
        str(metadata.get("execution_mode") or "").strip()
        == "validation_only_replay"
        and metadata.get("provider_launched") is False
    )


MYPY_DIAGNOSTIC_RE = re.compile(
    r"^(?P<path>[^:\n]+):\d+(?::\d+)?\s*: error: (?P<message>.+?)"
    r"(?:\s+\[(?P<code>[^\]]+)\])?$"
)


def exact_schema_mypy_invocation(row: Mapping[str, Any]) -> bool:
    """Accept only the trusted mypy executable or ``python -m mypy``."""
    argv = tuple(
        str(value)
        for value in (row.get("executed_argv") or row.get("argv") or ())
    )
    if not argv:
        return False
    executable = Path(argv[0]).name.lower()
    if executable in {"mypy", "mypy.exe"}:
        return True
    return bool(
        len(argv) >= 3
        and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", executable)
        and argv[1:3] == ("-m", "mypy")
    )


def schema_mypy_diagnostics(
    row: Mapping[str, Any],
) -> Counter[tuple[str, str, str]]:
    """Parse exactly the comparable portion of a real mypy failure."""
    if row.get("timed_out") or row.get("returncode") != 1:
        raise WorkspaceError("baseline_mypy_candidate_not_comparable")
    if _worker_workspace._validation_failure_class(row) != "type_check_failure":
        raise WorkspaceError("baseline_mypy_candidate_not_comparable")
    if row.get("stdout_truncated") or row.get("stderr_truncated"):
        raise WorkspaceError("baseline_mypy_output_truncated")
    output = "\n".join(
        (str(row.get("stdout_tail") or ""), str(row.get("stderr_tail") or ""))
    )
    diagnostics: Counter[tuple[str, str, str]] = Counter()
    saw_error = False
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("Found ") or line.startswith("Success:"):
            continue
        if " error: " not in line:
            if "Traceback" in line or "INTERNAL ERROR" in line:
                raise WorkspaceError("baseline_mypy_output_malformed")
            continue
        saw_error = True
        match = MYPY_DIAGNOSTIC_RE.fullmatch(line)
        if match is None:
            raise WorkspaceError("baseline_mypy_output_malformed")
        raw_path = match.group("path").replace("\\", "/")
        path_parts = PurePosixPath(raw_path).parts
        if (
            not path_parts
            or raw_path.startswith("/")
            or ".." in path_parts
            or path_parts == (".",)
        ):
            raise WorkspaceError("baseline_mypy_path_invalid")
        path = PurePosixPath(*path_parts).as_posix()
        code = str(match.group("code") or "").strip()
        message = " ".join(match.group("message").split())
        if not code or not message:
            raise WorkspaceError("baseline_mypy_output_malformed")
        diagnostics[(path, code, message)] += 1
    if not saw_error:
        raise WorkspaceError("baseline_mypy_diagnostics_absent")
    return diagnostics


def baseline_validation_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return execution facts that must match before diagnostics compare."""
    return {
        "declared_command": str(
            row.get("declared_command") or row.get("command") or ""
        ),
        "declared_argv": list(row.get("declared_argv") or row.get("argv") or ()),
        "executed_argv": list(row.get("executed_argv") or row.get("argv") or ()),
        "interpreter_authority": row.get("interpreter_authority"),
        "sandbox_backend": row.get("sandbox_backend"),
        "execution_boundary": row.get("execution_boundary"),
        "cwd": row.get("cwd"),
        "env_override": row.get("env_override"),
        "timeout_seconds": row.get("timeout_seconds"),
    }


def diagnostic_multiset_digest(
    diagnostics: Counter[tuple[str, str, str]],
) -> str:
    payload = [
        {"path": key[0], "code": key[1], "message": key[2], "count": count}
        for key, count in sorted(diagnostics.items())
    ]
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
    ).hexdigest()


def compare_schema_mypy_baseline(
    workspace: WorkerWorkspace,
    authority: Mapping[str, Any],
    route_metadata: Mapping[str, Any],
    candidate: list[dict[str, Any]],
    *,
    create_workspace: WorkspaceCreator,
    cleanup_workspace: WorkspaceCleanup,
    run_validations: ValidationRunner,
    route_resolver: RouteResolver,
) -> list[dict[str, Any]]:
    """Compare candidate mypy diagnostics with its pinned-base execution."""
    failed = [
        row
        for row in candidate
        if row.get("timed_out") or row.get("returncode") != 0
    ]
    if not failed or any(
        str(row.get("behavioral_role") or "").lower() not in {"schema", "parity"}
        or not exact_schema_mypy_invocation(row)
        for row in failed
    ):
        raise WorkspaceError("baseline_comparison_ineligible")
    if not workspace.base_oid:
        raise WorkspaceError("baseline_base_oid_missing")
    adapter_id = str(route_metadata.get("adapter_id") or "").strip()
    if not adapter_id:
        raise WorkspaceError("baseline_validation_adapter_missing")
    baseline_card = dict(authority)
    baseline_card.pop("rework_predecessor", None)
    baseline_workspace: WorkerWorkspace | None = None
    try:
        baseline_workspace = create_workspace(
            workspace.repo,
            f"baseline_{uuid.uuid4().hex}",
            baseline_card,
            adapter_id,
            pinned_base_oid=workspace.base_oid,
        )
        for row in failed:
            command = str(row.get("declared_command") or row.get("command") or "")
            if not command:
                raise WorkspaceError("baseline_mypy_command_missing")
            try:
                baseline_rows = run_validations(
                    baseline_workspace, [command], **route_resolver(route_metadata)
                )
            except ValidationRunError as exc:
                baseline_rows = exc.results
            if len(baseline_rows) != 1:
                raise WorkspaceError("baseline_validation_receipt_count_mismatch")
            baseline_row = dict(baseline_rows[0])
            if baseline_validation_identity(row) != baseline_validation_identity(
                baseline_row
            ):
                raise WorkspaceError("baseline_validation_authority_mismatch")
            candidate_diagnostics = schema_mypy_diagnostics(row)
            baseline_diagnostics = schema_mypy_diagnostics(baseline_row)
            new_diagnostics = sorted(
                (candidate_diagnostics - baseline_diagnostics).elements()
            )
            outcome = (
                "baseline_no_new_diagnostics"
                if not new_diagnostics
                else "baseline_new_diagnostics"
            )
            row["baseline_comparison"] = {
                "schema_id": "aiworkhub.baseline_comparison.v1",
                "outcome": outcome,
                "base_oid": workspace.base_oid,
                "candidate_count": sum(candidate_diagnostics.values()),
                "baseline_count": sum(baseline_diagnostics.values()),
                "candidate_digest": diagnostic_multiset_digest(candidate_diagnostics),
                "baseline_digest": diagnostic_multiset_digest(baseline_diagnostics),
                "candidate_authority": baseline_validation_identity(row),
                "baseline_authority": baseline_validation_identity(baseline_row),
                "new_diagnostics": [list(value) for value in new_diagnostics],
            }
            if new_diagnostics:
                raise WorkspaceError("baseline_mypy_new_diagnostics")
        return candidate
    finally:
        if baseline_workspace is not None:
            cleanup_workspace(
                baseline_workspace.repo,
                baseline_workspace.path,
                baseline_workspace.home,
            )


def run_declared_validations(
    workspace: WorkerWorkspace,
    authority: Mapping[str, Any],
    route_metadata: Mapping[str, Any],
    *,
    run_validations: ValidationRunner,
    route_resolver: RouteResolver,
    baseline_comparer: Callable[..., list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    commands = declared_validation_commands(authority)
    if not commands:
        return []
    try:
        _work_kind, roles = quality_evidence.normalize_behavioral_contract(
            authority.get("work_kind"), commands, authority.get("validation_roles")
        )
    except ValueError as exc:
        raise WorkspaceError(str(exc)) from exc

    def with_roles(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        materialized = [dict(row) for row in rows]
        if len(materialized) != len(roles):
            raise WorkspaceError("validation_receipt_count_mismatch")
        for row, role in zip(materialized, roles, strict=True):
            row["behavioral_role"] = role
        return materialized

    try:
        results = run_validations(workspace, commands, **route_resolver(route_metadata))
    except ValidationRunError as exc:
        rows = with_roles(exc.results)
        if isinstance(exc, ValidationEnvironmentBlocked):
            exc.results = rows
            raise
        try:
            return baseline_comparer(workspace, authority, route_metadata, rows)
        except WorkspaceError as baseline_exc:
            raise ValidationRunError(
                f"{exc}:baseline_comparison_failed:{baseline_exc}", rows
            ) from baseline_exc
    return with_roles(results)


def enforce_behavioral_gate(
    authority: Mapping[str, Any],
    validations: Iterable[Mapping[str, Any]],
    quality_gate: dict[str, Any],
) -> dict[str, Any]:
    gate = quality_evidence.evaluate_behavioral_gate(authority, validations)
    quality_gate["behavioral_gate"] = gate
    if gate.get("applicable") and not gate.get("passed"):
        raise WorkspaceError(
            "behavioral_gate_failed:" + str(gate.get("reason") or "unknown")[:300]
        )
    return gate


def is_operational_validation_failure(terminal_state: str, error: str) -> bool:
    return terminal_state == "validation_failed" and (
        error.startswith("validation_exec_scratch_unavailable:")
        or error.startswith("validation_failed:validation_exec_scratch_unavailable:")
    )


VALIDATION_ENVIRONMENT_RESTRICTION_PREFIXES = (
    "validation_executable_unavailable:",
    "validation_pytest_runtime_unavailable:",
    "validation_pytest_runtime_missing_pytest:",
    "validation_exec_scratch_unavailable:",
    "unsupported_sandbox_backend:",
    "validation_unsupported_in_sandbox:",
)


def terminal_state_for_workspace_error(exc: WorkspaceError) -> str:
    if isinstance(exc, ValidationEnvironmentBlocked):
        return "finalize_failed"
    if isinstance(exc, ValidationRunError):
        return "validation_failed"
    error = str(exc)
    if error.startswith("scope_violation") or error.startswith("symlink_output"):
        return "scope_rejected"
    validation_failures = (
        "required_output",
        "quality_gate",
        "behavioral_gate",
        "residual_contract",
        "research_result",
    )
    if error.startswith(validation_failures):
        return "validation_failed"
    if error.startswith(("parent_changed", "promotion_scope")):
        return "promotion_conflict"
    if error.startswith(VALIDATION_ENVIRONMENT_RESTRICTION_PREFIXES):
        return "finalize_failed"
    if error.startswith("validation_") or error.startswith(
        "invalid_validation_command"
    ):
        return "validation_failed"
    return "finalize_failed"
