"""Pure and dependency-injected validation helpers for ``process_launcher``."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Any, Callable, Iterable, Mapping

from . import quality_evidence, validation_runner
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
CombinedWorkspaceFactory = Callable[..., tuple[WorkerWorkspace, dict[str, Any]]]

_VALIDATION_REPLAY_LOCK = Lock()
_VALIDATION_REPLAYS_IN_FLIGHT: set[tuple[str, str, str]] = set()

# --- NF-2026-01030: the manager-authorized host validation lane -------------
#
# A candidate whose declared validations fail only because the Windows
# AppContainer validation lane cannot do something (symlink WinError 1314,
# SemLock/fork-pool multiprocessing, dedicated-subprocess builds) terminalizes
# as ``validation_failed``/``finalize_failed`` and nothing the hub measures can
# move it: ``validation_runner.row_restriction`` deliberately refuses to
# attribute an in-test failure to the environment.  The remedy is therefore a
# MEASUREMENT in another lane -- never a classification of candidate stdout --
# so a manager opts ONE exact request in and the same declared commands, the
# same toolchain receipt and the same gates run again with the validation route
# forced to the host backend.
MANAGER_HOST_VALIDATION_LANE = "manager_host"
# ``""`` is today's behaviour, byte for byte. Any other value is refused by name.
VALIDATION_LANES: tuple[str, ...] = ("", MANAGER_HOST_VALIDATION_LANE)
# The one existing no-sandbox backend token: ``_sandbox_backend_for_adapter``
# already returns it for the editor-hosted routes and ``run_validations``
# already executes the card's exact shell-free argv under it.  Reused rather
# than restated, so this lane adds no second validation runner.
HOST_VALIDATION_LANE_BACKEND = _worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND
HOST_LANE_SEAL_UNVERIFIED = "host_lane_candidate_seal_unverified"
# The only states whose retained candidate is still the review surface, so the
# only ones a host-lane re-measurement can speak about.
HOST_LANE_RETRYABLE_STATES = frozenset({"validation_failed", "finalize_failed"})
PRIOR_LANE_FAILURE_SCHEMA_ID = "aiworkhub.prior_validation_lane_failure.v1"


def normalized_validation_lane(value: Any) -> str:
    """Return the empty default or the host lane; refuse any other by name."""
    lane = str(value or "").strip()
    if lane not in VALIDATION_LANES:
        raise WorkspaceError(f"validation_lane_unsupported:{lane[:120]}")
    return lane


def is_manager_host_lane(metadata: Mapping[str, Any]) -> bool:
    """Whether this exact request or route is recorded in the host lane."""
    return (
        normalized_validation_lane(metadata.get("validation_lane"))
        == MANAGER_HOST_VALIDATION_LANE
    )


def prior_lane_failure_digest(
    rows: Iterable[Mapping[str, Any]], *, backend: str
) -> str:
    """Digest exactly the failing rows of the lane this retry is leaving."""
    payload = {
        "schema_id": PRIOR_LANE_FAILURE_SCHEMA_ID,
        "backend": str(backend or ""),
        "rows": [
            {
                "command": str(row.get("command") or ""),
                "returncode": row.get("returncode"),
                "timed_out": bool(row.get("timed_out")),
                "restriction": str(row.get("restriction") or ""),
                "execution_boundary": str(row.get("execution_boundary") or ""),
            }
            for row in rows
            if isinstance(row, Mapping)
            and (row.get("timed_out") or row.get("returncode") not in (0, None))
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    ).hexdigest()


def host_lane_receipt_stamp(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """The lane fields every host-lane receipt row and event must carry.

    ``{}`` for a request without the opt-in, so a receipt row, a lifecycle
    event and a route produced outside the host lane stay byte-identical.
    """
    if not is_manager_host_lane(metadata):
        return {}
    return {
        "validation_lane": MANAGER_HOST_VALIDATION_LANE,
        "prior_validation_lane": str(metadata.get("prior_validation_lane") or ""),
        "prior_lane_failure_digest": str(
            metadata.get("prior_lane_failure_digest") or ""
        ),
    }


def host_lane_request_metadata(
    latest_event: Mapping[str, Any], metadata: Mapping[str, Any]
) -> dict[str, Any]:
    """The lane facts persisted on the request before finalization re-runs."""
    prior_backend = str(
        latest_event.get("sandbox_backend") or metadata.get("sandbox_backend") or ""
    )
    rows = latest_event.get("validation")
    return {
        "validation_lane": MANAGER_HOST_VALIDATION_LANE,
        "prior_validation_lane": prior_backend,
        "prior_lane_failure_digest": prior_lane_failure_digest(
            rows if isinstance(rows, list) else (), backend=prior_backend
        ),
    }


def retained_candidate_seal(
    card: Mapping[str, Any], request_id: str
) -> dict[str, Any]:
    """The seal THIS request recorded at terminalization, or ``{}``.

    ``retained_candidate_seal_evidence`` publishes it inside the terminal
    envelope the card carries -- ``terminal_review`` for a review-visible
    failure, ``terminal_failure`` otherwise.  The request identity is matched
    so a newer attempt's seal can never authorize an older one.
    """
    for key in ("terminal_review", "terminal_failure"):
        envelope = card.get(key)
        if not isinstance(envelope, Mapping):
            continue
        evidence = envelope.get("evidence")
        if not isinstance(evidence, Mapping):
            continue
        identity = evidence.get("request_identity")
        identity = identity if isinstance(identity, Mapping) else {}
        if request_id and request_id in {
            str(evidence.get("request_id") or ""),
            str(identity.get("request_id") or ""),
        }:
            return dict(evidence)
    return {}


def host_lane_seal_refusal(
    seal: Mapping[str, Any],
    observed_path_hashes: Callable[[list[str]], Mapping[str, str | None]],
    current_changed_paths: Iterable[str],
) -> str:
    """Empty when the retained bytes still match the seal, else the reason.

    Absent or incomplete evidence is never a pass: a request whose seal could
    not be taken at terminalization is refused here by the same name as one
    whose retained bytes have since drifted.  ``current_changed_paths`` is the
    retained workspace's changed set as the caller's own enumerator reads it
    NOW, and it is required rather than defaulted: a path that only started
    differing AFTER the seal was taken is absent from the seal, so the per-path
    comparison below could never see it and the host lane would re-measure
    bytes the seal does not speak for.
    """
    sealed = seal.get("changed_path_hashes")
    if not isinstance(sealed, Mapping) or not sealed:
        return f"{HOST_LANE_SEAL_UNVERIFIED}:seal_missing"
    paths = sorted(str(key) for key in sealed)
    if any(not isinstance(sealed[path], str) or not sealed[path] for path in paths):
        return f"{HOST_LANE_SEAL_UNVERIFIED}:seal_incomplete"
    unsealed = sorted({str(path) for path in current_changed_paths} - set(paths))
    if unsealed:
        return (
            f"{HOST_LANE_SEAL_UNVERIFIED}:unsealed_paths:" + ",".join(unsealed[:5])
        )
    observed = observed_path_hashes(paths)
    drifted = [path for path in paths if observed.get(path) != sealed[path]]
    if drifted:
        return (
            f"{HOST_LANE_SEAL_UNVERIFIED}:retained_bytes_changed:"
            + ",".join(drifted[:5])
        )
    return ""


def validation_route_kwargs(
    metadata: Mapping[str, Any],
    sandbox_backend_for_adapter: Callable[[str], str],
) -> dict[str, Any]:
    """Return the exact launch-bound validation route, failing on drift."""
    adapter_id = str(metadata.get("adapter_id") or "").strip()
    if not adapter_id:
        raise WorkspaceError("validation_route_adapter_missing")
    recorded_backend = str(metadata.get("sandbox_backend") or "").strip()
    execution_mode = str(metadata.get("execution_mode") or "").strip()
    route: dict[str, Any]
    if is_manager_host_lane(metadata):
        # NF-2026-01030: the manager authorized THIS request's rerun in the
        # host lane, so the launch-bound backend is history rather than drift
        # -- and the sandbox backend is not resolved at all, because the lane
        # exists precisely for hosts where resolving it is the problem.
        # ``worker_workspace.run_validations`` still owns which adapters may
        # execute under this token and refuses the rest by name: the lane
        # forces the route, it never widens that boundary.
        route = {
            "backend": HOST_VALIDATION_LANE_BACKEND,
            "adapter_id": adapter_id,
        }
        if execution_mode == "validation_only_replay":
            route["outer_validation_authority"] = True
        return route
    expected_backend = sandbox_backend_for_adapter(adapter_id)
    route = {"backend": expected_backend, "adapter_id": adapter_id}
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

    # NF-2026-01030: the lane that produced a row travels ON the row, so the
    # manager reads a measurement rather than inferring one.  Empty outside
    # the host lane, which keeps every existing receipt row unchanged.
    lane_stamp = host_lane_receipt_stamp(route_metadata)

    def with_roles(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        materialized = [dict(row) for row in rows]
        if len(materialized) != len(roles):
            raise WorkspaceError("validation_receipt_count_mismatch")
        for row, role in zip(materialized, roles, strict=True):
            row["behavioral_role"] = role
            row.update(lane_stamp)
        return materialized

    route = route_resolver(route_metadata)
    try:
        results = run_validations(workspace, commands, **route)
    except ValidationRunError as exc:
        rows = with_roles(exc.results)
        if isinstance(exc, ValidationEnvironmentBlocked):
            decision = validation_runner.plan_validation_capability_replay(
                commands,
                rows,
                backend=str(route.get("backend") or ""),
                already_replayed=bool(route_metadata.get("validation_capability_replayed")),
            )
            if not decision.replay or decision.profile is None:
                exc.results = rows
                if decision.reason.startswith("compatible_validation_lane_unavailable"):
                    raise ValidationEnvironmentBlocked(
                        f"validation_environment_blocked:{decision.reason}",
                        rows,
                        restriction=decision.reason,
                    ) from exc
                raise
            recorded_workspace = route_metadata.get("workspace")
            recorded_workspace = (
                recorded_workspace if isinstance(recorded_workspace, Mapping) else {}
            )
            if (
                str(route_metadata.get("request_id") or "") != workspace.request_id
                or str(recorded_workspace.get("request_id") or "") != workspace.request_id
                or str(recorded_workspace.get("path") or "") != str(workspace.path)
                or str(recorded_workspace.get("repo") or "") != str(workspace.repo)
            ):
                raise ValidationRunError(
                    "validation_failed:validation_capability_replay_identity_mismatch",
                    rows,
                ) from exc
            replay_identity = (
                workspace.request_id,
                str(workspace.path),
                str(workspace.repo),
            )
            with _VALIDATION_REPLAY_LOCK:
                if replay_identity in _VALIDATION_REPLAYS_IN_FLIGHT:
                    restriction = "validation_capability_replay_in_flight"
                    raise ValidationEnvironmentBlocked(
                        f"validation_environment_blocked:{restriction}",
                        rows,
                        restriction=restriction,
                        restrictions=(restriction,),
                    ) from exc
                _VALIDATION_REPLAYS_IN_FLIGHT.add(replay_identity)
            try:
                replay_route = dict(route)
                replay_route["outer_validation_authority"] = True
                try:
                    replayed = run_validations(workspace, commands, **replay_route)
                except ValidationRunError as replay_exc:
                    replay_rows = with_roles(replay_exc.results)
                    for row in replay_rows:
                        row["validation_capability_replay"] = {
                            "attempt": 1,
                            "backend": decision.profile.backend,
                            "profile": decision.profile.profile,
                            "capabilities": list(decision.profile.capabilities),
                            "original_denial": rows,
                            "request_identity": {
                                "request_id": str(route_metadata.get("request_id") or ""),
                                "task_id": str(route_metadata.get("task_id") or ""),
                                "workspace": str(workspace.path),
                                "repository": str(workspace.repo),
                            },
                        }
                    replay_exc.results = replay_rows
                    raise
                results = with_roles(replayed)
                for row in results:
                    row["validation_capability_replay"] = {
                        "attempt": 1,
                        "backend": decision.profile.backend,
                        "profile": decision.profile.profile,
                        "capabilities": list(decision.profile.capabilities),
                        "original_denial": rows,
                        "request_identity": {
                            "request_id": str(route_metadata.get("request_id") or ""),
                            "task_id": str(route_metadata.get("task_id") or ""),
                            "workspace": str(workspace.path),
                            "repository": str(workspace.repo),
                        },
                    }
                return results
            finally:
                with _VALIDATION_REPLAY_LOCK:
                    _VALIDATION_REPLAYS_IN_FLIGHT.discard(replay_identity)
        try:
            return baseline_comparer(workspace, authority, route_metadata, rows)
        except WorkspaceError as baseline_exc:
            raise ValidationRunError(
                f"{exc}:baseline_comparison_failed:{baseline_exc}", rows
            ) from baseline_exc
    return with_roles(results)


def run_full_snapshot_validations(
    workspace: WorkerWorkspace,
    authority: Mapping[str, Any],
    route_metadata: Mapping[str, Any],
    candidate_changed_paths: Iterable[str],
    *,
    create_snapshot: CombinedWorkspaceFactory,
    cleanup_workspace: WorkspaceCleanup,
    run_validations: ValidationRunner,
    route_resolver: RouteResolver,
    baseline_comparer: Callable[..., list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run validation against the complete canonical-plus-candidate snapshot."""

    candidate = sorted({str(value) for value in candidate_changed_paths})
    if not candidate:
        raise WorkspaceError("full_snapshot_candidate_empty")
    if not declared_validation_commands(authority):
        return [], {
            "schema_id": "aiworkhub.full_validation_snapshot.v1",
            "applicable": False,
            "reason": "no_declared_validations",
            "request_id": workspace.request_id,
            "repo": str(workspace.repo),
            "source_base_oid": workspace.base_oid,
            "candidate_changed_paths": candidate,
        }
    snapshot, combined_tree = create_snapshot(workspace, authority, candidate)
    try:
        validations = run_declared_validations(
            snapshot,
            authority,
            route_metadata,
            run_validations=run_validations,
            route_resolver=route_resolver,
            baseline_comparer=baseline_comparer,
        )
        return validations, {
            "schema_id": "aiworkhub.full_validation_snapshot.v1",
            "request_id": workspace.request_id,
            "repo": str(workspace.repo),
            "source_base_oid": workspace.base_oid,
            "snapshot_base_oid": snapshot.base_oid,
            "combined_tree": combined_tree,
        }
    finally:
        cleanup_workspace(snapshot.repo, snapshot.path, snapshot.home)


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
    # promotion_merge_* errors (conflict / changed since validation / parent changed) are promotion conflicts (NF-2026-01381).
    if error.startswith(("parent_changed", "promotion_scope", "promotion_merge")):
        return "promotion_conflict"
    if error.startswith(VALIDATION_ENVIRONMENT_RESTRICTION_PREFIXES):
        return "finalize_failed"
    if error.startswith("validation_") or error.startswith(
        "invalid_validation_command"
    ):
        return "validation_failed"
    return "finalize_failed"


def replay_terminal_state(
    terminal_state: str,
    metadata: Mapping[str, Any],
    validations: list[dict[str, Any]],
) -> str:
    """Reclassify an unrun validation-only replay from validation_failed."""
    if terminal_state != "validation_failed":
        return terminal_state
    if metadata.get("execution_mode") != "validation_only_replay":
        return terminal_state
    try:
        declared = declared_validation_commands(metadata)
    except WorkspaceError:
        return terminal_state
    if not declared or list(validations):
        return terminal_state
    return "finalize_failed"
