"""DRYRUN-ONLY local CLI adapter contract for the AIWorkHub task MCP.

This module defines the next safe step toward launching Claude/Codex/DeepSeek
workers through a static command allowlist. It VALIDATES commands and writes
append-only audit entries ONLY. It launches NOTHING.

Hard invariants (asserted by tests):
  * This module never imports the process-spawn machinery and has no code path
    that starts a child process, execs, forks, or invokes a shell. Launch is
    NOT implemented here; ``LAUNCH_IMPLEMENTED`` is a constant False, not a
    toggle. Setting any env flag CANNOT enable a launch, because no launcher
    exists in this contract.
  * Command validation is a STATIC human-configured safety allowlist (mirrors
    ``phase2_orchestration_contract_b104_v1``), not a runtime cue router.
  * The write gate is OFF by default (``AIWORKHUB_ALLOW_WRITES != '1'``);
    task-queue state is never mutated. Only the append-only audit log is
    written, and only env var NAMES / set-unset status are recorded -- env
    VALUES (including secrets) are never emitted.

Neural-bridge note (AIWorkHub neural-control-first doctrine): the FUTURE selection
of which adapter/runner/topic to launch for a task must become a LEARNED
routing + abstain policy (tracked as MCP_NEURAL_LAUNCH_ROUTING_MIGRATION_V1),
never a regex/keyword cue router. The allowlist below is a static safety
boundary only; it is not the intelligence layer and does not decide capability.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from aiworkhub import core

# ---------------------------------------------------------------------------
# Static safety boundaries (mirror phase2_orchestration_contract_b104_v1)
# ---------------------------------------------------------------------------

# Only these local binaries may EVER be prepared for a (future, gated) launch.
LAUNCHABLE_BINARIES: tuple[str, ...] = ("claude", "codex")

# DeepSeek is a manual, human-in-the-loop external adapter -- never a local
# launchable command.
MANUAL_ONLY: tuple[str, ...] = ("deepseek_manual",)

# The full command allowlist: launchable binaries plus the taskctl subcommands
# a launched worker is expected to run. Anything not prefix-matching an entry
# is blocked and audited.
COMMAND_ALLOWLIST: tuple[str, ...] = (
    "claude",
    "codex",
    "python3 AITools/taskctl.py auto-pickup",
    "python3 AITools/taskctl.py contract",
    "python3 AITools/taskctl.py show",
    "python3 AITools/taskctl.py review",
    "python3 AITools/taskctl.py collision-guard",
    "python3 AITools/taskctl.py verify",
    "python3 AITools/taskctl.py usage",
)

# Argv-list templates prepared ONLY -- never executed by this contract.
ADAPTER_ARGV_TEMPLATES: dict[str, list[str]] = {
    "claude_cli": ["claude", "-p", "<prompt>", "--cwd", "<AIWORKHUB_REPO>"],
    "codex_cli": ["codex", "exec", "<prompt>", "--cwd", "<AIWORKHUB_REPO>"],
    # deepseek_manual has NO local argv: it is a human-paste handoff.
    "deepseek_manual": [],
}

# HARD INVARIANT: launch is not implemented in this contract. There is no code
# path that spawns a process. This is not an env-controlled toggle.
LAUNCH_IMPLEMENTED: bool = False

# Reuse the parent module's secret-name patterns so redaction stays consistent.
_SECRET_ENV_PATTERNS: tuple[str, ...] = core._SECRET_ENV_PATTERNS


# ---------------------------------------------------------------------------
# Gate helpers
# ---------------------------------------------------------------------------

def launch_enabled() -> bool:
    """Process launch is impossible in this contract; always False.

    No env flag can flip this: the launcher does not exist here. The gated
    enablement path is deferred to CLAUDE_TASK_MCP_ORCH_GATED_ENABLEMENT_B106_V1.
    """
    return False


def writes_allowed() -> bool:
    """Mirror the parent write gate (default OFF)."""
    return core.writes_allowed()


def _is_secret_env_name(name: str) -> bool:
    upper = name.upper()
    return any(pat in upper for pat in _SECRET_ENV_PATTERNS)


def redact_env(env: dict[str, str] | None) -> dict[str, str]:
    """Redact ALL env values. Returns only status tokens, never a value.

    * secret-bearing names -> ``"<redacted-secret>"``
    * other names          -> ``"<set>"`` if non-empty else ``"<unset>"``

    An actual env value is never returned, so this output is safe to log.
    """
    env = env or {}
    out: dict[str, str] = {}
    for name, value in env.items():
        if _is_secret_env_name(name):
            out[name] = "<redacted-secret>"
        else:
            out[name] = "<set>" if value != "" else "<unset>"
    return out


# ---------------------------------------------------------------------------
# Command validation (static allowlist only)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandValidation:
    ok: bool
    binary: str
    argv: list[str]
    decision: str
    blocked_reason: str
    is_manual_only: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "binary": self.binary,
            "argv": self.argv,
            "decision": self.decision,
            "blocked_reason": self.blocked_reason,
            "is_manual_only": self.is_manual_only,
        }


def validate_command(argv: list[str]) -> CommandValidation:
    """Validate an argv against the static command allowlist. Executes nothing."""
    if not argv:
        return CommandValidation(
            ok=False, binary="", argv=[], decision="blocked",
            blocked_reason="empty argv", is_manual_only=False,
        )

    argv = list(argv)
    binary = argv[0]

    # Manual-only external adapters are never a local launchable command.
    if binary in MANUAL_ONLY:
        return CommandValidation(
            ok=False, binary=binary, argv=argv, decision="blocked",
            blocked_reason="manual_only adapter is never a local launchable command",
            is_manual_only=True,
        )

    joined = " ".join(argv)
    for prefix in COMMAND_ALLOWLIST:
        if joined == prefix or joined.startswith(prefix + " "):
            return CommandValidation(
                ok=True, binary=binary, argv=argv, decision="allow_dryrun",
                blocked_reason="", is_manual_only=False,
            )

    return CommandValidation(
        ok=False, binary=binary, argv=argv, decision="blocked",
        blocked_reason=f"command not on allowlist: {joined!r}",
        is_manual_only=False,
    )


def build_argv_template(adapter_id: str, prompt: str, repo: str | None = None) -> list[str]:
    """Return the argv-list template an adapter WOULD run. Executes nothing.

    Substitutes ``<prompt>`` and ``<AIWORKHUB_REPO>`` placeholders. Returns [] for
    the manual external adapter (no local argv).
    """
    template = ADAPTER_ARGV_TEMPLATES.get(adapter_id, [])
    repo = repo or str(core.repo_root())
    argv: list[str] = []
    for tok in template:
        if tok == "<prompt>":
            argv.append(prompt)
        elif tok == "<AIWORKHUB_REPO>":
            argv.append(repo)
        else:
            argv.append(tok)
    return argv


# ---------------------------------------------------------------------------
# Dry-run planner (validate + audit only; no execution)
# ---------------------------------------------------------------------------

def plan_dryrun(
    *,
    task_id: str,
    runner: str,
    topic: str,
    adapter_id: str,
    argv: list[str],
    repo: Any = None,
    audit: bool = True,
) -> dict[str, Any]:
    """Validate an allowlisted command and produce a DRYRUN plan.

    Executes NOTHING: no process, no exec, no shell. When ``audit`` is True an
    append-only audit entry is written via ``core.write_audit_entry`` (which
    records only env NAMES / set-unset status, never values).
    """
    validation = validate_command(argv)

    if validation.ok:
        decision = "launch_planned"
        blocked_reason = "dryrun_only_launch_disabled"
        would_run_argv = list(argv)
    else:
        decision = "launch_blocked_invalid_command"
        blocked_reason = validation.blocked_reason
        would_run_argv = []

    plan: dict[str, Any] = {
        "ok": validation.ok,
        "mode": "dryrun_only",
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "adapter_id": adapter_id,
        "binary": validation.binary,
        "command_valid": validation.ok,
        "would_run_argv": would_run_argv,
        "decision": decision,
        "blocked_reason": blocked_reason,
        "launch_enabled": launch_enabled(),
        "launch_implemented": LAUNCH_IMPLEMENTED,
        "writes_allowed": writes_allowed(),
        "authority_flags": {
            "write_gate_enabled": not writes_allowed(),
            "process_launch": False,
            "agent_launch": False,
            "shell_invocation": False,
        },
    }

    if audit:
        entry = core.write_audit_entry(
            tool_name="cli_adapter_dryrun",
            action=decision,
            blocked_reason=blocked_reason,
            repo=repo,
        )
        plan["audit_entry_action"] = entry["action"]

    return plan


def contract_summary() -> dict[str, Any]:
    """Return the static contract shape (no side effects, no launch)."""
    return {
        "mode": "mcp_cli_adapter_contract_no_process_launch",
        "launch_implemented": LAUNCH_IMPLEMENTED,
        "launch_enabled": launch_enabled(),
        "launchable_binaries": list(LAUNCHABLE_BINARIES),
        "manual_only": list(MANUAL_ONLY),
        "command_allowlist": list(COMMAND_ALLOWLIST),
        "writes_allowed_default_off": not writes_allowed(),
        "audit_reuse": "core.write_audit_entry",
        "neural_bridge_migration": "MCP_NEURAL_LAUNCH_ROUTING_MIGRATION_V1",
    }
