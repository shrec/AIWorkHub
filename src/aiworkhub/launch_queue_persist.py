"""Append-only persistence + audit for the disabled MCP launch-queue contract (B119).

This module PERSISTS the pure ``launch_queue_contract`` schemas (LaunchRequest /
LaunchDecision / ProcessLogEntry) to an append-only JSONL log so a future gated
launcher (CLAUDE_TASK_MCP_ORCH_GATED_ENABLEMENT_B106_V1) inherits a durable audit
trail. It still LAUNCHES NOTHING: no subprocess/exec/fork/shell/network code
exists anywhere in this file, and every persisted record's ``decision`` +
``to_state`` stay ``blocked_launch_disabled`` while the env gates are unset
(the default) because ``launch_queue_contract.evaluate_launch`` -- which this
module calls unmodified -- always returns that verdict short of a launcher
actually being implemented.

Hard invariants (asserted by test_launch_queue_persist_audit_b119_v1.sh):
  * Append-only: records are always opened in ``"a"`` mode; nothing is ever
    truncated, rewritten, or deleted. Two calls in sequence strictly grow the
    file by one line each.
  * With both env gates unset (the default), every persisted entry's
    ``to_state`` == ``ProcessState.BLOCKED_LAUNCH_DISABLED`` and ``decision``
    == ``"blocked_launch_disabled"``.
  * No child-process is ever spawned: no ``subprocess`` module, and no
    os-level fork/exec/spawn/system/popen call, no shell, no network call.
    Only ``json``, ``os.environ`` (pure reads), and ``pathlib`` file I/O are
    used.
  * Secrets never leak: persisted records carry only the env NAME->status
    tokens already produced by ``launch_queue_contract.env_gate_status_tokens``
    (``"<set>"``/``"<unset>"``), never a raw env value. ``_scrub_entry`` also
    strips any stray top-level key outside the frozen ``ProcessLogEntry``
    schema before a record is written, as defense in depth against a caller
    passing extra kwargs.
  * Isolation-safe / parallel-safe: the log path is resolvable from an explicit
    argument, then ``AIWORKHUB_LAUNCH_QUEUE_LOG_PATH``, then a repo-root
    default -- so concurrent test runs can each point at their own mktemp file
    and never race on a shared path.

The sibling ``launch_queue_contract`` module is loaded by FILE PATH (not via
the package ``__init__``), matching the isolation pattern already used by
``launch_disabled_queue_contract_smoke.py``: a concurrent worker editing
``__init__.py`` or ``server.py`` cannot break this module's import, and this
module itself can also be loaded standalone by file path in a test harness.

Neural-bridge note: persistence here is a deterministic evidence/audit
substrate only, never a decision authority -- the eventual choice of which
adapter/runner/topic to launch remains scoped to the learned routing +
abstain policy tracked as MCP_NEURAL_LAUNCH_ROUTING_MIGRATION_V1.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Load launch_queue_contract by FILE PATH (isolation-safe; no package import).
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_CONTRACT_PATH = _HERE / "launch_queue_contract.py"
_CONTRACT_MODULE_NAME = "aiworkhub._launch_queue_contract_b119_persist"


def _load_contract_module():
    cached = sys.modules.get(_CONTRACT_MODULE_NAME)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(_CONTRACT_MODULE_NAME, _CONTRACT_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build import spec for {_CONTRACT_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_CONTRACT_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


lqc = _load_contract_module()

# ---------------------------------------------------------------------------
# Log path resolution (explicit arg > env override > repo-root default)
# ---------------------------------------------------------------------------
LAUNCH_QUEUE_LOG_ENV: str = "AIWORKHUB_LAUNCH_QUEUE_LOG_PATH"
# Repository-local, non-durable runtime tree (never the historical
# any package-install/monorepo log path): .aiworkhub/runtime/process_logs/.
LAUNCH_QUEUE_LOG_DEFAULT_REL = Path(".aiworkhub/runtime/process_logs/launch_queue_audit.jsonl")

# Frozen ProcessLogEntry schema; any other top-level key is stripped before
# writing (defense in depth -- see _scrub_entry).
_PROCESS_LOG_KEYS = (
    "request_id", "task_id", "runner", "topic", "adapter_id",
    "from_state", "to_state", "decision", "blocked_reason",
    "env_gate_status", "ts", "model", "owner_prompt", "requested_at",
    "stale_timeout_seconds", "rollback_of_request_id",
)


def default_repo_root() -> Path:
    return Path(os.environ.get("AIWORKHUB_REPO", str(Path.cwd()))).expanduser().resolve()


def resolve_log_path(
    *, repo: Path | None = None, log_path: str | os.PathLike[str] | None = None
) -> Path:
    """Resolve the append-only log path. Never creates or opens the file."""
    if log_path is not None:
        return Path(log_path).expanduser().resolve()
    env_path = os.environ.get(LAUNCH_QUEUE_LOG_ENV, "")
    if env_path:
        return Path(env_path).expanduser().resolve()
    root = repo or default_repo_root()
    return root / LAUNCH_QUEUE_LOG_DEFAULT_REL


def _scrub_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Keep only the frozen ProcessLogEntry keys; drop anything else.

    Defense in depth: even if a caller's dict grew extra keys (e.g. a raw env
    mapping accidentally merged in), only the known-safe schema keys are ever
    written to disk. ``env_gate_status`` itself is already NAME->status-token
    only (never a value) by construction in launch_queue_contract.
    """
    return {k: entry[k] for k in _PROCESS_LOG_KEYS if k in entry}


def persist_transition(
    request: "lqc.LaunchRequest",
    decision: "lqc.LaunchDecision | None" = None,
    *,
    ts: float | None = None,
    rollback_of_request_id: str | None = None,
    repo: Path | None = None,
    log_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Append one state-transition record to the JSONL audit log. No launch.

    ``decision`` defaults to ``evaluate_launch(request)`` (always
    ``permitted=False`` / ``blocked_launch_disabled`` while no launcher is
    implemented). The file is opened in append mode only -- an existing log
    is never truncated or rewritten. Returns the exact scrubbed entry dict
    that was written.
    """
    if decision is None:
        decision = lqc.evaluate_launch(request)
    entry = _scrub_entry(
        lqc.build_log_entry(
            request,
            decision,
            ts=ts,
            rollback_of_request_id=rollback_of_request_id,
        ).as_dict()
    )

    path = resolve_log_path(repo=repo, log_path=log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def persist_intent(
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
    rollback_of_request_id: str | None = None,
    ts: float | None = None,
    repo: Path | None = None,
    log_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Full pure pipeline: enqueue -> evaluate -> persist. Launches nothing.

    Convenience wrapper over enqueue_intent/evaluate_launch/persist_transition
    from launch_queue_contract; every field is pure data, and the persisted
    record is append-only.
    """
    request = lqc.enqueue_intent(
        task_id=task_id,
        runner=runner,
        topic=topic,
        adapter_id=adapter_id,
        argv_template=argv_template,
        cwd_pin=cwd_pin,
        request_id=request_id,
        created_ts=created_ts,
        priority=priority,
        model=model,
        owner_prompt=owner_prompt,
        requested_at=requested_at,
        stale_timeout_seconds=stale_timeout_seconds,
    )
    decision = lqc.evaluate_launch(request)
    entry = persist_transition(
        request,
        decision,
        ts=ts,
        rollback_of_request_id=rollback_of_request_id,
        repo=repo,
        log_path=log_path,
    )
    return {
        "request": request.as_dict(),
        "decision": decision.as_dict(),
        "persisted_entry": entry,
    }


def find_open_requests(
    task_id: str,
    *,
    repo: Path | None = None,
    log_path: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """Read-only helper: return non-terminal, non-rolled-back entries for task_id.

    ``blocked_launch_disabled`` is intentionally treated as open for the MCP
    MVP: it is the durable queued/blocked request record while launch remains
    disabled, and it should still participate in idempotency and duplicate
    runner protection.
    """
    summary = read_persisted_log(repo=repo, log_path=log_path, max_entries=1_000_000)
    entries = summary.get("last_entries", []) or []
    rollback_ids = {
        e.get("rollback_of_request_id")
        for e in entries
        if e.get("decision") == "rolled_back" and e.get("rollback_of_request_id")
    }
    open_states = {
        lqc.ProcessState.QUEUED.value,
        lqc.ProcessState.PLANNED.value,
        lqc.ProcessState.BLOCKED_LAUNCH_DISABLED.value,
    }
    out: list[dict[str, Any]] = []
    for entry in entries:
        request_id = entry.get("request_id")
        if entry.get("task_id") != task_id:
            continue
        if request_id in rollback_ids:
            continue
        if entry.get("decision") == "rolled_back":
            continue
        if entry.get("to_state") not in open_states:
            continue
        out.append(entry)
    return out


def read_persisted_log(
    *,
    repo: Path | None = None,
    log_path: str | os.PathLike[str] | None = None,
    max_entries: int = 200,
) -> dict[str, Any]:
    """Read-only summary of the append-only launch-queue audit log.

    Never writes, never enables writes, never launches a process. Reports
    whether every persisted record so far is ``blocked_launch_disabled`` --
    the expected state while both env gates are unset (default).
    """
    path = resolve_log_path(repo=repo, log_path=log_path)
    result: dict[str, Any] = {
        "ok": True,
        "log_path": str(path),
        "log_exists": path.exists(),
        "total_entries": 0,
        "entries_by_decision": {},
        "entries_by_to_state": {},
        "all_blocked_launch_disabled": True,
        "last_entries": [],
    }
    if not path.exists():
        return result

    try:
        raw = path.read_text(encoding="utf-8")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        entries: list[dict[str, Any]] = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        result["total_entries"] = len(entries)

        decision_counts: dict[str, int] = {}
        state_counts: dict[str, int] = {}
        all_blocked = True
        blocked_value = lqc.ProcessState.BLOCKED_LAUNCH_DISABLED.value
        for e in entries:
            d = e.get("decision", "unknown")
            decision_counts[d] = decision_counts.get(d, 0) + 1
            s = e.get("to_state", "unknown")
            state_counts[s] = state_counts.get(s, 0) + 1
            if s != blocked_value:
                all_blocked = False

        result["entries_by_decision"] = decision_counts
        result["entries_by_to_state"] = state_counts
        result["all_blocked_launch_disabled"] = all_blocked
        result["last_entries"] = entries[-max_entries:] if max_entries > 0 else []
    except OSError as exc:
        result["ok"] = False
        result["error"] = str(exc)

    return result


def contract_summary() -> dict[str, Any]:
    """Static introspection: this module's persistence contract shape."""
    return {
        "mode": "launch_queue_persist_audit_no_process_launch",
        "log_path_env": LAUNCH_QUEUE_LOG_ENV,
        "log_path_default_rel": str(LAUNCH_QUEUE_LOG_DEFAULT_REL),
        "append_only": True,
        "process_log_schema": list(_PROCESS_LOG_KEYS),
        "env_recorded_as_status_tokens_only": True,
        "launch_implemented": lqc.LAUNCH_IMPLEMENTED,
        "launch_enabled": lqc.launch_enabled(),
        "no_launch_code_marker": (
            "no child-process spawn, no os-level fork/exec/spawn/system/popen, "
            "no shell invocation, and no network/API call in this module"
        ),
        "deferred_to_gated_phase": "CLAUDE_TASK_MCP_ORCH_GATED_ENABLEMENT_B106_V1",
        "neural_bridge_migration": "MCP_NEURAL_LAUNCH_ROUTING_MIGRATION_V1",
        "public_api": {
            "resolve_log_path": "-> Path, pure, never opens the file",
            "persist_transition": "-> dict, appends one scrubbed ProcessLogEntry record",
            "persist_intent": "-> dict(request, decision, persisted_entry), full pure pipeline + append",
            "find_open_requests": "-> list[dict], read-only duplicate/idempotency helper",
            "read_persisted_log": "-> dict summary, read-only, no side effects",
            "contract_summary": "-> dict, pure introspection",
        },
    }
