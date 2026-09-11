"""One isolated worker launch, extracted from ``ProcessManager``.

``ProcessManager._launch_isolated`` is a thin delegation to
:func:`launch_isolated` below.  The move is byte-for-byte apart from the
four-space dedent a method-to-function move forces and eight annotations named
in ``LAUNCH_ISOLATED_ANNOTATION_ALIASES``; ``self`` is still the
:class:`ProcessManager` instance, so every ``self.`` collaborator resolves
exactly as before.  Module level collaborators are re-bound from the
``process_launcher`` module object on entry rather than imported here, because
the test suite monkeypatches that module and a name captured at import time
would leave those patches pointing at code this function no longer calls --
silently, with the tests still green.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # names used only in annotations, which are never evaluated
    from pathlib import Path as _PathT

    from .vscode_lm_bridge import BridgeRequest as _BridgeRequestT
    from .worker_workspace import WorkerWorkspace as _WorkerWorkspaceT

__all__ = ["launch_isolated"]

# Eight local-variable annotations in the moved body named ``Path``,
# ``WorkerWorkspace`` and ``vscode_lm_bridge`` -- module-level imports in
# ``process_launcher`` that the seam preamble below turns into function locals.
# A variable is not valid as a type, so each was re-pointed at the alias above.
# ``from __future__ import annotations`` leaves annotations as strings and a
# local variable annotation is never evaluated at runtime either way, so this is
# a type-checker-only change with no runtime effect whatsoever.
LAUNCH_ISOLATED_ANNOTATION_ALIASES: tuple[str, ...] = (
    "_BridgeRequestT",
    "_PathT",
    "_WorkerWorkspaceT",
)

# The seams are runtime names only.  ``Any`` and ``Callable`` appear solely in
# annotations -- which are never evaluated -- so they are resolved from this
# module's own imports and are deliberately NOT re-bound below.  A name that is
# never evaluated cannot carry a monkeypatch, and shadowing it here would only
# hide it from the type checker.
LAUNCH_ISOLATED_SEAM_NAMES: tuple[str, ...] = (
    "LAUNCH_IMPLEMENTED",
    "LaunchRejected",
    "MAX_WORKER_STREAM_LOG_BYTES",
    "Mapping",
    "Path",
    "VSCODE_LM_IN_PROCESS_BACKEND",
    "WorkerWorkspace",
    "WorkspaceError",
    "_LiveProcess",
    "_ReviewerReservationTerminalized",
    "_VSCODE_LM_IN_PROCESS_ADAPTERS",
    "_appcontainer_supervisor_identity",
    "_bounded_launch_diagnostic",
    "_committed_claim_card",
    "_enforce_quality_review_launch_binding",
    "_external_readonly_dirs",
    "_launch_project_context",
    "_launch_source_graph_request",
    "_legacy_timeout_fields",
    "_materialize_crash_retry_packet",
    "_materialize_worker_rework_overlay",
    "_memory_launch_admission",
    "_path_manifest",
    "_pid_start_ticks",
    "_project_context_delivery",
    "_provision_worker_mcp_runtime_for_authority",
    "_release_launch_request_resources",
    "_sandbox_backend_for_adapter",
    "_task_authority_repo",
    "_terminate_process_group",
    "_toolchain_authority",
    "_touch_0600",
    "_utcnow",
    "_validate_adapter_identity",
    "_validation_only_replay_authorization",
    "_vscode_lm_worker_env",
    "_worker_launch_cwd",
    "_worker_mcp_session_topic",
    "_worker_mcp_source_graph_targets",
    "_worker_supervisor_script",
    "_write_terminal_authority_grant",
    "build_residual_contract_manifest",
    "build_worker_prompt",
    "chmod_path",
    "cleanup_workspace",
    "core",
    "create_quality_review_workspace",
    "create_workspace",
    "hashlib",
    "json",
    "kilo_auth",
    "launch_gates_open",
    "nullcontext",
    "os",
    "process_group_launch_kwargs",
    "project_context",
    "quality_review",
    "runtime_adapters",
    "sandbox_argv",
    "subprocess",
    "sys",
    "task_engine",
    "threading",
    "unlink_if_regular",
    "uuid",
    "validate_workforce_identity",
    "vscode_lm_bridge",
    "worker_ai_tools_mcp",
    "worker_launch_env",
    "write_json_0600",
)

# Names declared above that are genuinely new to this module rather than
# re-bound from ``process_launcher``: ``_appcontainer_supervisor_identity`` is
# defined below and is a free variable of ``launch_isolated`` like every other
# seam, but it has no counterpart on ``process_launcher`` to read at call time
# and it is expected to already be an attribute of this module -- both things
# that would be drift for every other seam in ``LAUNCH_ISOLATED_SEAM_NAMES``.
LAUNCH_ISOLATED_LOCAL_SEAM_NAMES: tuple[str, ...] = (
    "_appcontainer_supervisor_identity",
)


def _appcontainer_supervisor_identity(
    *, repo_id: str, worker_kind: str, platform: str
) -> dict[str, str]:
    """Shape the AppContainer identity fields carried into the supervisor spec.

    Only the ``win32`` path adds ``backend`` and normalizes ``worker_kind``;
    every other platform passes both values through unchanged so editor-hosted
    and non-Windows behaviour is untouched.
    """
    if platform != "win32":
        return {"repo_id": repo_id, "worker_kind": worker_kind}
    if not repo_id:
        raise ValueError("repo_id must be a non-empty string for windows_appcontainer")
    normalized_kind = "_".join(worker_kind.lower().replace("-", "").split())
    if not normalized_kind:
        raise ValueError("worker_kind must be a non-empty string for windows_appcontainer")
    return {
        "backend": "windows_appcontainer",
        "repo_id": repo_id,
        "worker_kind": normalized_kind,
    }


def launch_isolated(
    self,
    *,
    task_id: str,
    runner: str,
    topic: str,
    adapter_id: str,
    model: str | None,
    owner_prompt: str,
    timeout_seconds: int,
    quality_review_binding: dict[str, Any] | None = None,
    reserved_request_id: str | None = None,
    prewarm_progress: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Start one worker in an isolated worktree, or refuse without claiming.

    The implementation is the one that lived in ``ProcessManager`` as
    ``_launch_isolated``; the contract, the ordering of every gate, and every
    receipt it returns are unchanged by the move.  ``self`` is the
    :class:`~aiworkhub.process_launcher.ProcessManager`.
    """
    # Every module-level name this function used while it lived in
    # ``process_launcher`` is re-bound here from that exact module object, so a
    # ``monkeypatch.setattr(process_launcher, name, ...)`` seam still reaches the
    # code below.  The list is the function's complete free-variable set and is
    # asserted against it by ``test_process_launcher_launch_isolated.py``;
    # nothing in the body was rewritten to get here.
    from . import process_launcher as _pl

    LAUNCH_IMPLEMENTED = _pl.LAUNCH_IMPLEMENTED
    LaunchRejected = _pl.LaunchRejected
    MAX_WORKER_STREAM_LOG_BYTES = _pl.MAX_WORKER_STREAM_LOG_BYTES
    Mapping = _pl.Mapping
    Path = _pl.Path
    VSCODE_LM_IN_PROCESS_BACKEND = _pl.VSCODE_LM_IN_PROCESS_BACKEND
    WorkerWorkspace = _pl.WorkerWorkspace
    WorkspaceError = _pl.WorkspaceError
    _LiveProcess = _pl._LiveProcess
    _ReviewerReservationTerminalized = _pl._ReviewerReservationTerminalized
    _VSCODE_LM_IN_PROCESS_ADAPTERS = _pl._VSCODE_LM_IN_PROCESS_ADAPTERS
    _bounded_launch_diagnostic = _pl._bounded_launch_diagnostic
    _committed_claim_card = _pl._committed_claim_card
    _enforce_quality_review_launch_binding = _pl._enforce_quality_review_launch_binding
    _external_readonly_dirs = _pl._external_readonly_dirs
    _launch_project_context = _pl._launch_project_context
    _launch_source_graph_request = _pl._launch_source_graph_request
    _legacy_timeout_fields = _pl._legacy_timeout_fields
    _materialize_crash_retry_packet = _pl._materialize_crash_retry_packet
    _materialize_worker_rework_overlay = _pl._materialize_worker_rework_overlay
    _memory_launch_admission = _pl._memory_launch_admission
    _path_manifest = _pl._path_manifest
    _pid_start_ticks = _pl._pid_start_ticks
    _project_context_delivery = _pl._project_context_delivery
    _provision_worker_mcp_runtime_for_authority = _pl._provision_worker_mcp_runtime_for_authority
    _release_launch_request_resources = _pl._release_launch_request_resources
    _sandbox_backend_for_adapter = _pl._sandbox_backend_for_adapter
    _task_authority_repo = _pl._task_authority_repo
    _terminate_process_group = _pl._terminate_process_group
    _toolchain_authority = _pl._toolchain_authority
    _touch_0600 = _pl._touch_0600
    _utcnow = _pl._utcnow
    _validate_adapter_identity = _pl._validate_adapter_identity
    _validation_only_replay_authorization = _pl._validation_only_replay_authorization
    _vscode_lm_worker_env = _pl._vscode_lm_worker_env
    _worker_launch_cwd = _pl._worker_launch_cwd
    _worker_mcp_session_topic = _pl._worker_mcp_session_topic
    _worker_mcp_source_graph_targets = _pl._worker_mcp_source_graph_targets
    _worker_supervisor_script = _pl._worker_supervisor_script
    _write_terminal_authority_grant = _pl._write_terminal_authority_grant
    build_residual_contract_manifest = _pl.build_residual_contract_manifest
    build_worker_prompt = _pl.build_worker_prompt
    chmod_path = _pl.chmod_path
    cleanup_workspace = _pl.cleanup_workspace
    core = _pl.core
    create_quality_review_workspace = _pl.create_quality_review_workspace
    create_workspace = _pl.create_workspace
    hashlib = _pl.hashlib
    json = _pl.json
    kilo_auth = _pl.kilo_auth
    launch_gates_open = _pl.launch_gates_open
    nullcontext = _pl.nullcontext
    os = _pl.os
    process_group_launch_kwargs = _pl.process_group_launch_kwargs
    project_context = _pl.project_context
    quality_review = _pl.quality_review
    runtime_adapters = _pl.runtime_adapters
    sandbox_argv = _pl.sandbox_argv
    subprocess = _pl.subprocess
    sys = _pl.sys
    task_engine = _pl.task_engine
    threading = _pl.threading
    unlink_if_regular = _pl.unlink_if_regular
    uuid = _pl.uuid
    validate_workforce_identity = _pl.validate_workforce_identity
    vscode_lm_bridge = _pl.vscode_lm_bridge
    worker_ai_tools_mcp = _pl.worker_ai_tools_mcp
    worker_launch_env = _pl.worker_launch_env
    write_json_0600 = _pl.write_json_0600

    if not launch_gates_open():
        return self._blocked(
            task_id, runner, topic, adapter_id,
            "dual_gate_closed: require AIWORKHUB_ALLOW_LAUNCH=1 and AIWORKHUB_ALLOW_WRITES=1",
        )
    if timeout_seconds < 30 or timeout_seconds > 86_400:
        return self._blocked(task_id, runner, topic, adapter_id, "timeout_out_of_range")

    request_id: str | None = None
    workspace: _WorkerWorkspaceT | None = None
    spec_path: _PathT | None = None
    authority_path: _PathT | None = None
    bridge_request: _BridgeRequestT | None = None
    residual_contract_manifest: list[dict[str, Any]] = []
    claimed = False
    provider_env: dict[str, str] | None = None
    kilo_auth_source: _PathT | None = None
    kilo_auth_evidence: dict[str, Any] | None = None
    launch_phase = "preflight"

    def _abandon_terminalized_reviewer() -> dict[str, Any]:
        # A stale pre-provider owner discovered its exact ``starting``
        # reservation was already terminalized.  Clean up partial artifacts
        # and return a bounded non-ok receipt WITHOUT appending any event,
        # so terminalization stays exactly-once and a live/terminalized
        # reservation is never stolen.
        if workspace is not None:
            try:
                cleanup_workspace(workspace.repo, workspace.path, workspace.home)
            except WorkspaceError:
                pass
        if spec_path is not None:
            unlink_if_regular(spec_path)
        if authority_path is not None:
            unlink_if_regular(authority_path)
        if bridge_request is not None:
            vscode_lm_bridge.cancel_request(bridge_request)
        claimed_task_transition = "not_claimed"
        if claimed:
            # The reservation is somebody else's to terminalize, but the
            # CARD this launch claimed is not: returning here without a
            # transition leaves it ``processing`` under a claim no live
            # owner holds, and nothing else brings a later pass back to
            # it.  The reservation event stays untouched -- this moves the
            # exact claimed task, once, to the truthful failure state.
            claimed_task_transition = "launch_failed"
            failed = task_engine.mark_launch_failed(
                self.repo,
                task_id,
                runner,
                reason="quality_review_reservation_terminalized",
                request_id=request_id or reserved_request_id or "",
            )
            if not failed.get("ok"):
                # Naming the refusal keeps a still-processing card visible
                # in the receipt instead of the caller reading a bounded
                # blocked reason as proof the claim was released.
                claimed_task_transition = (
                    "launch_failure_transition_failed:"
                    + str(failed.get("stderr") or failed.get("stdout") or "")[:200]
                )
        return {
            "ok": False,
            "launch_implemented": LAUNCH_IMPLEMENTED,
            "launch_enabled": True,
            "request_id": reserved_request_id,
            "task_id": task_id,
            "runner": runner,
            "topic": topic,
            "adapter_id": adapter_id,
            "state": "blocked",
            "blocked_reason": "quality_review_reservation_terminalized",
            "claimed_task_transition": claimed_task_transition,
            "shell": False,
        }

    try:
        if (
            reserved_request_id is not None
            and not self._reviewer_reservation_still_held(reserved_request_id)
        ):
            raise _ReviewerReservationTerminalized(reserved_request_id)
        _validate_adapter_identity(runner, adapter_id)
        # Materialize completed dependencies' promoted (accepted-but-not-yet-
        # committed) outputs into this dependent's isolated worktree by
        # declaring them as immutable inputs before create_workspace and the
        # B919 input-drift snapshot see the card.
        if reserved_request_id is not None:
            card = self._preflight_card(
                task_id,
                runner,
                topic,
                adapter_id,
                reserved_request_id=reserved_request_id,
            )
        else:
            card = self._preflight_card(task_id, runner, topic, adapter_id)
        request_id = str(
            card.get("request_id") or reserved_request_id or uuid.uuid4().hex
        )
        card.setdefault("request_id", request_id)
        preflight_card = dict(card)
        claimed = core._lifecycle_state(card) == "processing"
        _enforce_quality_review_launch_binding(topic, quality_review_binding)

        card = self._with_dependency_inputs(card)
        replay_authorization = _validation_only_replay_authorization(
            card, task_id
        )
        if replay_authorization is not None:
            return self._launch_validation_only_replay(
                task_id=task_id,
                runner=runner,
                topic=topic,
                adapter_id=adapter_id,
                model=model,
                timeout_seconds=timeout_seconds,
                card=card,
                authorization=replay_authorization,
                request_id=request_id,
            )
        model = validate_workforce_identity(
            runner,
            adapter_id,
            model,
            risk_tier=card.get("risk_tier"),
            repo=self.repo,
        )
        memory_admission = _memory_launch_admission()
        if not memory_admission["admit"]:
            raise LaunchRejected(
                "memory_launch_capacity_denied:"
                + json.dumps(
                    memory_admission, sort_keys=True, separators=(",", ":")
                )
            )
        external_readonly_dirs = _external_readonly_dirs(card, adapter_id)
        authority_repo = _task_authority_repo(self.repo, card)
        context_result = _launch_project_context(
            self.repo, card, quality_review_binding
        )
        # Load the BYOK credential (deepseek_copilot_cli) BEFORE claim-start.
        # A missing/invalid credential raises here, leaving the task
        # pending/unclaimed -- never claim on a missing credential.
        provider_env, model = self._resolve_provider_env(adapter_id, model)
        if adapter_id == runtime_adapters.GROK_KILO_ADAPTER:
            try:
                kilo_auth_source = kilo_auth.resolve_kilo_auth_source(
                    home=Path.home(),
                    xdg_data_home=os.environ.get("XDG_DATA_HOME") or None,
                    platform_name=os.name,
                )
            except kilo_auth.KiloAuthError as exc:
                raise LaunchRejected(
                    f"grok_kilo_auth_unavailable:{exc.reason}"
                ) from exc
        sandbox_backend = _sandbox_backend_for_adapter(adapter_id)
        launch_phase = "workspace_and_runtime_provision"
        reservation_ctx = (
            nullcontext()
            if reserved_request_id is not None
            else self._launch_reservation({
                "request_id": request_id,
                "task_id": task_id,
                "runner": runner,
                "topic": topic,
                "adapter_id": adapter_id,
                "model": model,
                "timeout_seconds": timeout_seconds,
                "authority": f"coordinator_claim_isolated_worktree_{sandbox_backend}",
                "sandbox_backend": sandbox_backend,
                "project_context": (
                    context_result.metadata if context_result is not None else None
                ),
            })
        )
        with reservation_ctx:
            self.process_dir.mkdir(parents=True, exist_ok=True)
            chmod_path(self.process_dir, 0o700)
            stdout_path = self.process_dir / f"{request_id}.stdout.log"
            stderr_path = self.process_dir / f"{request_id}.stderr.log"
            status_path = self.process_dir / f"{request_id}.supervisor.json"
            cancel_path = self.process_dir / f"{request_id}.cancel.json"
            metadata_path = self.process_dir / f"{request_id}.request.json"
            spec_path = self.process_dir / f"{request_id}.supervisor-spec.json"
            _touch_0600(stdout_path)
            _touch_0600(stderr_path)

            review_packet_path: _PathT | None = None
            review_workspace_evidence: dict[str, Any] | None = None
            rework_overlay_path: _PathT | None = None
            rework_overlay_packet: dict[str, Any] | None = None
            crash_retry_packet_path: _PathT | None = None
            crash_retry_packet: dict[str, Any] | None = None
            if quality_review_binding is not None:
                source_workspace = WorkerWorkspace.from_metadata(
                    dict(quality_review_binding["source_workspace"])
                )
                workspace, review_workspace_evidence = create_quality_review_workspace(
                    source_workspace,
                    request_id,
                    quality_review_binding["candidate_paths"],
                    adapter_id,
                    quality_review_binding.get("read_only_input_paths") or (),
                )
                review_packet_path = (
                    workspace.home
                    / "task_mcp_worker_runtime"
                    / "quality_review_packet.json"
                )
                # The packet written into the reviewer's runtime is the exact
                # bytes packet_read, submit and the receipt verifier bind to.
                # It must carry this lens's scope and no other: the launcher
                # derives it with ``quality_reviewer.build_lens_packet`` and a
                # packet still carrying every lens here is a contract error,
                # not a bigger file to ship.  A packet without scoped audits
                # (synthetic fixtures, no Source Graph scope) has nothing to
                # check.
                review_packet = dict(quality_review_binding["packet"])
                review_candidate = review_packet.get("candidate")
                review_scopes = (
                    review_candidate.get("scoped_audits")
                    if isinstance(review_candidate, Mapping)
                    else None
                )
                if isinstance(review_scopes, Mapping) and set(review_scopes) != {
                    str(quality_review_binding["lens"])
                }:
                    raise LaunchRejected(
                        "quality_review_packet_lens_scope_mismatch:"
                        + ",".join(sorted(str(key) for key in review_scopes))[:200]
                    )
                write_json_0600(review_packet_path, review_packet)
                launch_phase = "quality_review_source_graph_prewarm"
                try:
                    worker_ai_tools_mcp.verify_quality_review_prewarm_authority(
                        authority_repo
                    )
                except worker_ai_tools_mcp.WorkerToolError as exc:
                    raise LaunchRejected(
                        "quality_review_source_graph_authority_unverified:"
                        + str(exc)[:240]
                    ) from exc
                if prewarm_progress is not None:
                    prewarm_progress("reviewer_source_graph_prewarm_started")
                try:
                    worker_ai_tools_mcp.prewarm_quality_review_source_graph(
                        review_packet_path,
                        repo=workspace.path,
                        authority_repo=authority_repo,
                    )
                except worker_ai_tools_mcp.WorkerToolError as exc:
                    # Prewarm prebuilds the reviewer's candidate Source Graph
                    # overlay only to make its queries fast; it is an
                    # optimisation, never a correctness precondition, because
                    # the sealed review packet already carries every
                    # candidate's content.  A path Source Graph *deliberately*
                    # does not index -- an eval artifact, a generated fixture,
                    # an unsupported extension -- is therefore skipped and
                    # recorded, and the reviewer still launches from the
                    # packet alone.  Every other prewarm failure (a hash
                    # mismatch against the sealed packet, an
                    # unreadable-but-indexable file, a path-safety violation,
                    # a clone/backup I/O error) is a genuine problem on a file
                    # that SHOULD be indexable and is re-raised loudly so it
                    # is never swallowed.
                    skip = quality_review.classify_prewarm_error(str(exc))
                    if not skip["tolerated"]:
                        if prewarm_progress is not None:
                            prewarm_progress(
                                "reviewer_source_graph_prewarm_failed"
                            )
                        raise LaunchRejected(
                            "quality_review_source_graph_prewarm_failed:"
                            + str(exc)[:240]
                        ) from exc
                    if prewarm_progress is not None:
                        prewarm_progress(
                            "reviewer_source_graph_prewarm_skipped_excluded",
                            skip["reason"],
                        )
                else:
                    if prewarm_progress is not None:
                        prewarm_progress(
                            "reviewer_source_graph_prewarm_complete"
                        )
                # Any further exception in this block belongs to worker MCP
                # runtime registration, not the reviewer Source Graph
                # prewarm this block just completed -- restore the phase so
                # an unexpected failure there is never misclassified as a
                # prewarm contract/data failure.
                launch_phase = "workspace_and_runtime_provision"
            else:
                workspace = create_workspace(self.repo, request_id, card, adapter_id)
                residual_contract_manifest = build_residual_contract_manifest(
                    workspace, card
                )
                (
                    rework_overlay_path,
                    rework_overlay_packet,
                ) = _materialize_worker_rework_overlay(
                    workspace,
                    task_id=task_id,
                    card=card,
                )
                (
                    crash_retry_packet_path,
                    crash_retry_packet,
                ) = _materialize_crash_retry_packet(
                    self.process_dir,
                    workspace,
                    task_id=task_id,
                    card=card,
                    rework_overlay_packet=rework_overlay_packet,
                )
            if adapter_id == runtime_adapters.GROK_KILO_ADAPTER:
                if kilo_auth_source is None:
                    raise LaunchRejected("grok_kilo_auth_unavailable:source_unresolved")
                try:
                    projection = kilo_auth.project_xai_auth(
                        kilo_auth_source, workspace.home
                    )
                except kilo_auth.KiloAuthError as exc:
                    # Never include the coordinator auth path or provider
                    # record in launch evidence.  The typed reason is
                    # sufficient for a bounded pre-claim failure.
                    raise LaunchRejected(
                        f"grok_kilo_auth_unavailable:{exc.reason}"
                    ) from exc
                kilo_auth_evidence = {
                    "provider": projection.provider,
                    "status": projection.status,
                    "destination_bytes": projection.destination_bytes,
                    "destination_sha256": projection.destination_sha256,
                }
            worker_source_graph_targets = _worker_mcp_source_graph_targets(context_result)
            worker_session_topic = _worker_mcp_session_topic(context_result, topic)
            worker_mcp_runtime = _provision_worker_mcp_runtime_for_authority(
                workspace,
                request_id=request_id,
                task_id=task_id,
                runner=runner,
                topic=topic,
                backend=sandbox_backend,
                authority_repo=authority_repo,
                source_graph_targets=worker_source_graph_targets,
                allowed_writes=[str(value) for value in card.get("allowed_writes") or []],
                session_topic=worker_session_topic,
                quality_review_packet_path=review_packet_path,
                rework_overlay_path=rework_overlay_path,
            )
            vscode_source_graph_request = _launch_source_graph_request(
                card, quality_review_binding
            )
            vscode_source_graph_result: dict[str, Any] | None = None
            if (
                adapter_id in _VSCODE_LM_IN_PROCESS_ADAPTERS
                and isinstance(vscode_source_graph_request, dict)
                and vscode_source_graph_request.get("query")
            ):
                # Execute the mandatory orientation query before the
                # request enters the editor-host spool.  Calling back over
                # the coordinator's single MCP stdio connection from four
                # concurrent VS Code LM workers caused head-of-line
                # blocking and 90-second bootstrap timeouts.  This uses
                # the exact same immutable worker identity, target scope,
                # HMAC ledger and canonical authority as later live tool
                # calls, so the receipt remains genuine worker-scoped
                # evidence.  Only the first query is prefetched; every
                # implementation/review re-query still uses live MCP.
                source_graph_input = {
                    key: vscode_source_graph_request[key]
                    for key in (
                        "mode", "query", "budget", "target",
                        "bundle_type", "workflow_stage",
                    )
                    if vscode_source_graph_request.get(key) is not None
                }
                source_graph_input.setdefault("mode", "focus")
                source_graph_input.setdefault("workflow_stage", "orientation")
                if rework_overlay_packet is not None:
                    try:
                        worker_ai_tools_mcp._verify_rework_overlay_packet(
                            rework_overlay_packet,
                            task_id,
                            request_id,
                            runner,
                            authority_repo,
                        )
                    except worker_ai_tools_mcp.WorkerToolError as exc:
                        raise LaunchRejected(
                            "vscode_lm_initial_source_graph_prefetch_failed:"
                            + str(exc)[:300]
                        ) from exc
                prefetch_ctx = worker_ai_tools_mcp.WorkerToolContext(
                    task_id=task_id,
                    runner=runner,
                    topic=topic,
                    request_id=request_id,
                    repo=workspace.path,
                    authority_repo=authority_repo,
                    source_graph_targets=tuple(worker_source_graph_targets),
                    allowed_writes=tuple(
                        str(value) for value in card.get("allowed_writes") or []
                    ),
                    session_topic=worker_session_topic,
                    audit_ledger_path=worker_mcp_runtime.audit_ledger_path,
                    audit_hmac_key_path=worker_mcp_runtime.audit_hmac_key_path,
                    quality_review_packet_path=review_packet_path,
                    rework_overlay_packet=rework_overlay_packet,
                    rework_overlay_packet_path=rework_overlay_path,
                    provenance="prefetch",
                )
                try:
                    vscode_source_graph_result = worker_ai_tools_mcp.source_graph_query(
                        prefetch_ctx,
                        **source_graph_input,
                    )
                except worker_ai_tools_mcp.WorkerToolError as exc:
                    raise LaunchRejected(
                        "vscode_lm_initial_source_graph_prefetch_failed:"
                        + str(exc)[:300]
                    ) from exc
                if vscode_source_graph_result.get("ok") is not True:
                    reason = str(
                        vscode_source_graph_result.get("reason")
                        or vscode_source_graph_result.get("error")
                        or "source_graph_result_not_ok"
                    )
                    raise LaunchRejected(
                        "vscode_lm_initial_source_graph_prefetch_failed:"
                        + reason[:300]
                    )
            launch_phase = "prompt_and_adapter_plan"
            if quality_review_binding is not None:
                private_tool_name = "aiworkhub_worker_quality_review_submit"
                prompt = quality_review.assemble_reviewer_prompt(
                    quality_review_binding["packet"],
                    lens=str(quality_review_binding["lens"]),
                    adapter_id=adapter_id,
                    submit_tool_name=private_tool_name,
                    packet_path=(
                        str(review_packet_path) if review_packet_path is not None else None
                    ),
                    packet_root=workspace.home / "task_mcp_worker_runtime",
                    # NF-2026-00667.  The ONE launcher-side line of the
                    # bounded schema-repair turn.  ``core.retry_terminal_
                    # task`` records the refusal that produced THIS launch
                    # on the card it requeued and clears every other
                    # terminal field, so ``terminal_retry.reason`` is
                    # exactly "why the previous attempt at this review was
                    # thrown away" and nothing else -- which is why the
                    # repair is naturally one-shot and needs no new
                    # counter. Deriving the ask stays in
                    # ``quality_review_ingest``, the module that refused
                    # the answer; this only hands it the reason.
                    prior_rejection=str(
                        (card.get("terminal_retry") or {}).get("reason") or ""
                    ),
                )
                prompt_budget = {
                    "schema_id": "aiworkhub.worker_prompt_budget.v1",
                    "mode": "quality_review",
                    "total_bytes": len(prompt.encode("utf-8")),
                    "byte_labels_are_token_truth": False,
                }
            else:
                prompt_budget = {}
                prompt = build_worker_prompt(
                    task_id=task_id,
                    runner=runner,
                    topic=topic,
                    request_id=request_id,
                    card=card,
                    owner_prompt=owner_prompt,
                    project_context_bundle=(
                        context_result.prompt_bundle if context_result is not None else ""
                    ),
                    crash_retry_packet=crash_retry_packet,
                    adapter_id=adapter_id,
                    _budget_report=prompt_budget,
                )
            include_partial_messages = (
                adapter_id == "claude_cli"
                and isinstance(card.get("token_budget"), dict)
                and bool(card["token_budget"])
            )
            if adapter_id in _VSCODE_LM_IN_PROCESS_ADAPTERS:
                bridge_request = vscode_lm_bridge.create_request(
                    repo=self.repo,
                    request_id=request_id,
                    workspace_path=workspace.path,
                    workspace_home=workspace.home,
                    prompt=prompt,
                    model=str(model or runtime_adapters.GLM_DEFAULT_MODEL),
                    allowed_writes=workspace.allowed_writes,
                    workspace_parent_baseline=workspace.parent_baseline,
                    timeout_seconds=timeout_seconds,
                    source_graph_request=vscode_source_graph_request,
                    source_graph_result=vscode_source_graph_result,
                    request_kind=(
                        "quality_review"
                        if quality_review_binding is not None
                        else "worker"
                    ),
                )
                plan = runtime_adapters.RuntimeAdapterPlan(
                    adapter_id=adapter_id,
                    argv=[sys.executable, "-m", "aiworkhub.vscode_lm_worker"],
                    cwd=str(workspace.path),
                    executable=sys.executable,
                    launchable=True,
                    manual_only=False,
                    validation_ok=True,
                    validation_reason="",
                )
                provider_env = _vscode_lm_worker_env(
                    provider_env,
                    worker_mcp_runtime.package_import_root,
                )
            else:
                plan = self._build_adapter(
                    adapter_id=adapter_id,
                    prompt=prompt,
                    repo=workspace.path,
                    model=model,
                    outer_sandbox_backend=sandbox_backend,
                    additional_readonly_dirs=external_readonly_dirs,
                    include_partial_messages=include_partial_messages,
                    # A read-only card gets a read-only toolset.
                    read_only=bool(card.get("read_only")),
                )
            if not getattr(plan, "launchable", False):
                reason = getattr(plan, "reason", "adapter_not_launchable")
                raise LaunchRejected(reason or "adapter_not_launchable")
            if isinstance(plan, runtime_adapters.RuntimeAdapterPlan):
                worker_mcp_config_path = {
                    "claude_cli": worker_mcp_runtime.claude_mcp_config_path,
                    runtime_adapters.DEEPSEEK_COPILOT_ADAPTER: worker_mcp_runtime.copilot_mcp_config_path,
                    runtime_adapters.GLM_COPILOT_ADAPTER: worker_mcp_runtime.copilot_mcp_config_path,
                }.get(adapter_id)
                if worker_mcp_config_path is not None:
                    plan = runtime_adapters.inject_worker_mcp_config(plan, worker_mcp_config_path)
            # Provision the request-owned temp authority before composing
            # the Landlock command.  sandbox_argv deliberately grants
            # --worker-temp only for an already-provisioned directory;
            # creating TMPDIR later at supervisor spawn leaves provider
            # runtimes unable to create their own nested temp directories.
            launch_env = worker_launch_env(
                adapter_id,
                repo=self.repo,
                request_id=request_id,
                home=(
                    workspace.home
                    if sandbox_backend in {"landlock", VSCODE_LM_IN_PROCESS_BACKEND}
                    else None
                ),
                isolated_task_queue_db=True,
                provider_env=provider_env,
                sandbox_backend=sandbox_backend,
            )
            worker_argv = sandbox_argv(
                workspace,
                adapter_id,
                list(plan.argv),
                backend=sandbox_backend,
                package_import_root=worker_ai_tools_mcp.resolve_host_package_import_root(),
            )
            launch_cwd = _worker_launch_cwd(workspace.path)

            # B919: snapshot every declared immutable/dependency input from
            # the canonical repo *before* claim_start_exact, while it is
            # still exactly the input state this launch will validate
            # against. accept_review re-reads the same declared paths
            # from the canonical repo immediately before promotion and
            # fails closed on any drift (B914).
            declared_immutable_inputs = [
                str(p) for p in (card.get("immutable_inputs") or [])
            ]
            immutable_input_manifest = _path_manifest(
                self.repo, declared_immutable_inputs
            )

            if (
                reserved_request_id is not None
                and not self._reviewer_reservation_still_held(reserved_request_id)
            ):
                raise _ReviewerReservationTerminalized(reserved_request_id)
            launch_phase = "canonical_claim"
            if claimed and reserved_request_id is not None:
                if str(card.get("launch_request_id") or "") != str(request_id):
                    raise LaunchRejected(
                        "claim_start_failed:"
                        "card_scoped_claim_start_ineligible:"
                        + str(core._lifecycle_state(card) or "processing")
                    )
                claim_epoch = card.get("claim_epoch")
                if type(claim_epoch) is not int or claim_epoch < 1:
                    raise LaunchRejected("claim_receipt_invalid:claim_epoch")
            else:
                claim = task_engine.claim_start_exact(
                    self.repo, task_id, runner, topic, request_id=request_id
                )
                if not claim.get("ok"):
                    raise LaunchRejected(
                        "claim_start_failed:"
                        + str(claim.get("stderr") or claim.get("stdout") or "")[:300]
                    )
                card = _committed_claim_card(
                    claim,
                    request_id=request_id,
                    task_id=task_id,
                    runner=runner,
                    topic=topic,
                )
                card["request_id"] = request_id
                receipt = preflight_card.get(_toolchain_authority.RECEIPT_CARD_KEY)
                if isinstance(receipt, Mapping):
                    card[_toolchain_authority.RECEIPT_CARD_KEY] = dict(receipt)
                claimed = True

            prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            context_delivery = _project_context_delivery(context_result, prompt_hash)
            metadata = {
                "schema_id": "aiworkhub.task_mcp.isolated_request.v1",
                "request_id": request_id,
                "task_id": task_id,

                "runner": runner,
                "topic": topic,
                "claim_epoch": card["claim_epoch"],
                "rework_predecessor": (
                    dict(card["rework_predecessor"])
                    if isinstance(card.get("rework_predecessor"), dict)
                    else None
                ),
                "validation_only_replay_authorization": (
                    dict(card["validation_only_replay_authorization"])
                    if isinstance(
                        card.get("validation_only_replay_authorization"), dict
                    )
                    else None
                ),
                "adapter_id": adapter_id,
                "model": model,
                "provider_stream_mode": (
                    "partial_messages_for_explicit_live_budget"
                    if include_partial_messages
                    else "terminal_events"
                ),
                **_legacy_timeout_fields(timeout_seconds),
                "token_budget": (
                    dict(token_budget_value)
                    if isinstance(
                        token_budget_value := card.get("token_budget"), dict
                    )
                    else None
                ),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "worker_argv": list(worker_argv),
                "worker_cwd": launch_cwd,
                "claude_auth_retry_count": 0,
                "supervisor_status_path": str(status_path),
                "cancel_path": str(cancel_path),
                "metadata_path": str(metadata_path),
                "prompt_sha256": prompt_hash,
                "prompt_budget": prompt_budget,
                "vscode_lm_bridge": (
                    vscode_lm_bridge.bridge_request_metadata(bridge_request)
                    if bridge_request is not None
                    else None
                ),
                "project_context": (
                    context_result.metadata if context_result is not None else None
                ),
                "project_context_delivery": context_delivery,
                "worker_mcp": {
                    "schema_id": worker_ai_tools_mcp.RUNTIME_SCHEMA_ID,
                    "server_name": worker_mcp_runtime.server_name,
                    "tool_names": list(worker_mcp_runtime.tool_names),
                    "audit_ledger_path": str(worker_mcp_runtime.audit_ledger_path),
                    "audit_hmac_key_path": str(worker_mcp_runtime.audit_hmac_key_path),
                    "claude_mcp_config_path": str(worker_mcp_runtime.claude_mcp_config_path),
                    "copilot_mcp_config_path": str(worker_mcp_runtime.copilot_mcp_config_path),
                    "codex_config_toml_path": str(worker_mcp_runtime.codex_config_toml_path),
                    "kilo_config_path": str(worker_mcp_runtime.kilo_config_path),
                    "kilo_auth": kilo_auth_evidence,
                    "authority_repo": str(authority_repo),
                    "source_graph_targets": worker_source_graph_targets,
                    "allowed_writes": [
                        str(value) for value in card.get("allowed_writes") or []
                    ],
                    "session_topic": worker_session_topic,
                    "source_graph_authority": (
                        {
                            "authority_source": "candidate_overlay",
                            "authority_state": "quality_review_readonly",
                            "target_request_id": str(
                                quality_review_binding["target_request_id"]
                            ),
                            "target_task_id": str(
                                quality_review_binding["target_task_id"]
                            ),
                            "packet_sha256": str(
                                quality_review_binding["packet"]["packet_sha256"]
                            ),
                        }
                        if quality_review_binding is not None
                        else (
                            {
                                "authority_source": "rework_overlay",
                                "authority_state": "request_scoped_predecessor",
                                "packet_path": str(rework_overlay_path),
                                "target_request_id": str(
                                    rework_overlay_packet.get(
                                        "predecessor_request_id"
                                    )
                                ),
                                "target_task_id": str(
                                    rework_overlay_packet.get(
                                        "predecessor_task_id"
                                    )
                                ),
                                "packet_sha256": str(
                                    rework_overlay_packet.get(
                                        "canonical_digest"
                                    )
                                ),
                            }
                            if rework_overlay_packet is not None
                            else {
                            "authority_source": "canonical",
                            "authority_state": "sole_authority",
                            }
                        )
                    ),
                },
                "sandbox_backend": sandbox_backend,
                "validation": list(card.get("validation") or []),
                "validation_roles": list(card.get("validation_roles") or []),
                "work_kind": str(card.get("work_kind") or "generic"),
                "required_outputs": list(card.get("required_outputs") or []),
                "read_only": card.get("read_only") is True,
                "allow_empty_required_outputs": list(
                    card.get("allow_empty_required_outputs") or []
                ),
                "allow_unchanged_required_outputs": list(
                    card.get("allow_unchanged_required_outputs") or []
                ),
                "immutable_inputs": declared_immutable_inputs,
                "immutable_input_manifest": immutable_input_manifest,
                "residual_contract_manifest": residual_contract_manifest,
                "crash_retry_packet": (
                    {
                        "path": str(crash_retry_packet_path),
                        "packet_sha256": str(
                            crash_retry_packet.get("packet_sha256") or ""
                        ),
                        "predecessor_request_id": str(
                            crash_retry_packet.get("predecessor_request_id") or ""
                        ),
                    }
                    if crash_retry_packet is not None
                    else None
                ),
                "external_readonly_dirs": external_readonly_dirs,
                "workspace": workspace.as_metadata(),
                "quality_review": (
                    {
                        "target_request_id": str(
                            quality_review_binding["target_request_id"]
                        ),
                        "target_task_id": str(
                            quality_review_binding["target_task_id"]
                        ),
                        "target_claim_epoch": quality_review_binding[
                            "target_claim_epoch"
                        ],
                        "adapter_id": str(adapter_id),
                        "lens": str(quality_review_binding["lens"]),
                        "packet_sha256": str(
                            quality_review_binding["packet"]["packet_sha256"]
                        ),
                        "packet_path": str(review_packet_path),
                        "workspace": review_workspace_evidence,
                    }
                    if quality_review_binding is not None
                    else None
                ),
            }
            launch_phase = "request_metadata"
            write_json_0600(metadata_path, metadata)
            authority_path = self._terminal_authority_grant_path(request_id)
            launch_phase = "terminal_authority"
            _write_terminal_authority_grant(
                authority_path,
                self._terminal_authority_key(),
                repo=self.repo,
                task_id=task_id,
                runner=runner,
                topic=topic,
                request_id=request_id,
            )
            launch_phase = "supervisor_spec"
            appcontainer_identity_fields: dict[str, str] = {}
            if sandbox_backend == "windows_appcontainer":
                try:
                    canonical_repo_id = project_context.repository_state.inspect_repository(
                        authority_repo
                    ).manifest.repo_id
                except project_context.repository_state.RepositoryStateError:
                    canonical_repo_id = ""
                identity = _appcontainer_supervisor_identity(
                    repo_id=canonical_repo_id,
                    worker_kind=adapter_id,
                    platform=sys.platform,
                )
                appcontainer_identity_fields = {
                    "execution_backend": identity.get("backend", "windows_appcontainer"),
                    "repo_id": identity["repo_id"],
                    "worker_kind": identity["worker_kind"],
                }
            write_json_0600(spec_path, {
                "argv": worker_argv,
                "cwd": launch_cwd,
                **_legacy_timeout_fields(timeout_seconds),
                "status_path": str(status_path),
                "cancel_path": str(cancel_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "max_output_bytes": MAX_WORKER_STREAM_LOG_BYTES,
                "adapter_id": adapter_id,
                "token_budget": metadata.get("token_budget"),
                **appcontainer_identity_fields,
            })

            supervisor = _worker_supervisor_script()
            # landlock confines the *real* isolated workspace.home
            # directory directly, so HOME must literally be that path.
            # bubblewrap instead remounts workspace.home onto the string
            # from worker_workspace.bubblewrap_home_env_value() inside
            # its own mount namespace (see sandbox_argv); passing
            # home=None here makes sanitized_env() seed that identical
            # shared string as HOME so the two line up by construction,
            # not by two independently-coincidental Path.home() calls
            # (B314_F004).
            if (
                reserved_request_id is not None
                and not self._reviewer_spawn_transition(
                    reserved_request_id,
                    binding=quality_review_binding,
                    # The reviewer card is already claimed at this point, so
                    # the committed phase can carry the exact epoch a later
                    # owner/provider-dead recovery must bind to.
                    reviewer_claim_epoch=metadata.get("claim_epoch"),
                )
            ):
                raise _ReviewerReservationTerminalized(reserved_request_id)
            launch_phase = "supervisor_spawn"
            process = self._popen(
                [sys.executable, str(supervisor), "--spec", str(spec_path)],
                cwd=launch_cwd,
                env=launch_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                **process_group_launch_kwargs(os.name),
            )
            started_at = _utcnow()
            launch_phase = "supervisor_pid_identity"
            start_ticks = _pid_start_ticks(process.pid)
            if start_ticks is None:
                _terminate_process_group(process.pid, grace_seconds=5.0)
                raise LaunchRejected("supervisor_pid_identity_unavailable")
            if reserved_request_id is not None and not (
                self._reviewer_attach_provider_identity(
                    reserved_request_id,
                    pid=process.pid,
                    pid_start_ticks=start_ticks,
                )
            ):
                # Another owner already attached a different provider pid for
                # this exact request/task/packet binding: this spawn is the
                # loser and must never become live or durable.
                _terminate_process_group(process.pid, grace_seconds=5.0)
                raise _ReviewerReservationTerminalized(reserved_request_id)
            live = _LiveProcess(
                request_id=request_id,
                task_id=task_id,
                runner=runner,
                topic=topic,
                adapter_id=adapter_id,
                model=model,
                process=process,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                started_at=started_at,
                timeout_seconds=timeout_seconds,
                isolated=True,
                metadata_path=metadata_path,
                supervisor_status_path=status_path,
                pid_start_ticks=start_ticks,
                bridge_request=bridge_request,
                claim_epoch=(
                    metadata.get("claim_epoch")
                    if type(metadata.get("claim_epoch")) is int
                    else None
                ),
            )
            with self._lock:
                self._live[request_id] = live
            event = self._append_event({
                "request_id": request_id,
                "task_id": task_id,
                "runner": runner,
                "topic": topic,
                "adapter_id": adapter_id,
                "model": model,
                "state": "running",
                "pid": process.pid,
                "pid_start_ticks": start_ticks,
                "started_at": started_at,
                "timeout_seconds": timeout_seconds,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "metadata_path": str(metadata_path),
                "supervisor_status_path": str(status_path),
                "prompt_sha256": prompt_hash,
                "prompt_budget": prompt_budget,
                "project_context": (
                    context_result.metadata if context_result is not None else None
                ),
                "project_context_delivery": context_delivery,
                "workspace_isolated": True,
                "sandbox_backend": sandbox_backend,
                "shell": False,
            })
            thread = threading.Thread(
                target=self._monitor,
                args=(live,),
                name=f"aiworkhub-task-{request_id[:8]}",
                daemon=True,
            )
            thread.start()

        return {
            "ok": True,
            "launch_implemented": LAUNCH_IMPLEMENTED,
            "launch_enabled": True,
            "request_id": request_id,
            "task_id": task_id,
            "runner": runner,
            "topic": topic,
            "adapter_id": adapter_id,
            "model": model,
            "state": event["state"],
            "pid": process.pid,
            "card_priority": card.get("priority"),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "workspace_isolated": True,
            "sandbox_backend": sandbox_backend,
            "prompt_budget": prompt_budget,
            "shell": False,
            **_legacy_timeout_fields(timeout_seconds),
        }
    except _ReviewerReservationTerminalized:
        return _abandon_terminalized_reviewer()
    except Exception as exc:  # noqa: BLE001 - return durable launch diagnostics
        if (
            reserved_request_id is not None
            and not self._reviewer_reservation_still_held(reserved_request_id)
            and not self._reviewer_provider_committed(reserved_request_id)
        ):
            # The bounded launch owner terminalized this exact reservation
            # while the stale owner was still preparing and then raised.
            # Avoid a second terminal event; preserve exactly-once
            # terminalization and never steal the terminalized reservation.
            return _abandon_terminalized_reviewer()
        expected = isinstance(
            exc,
            (
                LaunchRejected,
                project_context.ProjectContextError,
                WorkspaceError,
                OSError,
                ValueError,
            ),
        )
        diagnostic = (
            None
            if expected
            else _bounded_launch_diagnostic(
                exc,
                phase=launch_phase,
                repo=self.repo,
            )
        )
        reason = (
            str(exc)
            if expected
            else f"unexpected_launch_error:{type(exc).__name__}:{exc}"
        )
        # Provisioned failures must become recoverable blocked claim episodes.
        claim_release_retained = False
        if not claimed and workspace is not None and request_id:
            failed_claim = task_engine.claim_start_exact(
                self.repo, task_id, runner, topic, request_id=request_id
            )
            if failed_claim.get("ok"):
                claimed = True
                card = _committed_claim_card(
                    failed_claim, request_id=request_id, task_id=task_id,
                    runner=runner, topic=topic,
                )
            else:
                detail = failed_claim.get("stderr") or failed_claim.get("stdout") or ""
                reason += ":launch_failure_claim_failed:" + str(detail)[:200]
        if claimed:
            claim_epoch = card.get("claim_epoch")
            if reserved_request_id is not None and (
                type(claim_epoch) is int and claim_epoch >= 1
            ):
                blocked_result, claim_release_retained = (
                    self._release_or_retain_reviewer_claim_after_launch_failure(
                        request_id=reserved_request_id, task_id=task_id,
                        runner=runner, reviewer_claim_epoch=claim_epoch,
                        reason=reason,
                    )
                )
            else:
                blocked_result = task_engine.mark_launch_failed(
                    self.repo, task_id, runner, reason=reason[:500],
                    request_id=request_id or reserved_request_id or "",
                )
            if not blocked_result.get("ok"):
                release_detail = str(
                    blocked_result.get("stderr") or blocked_result.get("stdout")
                    or blocked_result.get("error") or ""
                )[:200]
                reason += ":launch_failure_transition_failed:" + release_detail
        else:
            blocker_result = task_engine.record_launch_blocker(
                self.repo, task_id, runner, topic,
                adapter_id=adapter_id, reason=reason,
            )
            if not blocker_result.get("ok"):
                reason += ":launch_blocker_record_failed:" + str(
                    blocker_result.get("stderr") or ""
                )[:200]
        # Cancel the VS Code LM claim BEFORE deleting the request workspace
        # it refers to, so a claim never outlives its workspace.
        for release_error in _release_launch_request_resources(
            bridge_request=bridge_request,
            workspace=workspace,
        ):
            reason += ":" + release_error
        if spec_path is not None:
            unlink_if_regular(spec_path)
        if authority_path is not None:
            unlink_if_regular(authority_path)
        if claim_release_retained:
            return {
                "ok": False,
                "launch_implemented": LAUNCH_IMPLEMENTED,
                "launch_enabled": True,
                "request_id": reserved_request_id,
                "task_id": task_id,
                "runner": runner,
                "topic": topic,
                "adapter_id": adapter_id,
                "state": "starting",
                "reason": reason,
                "recovery_state": "starting_reservation_retained",
                "reviewer_claim_epoch": card["claim_epoch"],
            }
        return self._blocked(
            task_id,
            runner,
            topic,
            adapter_id,
            reason,
            request_id=request_id,
            state="launch_failed" if claimed else "blocked",
            diagnostic=diagnostic,
        )
