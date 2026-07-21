"""Disabled-by-default MCP launch-queue CONTRACT (no process launch).

Pure, side-effect-free schema + env-gate contract for a FUTURE launch queue that
could LATER start local Claude/Codex worker adapters (and hand off to a manual
DeepSeek adapter) -- but ONLY after explicit env gates, and only once a gated
launcher is actually implemented in a later task. THIS MODULE LAUNCHES NOTHING.

Hard invariants (asserted by launch_disabled_queue_contract_smoke.py):
  * No child-process, no exec/fork, no shell, and no network/API call exists in
    any code path here. The process-spawn machinery is never imported. Only the
    stdlib os module is used, and only os.environ.get (a pure read).
  * ``LAUNCH_IMPLEMENTED`` is a constant ``False`` -- not a toggle. No env flag
    can start anything in this contract, because no launcher exists here.
  * ``launch_enabled()`` is always ``False``. A future launch-capable tool must
    require BOTH ``AIWORKHUB_ALLOW_LAUNCH=1`` AND
    ``AIWORKHUB_ALLOW_WRITES=1`` (both default '0' = disabled). This module
    only *describes* whether those gates are open; it never acts on them.
  * ``attempt_start()`` ALWAYS refuses (raises ``LaunchDisabledError``) and spawns
    nothing, even when both env gates are forced on.
  * Pure data only: enqueue / describe / evaluate return dataclasses/dicts and
    write NOTHING to disk. Persistence + audit are deferred to the gated phase
    (CLAUDE_TASK_MCP_ORCH_GATED_ENABLEMENT_B106_V1).
  * workflow_switch (runtime switch to MCP) and parent-queue mutation stay False.

Contract SHAPE mirrors cli_adapter_dryrun.py (frozen dataclasses + as_dict, a
launch_enabled()==False gate, a constant LAUNCH_IMPLEMENTED=False, and a
contract_summary()); it deliberately does NOT import that module or ``core`` so
it stays import-safe and side-effect-free on its own.

Neural-bridge note (AIWorkHub neural-control-first doctrine): the FUTURE selection of
which adapter/runner/topic to launch for a task must become a LEARNED routing +
abstain policy (tracked as MCP_NEURAL_LAUNCH_ROUTING_MIGRATION_V1), never a
regex/keyword cue router. The static allowlists below are human-configured safety
boundaries only; they are not the intelligence layer and decide no capability.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Explicit env gate names (both default disabled '0')
# ---------------------------------------------------------------------------
ALLOW_LAUNCH_ENV: str = "AIWORKHUB_ALLOW_LAUNCH"
ALLOW_WRITES_ENV: str = "AIWORKHUB_ALLOW_WRITES"
DISABLED_DEFAULT: str = "0"
ENABLED_VALUE: str = "1"

# ---------------------------------------------------------------------------
# Hard invariants (constants, NOT toggles)
# ---------------------------------------------------------------------------
LAUNCH_IMPLEMENTED: bool = False
WORKFLOW_SWITCH_ENABLED: bool = False
PARENT_QUEUE_MUTATION_ENABLED: bool = False

# ---------------------------------------------------------------------------
# Static safety boundaries (mirror phase2_orchestration_contract_b104_v1)
# ---------------------------------------------------------------------------
LAUNCHABLE_BINARIES: tuple[str, ...] = ("claude", "codex")
MANUAL_ONLY: tuple[str, ...] = ("deepseek_manual",)
KNOWN_ADAPTERS: tuple[str, ...] = ("claude_cli", "codex_cli", "deepseek_manual")
ADAPTER_BINARIES: dict[str, str] = {
    "claude_cli": "claude",
    "codex_cli": "codex",
    "deepseek_manual": "",  # manual external adapter: no local binary
}


class ProcessState(str, Enum):
    """Lifecycle states of a launch request. WOULD_RUN/TERMINAL are unreachable
    in this disabled contract; every request rests in BLOCKED_LAUNCH_DISABLED."""

    QUEUED = "queued"
    PLANNED = "planned"
    BLOCKED_LAUNCH_DISABLED = "blocked_launch_disabled"
    WOULD_RUN = "would_run"
    TERMINAL = "terminal"


class LaunchDisabledError(RuntimeError):
    """Raised by attempt_start(): this contract never launches a process."""


# ---------------------------------------------------------------------------
# Request identity helpers
# ---------------------------------------------------------------------------

def deterministic_request_id(task_id: str, runner: str, requested_at: str) -> str:
    """B281-compatible stable request id.

    This is a pure helper: no IO, no randomness, no launch authority.
    """
    return hashlib.sha1(f"{task_id}:{runner}:{requested_at}".encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Gate helpers (pure env reads; no side effects, no launch)
# ---------------------------------------------------------------------------

def launch_flag_set() -> bool:
    """True only when AIWORKHUB_ALLOW_LAUNCH == '1' (default off)."""
    return os.environ.get(ALLOW_LAUNCH_ENV, DISABLED_DEFAULT) == ENABLED_VALUE


def writes_allowed() -> bool:
    """True only when AIWORKHUB_ALLOW_WRITES == '1' (default off).

    Mirrors the parent write-gate semantics by NAME so a future launcher shares
    the same explicit gate; read-only, no side effects.
    """
    return os.environ.get(ALLOW_WRITES_ENV, DISABLED_DEFAULT) == ENABLED_VALUE


def env_gates_open() -> bool:
    """True only when BOTH explicit launch gates are '1'. Default False.

    Describes whether a FUTURE launcher WOULD be permitted by env; this contract
    still launches nothing regardless (see launch_enabled()).
    """
    return launch_flag_set() and writes_allowed()


def launch_enabled() -> bool:
    """Always False: no launcher is implemented in this contract."""
    return LAUNCH_IMPLEMENTED  # constant False; not env-controlled


def gate_status() -> dict[str, Any]:
    """Pure snapshot of gate + invariant state (no values, no side effects)."""
    return {
        "allow_launch_env": ALLOW_LAUNCH_ENV,
        "allow_writes_env": ALLOW_WRITES_ENV,
        "allow_launch_flag": launch_flag_set(),
        "allow_writes_flag": writes_allowed(),
        "env_gates_open": env_gates_open(),
        "launch_implemented": LAUNCH_IMPLEMENTED,
        "launch_enabled": launch_enabled(),
        "workflow_switch_enabled": WORKFLOW_SWITCH_ENABLED,
        "parent_queue_mutation_enabled": PARENT_QUEUE_MUTATION_ENABLED,
    }


def env_gate_status_tokens(names: Iterable[str] | None = None) -> dict[str, str]:
    """Return env NAME -> '<set>'/'<unset>' status only -- never a value.

    Safe to log: an actual env value (including any secret) is never returned.
    """
    names = tuple(names) if names is not None else (ALLOW_LAUNCH_ENV, ALLOW_WRITES_ENV)
    return {n: ("<set>" if os.environ.get(n, "") != "" else "<unset>") for n in names}


# ---------------------------------------------------------------------------
# Schemas: launch request, process state, process log, result handoff
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LaunchRequest:
    """A launch INTENT as pure data. Constructing one launches nothing."""

    task_id: str
    runner: str
    topic: str
    adapter_id: str
    argv_template: list[str] = field(default_factory=list)
    cwd_pin: str = "AIWORKHUB_REPO"
    request_id: str = ""
    created_ts: float | None = None
    priority: str = "normal"
    state: ProcessState = ProcessState.QUEUED
    model: str | None = None
    owner_prompt: str = ""
    requested_at: str | None = None
    stale_timeout_seconds: int = 7200

    def resolved_request_id(self) -> str:
        if self.request_id:
            return self.request_id
        if self.requested_at:
            return deterministic_request_id(self.task_id, self.runner, self.requested_at)
        return self.request_id or f"{self.task_id}:{self.adapter_id}"

    def binary(self) -> str:
        return ADAPTER_BINARIES.get(self.adapter_id, "")

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.resolved_request_id(),
            "task_id": self.task_id,
            "runner": self.runner,
            "topic": self.topic,
            "adapter_id": self.adapter_id,
            "binary": self.binary(),
            "argv_template": list(self.argv_template),
            "cwd_pin": self.cwd_pin,
            "created_ts": self.created_ts,
            "priority": self.priority,
            "state": self.state.value,
            "model": self.model,
            "owner_prompt": self.owner_prompt,
            "requested_at": self.requested_at,
            "stale_timeout_seconds": self.stale_timeout_seconds,
        }


@dataclass(frozen=True)
class LaunchDecision:
    """Result of a pure gate evaluation. ``permitted`` is always False here."""

    request_id: str
    task_id: str
    adapter_id: str
    permitted: bool
    state: ProcessState
    decision: str
    blocked_reason: str
    env_gates_open: bool
    launch_enabled: bool
    launch_implemented: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "adapter_id": self.adapter_id,
            "permitted": self.permitted,
            "state": self.state.value,
            "decision": self.decision,
            "blocked_reason": self.blocked_reason,
            "env_gates_open": self.env_gates_open,
            "launch_enabled": self.launch_enabled,
            "launch_implemented": self.launch_implemented,
        }


@dataclass(frozen=True)
class ProcessLogEntry:
    """SHAPE of an append-only process-log record.

    This pure contract never WRITES one; persistence is deferred to the gated
    phase. Env is captured as NAME->status tokens only, never values.
    """

    request_id: str
    task_id: str
    runner: str
    topic: str
    adapter_id: str
    from_state: ProcessState
    to_state: ProcessState
    decision: str
    blocked_reason: str
    env_gate_status: dict[str, str]
    ts: float | None = None
    model: str | None = None
    owner_prompt: str = ""
    requested_at: str | None = None
    stale_timeout_seconds: int = 7200
    rollback_of_request_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "runner": self.runner,
            "topic": self.topic,
            "adapter_id": self.adapter_id,
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "decision": self.decision,
            "blocked_reason": self.blocked_reason,
            "env_gate_status": dict(self.env_gate_status),
            "ts": self.ts,
            "model": self.model,
            "owner_prompt": self.owner_prompt,
            "requested_at": self.requested_at,
            "stale_timeout_seconds": self.stale_timeout_seconds,
            "rollback_of_request_id": self.rollback_of_request_id,
        }


@dataclass(frozen=True)
class ResultHandoff:
    """Codex-review handoff packet SHAPE (mirrors phase2 codex_review_handoff).

    A launched worker WOULD return this; here it is pure data and no launch
    occurred. The executor never finalizes its own task (separation of duties).
    """

    task_id: str
    runner: str
    topic: str
    adapter_id: str
    changed_paths: list[str] = field(default_factory=list)
    tests_run: list[str] = field(default_factory=list)
    verdict: str = "pending"
    token_cost_report: dict[str, Any] = field(default_factory=dict)
    audit_log_ref: str = ""
    finalizer: str = "codex"

    def review_command(self) -> str:
        return f"python3 AITools/taskctl.py review {self.task_id} --runner {self.runner}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "runner": self.runner,
            "topic": self.topic,
            "adapter_id": self.adapter_id,
            "changed_paths": list(self.changed_paths),
            "tests_run": list(self.tests_run),
            "verdict": self.verdict,
            "token_cost_report": dict(self.token_cost_report),
            "audit_log_ref": self.audit_log_ref,
            "review_command": self.review_command(),
            "finalizer": self.finalizer,
            "worker_must_not": [
                "run taskctl done",
                "create follow-up task cards",
                "self-review own executed task",
            ],
        }


# ---------------------------------------------------------------------------
# Enqueue / evaluate / describe (pure) and attempt_start (always refuses)
# ---------------------------------------------------------------------------

def enqueue_intent(
    *,
    task_id: str,
    runner: str,
    topic: str,
    adapter_id: str,
    argv_template: list[str] | None = None,
    cwd_pin: str = "AIWORKHUB_REPO",
    request_id: str = "",
    created_ts: float | None = None,
    priority: str = "normal",
    model: str | None = None,
    owner_prompt: str = "",
    requested_at: str | None = None,
    stale_timeout_seconds: int = 7200,
) -> LaunchRequest:
    """Represent a launch INTENT as pure data (state=QUEUED). Launches nothing."""
    return LaunchRequest(
        task_id=task_id,
        runner=runner,
        topic=topic,
        adapter_id=adapter_id,
        argv_template=list(argv_template or []),
        cwd_pin=cwd_pin,
        request_id=request_id,
        created_ts=created_ts,
        priority=priority,
        state=ProcessState.QUEUED,
        model=model,
        owner_prompt=owner_prompt,
        requested_at=requested_at,
        stale_timeout_seconds=stale_timeout_seconds,
    )


def evaluate_launch(request: LaunchRequest) -> LaunchDecision:
    """Pure gate evaluation. NEVER launches.

    ``permitted`` is always False in this contract: even with both env gates
    open, no launcher is implemented, so nothing can start.
    """
    reasons: list[str] = []
    if not launch_flag_set():
        reasons.append(f"{ALLOW_LAUNCH_ENV}!='1'")
    if not writes_allowed():
        reasons.append(f"{ALLOW_WRITES_ENV}!='1'")
    if request.adapter_id not in KNOWN_ADAPTERS:
        reasons.append(f"unknown_adapter:{request.adapter_id}")
    if request.adapter_id in MANUAL_ONLY:
        reasons.append("manual_external_adapter_never_local_launch")
    if not LAUNCH_IMPLEMENTED:
        reasons.append("launcher_not_implemented_in_contract")

    return LaunchDecision(
        request_id=request.resolved_request_id(),
        task_id=request.task_id,
        adapter_id=request.adapter_id,
        permitted=False,  # invariant: this contract never permits a launch
        state=ProcessState.BLOCKED_LAUNCH_DISABLED,
        decision="blocked_launch_disabled",
        blocked_reason="; ".join(reasons),
        env_gates_open=env_gates_open(),
        launch_enabled=launch_enabled(),
        launch_implemented=LAUNCH_IMPLEMENTED,
    )


def describe_intent(request: LaunchRequest, *, audit: bool = False) -> dict[str, Any]:
    """DRY-RUN: return the launch INTENT + gate evaluation as pure data.

    Side-effect-free. ``audit`` is accepted only for signature parity with the
    gated phase and MUST stay False here; True raises rather than silently
    writing (this pure contract has no audit writer).
    """
    if audit:
        raise LaunchDisabledError(
            "audit persistence is deferred to the gated phase "
            "(CLAUDE_TASK_MCP_ORCH_GATED_ENABLEMENT_B106_V1); this pure contract "
            "writes nothing -- keep audit=False"
        )
    decision = evaluate_launch(request)
    return {
        "mode": "launch_queue_contract_dryrun",
        "intent": request.as_dict(),
        "decision": decision.as_dict(),
        "gate_status": gate_status(),
        "would_run_argv": [],  # never populated: launch not implemented here
        "authority_flags": {
            "process_launch": False,
            "agent_launch": False,
            "shell_invocation": False,
            "network_call": False,
            "workflow_switch": WORKFLOW_SWITCH_ENABLED,
            "parent_queue_mutation": PARENT_QUEUE_MUTATION_ENABLED,
        },
    }


def build_log_entry(
    request: LaunchRequest,
    decision: LaunchDecision,
    *,
    ts: float | None = None,
    rollback_of_request_id: str | None = None,
) -> ProcessLogEntry:
    """Construct a process-log record (pure data; NOT written anywhere)."""
    return ProcessLogEntry(
        request_id=decision.request_id,
        task_id=request.task_id,
        runner=request.runner,
        topic=request.topic,
        adapter_id=request.adapter_id,
        from_state=request.state,
        to_state=decision.state,
        decision=decision.decision,
        blocked_reason=decision.blocked_reason,
        env_gate_status=env_gate_status_tokens(),
        ts=ts,
        model=request.model,
        owner_prompt=request.owner_prompt,
        requested_at=request.requested_at,
        stale_timeout_seconds=request.stale_timeout_seconds,
        rollback_of_request_id=rollback_of_request_id,
    )


def attempt_start(request: LaunchRequest) -> None:
    """Entry point a FUTURE launch-capable tool would call.

    In this disabled contract it ALWAYS refuses and spawns NOTHING: it raises
    ``LaunchDisabledError`` before any action. It refuses even when both env
    gates are forced on, because ``LAUNCH_IMPLEMENTED`` is False (no launcher
    exists here). Performs no I/O and starts no process.
    """
    decision = evaluate_launch(request)
    raise LaunchDisabledError(
        f"launch refused for task={request.task_id!r} adapter={request.adapter_id!r}: "
        f"launch_implemented={LAUNCH_IMPLEMENTED} env_gates_open={decision.env_gates_open} "
        f"reason={decision.blocked_reason!r}. This contract launches no process."
    )


def contract_summary() -> dict[str, Any]:
    """Return the static contract shape (no side effects, no launch)."""
    return {
        "mode": "launch_queue_contract_disabled_no_process_launch",
        "launch_implemented": LAUNCH_IMPLEMENTED,
        "launch_enabled": launch_enabled(),
        "workflow_switch_enabled": WORKFLOW_SWITCH_ENABLED,
        "parent_queue_mutation_enabled": PARENT_QUEUE_MUTATION_ENABLED,
        "env_gates": {
            "allow_launch_env": ALLOW_LAUNCH_ENV,
            "allow_writes_env": ALLOW_WRITES_ENV,
            "default": DISABLED_DEFAULT,
            "both_required_for_future_launch": True,
        },
        "launchable_binaries": list(LAUNCHABLE_BINARIES),
        "manual_only": list(MANUAL_ONLY),
        "known_adapters": list(KNOWN_ADAPTERS),
        "process_states": [s.value for s in ProcessState],
        "schemas": {
            "launch_request": [
                "request_id", "task_id", "runner", "topic", "adapter_id",
                "binary", "argv_template", "cwd_pin", "created_ts", "priority",
                "state", "model", "owner_prompt", "requested_at",
                "stale_timeout_seconds",
            ],
            "process_state": [s.value for s in ProcessState],
            "process_log": [
                "request_id", "task_id", "runner", "topic", "adapter_id",
                "from_state", "to_state", "decision", "blocked_reason",
                "env_gate_status", "ts", "model", "owner_prompt",
                "requested_at", "stale_timeout_seconds",
                "rollback_of_request_id",
            ],
            "result_handoff": [
                "task_id", "runner", "topic", "adapter_id", "changed_paths",
                "tests_run", "verdict", "token_cost_report", "audit_log_ref",
                "review_command", "finalizer", "worker_must_not",
            ],
        },
        "public_api": {
            "enqueue_intent": "-> LaunchRequest (state=queued), pure",
            "deterministic_request_id": "-> sha1(task_id:runner:requested_at)[:16], pure",
            "describe_intent": "-> dry-run intent+decision dict, side-effect-free",
            "evaluate_launch": "-> LaunchDecision (always permitted=False)",
            "attempt_start": "-> ALWAYS raises LaunchDisabledError (no spawn)",
            "build_log_entry": "-> ProcessLogEntry shape (not written)",
            "gate_status/contract_summary": "pure introspection",
        },
        "deferred_to_gated_phase": "CLAUDE_TASK_MCP_ORCH_GATED_ENABLEMENT_B106_V1",
        "neural_bridge_migration": "MCP_NEURAL_LAUNCH_ROUTING_MIGRATION_V1",
        "no_launch_code_marker": (
            "no child-process spawn, no os-level exec/fork, no shell invocation, "
            "and no network/API call in this module"
        ),
    }
