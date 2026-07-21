"""READ-ONLY MCP tool contract exposing the B105 CLI adapter dryrun.

B106 wraps the B105 dryrun adapter (``cli_adapter_dryrun``) as a strictly
READ-ONLY MCP tool contract: it returns validated launch PLANS and audit
SUMMARIES, and it writes NOTHING -- not even the append-only audit entry that
the underlying dryrun planner can emit. Process launch is impossible here and
stays impossible even if every env flag is set, because no launcher exists in
this module or in the code path it reaches.

Hard invariants (asserted by tests):
  * READ-ONLY: no code path writes the task queue OR the audit log. The plan
    builder calls ``plan_dryrun(audit=False)``; the audit view calls the
    read-only ``core.read_audit_log``. ``READONLY`` is a constant ``True``,
    not a toggle. This holds even with ``AIWORKHUB_ALLOW_WRITES=1``.
  * NO LAUNCH: this module never imports process-spawn machinery and has no
    exec/fork/shell/spawn code. ``launch_enabled()`` is always ``False``; no
    env flag (``AIWORKHUB_ALLOW_LAUNCH`` / ``_ALLOW_WRITES``) can enable a
    launch, because no launcher exists.
  * REDACTION: env values (incl. secrets) are never emitted; only NAME/status
    tokens appear, reusing the parent redaction patterns.
  * WRITE GATE OFF by default; every ``authority_flags`` entry stays false.

Neural-bridge note (AIWorkHub neural-control-first doctrine): the FUTURE selection
of which adapter/runner/topic to launch for a task must become a LEARNED
routing + abstain policy (tracked as ``MCP_NEURAL_LAUNCH_ROUTING_MIGRATION_V1``),
never a regex/keyword cue router. This module is a static, read-only
safety-boundary VIEW over the dryrun contract; it is not the intelligence layer
and decides no capability.

Server wiring -- registering these functions as live ``@mcp.tool()``s in
``server.py`` -- is the gated NEXT step tracked in
``cli_adapter_readonly_tool_next_wave_b106_v1.json``. ``server.py`` is
intentionally NOT modified by this contract task; ``register(mcp)`` makes the
future wiring a one-liner without this task touching the server module.

B107 adds a READ-ONLY collision preflight to the plan output (``would_collide``
/ ``collision_preflight``). It answers "would this task_id/runner collide with
an already-active claim?" by reading the SAME task-card JSONL the tasking
collision guard scans (``scripts/build_tasking_parallel_group_collision_guard_
v1.py``: ``load_cards`` + ``canonical_status`` + ``ACTIVE_STATUSES``) with a
plain file read. No lock is taken, no claim is written, and no queue/audit
state is ever mutated by this preflight -- it only classifies cards that are
already on disk. The card source path is env-overridable
(``AIWORKHUB_COLLISION_CARDS_PATH``, falling back to the existing
``BITNN_TASK_CARDS_PATH`` override taskctl.py already honors) so tests can
point it at an isolated temp directory instead of the shared production file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from aiworkhub import cli_adapter_dryrun as dryrun
from aiworkhub import core


# HARD INVARIANT: this tool is read-only. It never writes queue/audit state.
READONLY: bool = True

# Re-exported for callers/tests: launch is not implemented anywhere in the
# read path this module reaches.
LAUNCH_IMPLEMENTED: bool = dryrun.LAUNCH_IMPLEMENTED  # False


# ---------------------------------------------------------------------------
# Gate helpers
# ---------------------------------------------------------------------------

def launch_enabled() -> bool:
    """Always False; no env flag can enable launch (delegates to B105)."""
    return dryrun.launch_enabled()


def writes_allowed() -> bool:
    """Mirror the parent write gate (default OFF). Informational only here."""
    return core.writes_allowed()


def redact_env(env: dict[str, str] | None) -> dict[str, str]:
    """Redact ALL env values (delegates to the B105 adapter redactor)."""
    return dryrun.redact_env(env)


def _authority_flags() -> dict[str, bool]:
    """Read-only authority flags. Nothing here can ever be true for launch."""
    return {
        "write_gate_enabled": not writes_allowed(),
        "readonly": READONLY,
        "process_launch": False,
        "agent_launch": False,
        "shell_invocation": False,
        "queue_write": False,
        "audit_write": False,
    }


# ---------------------------------------------------------------------------
# Collision preflight (B107) -- read-only, no lock, no claim, no mutation
# ---------------------------------------------------------------------------

_COLLISION_CARDS_ENV = "AIWORKHUB_COLLISION_CARDS_PATH"
_COLLISION_CARDS_LEGACY_ENV = "BITNN_TASK_CARDS_PATH"
_COLLISION_CARDS_DEFAULT_REL = Path("bitnnv2") / "data" / "tasking" / "machine_task_cards_v1.jsonl"


def collision_cards_path(repo: Any = None) -> Path:
    """Resolve the read-only task-card source scanned for the preflight.

    Priority: ``AIWORKHUB_COLLISION_CARDS_PATH`` (dedicated, test-safe
    override) > ``BITNN_TASK_CARDS_PATH`` (the existing taskctl.py override,
    kept for production consistency) > the default cards JSONL under the
    repo root. Never falls back to any lock/claim file.
    """
    override = os.environ.get(_COLLISION_CARDS_ENV, "")
    if override:
        return Path(override).expanduser().resolve()
    legacy = os.environ.get(_COLLISION_CARDS_LEGACY_ENV, "")
    if legacy:
        return Path(legacy).expanduser().resolve()
    root = repo or core.repo_root()
    return Path(root) / _COLLISION_CARDS_DEFAULT_REL


def _collision_guard_helpers():
    """Lazily import the pure, read-only card classification helpers.

    Reuses ``scripts/build_tasking_parallel_group_collision_guard_v1.py`` so
    the preflight never drifts from the production ``canonical_status`` /
    ``ACTIVE_STATUSES`` semantics. This is a plain in-process module import
    (no CLI invocation of any kind) of a module that itself contains no
    child-process launch code.
    """
    root = str(core.repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    from scripts.build_tasking_parallel_group_collision_guard_v1 import (
        ACTIVE_STATUSES,
        canonical_status,
        load_cards,
    )

    return ACTIVE_STATUSES, canonical_status, load_cards


def collision_preflight(
    *,
    task_id: str,
    runner: str,
    topic: str,
    repo: Any = None,
) -> dict[str, Any]:
    """READ-ONLY: would this task_id/runner collide with an active claim?

    Reads the task-card JSONL with a plain file read -- no lock is acquired,
    no claim is written, and no queue/audit state is mutated. ``would_collide``
    is true when an ACTIVE (pending/processing/review/blocked) card already
    claims this exact ``task_id``, or when the same ``runner`` already holds a
    different active task.
    """
    cards_path = collision_cards_path(repo)
    exists = cards_path.exists()
    active_cards: list[dict[str, Any]] = []
    if exists:
        active_statuses, canonical_status, load_cards = _collision_guard_helpers()
        for card in load_cards(cards_path):
            if canonical_status(card) in active_statuses:
                active_cards.append(card)

    same_task = [c for c in active_cards if str(c.get("task_id", "")) == task_id]
    same_runner_other_task = [
        c for c in active_cards
        if str(c.get("runner", "")) == runner and str(c.get("task_id", "")) != task_id
    ]

    reasons: list[str] = []
    if same_task:
        reasons.append("active_claim_exists_for_task_id")
    if same_runner_other_task:
        reasons.append("runner_already_claims_other_active_task")

    return {
        "would_collide": bool(reasons),
        "read_only": True,
        "mutated_state": False,
        "lock_acquired": False,
        "claim_written": False,
        "cards_source": str(cards_path),
        "cards_source_exists": exists,
        "active_card_count": len(active_cards),
        "matching_task_id_active_claims": len(same_task),
        "same_runner_other_active_claims": len(same_runner_other_task),
        "collision_reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Read-only MCP tool functions
# ---------------------------------------------------------------------------

def plan_command_readonly(
    *,
    task_id: str,
    runner: str,
    topic: str,
    adapter_id: str,
    argv: list[str],
    env: dict[str, str] | None = None,
    repo: Any = None,
) -> dict[str, Any]:
    """Return a validated launch PLAN without writing anything.

    Wraps ``plan_dryrun(audit=False)``: validates ``argv`` against the static
    allowlist and returns the would-run plan. Writes NO audit entry, mutates NO
    task-queue state, and launches NOTHING. Any provided ``env`` is redacted
    (values, incl. secrets, are never emitted). Read-only holds regardless of
    the write gate. Also runs the B107 collision preflight (``would_collide`` /
    ``collision_preflight``): a plain read of the task-card JSONL, no lock, no
    claim, no mutation.
    """
    plan = dryrun.plan_dryrun(
        task_id=task_id,
        runner=runner,
        topic=topic,
        adapter_id=adapter_id,
        argv=argv,
        repo=repo,
        audit=False,  # READ-ONLY: never append to the audit log
    )
    plan["readonly"] = True
    plan["audit_written"] = False
    plan["redacted_env"] = redact_env(env)
    preflight = collision_preflight(task_id=task_id, runner=runner, topic=topic, repo=repo)
    plan["would_collide"] = preflight["would_collide"]
    plan["collision_preflight"] = preflight
    plan["authority_flags"].update(_authority_flags())
    return plan


def audit_summary_readonly(max_entries: int = 100, *, repo: Any = None) -> dict[str, Any]:
    """Return a read-only summary of the write-gate audit log.

    Delegates to ``core.read_audit_log`` (which never writes, never enables
    writes, never launches). Adds the read-only authority flags for a
    consistent contract shape. Env values are absent from audit entries by
    construction, so this output is safe to log.
    """
    summary = core.read_audit_log(max_entries=max_entries, repo=repo)
    summary["readonly"] = True
    summary["authority_flags"] = _authority_flags()
    return summary


def readonly_tool_report(
    *,
    task_id: str,
    runner: str,
    topic: str,
    adapter_id: str,
    argv: list[str],
    env: dict[str, str] | None = None,
    max_audit_entries: int = 20,
    repo: Any = None,
) -> dict[str, Any]:
    """One read-only call returning BOTH a validated plan and an audit summary."""
    return {
        "ok": True,
        "mode": "mcp_readonly_cli_plan_tool_no_process_launch",
        "readonly": True,
        "plan": plan_command_readonly(
            task_id=task_id,
            runner=runner,
            topic=topic,
            adapter_id=adapter_id,
            argv=argv,
            env=env,
            repo=repo,
        ),
        "audit_summary": audit_summary_readonly(max_entries=max_audit_entries, repo=repo),
        "authority_flags": _authority_flags(),
    }


def contract_summary() -> dict[str, Any]:
    """Return the static read-only contract shape (no side effects, no launch)."""
    base = dryrun.contract_summary()
    base.update(
        {
            "mode": "mcp_readonly_cli_plan_tool_no_process_launch",
            "readonly": READONLY,
            "audit_write": False,
            "queue_write": False,
            "readonly_tools": list(READONLY_TOOL_NAMES),
            "server_wiring": "next_wave: cli_adapter_readonly_tool_next_wave_b106_v1",
            "neural_bridge_migration": "MCP_NEURAL_LAUNCH_ROUTING_MIGRATION_V1",
        }
    )
    return base


# ---------------------------------------------------------------------------
# MCP tool contract surface (registration deferred to the gated next wave)
# ---------------------------------------------------------------------------
# These are the read-only functions a future ``server.py`` change would
# register as ``@mcp.tool()``s. Registration is intentionally NOT performed
# here (server.py is untouched by this contract task); ``register(mcp)`` keeps
# the future wiring a single call. Every registered function is read-only.

READONLY_TOOL_NAMES: tuple[str, ...] = (
    "aiworkhub_cli_adapter_plan_readonly",
    "aiworkhub_cli_adapter_audit_summary_readonly",
    "aiworkhub_cli_adapter_report_readonly",
)

# Stable tool_name -> callable map for the future server wiring / introspection.
READONLY_TOOLS: dict[str, Any] = {
    "aiworkhub_cli_adapter_plan_readonly": plan_command_readonly,
    "aiworkhub_cli_adapter_audit_summary_readonly": audit_summary_readonly,
    "aiworkhub_cli_adapter_report_readonly": readonly_tool_report,
}


def register(mcp: Any) -> tuple[str, ...]:
    """Register the read-only tools on a FastMCP-like server; return names.

    Kept out of ``server.py`` by design (this is a contract task; a later gated
    task performs the real wiring). Accepts any object exposing a FastMCP-style
    ``tool(name=...)`` decorator factory. All registered tools are read-only:
    none writes queue/audit state and none launches a process.
    """
    for name, fn in READONLY_TOOLS.items():
        mcp.tool(name=name)(fn)
    return READONLY_TOOL_NAMES
