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
an already-active claim?" with the SAME active-bucket semantics the live
``core.collision_guard`` uses (``task_store.canonical_status`` +
``pending``/``processing``/``review``), so the two can never disagree on the
active-card count. No lock is taken, no claim is written, and no queue/audit
state is ever mutated -- it only classifies cards that are already on disk.

Card source resolution is FAIL-CLOSED. When an operator EXPLICITLY configures a
JSONL source (``AIWORKHUB_COLLISION_CARDS_PATH`` or the legacy
``BITNN_TASK_CARDS_PATH``) that source is authoritative; a configured source
that cannot be READ -- whether it is MISSING or exists but is UNREADABLE (a
directory where a file was expected, a permission-denied file, an unreadable
device, undecodable bytes) -- is reported as UNRESOLVED (``cards_source_resolved``
false, ``active_card_count`` / ``would_collide`` ``None``) rather than a
confident "0 active cards, safe to proceed". A missing file and an unreadable
file are the SAME epistemic state -- no cards were observed -- so the preflight
must never answer "no collisions" when what actually happened is "I could not
look"; approving overlapping write scopes because the preflight could not read
its own source is the original defect. With no override the preflight resolves
from the canonical task store of the BOUND repository (no hardcoded foreign
``bitnnv2`` path), and an unavailable store -- a readiness error OR a
non-readiness SQLite failure (a deleted/locked database after the readiness
check) -- is UNRESOLVED for the same reason. A ``repo`` argument SCOPES that
canonical scan to a named repository (the same parameter ``plan_dryrun``
honours), so the preflight and the plan answer about the SAME repository rather
than one silently answering about the process-bound one.

REDACTION applies to EVERY branch, not just the failure path (the success path
is the one that runs constantly): neither the UNRESOLVED structure
(``unresolved_reason`` AND ``cards_source``) NOR the RESOLVED explicit-source
result leaks the operator's absolute filesystem path -- ``cards_source`` names
the env var (``configured:<ENV>``) or the canonical store, never the path.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from aiworkhub import cli_adapter_dryrun as dryrun
from aiworkhub import core
from aiworkhub import sqlite_readonly
from aiworkhub import task_store


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

# The active lifecycle buckets that count as a live claim -- the EXACT set the
# production collision guard uses (see core._active_cards_for_collision_guard).
_ACTIVE_STATUSES = frozenset({"pending", "processing", "review"})


def _explicit_cards_source() -> tuple[str | None, str | None]:
    """Return ``(raw_path, env_name)`` when an operator EXPLICITLY configured a
    JSONL card source, else ``(None, None)``.

    There is no hardcoded default. The former
    ``bitnnv2/data/tasking/machine_task_cards_v1.jsonl`` fallback pointed at a
    foreign repo layout that does not exist in an installed/VSIX runtime, so an
    unconfigured preflight now resolves from the canonical task store of the
    bound repository instead (see :func:`collision_preflight`).
    """
    override = os.environ.get(_COLLISION_CARDS_ENV, "").strip()
    if override:
        return override, _COLLISION_CARDS_ENV
    legacy = os.environ.get(_COLLISION_CARDS_LEGACY_ENV, "").strip()
    if legacy:
        return legacy, _COLLISION_CARDS_LEGACY_ENV
    return None, None


def _load_cards(cards_path: Path) -> tuple[list[dict[str, Any]], int]:
    """Read the machine task-card JSONL into ``(cards, skipped_malformed)``.

    Tolerant to a bad ROW: a blank line is skipped, and an individually
    malformed JSON line or a non-object line is SKIPPED and COUNTED rather than
    disabling the whole diagnostic. It is deliberately NOT tolerant to a source
    it cannot READ: a read failure (missing / directory / permission-denied /
    undecodable bytes) PROPAGATES so the caller fails closed with the UNRESOLVED
    shape instead of a confident zero. Never acquires a lock and never mutates
    the file.
    """
    cards: list[dict[str, Any]] = []
    skipped_malformed = 0
    text = cards_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            card = json.loads(line)
        except json.JSONDecodeError:
            skipped_malformed += 1
            continue
        if isinstance(card, dict):
            cards.append(card)
        else:
            skipped_malformed += 1
    return cards, skipped_malformed


def _collision_guard_helpers():
    """Pure, read-only card classification helpers for the B107 preflight:
    ``(ACTIVE_STATUSES, canonical_status, load_cards)``.

    ``canonical_status`` is the production ``task_store.canonical_status`` and
    ``ACTIVE_STATUSES`` is the exact active-bucket set the live
    ``core.collision_guard`` uses, so the preflight can never drift from
    production semantics. These ship INSIDE the installed package: the former
    ``scripts/build_tasking_parallel_group_collision_guard_v1.py`` import was
    unshippable (that path is not packaged into the wheel or the VSIX runtime,
    and that file no longer exists anywhere), so any invocation on an
    installed/VSIX runtime raised ``ModuleNotFoundError``.
    """
    return _ACTIVE_STATUSES, task_store.canonical_status, _load_cards


def _active_cards_for_repo(repo: Any) -> list[dict[str, Any]]:
    """READ-ONLY active-card scan scoped to an EXPLICIT repository ``repo``.

    Mirrors :func:`core._active_cards_for_collision_guard` EXACTLY -- the same
    canonical ``tasks`` scan, the SAME production classifier
    (``task_store.canonical_status``) and the SAME active bucket set
    (:data:`_ACTIVE_STATUSES`) -- but against the canonical store of the
    repository the CALLER named rather than the process-bound one, so a preflight
    can never answer about the ambient repo when it was asked about another (the
    same defect this card exists to kill, one level up in the signature).

    The store is opened through the hardened read-only helper
    (``sqlite_readonly.connect_readonly`` -- ``as_uri`` percent-encoding plus
    ``PRAGMA query_only``), so it can neither create nor mutate a file. An
    unavailable store raises (``StorageNotReadyError`` from the readiness check,
    or a bare ``sqlite3.Error`` from a deleted/locked DB after it) exactly like
    the canonical path, so the caller fails closed with the UNRESOLVED shape.
    """
    readiness = task_store.storage_readiness(repo)
    if not readiness.ready:
        raise task_store.StorageNotReadyError(readiness.reason)
    conn = sqlite_readonly.connect_readonly(Path(readiness.canonical_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT task_id, runner, topic, status, worker_status, card_json, archived_at FROM tasks"
        ).fetchall()
    finally:
        conn.close()
    active: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        bucket = task_store.canonical_status(item)
        if bucket not in _ACTIVE_STATUSES:
            continue
        try:
            card_json = json.loads(item.get("card_json") or "{}")
        except json.JSONDecodeError:
            card_json = {}
        card = {**card_json, **{k: v for k, v in item.items() if k != "card_json"}}
        card["status"] = bucket
        active.append(card)
    return active


def _classify_collision(
    active_cards: list[dict[str, Any]], *, task_id: str, runner: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
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
    return same_task, same_runner_other_task, reasons


def _resolved_preflight(
    active_cards: list[dict[str, Any]],
    *,
    task_id: str,
    runner: str,
    cards_source: str,
    skipped_malformed: int,
) -> dict[str, Any]:
    same_task, same_runner_other_task, reasons = _classify_collision(
        active_cards, task_id=task_id, runner=runner
    )
    return {
        "would_collide": bool(reasons),
        "read_only": True,
        "mutated_state": False,
        "lock_acquired": False,
        "claim_written": False,
        "cards_source": cards_source,
        "cards_source_resolved": True,
        "cards_source_exists": True,
        "active_card_count": len(active_cards),
        "matching_task_id_active_claims": len(same_task),
        "same_runner_other_active_claims": len(same_runner_other_task),
        "skipped_malformed_rows": skipped_malformed,
        "collision_reasons": reasons,
        "unresolved_reason": None,
    }


def _unresolved_preflight(*, cards_source: str, unresolved_reason: str) -> dict[str, Any]:
    """FAIL-CLOSED result: an unusable card source is UNRESOLVED, never a
    confident zero. The WHOLE structure is path-free -- both ``unresolved_reason``
    and ``cards_source`` name WHY (the env var / store) without leaking an
    absolute filesystem path; ``active_card_count`` / ``would_collide`` are
    ``None`` so a caller can never misread "could not resolve" as "safe to
    proceed". Callers MUST pass a path-free ``cards_source`` here.
    """
    return {
        "would_collide": None,
        "read_only": True,
        "mutated_state": False,
        "lock_acquired": False,
        "claim_written": False,
        "cards_source": cards_source,
        "cards_source_resolved": False,
        "cards_source_exists": False,
        "active_card_count": None,
        "matching_task_id_active_claims": None,
        "same_runner_other_active_claims": None,
        "skipped_malformed_rows": None,
        "collision_reasons": [],
        "unresolved_reason": unresolved_reason,
    }


def collision_preflight(
    *,
    task_id: str,
    runner: str,
    topic: str | None = None,
    repo: Any = None,
) -> dict[str, Any]:
    """READ-ONLY: would this task_id/runner collide with an active claim?

    No lock is acquired, no claim is written, and no queue/audit state is
    mutated. ``would_collide`` is true when an ACTIVE (pending/processing/
    review) card already claims this exact ``task_id``, or when the same
    ``runner`` already holds a different active task.

    ``repo`` SCOPES the canonical scan to a named repository -- the same
    parameter ``plan_dryrun`` honours -- so the two sibling surfaces answer
    about the SAME repository. When ``repo`` is None the process-bound repository
    is scanned through the canonical guard's own function (so the counts can
    never drift from the authority); when a ``repo`` is given, that repository's
    canonical store is scanned with identical production semantics
    (:func:`_active_cards_for_repo`). A parameter that were accepted and silently
    dropped would let a caller get a confident answer about the WRONG repository
    -- the very defect this card exists to eliminate -- so ``repo`` is either
    honoured or (on an unreadable store) reported UNRESOLVED, never swallowed.

    ``topic`` is accepted for call-shape symmetry with ``plan_dryrun`` but is
    DELIBERATELY not part of collision semantics and is therefore no longer
    required: the canonical collision guard scopes by ``task_id``/``runner``
    across ALL active cards regardless of topic, and narrowing the active set by
    ``topic`` here would WEAKEN the guard (it could miss a real collision), which
    the authority forbids. It is documented-and-unused rather than silently
    required so the signature promises only what it performs.

    Resolution is FAIL-CLOSED (see the module docstring): an EXPLICITLY
    configured JSONL source that does not exist, or an unavailable canonical
    task store, is reported as UNRESOLVED rather than a confident zero.
    """
    explicit, env_name = _explicit_cards_source()
    if explicit is not None:
        cards_path = Path(explicit).expanduser()
        # A configured source is UNRESOLVED unless it can actually be READ. A
        # missing file and an unreadable one (a directory where a file was
        # expected, a permission-denied file, an unreadable device, undecodable
        # bytes) are the SAME epistemic state -- no cards observed -- so BOTH
        # fail closed rather than returning a confident zero. The honest "why"
        # AND ``cards_source`` name the env var, NOT the (absolute) path it held.
        try:
            cards, skipped_malformed = _load_cards(cards_path)
        except FileNotFoundError:
            return _unresolved_preflight(
                cards_source=f"configured:{env_name}",
                unresolved_reason=f"configured_cards_source_missing:{env_name}",
            )
        except (OSError, UnicodeDecodeError) as exc:
            return _unresolved_preflight(
                cards_source=f"configured:{env_name}",
                unresolved_reason=(
                    f"configured_cards_source_unreadable:{env_name}:{type(exc).__name__}"
                ),
            )
        active_cards = [
            card for card in cards if task_store.canonical_status(card) in _ACTIVE_STATUSES
        ]
        # PATH-FREE on the RESOLVED branch too: the success path runs constantly,
        # so redacting only the failure path is not redaction. ``cards_source``
        # names the env var that configured the source, never the operator's
        # absolute filesystem path -- honouring the module's redaction invariant.
        return _resolved_preflight(
            active_cards,
            task_id=task_id,
            runner=runner,
            cards_source=f"configured:{env_name}",
            skipped_malformed=skipped_malformed,
        )

    # No JSONL override: resolve from the canonical task store, using the SAME
    # scan the live collision guard uses so the counts agree. ``repo`` SCOPES
    # which repository's store is read: with no ``repo`` the process-bound store
    # is read through the guard's OWN function (the authority, so counts can
    # never drift); with an explicit ``repo`` that repository's canonical store
    # is scanned with identical production semantics. Any failure to read the
    # store is UNRESOLVED, not a confident zero: a ``TaskStoreError`` from the
    # readiness check, and ALSO the non-readiness SQLite failures a
    # deleted/locked database raises AFTER it inside the scan (``sqlite3.Error``,
    # which is NOT a ``TaskStoreError`` so it would otherwise escape or degrade).
    try:
        if repo is None:
            active_cards = core._active_cards_for_collision_guard()
        else:
            active_cards = _active_cards_for_repo(repo)
    except (task_store.TaskStoreError, sqlite3.Error):
        return _unresolved_preflight(
            cards_source="canonical_task_store",
            unresolved_reason="canonical_task_store_unavailable",
        )
    return _resolved_preflight(
        active_cards,
        task_id=task_id,
        runner=runner,
        cards_source="canonical_task_store",
        skipped_malformed=0,
    )


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
    # ``repo`` is forwarded so the preflight scopes to the SAME repository the
    # plan does; ``topic`` is not part of collision semantics and so is not.
    preflight = collision_preflight(task_id=task_id, runner=runner, repo=repo)
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
