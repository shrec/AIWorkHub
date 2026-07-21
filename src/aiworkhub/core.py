from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from . import (
    COORDINATOR_TOKEN_ENV,
    COORDINATOR_TOKEN_FILE_ENV,
    coordinator_config,
    refresh_coordinator_config,
)
from . import repository_state
from . import task_store
from . import callback_store


DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("AIWORKHUB_TIMEOUT", "60"))
# Repository-local runtime tree (never durable, never shared across repos):
# .aiworkhub/runtime/process_logs/audit.jsonl -- see repository_state.py's
# RepositoryManifest runtime layout ("process_logs" is one of its declared
# contents). Never a fixed path relative to this package's own install
# location or to a historical monorepo layout.
AUDIT_LOG_DEFAULT_REL = (
    Path(repository_state.HUB_DIRNAME)
    / repository_state.RUNTIME_DIRNAME
    / "process_logs"
    / "audit.jsonl"
)

_SECRET_ENV_PATTERNS = (
    "SECRET", "TOKEN", "PASSWORD", "PASSWD", "KEY", "API_KEY",
    "CREDENTIAL", "AUTH", "PRIVATE", "CERT",
)


WRITE_COMMANDS = {
    "add-card",
    "archive",
    "auto-pickup",
    "claim-start",
    "done",
    "export-jsonl",
    "import-jsonl",
    "init-db",
    "owner-review-recover",
    "pickup",
    "recover-stale",
    "restore",
    "review",
    "reject-review",
    "release-launch",
    "stage",
    "start",
    "unstick-pending",
    "usage",
}

COORDINATOR_COMMANDS = frozenset({"done", "reject-review", "release-launch", "archive", "restore"})
# COORDINATOR_TOKEN_ENV / COORDINATOR_TOKEN_FILE_ENV are re-exported from the
# package __init__ (imported above) so existing callers keep working against
# core.COORDINATOR_TOKEN_ENV unchanged.
DEFAULT_COORDINATOR_TOKEN_FILE = Path.home() / ".config/aiworkhub/taskctl_coordinator.token"
# RFC 9562 defines UUID versions 6, 7 and 8 in addition to the historical
# 1-5 set.  Current Codex thread ids are UUIDv7, so rejecting the version
# nibble above 5 discards a genuine mux-owned origin thread.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_TASK_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

# ---------------------------------------------------------------------------
# B852: canonical, repo-local task-store write layer.
#
# Every function below this block talks directly to the per-repo canonical
# ``.aiworkhub/tasking/task_queue.sqlite`` via ``task_store.py``. None of them
# ever imports or shells out to ``AITools/taskctl.py`` / ``AITools/taskdb.py``:
# a repository with no ``AITools/`` directory at all gets a fully working
# AIWorkHub task lifecycle. Two repos with the same task_id stay isolated
# because ``repo_root()`` (and therefore the canonical DB path each of these
# helpers resolves) is per-process/per-config, never a shared fixed path.
# ---------------------------------------------------------------------------


def _canonical_db_path() -> Path:
    readiness = task_store.storage_readiness(repo_root())
    if not readiness.ready:
        raise task_store.StorageNotReadyError(readiness.reason)
    return Path(readiness.canonical_db)


def _canonical_connect(*, readonly: bool = False) -> sqlite3.Connection:
    path = _canonical_db_path()
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _canonical_result(
    *,
    ok: bool,
    stdout: str = "",
    stderr: str = "",
    returncode: int | None = None,
    command: list[str] | None = None,
) -> dict[str, Any]:
    """Build a ``TaskCtlResult.as_dict()``-shaped envelope without ever
    launching a subprocess. Keeps every existing caller (server.py tool
    wrappers, ``_live_card``, ``_parse_show_task_card``,
    ``_extract_collision_report``, ``_parse_jsonl``) working unchanged."""
    return {
        "ok": ok,
        "returncode": returncode if returncode is not None else (0 if ok else 1),
        "command": command or [],
        "stdout": stdout,
        "stderr": stderr,
    }


def _claude_manager_identity() -> dict[str, str] | None:
    """Verify that this MCP server is the direct child of an interactive
    Claude Code VS Code session bound to the same repository.

    The parent process and Claude's per-PID session descriptor are both
    same-uid local runtime state. No credential is read or exposed.
    """
    parent_pid = os.getppid()
    try:
        cmdline = Path(f"/proc/{parent_pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8")
        descriptor_path = Path.home() / ".claude" / "sessions" / f"{parent_pid}.json"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(descriptor, dict) or "/claude " not in cmdline:
        return None
    if int(descriptor.get("pid") or -1) != parent_pid:
        return None
    if descriptor.get("kind") != "interactive" or descriptor.get("entrypoint") != "claude-vscode":
        return None
    session_id = str(descriptor.get("sessionId") or "").strip()
    if not _UUID_RE.fullmatch(session_id):
        return None
    try:
        descriptor_cwd = Path(str(descriptor.get("cwd") or "")).resolve()
    except (OSError, RuntimeError):
        return None
    if descriptor_cwd != repo_root():
        return None
    return {
        "provider": "claude",
        "session_id": session_id,
        "window_id": f"claude_vscode_{parent_pid}",
    }


def _codex_manager_identity() -> dict[str, str] | None:
    """Verify the bounded local process chain for this Codex chat MCP.

    Codex spawns MCP servers below its App Server process, so the server's
    *direct* parent is not named ``codex``.  Requiring a direct parent made a
    genuine manager look like a headless worker and forced users to shuttle a
    mutable host-global token.  Accept only a same-uid, short ancestor chain
    containing BOTH the Codex App Server and the installed AIWorkHub mux.
    """
    pid = os.getppid()
    saw_codex_app_server = False
    mux_pid = 0
    for _ in range(6):
        if pid <= 1:
            break
        proc = Path(f"/proc/{pid}")
        try:
            if proc.stat().st_uid != os.geteuid():
                return None
            cmdline = proc.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8")
            status = proc.joinpath("status").read_text(encoding="utf-8")
            parent_pid = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
        except (OSError, UnicodeDecodeError, StopIteration, ValueError):
            return None
        lowered = cmdline.lower()
        if "codex" in lowered and "app-server" in lowered:
            saw_codex_app_server = True
        if "aiworkhub-app-server-mux" in lowered:
            mux_pid = pid
            break
        pid = parent_pid
    if not saw_codex_app_server or not mux_pid:
        return None
    identity = {
        "provider": "codex",
        "session_id": f"codex_mux_{mux_pid}",
        "window_id": f"codex_vscode_{mux_pid}",
    }
    try:
        from . import app_server_mux

        matches = [
            instance for instance in app_server_mux.list_live_sideband_instances(
                app_server_mux.default_sideband_dir()
            )
            if instance.pid == mux_pid and instance.owned_thread_ids
        ]
        if len(matches) == 1:
            thread_id = matches[0].owned_thread_ids[-1]
            if _UUID_RE.fullmatch(thread_id):
                identity["thread_id"] = thread_id
                identity["session_id"] = thread_id
    except (OSError, RuntimeError):
        pass
    return identity


def _current_chat_provider(card: dict[str, Any] | None = None) -> str:
    """Resolve callback ownership from the task/session, never a dashboard
    global toggle when a concrete chat identity is available."""
    explicit = str((card or {}).get("coordinator_provider") or "").strip().lower()
    if explicit in ("codex", "claude", "copilot"):
        return explicit
    if _claude_manager_identity() is not None:
        return "claude"
    try:
        parent_cmd = Path(f"/proc/{os.getppid()}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8").lower()
    except (OSError, UnicodeDecodeError):
        parent_cmd = ""
    if "codex" in parent_cmd:
        return "codex"
    try:
        selected = read_selected_coordinator_target(repo_root()).get("selected_provider")
    except Exception:
        selected = ""
    return selected if selected in ("codex", "claude", "copilot") else "codex"


def _verify_coordinator_capability(runner: str | None) -> tuple[bool, str]:
    """In-process equivalent of ``AITools/taskctl.py::_require_coordinator``.

    Reuses the exact same trusted pieces core.py already had for this
    (``coordinator_config()``, ``DEFAULT_COORDINATOR_TOKEN_FILE``,
    ``scrub_coordinator_capability_from_environment``) instead of shelling
    out to taskctl.py and letting ITS ``_require_coordinator`` do the check
    inside a child process.
    """
    claude_identity = _claude_manager_identity()
    if claude_identity is not None:
        return True, "trusted_claude_manager_route"
    if runner == CODEX_RUNNER and _codex_manager_identity() is not None:
        return True, "trusted_codex_manager_route"
    if runner != CODEX_RUNNER:
        return False, f"coordinator_runner_mismatch:expected={CODEX_RUNNER}:got={runner}"
    scrub_coordinator_capability_from_environment()
    supplied, configured_path = coordinator_config()
    token_path = (
        Path(configured_path).expanduser() if configured_path else DEFAULT_COORDINATOR_TOKEN_FILE
    ).resolve()
    try:
        fd = os.open(str(token_path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return False, "coordinator_capability_denied:token_file_unreadable"
    try:
        file_stat = os.fstat(fd)
        mode = stat.S_IMODE(file_stat.st_mode)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.geteuid():
            return False, "coordinator_capability_denied:token_file_not_owner_regular"
        with os.fdopen(fd, encoding="utf-8") as fh:
            fd = -1
            expected = fh.read().strip()
    finally:
        if fd >= 0:
            os.close(fd)
    if mode != 0o600:
        return False, "coordinator_capability_denied:token_file_permissions"
    if not supplied or not expected or not hmac.compare_digest(supplied, expected):
        return False, "coordinator_capability_denied:token_mismatch"
    return True, "ok"


def _canonical_write_gate(
    action: str,
    *,
    runner: str | None = None,
    topic: str | None = None,
    coordinator_capability: bool = False,
    task_id: str | None = None,
) -> dict[str, Any] | None:
    """Faithful in-process replica of ``run_taskctl``'s write-protection
    sequence (write gate -> runner/topic allowlist -> coordinator token),
    minus the subprocess launch. Returns a blocked ``TaskCtlResult``-shaped
    dict if the write is refused, else ``None``."""
    scrub_coordinator_capability_from_environment()

    if not writes_allowed():
        blocked_reason = (
            "write command blocked; set AIWORKHUB_ALLOW_WRITES=1 "
            "and pass allow_write=True"
        )
        write_audit_entry(tool_name=action, action="blocked_write", blocked_reason=blocked_reason)
        return _canonical_result(ok=False, returncode=126, stderr=blocked_reason, command=[action])

    if runner is not None or topic is not None:
        decision = check_runner_topic_allowlist(runner, topic, action)
        if not decision["allowed"]:
            card_decision: dict[str, Any] = {"allowed": False, "reason": "card_scoped_task_id_required"}
            if task_id is not None:
                card_decision = _check_card_scoped_write_authority([action, task_id], runner, topic)
            if card_decision.get("allowed"):
                decision = card_decision
            else:
                reason = card_decision.get("reason") or decision["reason"]
                audit_action = (
                    "blocked_malformed_runner_or_topic"
                    if str(reason).startswith("malformed_")
                    else "blocked_runner_topic_not_in_allowlist"
                )
                blocked_reason = f"runner/topic allowlist denied: {reason}"
                write_audit_entry(tool_name=action, action=audit_action, blocked_reason=blocked_reason)
                return _canonical_result(ok=False, returncode=126, stderr=blocked_reason, command=[action])

    if coordinator_capability:
        cap_ok, cap_reason = _verify_coordinator_capability(runner)
        if not cap_ok:
            write_audit_entry(
                tool_name=action, action="blocked_coordinator_capability", blocked_reason=cap_reason
            )
            return _canonical_result(ok=False, returncode=126, stderr=cap_reason, command=[action])

    return None

# The actual env-var pop happens at package-init time (see
# aiworkhub/__init__.py::refresh_coordinator_config) -- before this
# module or any sibling submodule executes any of its own top-level code.
# That closes the import-order race a module-local pop here could not
# guarantee: no matter which submodule a caller imports first, the package's
# __init__.py always runs first. This module never keeps its own separate
# copy of the secret; it always reads the late-bound cache below.


# ---------------------------------------------------------------------------
# B119: runner/topic allowlist enforcement for write-gated taskctl actions.
#
# Deterministic safety boundary, NOT a neural learning target. It starts from
# the pinned runner/topic allowlist design evaluation and
# adds the exact ``claim-start`` lifecycle action. The neural controller may
# later choose among these identities for routing; this static allowlist is the
# hard boundary that blocks any identity not on the list.
#
# This layer only ever NARROWS an already-open AIWORKHUB_ALLOW_WRITES
# gate (see run_taskctl()) -- it never widens it and never runs when the
# write gate is closed. It only activates when a caller explicitly supplies
# a ``runner`` and/or ``topic`` identity. Exact lifecycle call sites always
# thread both values; legacy identity-free helpers such as export-jsonl retain
# their prior behavior.
# ---------------------------------------------------------------------------

_RUNNER_TOPIC_TOKEN_RE = re.compile(r"^[A-Za-z0-9_]+$")

# (runner, topic) -> allowed write_actions.
RUNNER_TOPIC_ALLOWLIST: dict[tuple[str, str], frozenset[str]] = {
    ("claude_coding", "coding"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("claude_stem", "stem"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("claude_general_reasoning", "general_reasoning"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("claude_capability_eval", "capability_eval"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("claude_context_graph", "context_graph"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("claude_translate", "translation"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("claude_c_native", "c_native"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("claude_open_generation", "open_generation"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("deepseek_finance", "finance"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("deepseek_routing", "routing_safety"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("deepseek_tasking", "tasking_system"): frozenset({"auto-pickup", "claim-start", "review", "start", "usage"}),
    ("codex_spark_lexicon_multi_axis_enrichment_b367", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_real_generation_canary_b368", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_real_canary_shard_00_b368", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_real_canary_shard_01_b368", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_real_canary_shard_02_b368", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_real_canary_shard_03_b368", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_direct_micro_shard_00_b369", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_direct_micro_shard_01_b369", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_direct_micro_shard_02_b369", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_spark_lexicon_direct_micro_shard_03_b369", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_direct_micro_shard_00_b370", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_direct_micro_shard_01_b370", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_direct_micro_shard_02_b370", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_direct_micro_shard_03_b370", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_semantic_shard_00_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_semantic_shard_01_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_semantic_shard_02_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_semantic_shard_03_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_semantic_shard_04_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_semantic_shard_05_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_semantic_shard_06_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_semantic_shard_07_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_valency_shard_00_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_closure_valency_shard_01_b372", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_surface_shard_00_b380", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_surface_shard_01_b380", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_surface_apply_b381", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_surface_apply_b387", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_offline_pos_reconcile_b388", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_exact_repair_shard_00_b389", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_exact_repair_shard_01_b389", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_exact_repair_shard_02_b389", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_exact_repair_shard_03_b389", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_exact_selective_apply_b390", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_low_frame_repair_b391", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_applicability_repair_b392", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_offline_nonverb_reconcile_b393", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_null_pos_repair_b394", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_role_calibration_b395", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_role_calibration_b396", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_role_calibration_b397", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_recognizable_final_apply_b398", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_task_mcp_callback_bridge_finish_b384", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_task_mcp_callback_compact_b435", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_task_mcp_worker_ai_infra_b434", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_task_mcp_ai_infra_context_b437", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_task_mcp_ai_infra_canary_b439", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b438_residual_b440", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b438_residual_direct_s00_b441", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b440_partition_b442", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b441_partition_b443", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b438_residual_direct_s01_b444", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_task_mcp_vscode_dashboard_extension_b445", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b438_remaining_b446", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_b441_rework_b447", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b440_rework_b448", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b427_rework_b449", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b444_partition_b450", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_task_mcp_dashboard_exact_counts_b451", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_missing84_b452", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_00_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_01_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_02_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_03_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_04_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_05_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_06_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_07_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_08_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_09_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_10_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_11_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_12_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_13_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_14_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_15_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_16_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_17_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_18_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_19_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_20_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_21_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_22_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_23_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_24_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_25_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_26_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_27_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_28_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_quarantine_literal_mod32_29_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_literal_mod32_30_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_mod32_31_b461", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_00_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_02_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_03_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_05_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_06_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_08_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_09_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_11_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_12_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_14_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_15_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_17_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_18_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_20_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_21_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_23_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_24_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_26_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_27_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_29_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_quarantine_literal_retry_mod32_30_b462", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_00_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_01_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_02_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_03_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_04_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_05_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_06_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_07_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_08_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_09_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_10_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_11_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_12_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_13_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_14_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_15_b463", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_00_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_01_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_02_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_03_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_04_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_05_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_06_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_07_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_08_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_09_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_10_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_11_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_12_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_13_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_14_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_direct_mod16_15_b464", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_b464_source_confirmed_lemma_apply_b465", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_b464_lemma_pair_adjudication_b466", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_b464_lemma_pair_adjudication_b466_v2", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_b464_shard12_lemma_adjudication_b467", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_b464_exact_lemma_adjudication_b468", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_morphology_residual_b469", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_source_heldout_b469", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_source_heldout_b469", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_morphology_selective_apply_b470", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_source_heldout_repair_b470", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_conditioned_precision_b470", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_task_mcp_multi_inprogress_callback_repair_b471", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_task_mcp_multi_instance_sideband_routing_b472", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_offline_exhaustion_b383", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_valency_surface_repair_b382", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_valency_conditioned_engine_b421", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_closure_semantic_canary_b373", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_canonical_semantic_apply_b373", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_closure_b377", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_canonical_enum_canary_b374", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_live_queue_rebase_b399", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_live_shard_00_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_live_shard_01_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_live_shard_02_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_live_shard_03_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_live_shard_04_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_live_shard_05_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_live_shard_06_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_live_shard_07_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_literal_chunk_00_b401", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_literal_chunk_01_b401", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_literal_chunk_02_b401", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_literal_chunk_03_b401", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_literal_chunk_04_b401", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_literal_chunk_05_b401", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_literal_chunk_06_b401", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_literal_chunk_07_b401", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_literal_selective_apply_b402", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_one_pass_complete_classification_b403", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_offline_full_axis_b410", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_compact_live_offline_b411", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_local_source_pack_repair_b413", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_consolidated_quarantine_b414", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_direct_b415_s00c00", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_quarantine_b418", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_source_heldout_b419", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_source_heldout_b419", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_ud_glc_valency_heldout_b420", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_conditioned_case_set_apply_b422", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_final_b423", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_literal_b424_s00", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_literal_b424_s01", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_literal_b424_s02", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_literal_b424_s03", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_literal_b424_s04", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_literal_b424_s05", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_literal_b424_s06", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_quarantine_literal_b424_s07", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_valency_quarantine_selective_apply_b425", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_live_residual_rebase_b426", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_final_shard_00_b427", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_final_shard_01_b427", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_final_shard_02_b427", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_final_shard_03_b427", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_final_shard_04_b427", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_final_shard_05_b427", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_final_shard_06_b427", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_final_shard_07_b427", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b427_incremental_apply_b428", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b427_shards_02_03_apply_b429", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_b427_shards_04_07_apply_b430", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_adjudication_b431_s00", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_adjudication_b431_s01", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_adjudication_b431_s02", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_adjudication_b431_s03", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_adjudication_b431_s04", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_adjudication_b431_s05", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_adjudication_b431_s06", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_adjudication_b431_s07", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_adjudication_canary_b432", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_adjudication_canary_b433", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_adjudication_canary_b433", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_adjudication_b436_s00", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_adjudication_b436_s01", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_adjudication_b440_s02", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_semantic_adjudication_b440_s03", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_morphology_b400_apply_b436", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_consensus_apply_b438", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_morphology_residual_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_source_heldout_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_b403_complete_selective_apply_builder_b408", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_source_heldout_b400", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    # B473 exact runners.  Keep these explicit: Lexicon does not use a broad
    # per-wave prefix allowlist, so every launched worker remains auditable.
    ("codex_gpt55_lexicon_morphology_final_s00_b473", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_morphology_final_s01_b473", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_morphology_final_s02_b473", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_morphology_final_s03_b473", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_conditioned_coverage_b473", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_heldout_family_a_b473", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_semantic_heldout_family_b_b473", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_sense_118_adjudication_b473", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    # B474: consume the already-completed B464 classifications exactly once.
    # These workers normalize disjoint axis families; none may mutate the
    # canonical Lexicon or reopen ACCEPT_VALID/terminal dispositions.
    ("deepseek_v4pro_lexicon_b464_form_axis_normalize_b474", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_b464_semantic_axis_normalize_b474", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_b464_structural_axis_normalize_b474", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_b464_disposition_accounting_b474", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_valency_heldout_expand_b474", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_semantic_heldout_expand_b474", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    # B475: exact failed-axis residual only. These eight runners consume the
    # non-overlapping semantic/ambiguity/valency shards left by the accepted
    # B474 rebase; broad Lexicon prefixes remain deliberately disallowed.
    ("codex_gpt55_lexicon_failed_axes_shard00_b475", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_failed_axes_shard01_b475", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_failed_axes_shard02_b475", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_failed_axes_shard03_b475", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_failed_axes_shard04_b475", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_failed_axes_shard05_b475", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_failed_axes_shard06_b475", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_lexicon_failed_axes_shard07_b475", "lexicon"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_existing_m1_m9_real_e2e_b498", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_atlas_true_feature_selector_b501", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_atlas_production_elex_pgeo_b502", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_atlas_pgeo_producer_b504", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_complex_verifier_producers_b506", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_lexicon_atlas_semantic_pgeo_b507", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_atlas_real_context_encoder_b509", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_blind_shadow_boundary_b510", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_task_mcp_finalized_worktree_gc_b511", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_task_mcp_zero_row_output_b536", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_signal_atlas_blind_sae_adapter_b513", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_atlas_native_learned_pgeo_b514", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_signal_atlas_blind_sae_adapter_b515", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_atlas_native_semantic_context_b516", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_signal_atlas_blind_sae_zero_authority_b517", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_lexicon_atlas_real_corpus_recurrent_b518", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_signal_atlas_chatengine_zero_authority_shadow_b519", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_representation_parse_place_production_rebase_b520", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_extraction_repair_b521", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_representation_parse_place_genuine_lgp_rebuild_b522", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_operand_repair_b524", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_ramaz044_source_adjudication_b525", "context_graph"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_representation_parse_place_lgp_axes_native_b527", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_semantic_adjudication_b528", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_topup_b529", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_canonical_merge_b530", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_noncount_audit_b531", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_basis_ternary_b532", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_relational_negative_b533", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_rare_case_local_corpus_b534", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_observer_packet_repair_b535", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_owner_gate_consolidation_b537", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_multihop_leipzig_b538", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_contradiction_matsne_b538", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_contradiction_matsne_b538_v2", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_contradiction_nplg_b539", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_contradiction_nplg_b539_v2", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_highrecall_leipzig_news_b540", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_highrecall_leipzig_webwiki_b540", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_webwiki_adjudication_b541", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_news_adjudication_b541", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_opposition_leipzig_b542", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_opposition_unique_b543", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_owner_close_b544", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_owner_apply_preflight_b545", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s1_owner_apply_preflight_b545_v2", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s1_final_owner_apply_b546", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s1_underdetermined_abstain_b547", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s0_abi_preflight_b548", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s0_abi_repair_b549", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s0_basis_relation_mapping_b550", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s1_qualitative_production_b551", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s1_entity_frame_inverse_repair_b552", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s1_exact_root_binding_b553", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s1_immutable_root_binding_b554", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s1_s0_abi_parity_b555", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s2_rigid_metric_kernel_b556", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s2_region_extent_sidecar_b557", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s2_explicit_metric_casebank_b558", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s2_casebank_adjudicate_materialize_b559", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s3_shape_sidecar_b560", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_task_mcp_required_ignored_output_promotion_b561", "task_mcp"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s2_direct_semantic_adjudication_b562", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s2_accepted_materialization_b563", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s2_context_recovery_b564", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s3_native_consumer_b565", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s2_canonical_metric_bridge_b566", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s3_review_packet_b567", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s2_context_canonical_merge_b568", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s3_sentence_disjoint_adjudication_b569", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s2_blind_heldout_validation_b570", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s3_full_direct_repair_rebuild_b571", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s2_exact_three_repair_b572", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s4_motion_time_preflight_b573", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s5_signature128_preflight_b574", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s6_shadow_cuda_preflight_b575", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s2_canonical_transport_b576", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s3_figure_bound_repair_b577", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s5_signature128_truth_repair_b578", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s3_figure_direct_b579a", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s3_bound_direct_b579b", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s4_real_corpus_packet_b580", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s2_canonical_writer_repair_b581", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s3_canonical_v2_merge_b582", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s3_canonical_v2_merge_b582_v2", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_candidate_direct_adjudication_b583a", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s4_blind_heldout_adjudication_b583b", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s2_native_abi_final_repair_b584", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_accepted22_grounding_b585", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s3_production_header_pack_b586", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_heldout21_outcome_b587", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_entity_frame_producer_b588", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s5_s2_adapter_b589", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s5_s2_adapter_b589_v2", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s5_s3_adapter_b590", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_scaleout_even_b591", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s4_scaleout_odd_b592", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_scaleout_odd_b592_v2", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_parse_place_spatial_s4_heldout_entity_frame_b593", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_combined_repair125_b594", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_b593_native_producer_repair_b595", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_parse_place_spatial_s4_b594_entity_frame_b596", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_b594_entity_frame_b596_v2", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_b591_accept25_entity_frame_b597", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_canonical_s5_joined_b598", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s4_native_s5_joined_b599", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_parse_place_spatial_s6_real_gpu_closure_b600", "representation"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_signal_atlas_representation_tensor_b601", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_representation_tensor_b601_v2", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_recurrent_completion_b602", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_s5_real_pair_sieve_b603", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_signal_atlas_hyperbolic_real_ablation_b604", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_signal_atlas_complex_verifier_real_ablation_b605", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_complex_verifier_real_ablation_b605_v2", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_signal_atlas_s5_sieve_truth_repair_b606", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_hyperbolic_decision_producer_b607", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_complex_support_producer_b608", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_signal_atlas_verifier_unguarded_producer_b609", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_real_executable_shadow_b610", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_s5_sieve_native_repair_b611", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_signal_atlas_s5_crosswalk_native_repair_b612", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_s5_crosswalk_native_repair_b612", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_root_signal_nonempty_canary_b613", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_task_mcp_b614_repair_b616", "tasking_system"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_gate17_hyperbolic_paired_ablation_b617", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("claude_sonnet5_signal_atlas_gate17_complex_paired_ablation_b618", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("deepseek_v4pro_signal_atlas_gate17_verifier_paired_ablation_b619", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_canonical_sae_runtime_b620", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_b613_query_packet_producer_b621", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_runtime_root_reconciliation_b622", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_runtime_root_reconciliation_b622_v2", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_runtime_root_reconciliation_b622_v3", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_signal_atlas_runtime_root_reconciliation_b622_v4", "signal_atlas"): frozenset({"claim-start", "review", "usage"}),
    ("codex_gpt55_task_mcp_current_tree_policy_repair_b623", "tasking_system"): frozenset({"claim-start", "review", "usage"}),
}

# codex is the coordinator: matches ANY topic (topic="*" in the design
# matrix) with a distinct action set. Codex never auto-picks-up or starts
# worker tasks; it finalizes (done), exports, reviews, and records usage.
CODEX_RUNNER = "codex"
CODEX_ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {"archive", "done", "export-jsonl", "restore", "review", "reject-review", "release-launch", "usage"}
)

# ---------------------------------------------------------------------------
# B06: bounded per-wave task_mcp prefix allowlist (applies the pinned B05
# dry-run proposal).
#
# This adds one topic key, ``task_mcp``, matched by a runner name prefix instead
# of an exact tuple. Task MCP runners are per-wave-numbered (e.g.
# ``claude_task_mcp_allowlist_patch_b06``) and cannot be enumerated in
# advance. Still a deterministic safety/support-boundary gate (CLAUDE.md
# carve-out), not a learned routing decision -- no regex/keyword cognition is
# introduced for task-type or route selection, only a widened prefix check on
# an already-static safety boundary. Exact ``claim-start`` is allowed;
# standalone ``start`` and coordinator-only ``done`` remain excluded.
# ---------------------------------------------------------------------------

PER_WAVE_RUNNER_TOPIC_ALLOWLIST: dict[str, dict[str, Any]] = {
    "task_mcp": {
        "runner_prefix": "claude_task_mcp_",
        "actions": frozenset({"auto-pickup", "claim-start", "review", "usage"}),
    },
}


def _is_malformed_identity_token(value: str) -> str | None:
    """Return a short reason string if ``value`` fails sanity checks, else None.

    Rejects: empty string, embedded null bytes, and anything outside the
    conservative ``[A-Za-z0-9_]+`` charset (this rejects whitespace, path
    traversal like ``../``, and shell metacharacters like ``; | & $`` in one
    check). Matching is exact/case-sensitive against ``RUNNER_TOPIC_ALLOWLIST``
    keys -- no case normalization is performed anywhere in this module, so a
    case-mismatched runner (e.g. ``CLAUDE_CODING``) passes this sanity check
    but is then rejected as an unknown pair by the allowlist lookup.
    """
    if value == "":
        return "empty_string"
    if "\x00" in value:
        return "null_byte"
    if not _RUNNER_TOPIC_TOKEN_RE.match(value):
        return "invalid_characters"
    return None


def check_runner_topic_allowlist(
    runner: str | None,
    topic: str | None,
    action: str,
) -> dict[str, Any]:
    """Pure allowlist decision for a (runner, topic, action) write triple.

    Returns ``{"allowed": bool, "reason": str}``. Never raises, never performs
    I/O, never mutates state, never launches a process. This is the
    deterministic safety gate from mcp_runner_topic_allowlist_design_b118_v1.json
    (it is explicitly NOT a neural learning target -- see module note above).
    """
    if runner is not None:
        runner_reason = _is_malformed_identity_token(runner)
        if runner_reason:
            return {"allowed": False, "reason": f"malformed_runner:{runner_reason}"}
    if topic is not None:
        topic_reason = _is_malformed_identity_token(topic)
        if topic_reason:
            return {"allowed": False, "reason": f"malformed_topic:{topic_reason}"}

    if runner == CODEX_RUNNER:
        if action in CODEX_ALLOWED_ACTIONS:
            return {"allowed": True, "reason": "codex_wildcard_topic_allowed"}
        return {"allowed": False, "reason": f"codex_action_not_allowed:{action}"}

    if runner is None or topic is None:
        return {"allowed": False, "reason": "runner_and_topic_required_for_non_codex"}

    allowed_actions = RUNNER_TOPIC_ALLOWLIST.get((runner, topic))
    if allowed_actions is not None:
        if action not in allowed_actions:
            return {"allowed": False, "reason": f"action_not_allowed_for_runner_topic:{action}"}
        return {"allowed": True, "reason": "allowlisted"}

    per_wave_rule = PER_WAVE_RUNNER_TOPIC_ALLOWLIST.get(topic)
    if per_wave_rule is not None and runner.startswith(per_wave_rule["runner_prefix"]):
        if action not in per_wave_rule["actions"]:
            return {"allowed": False, "reason": f"per_wave_action_not_allowed:{action}"}
        return {"allowed": True, "reason": "per_wave_prefix_allowlisted"}

    return {"allowed": False, "reason": "unknown_runner_topic_pair"}


@dataclass(frozen=True)
class TaskCtlResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.returncode == 0,
            "returncode": self.returncode,
            "command": self.command,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def repo_root() -> Path:
    env_override = os.environ.get("AIWORKHUB_REPO", "")
    if env_override:
        return Path(env_override).expanduser().resolve()
    return repository_state.resolve_repository_root(require_manifest=False)


def taskctl_path(repo: Path | None = None) -> Path:
    """Compatibility-only legacy path helper.

    The installable AIWorkHub runtime does not require or execute this path.
    Kept only for old tests/docs that compare historical taskctl locations.
    """
    root = repo or repo_root()
    return root / "AITools/taskctl.py"


def writes_allowed() -> bool:
    return os.environ.get("AIWORKHUB_ALLOW_WRITES", "0") == "1"


def scrub_coordinator_capability_from_environment() -> None:
    """Move late-bound coordinator configuration out of the inherited env.

    Delegates to the package-level scrub (``aiworkhub.refresh_coordinator_config``)
    so there is exactly one place that ever pops these env vars.
    """
    refresh_coordinator_config()


def _coordinator_taskctl_env() -> dict[str, str]:
    """Build an env that grants capability to one trusted taskctl process."""
    scrub_coordinator_capability_from_environment()
    configured_token, configured_path = coordinator_config()

    token_path = (
        Path(configured_path).expanduser()
        if configured_path
        else DEFAULT_COORDINATOR_TOKEN_FILE
    ).resolve()
    try:
        file_token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        file_token = ""

    child_env = os.environ.copy()
    child_env.pop(COORDINATOR_TOKEN_ENV, None)
    child_env.pop(COORDINATOR_TOKEN_FILE_ENV, None)
    child_env[COORDINATOR_TOKEN_FILE_ENV] = str(token_path)
    token = file_token or configured_token
    if token:
        child_env[COORDINATOR_TOKEN_ENV] = token
    return child_env


def require_repo() -> Path:
    """Return the bound repository root.

    Standalone VSIX/runtime installs must work in arbitrary repositories that
    have never contained ``AITools/``. Storage readiness is checked by the
    concrete operation via ``task_store``; this helper must not require a
    legacy parent-repo script.
    """
    return repo_root()


def _is_write_command(args: list[str]) -> bool:
    return bool(args) and args[0] in WRITE_COMMANDS


def _audit_log_path(repo: Path | None = None) -> Path:
    """Resolve audit log path from env or default, relative to repo root."""
    env_path = os.environ.get("AIWORKHUB_AUDIT_LOG_PATH", "")
    if env_path:
        return Path(env_path).expanduser().resolve()
    root = repo or repo_root()
    return root / AUDIT_LOG_DEFAULT_REL


def _sanitize_env_for_audit() -> dict[str, str]:
    """Return env var NAMES (not values) relevant to write-gate decisions."""
    relevant = ["AIWORKHUB_ALLOW_WRITES", "AIWORKHUB_AUDIT_LOG_PATH"]
    return {k: "<set>" if k in os.environ else "<unset>" for k in relevant}


def write_audit_entry(
    tool_name: str,
    action: str,
    blocked_reason: str,
    *,
    repo: Path | None = None,
) -> dict[str, Any]:
    """Append a single audit entry to the JSONL audit log.

    Never logs secret/env values — only env var NAMES and set/unset status.
    Returns the entry dict that was written (or would have been written).
    Does not raise on IO errors; writes a warning to stderr instead.
    """
    entry: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool_name": tool_name,
        "action": action,
        "blocked_reason": blocked_reason,
        "caller_info": {
            "pid": os.getpid(),
            "env_vars_checked": _sanitize_env_for_audit(),
        },
    }

    log_path = _audit_log_path(repo)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(
            f"[aiworkhub] WARNING: audit log write failed: {exc}",
            file=sys.stderr,
        )

    return entry


def run_taskctl(
    args: list[str],
    *,
    allow_write: bool = False,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    runner: str | None = None,
    topic: str | None = None,
    coordinator_capability: bool = False,
) -> TaskCtlResult:
    """Native taskctl-compat dispatcher with explicit write protection.

    Older in-package callers were written against ``core.run_taskctl([...])``.
    Keep that API, but execute against this package's canonical
    ``.aiworkhub/tasking/task_queue.sqlite`` task engine directly. This path
    never shells out and never requires a repository-local ``AITools/`` tree.
    """

    scrub_coordinator_capability_from_environment()
    if not args:
        raise ValueError("taskctl args must not be empty")
    if coordinator_capability and args[0] not in COORDINATOR_COMMANDS:
        raise ValueError(
            "coordinator capability may only be passed to done, reject-review, release-launch, archive, or restore"
        )

    command = list(args)
    cmd = args[0]

    def _as_result(result: dict[str, Any]) -> TaskCtlResult:
        return TaskCtlResult(
            command=list(result.get("command") or command),
            returncode=int(result.get("returncode") if result.get("returncode") is not None else (0 if result.get("ok") else 1)),
            stdout=str(result.get("stdout") or ""),
            stderr=str(result.get("stderr") or ""),
        )

    def _value(flag: str, default: str | None = None) -> str | None:
        try:
            idx = args.index(flag)
        except ValueError:
            return default
        if idx + 1 >= len(args):
            return default
        return args[idx + 1]

    def _int_value(flag: str, default: int) -> int:
        raw = _value(flag)
        if raw is None:
            return default
        try:
            return max(0, min(int(raw), 5000_000_000))
        except ValueError:
            return default

    try:
        if cmd == "verify":
            readiness = task_store.storage_readiness(require_repo())
            return TaskCtlResult(
                command=command,
                returncode=0 if readiness.ready else 1,
                stdout=json.dumps(readiness.as_dict(), ensure_ascii=False),
                stderr="" if readiness.ready else readiness.reason,
            )
        if cmd in {"init-db", "init-repo"}:
            blocked = _canonical_write_gate("init-db", runner=runner, topic=topic)
            if blocked is not None:
                return _as_result({**blocked, "command": command})
            return _as_result(_canonical_result(
                ok=True,
                stdout=json.dumps(task_store.initialize_repository(require_repo()), ensure_ascii=False),
                command=command,
            ))
        if cmd == "list":
            return _as_result(list_tasks(
                status=_value("--status", "pending") or "pending",
                topic=_value("--topic"),
                limit=_int_value("--limit", 80),
            ))
        if cmd == "review-queue":
            return _as_result(review_queue())
        if cmd == "show":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "show requires task_id")
            return _as_result(show_task(args[1]))
        if cmd == "export":
            selected_runner = _value("--runner", runner)
            if not selected_runner:
                return TaskCtlResult(command, 2, "", "export requires --runner")
            return _as_result(pending_for_runner(selected_runner, topic=_value("--topic", topic)))
        if cmd == "auto-pickup":
            selected_runner = _value("--runner", runner)
            if not selected_runner:
                return TaskCtlResult(command, 2, "", "auto-pickup requires --runner")
            return _as_result(auto_pickup(selected_runner, _value("--topic", topic)))
        if cmd == "claim-start":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "claim-start requires task_id")
            selected_runner = _value("--runner", runner)
            selected_topic = _value("--topic", topic)
            if not selected_runner or not selected_topic:
                return TaskCtlResult(command, 2, "", "claim-start requires --runner and --topic")
            return _as_result(claim_start_exact(args[1], selected_runner, selected_topic, _value("--request-id", "") or ""))
        if cmd == "review":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "review requires task_id")
            return _as_result(mark_review(args[1], runner=_value("--runner", runner), topic=_value("--topic", topic)))
        if cmd == "done":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "done requires task_id")
            return _as_result(mark_done(args[1], runner=_value("--runner", runner), topic=_value("--topic", topic)))
        if cmd == "reject-review":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "reject-review requires task_id")
            return _as_result(reject_review(args[1], reason=_value("--reason", "") or "", topic=_value("--topic", topic)))
        if cmd == "release-launch":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "release-launch requires task_id")
            return _as_result(release_launch(
                args[1],
                claimed_by=_value("--claimed-by", "") or "",
                reason=_value("--reason", "") or "",
                topic=_value("--topic", topic),
            ))
        if cmd == "archive":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "archive requires task_id")
            blocked = _canonical_write_gate("archive", runner=_value("--runner", runner), topic=_value("--topic", topic), coordinator_capability=coordinator_capability)
            if blocked is not None:
                return _as_result({**blocked, "command": command})
            ok, reason = task_store.archive_task(require_repo(), args[1], actor=_value("--runner", runner) or "dashboard", reason=_value("--reason", "") or "")
            return TaskCtlResult(command, 0 if ok else 1, reason, "" if ok else reason)
        if cmd == "restore":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "restore requires task_id")
            blocked = _canonical_write_gate("restore", runner=_value("--runner", runner), topic=_value("--topic", topic), coordinator_capability=coordinator_capability)
            if blocked is not None:
                return _as_result({**blocked, "command": command})
            ok, reason = task_store.restore_task(require_repo(), args[1], actor=_value("--runner", runner) or "dashboard", reason=_value("--reason", "") or "")
            return TaskCtlResult(command, 0 if ok else 1, reason, "" if ok else reason)
        if cmd == "collision-guard":
            return _as_result(collision_guard(print_json="--print" in args or "--json" in args))
        if cmd == "callback-outbox-status":
            return _as_result(callback_outbox_status())
        if cmd == "usage-report":
            return _as_result(usage_report(runner=_value("--runner", runner), topic=_value("--topic", topic), status=_value("--status")))
        if cmd == "usage":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "usage requires task_id")
            return _as_result(record_usage(
                args[1],
                runner=_value("--runner", runner) or "",
                topic=_value("--topic", topic),
                model=_value("--model", ""),
                provider=_value("--provider", ""),
                source=_value("--source", ""),
                note=_value("--note", ""),
                input_tokens=_int_value("--input-tokens", 0),
                output_tokens=_int_value("--output-tokens", 0),
                total_tokens=_int_value("--total-tokens", 0),
                cost_usd=float(_value("--cost-usd", "0") or 0),
            ))
        if cmd == "export-jsonl":
            return _as_result(export_jsonl(runner=_value("--runner", runner), topic=_value("--topic", topic)))
    except Exception as exc:
        return TaskCtlResult(command, 1, "", f"native_task_command_failed:{type(exc).__name__}:{str(exc)[:500]}")

    return TaskCtlResult(command, 127, "", f"unsupported_native_task_command:{cmd}")


def _task_id_from_write_args(args: list[str]) -> str | None:
    if not args or args[0] not in {"claim-start", "review", "usage"}:
        return None
    if len(args) < 2:
        return None
    task_id = str(args[1])
    return task_id if task_id else None


def _check_card_scoped_write_authority(
    args: list[str],
    runner: str | None,
    topic: str | None,
) -> dict[str, Any]:
    """Allow exact one-off card owners without widening the static matrix."""
    action = args[0] if args else ""
    if action not in {"claim-start", "review", "usage"}:
        return {"allowed": False, "reason": f"card_scoped_action_not_allowed:{action}"}
    if runner == CODEX_RUNNER:
        return {"allowed": False, "reason": "card_scoped_codex_forbidden"}
    if runner is None or topic is None:
        return {"allowed": False, "reason": "runner_and_topic_required_for_card_scoped_authority"}
    runner_reason = _is_malformed_identity_token(runner)
    if runner_reason:
        return {"allowed": False, "reason": f"malformed_runner:{runner_reason}"}
    topic_reason = _is_malformed_identity_token(topic)
    if topic_reason:
        return {"allowed": False, "reason": f"malformed_topic:{topic_reason}"}
    task_id = _task_id_from_write_args(args)
    if task_id is None:
        return {"allowed": False, "reason": "card_scoped_task_id_required"}
    task_reason = _is_malformed_identity_token(task_id)
    if task_reason:
        return {"allowed": False, "reason": f"malformed_task_id:{task_reason}"}
    card, error = _live_card(task_id)
    if error:
        return {"allowed": False, "reason": "card_scoped_task_unresolved"}
    assert card is not None
    if card.get("task_id") != task_id or card.get("runner") != runner or card.get("topic") != topic:
        return {"allowed": False, "reason": "card_scoped_identity_mismatch"}
    claimed_by = str(card.get("claimed_by") or "")
    lifecycle = _lifecycle_state(card)
    if action == "claim-start":
        worker_status = str(card.get("worker_status") or "unclaimed")
        if lifecycle == "pending" and worker_status == "unclaimed" and not claimed_by:
            return {"allowed": True, "reason": "card_scoped_claim_start_allowed"}
        return {"allowed": False, "reason": f"card_scoped_claim_start_ineligible:{lifecycle}"}
    if claimed_by != runner:
        return {"allowed": False, "reason": f"card_scoped_claimed_by_mismatch:{claimed_by}"}
    if action == "review":
        if lifecycle == "processing":
            return {"allowed": True, "reason": "card_scoped_review_allowed"}
        return {"allowed": False, "reason": f"card_scoped_review_ineligible:{lifecycle}"}
    if lifecycle in {"processing", "review"}:
        return {"allowed": True, "reason": "card_scoped_usage_allowed"}
    return {"allowed": False, "reason": f"card_scoped_usage_ineligible:{lifecycle}"}


def health() -> dict[str, Any]:
    root = repo_root()
    readiness = task_store.storage_readiness(root)
    verify = _canonical_result(
        ok=readiness.ready,
        returncode=0 if readiness.ready else 1,
        stdout=json.dumps(readiness.as_dict(), ensure_ascii=False),
        stderr="" if readiness.ready else readiness.reason,
        command=["verify"],
    )
    return {
        "ok": readiness.ready,
        "repo": str(root),
        "task_engine": "native_aiworkhub",
        "runtime_storage": str(Path(repository_state.HUB_DIRNAME)),
        "writes_allowed": writes_allowed(),
        "verify": verify,
        "storage": readiness.as_dict(),
    }


def review_queue() -> dict[str, Any]:
    command = ["review-queue"]
    try:
        rows = task_store.list_tasks(repo_root(), status="review", limit=500)
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    lines = [f"=== Codex Review Queue ({len(rows)}) ==="]
    lines.extend(
        f"  [{r.get('topic') or '?'}] [{r.get('runner') or '?'}] {r.get('task_id') or ''}"
        for r in rows
    )
    return _canonical_result(ok=True, returncode=0, stdout="\n".join(lines), command=command)


def list_tasks(status: str = "pending", topic: str | None = None, limit: int = 80) -> dict[str, Any]:
    """Stdout stays the exact ``taskctl.py cmd_list`` compact-line format
    (``[bucket] [topic] [runner] task_id``) -- ``dashboard.py::parse_task_list``
    and ``completion_inbox.py::_parse_list_task_ids`` both regex-parse this
    stdout and must keep working unchanged."""
    command = ["list", "--status", status]
    if topic:
        command.extend(["--topic", topic])
    command.extend(["--limit", str(limit)])
    try:
        rows = task_store.list_tasks(repo_root(), status=status, limit=5000)
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    if topic:
        rows = [r for r in rows if r.get("topic") == topic]
    rows = rows[: max(1, int(limit))]
    lines = [
        f"[{r.get('status') or 'pending'}] [{r.get('topic') or '?'}] [{r.get('runner') or '?'}] "
        f"{r.get('task_id') or ''}"
        for r in rows
    ]
    stdout = "\n".join(lines)
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)


def show_task(task_id: str) -> dict[str, Any]:
    command = ["show", task_id]
    try:
        card = task_store.get_task(repo_root(), task_id)
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    if card is None:
        return _canonical_result(ok=True, returncode=0, stdout=f"Task not found: {task_id}", command=command)
    stdout = json.dumps(card, indent=2, ensure_ascii=False, default=str)
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)


def pending_for_runner(runner: str, topic: str | None = None) -> dict[str, Any]:
    """Matches ``AITools/taskctl.py::_cmd_export``: pending-only, JSONL rows."""
    command = ["export", "--runner", runner]
    if topic:
        command.extend(["--topic", topic])
    try:
        rows = task_store.list_tasks(repo_root(), status="pending", limit=5000)
    except task_store.TaskStoreError as exc:
        result = _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
        result["jsonl_rows"] = []
        return result
    filtered = [r for r in rows if r.get("runner") == runner and (topic is None or r.get("topic") == topic)]
    stdout = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in filtered)
    result = _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)
    result["jsonl_rows"] = filtered
    return result


def auto_pickup(runner: str, topic: str | None = None) -> dict[str, Any]:
    command = ["auto-pickup", "--runner", runner]
    if topic:
        command.extend(["--topic", topic])
    blocked = _canonical_write_gate("auto-pickup", runner=runner, topic=topic)
    if blocked is not None:
        return blocked
    try:
        rows = task_store.list_tasks(repo_root(), status=None, limit=5000)
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    eligible = eligible_dryrun_candidates(rows, runner, topic)
    if not eligible:
        return _canonical_result(
            ok=False, returncode=1, stderr=f"no_eligible_task:runner={runner}:topic={topic}", command=command
        )
    task_id = str(eligible[0]["task_id"])
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        try:
            stored_card = json.loads(row["card_json"] or "{}") if row is not None else {}
        except (TypeError, json.JSONDecodeError):
            stored_card = {}
        if not isinstance(stored_card, dict):
            stored_card = {}
        try:
            claim_epoch = int(stored_card.get("claim_epoch") or 0) + 1
        except (TypeError, ValueError):
            claim_epoch = 1
        stored_card.update(
            claim_epoch=claim_epoch,
            status="processing",
            worker_status="claimed",
            claimed_by=runner,
        )
        cur = conn.execute(
            "UPDATE tasks SET card_json=?, worker_status='claimed', status='processing', claimed_by=?, "
            "claimed_at=?, started_at=?, updated_at=? WHERE task_id=? AND worker_status='unclaimed'",
            (json.dumps(stored_card, ensure_ascii=False), runner, now, now, now, task_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return _canonical_result(
                ok=False, returncode=1, stderr=f"claim_race_lost:task_id={task_id}", command=command
            )
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (task_id, "auto_pickup", runner, json.dumps({"runner": runner, "topic": topic}, ensure_ascii=False), now),
        )
        conn.commit()
    finally:
        conn.close()
    card = task_store.get_task(repo_root(), task_id)
    stdout = json.dumps(card, ensure_ascii=False, default=str) if card else ""
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)


def claim_start_exact(
    task_id: str, runner: str, topic: str, request_id: str = ""
) -> dict[str, Any]:
    """Atomic exact-task claim/start against the canonical task store."""
    command = ["claim-start", task_id, "--runner", runner, "--topic", topic]
    if request_id:
        command.extend(["--request-id", request_id])
    blocked = _canonical_write_gate("claim-start", runner=runner, topic=topic, task_id=task_id)
    if blocked is not None:
        return blocked
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        row = conn.execute(
            "SELECT runner, topic, card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return _canonical_result(ok=False, returncode=1, stderr=f"task_not_found:{task_id}", command=command)
        try:
            stored_card = json.loads(row["card_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            stored_card = {}
        if not isinstance(stored_card, dict):
            stored_card = {}
        stored_runner = str(row["runner"] or stored_card.get("runner") or "")
        stored_topic = str(row["topic"] or stored_card.get("topic") or "")
        if stored_runner != runner or stored_topic != topic:
            conn.rollback()
            return _canonical_result(
                ok=False, returncode=1, stderr=f"identity_mismatch:task_id={task_id}", command=command
            )
        try:
            claim_epoch = int(stored_card.get("claim_epoch") or 0) + 1
        except (TypeError, ValueError):
            claim_epoch = 1
        stored_card.update(
            claim_epoch=claim_epoch,
            status="processing",
            worker_status="claimed",
            claimed_by=runner,
        )
        cur = conn.execute(
            "UPDATE tasks SET card_json=?, runner=?, topic=?, worker_status='claimed', status='processing', claimed_by=?, "
            "claimed_at=?, started_at=?, updated_at=? WHERE task_id=? AND worker_status='unclaimed'",
            (json.dumps(stored_card, ensure_ascii=False), runner, topic, runner, now, now, now, task_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return _canonical_result(
                ok=False, returncode=1, stderr=f"claim_conflict:task_id={task_id}", command=command
            )
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (
                task_id,
                "claim_start",
                runner,
                json.dumps({"runner": runner, "topic": topic, "request_id": request_id}, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    card = task_store.get_task(repo_root(), task_id)
    stdout = json.dumps(card, ensure_ascii=False, default=str) if card else ""
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)


# ---------------------------------------------------------------------------
# B118: read-only auto-pickup DRY-RUN preview.
#
# Reports which task ``auto_pickup`` WOULD claim for a (runner, topic) WITHOUT
# mutating the parent queue and WITHOUT touching the write gate. It never
# invokes the write-gated ``auto-pickup`` taskctl command; it reuses only the
# read-only ``export`` command (see ``pending_for_runner``) and then replicates
# the exact candidate-selection predicate of ``taskctl.cmd_auto_pickup``
# (AITools/taskctl.py:953-1018): the first card that is unclaimed + pending +
# runner-matched + topic-matched. The real claim path (``auto_pickup``) stays
# behind the existing ``AIWORKHUB_ALLOW_WRITES`` gate; this preview offers
# no alternate write path and cannot bypass that gate.
# ---------------------------------------------------------------------------

def _lifecycle_state(card: dict[str, Any]) -> str:
    """Compact lifecycle state — faithful local replica of
    ``taskdb.canonical_status`` (AITools/taskdb.py:115-134).

    Kept as a local copy so this MCP module stays import-light (no parent
    ``AITools`` dependency). It MUST stay in sync with the helper it mirrors;
    it is a deterministic classifier, not a runtime cue router.
    """
    status = str(card.get("status") or "").strip().lower()
    worker_status = str(card.get("worker_status") or "").strip().lower()
    if status in {"finished", "completed", "stale_already_done"} or worker_status == "done":
        return "finished"
    if status.startswith("blocked") or worker_status.startswith(("blocked", "deferred")):
        return "blocked"
    if status in {"review", "ready_for_review", "codex_review", "awaiting_review"} or worker_status in {
        "review",
        "ready_for_review",
        "codex_review",
        "awaiting_review",
    }:
        return "review"
    if status in {"processing", "in_progress"} or worker_status in {"claimed", "in_progress"}:
        return "processing"
    return "pending"


def eligible_dryrun_candidates(
    rows: list[dict[str, Any]],
    runner: str,
    topic: str | None = None,
) -> list[dict[str, Any]]:
    """Ordered list of cards ``auto_pickup`` would consider claimable, mirroring
    the ``taskctl.cmd_auto_pickup`` predicate. Pure: no IO, no mutation.

    A card is eligible iff, in order:
      * worker_status (default 'unclaimed') == 'unclaimed'
      * lifecycle state == 'pending'
      * runner matches exactly
      * topic matches (``topic is None`` -> any topic)

    Order is preserved; ``auto_pickup`` claims element ``[0]``.
    """
    out: list[dict[str, Any]] = []
    for c in rows:
        worker_status = str(c.get("worker_status", "unclaimed") or "unclaimed").strip().lower()
        if worker_status != "unclaimed":
            continue
        if _lifecycle_state(c) != "pending":
            continue
        if c.get("runner") != runner:
            continue
        if topic is not None and c.get("topic") != topic:
            continue
        out.append(c)
    return out


def _compact_card(card: dict[str, Any]) -> dict[str, Any]:
    """Token-bounded summary of a card for the dry-run report."""
    return {
        "task_id": card.get("task_id"),
        "runner": card.get("runner"),
        "topic": card.get("topic"),
        "mode": card.get("mode"),
        "worker_status": card.get("worker_status", "unclaimed"),
        "priority": card.get("priority"),
        "objective": str(card.get("objective", ""))[:160],
    }


def auto_pickup_dryrun(runner: str, topic: str | None = None) -> dict[str, Any]:
    """READ-ONLY preview of ``auto_pickup``: report the task that WOULD be
    claimed for ``(runner, topic)`` without mutating the parent queue and
    without touching the write gate.

    Contract (all invariants asserted by the B118 smoke test):
      * Never invokes the write-gated ``auto-pickup`` command — the only
        subprocess is the read-only ``export`` command via ``pending_for_runner``.
      * Leaves the parent queue byte-identical (no card status change).
      * Respects the ``--runner``/``--topic`` filters and reports the filtering.
      * Does NOT bypass ``AIWORKHUB_ALLOW_WRITES``; the real claim path
        (``auto_pickup`` / ``aiworkhub_task_auto_pickup``) stays write-gated.
    """
    export_result = pending_for_runner(runner=runner, topic=topic)
    rows = export_result.get("jsonl_rows", []) or []

    eligible = eligible_dryrun_candidates(rows, runner, topic)
    candidate = eligible[0] if eligible else None
    excluded_claimed = sum(
        1
        for c in rows
        if str(c.get("worker_status", "unclaimed") or "unclaimed").strip().lower() != "unclaimed"
    )

    return {
        "ok": bool(export_result.get("ok", False)),
        "dry_run": True,
        "tool": "aiworkhub_task_auto_pickup_dryrun",
        "contract": "B118_v1_autopickup_dryrun",
        "runner": runner,
        "topic": topic,
        "would_claim_task_id": candidate.get("task_id") if candidate else None,
        "candidate": _compact_card(candidate) if candidate else None,
        "filtering": {
            "runner_filter": runner,
            "topic_filter": topic,
            "pending_for_runner_topic": len(rows),
            "eligible_unclaimed": len(eligible),
            "excluded_already_claimed": excluded_claimed,
        },
        "mutation": {
            "queue_mutated": False,
            "write_gate_bypassed": False,
            "write_command_invoked": False,
            "real_auto_pickup_write_gated": True,
            "writes_allowed_env": writes_allowed(),
        },
        "source_command": export_result.get("command"),
        "export_returncode": export_result.get("returncode"),
    }


def _queue_request_authority_flags() -> dict[str, bool]:
    return {
        "runtime_authority": False,
        "support_authority": False,
        "apply_authority": False,
        "training_authority": False,
        "source_registry_live_mutation": False,
        "process_launch": False,
        "process_launch_authority": False,
        "agent_launch": False,
        "shell_invocation": False,
        "queue_write": writes_allowed(),
        "audit_write": writes_allowed(),
        "write_gate_enabled": writes_allowed(),
    }


def queue_request(
    *,
    task_id: str,
    runner: str,
    topic: str,
    adapter_id: str,
    model: str | None = None,
    owner_prompt: str = "",
    argv_template: list[str] | None = None,
    priority: str = "normal",
    stale_timeout_seconds: int = 7200,
    requested_at: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Write-gated Task MCP launch-queue request.

    This does not launch a process. With writes disabled, it returns a blocked
    response and appends nothing. With writes enabled, it appends one entry to
    the existing launch-queue audit JSONL unless the request is idempotent or
    a different runner already has an open entry for the same task_id.
    """
    from aiworkhub import completion_inbox, launch_queue_contract, launch_queue_persist

    now = datetime.now(timezone.utc)
    requested_at = requested_at or now.isoformat()
    request_id = request_id or launch_queue_contract.deterministic_request_id(task_id, runner, requested_at)
    authority_flags = _queue_request_authority_flags()
    base = {
        "tool": "aiworkhub_task_queue_request",
        "contract": "B286_v1_mvp_queue_request_extends_launch_queue_contract",
        "readonly": False,
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "adapter_id": adapter_id,
        "request_id": request_id,
        "authority_flags": authority_flags,
        "server_tool": "aiworkhub_task_queue_request",
    }

    for label, value in (("runner", runner), ("topic", topic)):
        reason = _is_malformed_identity_token(value)
        if reason:
            return {
                **base,
                "ok": False,
                "blocked_reason": f"malformed_{label}:{reason}",
                "idempotent_noop": False,
                "duplicate_runner_blocked": False,
                "stale_recovery_requeue": False,
                "request": None,
                "decision": None,
                "persisted_entry": None,
            }

    if adapter_id not in launch_queue_contract.KNOWN_ADAPTERS:
        return {
            **base,
            "ok": False,
            "blocked_reason": f"unknown_adapter:{adapter_id}",
            "idempotent_noop": False,
            "duplicate_runner_blocked": False,
            "stale_recovery_requeue": False,
            "request": None,
            "decision": None,
            "persisted_entry": None,
        }

    request = launch_queue_contract.enqueue_intent(
        task_id=task_id,
        runner=runner,
        topic=topic,
        adapter_id=adapter_id,
        argv_template=argv_template,
        request_id=request_id,
        created_ts=now.timestamp(),
        priority=priority,
        model=model,
        owner_prompt=owner_prompt,
        requested_at=requested_at,
        stale_timeout_seconds=stale_timeout_seconds,
    )
    decision = launch_queue_contract.evaluate_launch(request)

    if not writes_allowed():
        return {
            **base,
            "ok": False,
            "blocked_reason": "write_gate_closed:AIWORKHUB_ALLOW_WRITES!=1",
            "idempotent_noop": False,
            "duplicate_runner_blocked": False,
            "stale_recovery_requeue": False,
            "request": request.as_dict(),
            "decision": decision.as_dict(),
            "persisted_entry": None,
        }

    open_requests = launch_queue_persist.find_open_requests(task_id)
    for entry in open_requests:
        if entry.get("request_id") == request_id:
            return {
                **base,
                "ok": True,
                "blocked_reason": "",
                "idempotent_noop": True,
                "duplicate_runner_blocked": False,
                "stale_recovery_requeue": False,
                "request": request.as_dict(),
                "decision": decision.as_dict(),
                "persisted_entry": None,
                "existing_entry": entry,
            }
    duplicate = next((e for e in open_requests if e.get("runner") != runner), None)
    if duplicate is not None:
        return {
            **base,
            "ok": False,
            "blocked_reason": f"duplicate_runner_open_request:{duplicate.get('runner')}",
            "idempotent_noop": False,
            "duplicate_runner_blocked": True,
            "stale_recovery_requeue": False,
            "request": request.as_dict(),
            "decision": decision.as_dict(),
            "persisted_entry": None,
            "existing_entry": duplicate,
        }

    stale_recovery_requeue = False
    try:
        inbox = completion_inbox.build_completion_inbox(topic=topic, limit=500)
        stale_recovery_requeue = any(
            row.get("task_id") == task_id for row in inbox.get("stale_processing", [])
        )
    except Exception:
        stale_recovery_requeue = False

    persisted_entry = launch_queue_persist.persist_transition(
        request,
        decision,
        ts=now.timestamp(),
    )
    return {
        **base,
        "ok": True,
        "blocked_reason": "",
        "idempotent_noop": False,
        "duplicate_runner_blocked": False,
        "stale_recovery_requeue": stale_recovery_requeue,
        "request": request.as_dict(),
        "decision": decision.as_dict(),
        "persisted_entry": persisted_entry,
    }


def _lifecycle_error(message: str, returncode: int = 126) -> dict[str, Any]:
    return {
        "ok": False,
        "returncode": returncode,
        "command": [],
        "stdout": "",
        "stderr": message,
    }


def _live_card(task_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    shown = show_task(task_id)
    try:
        card = json.loads(shown.get("stdout", ""))
    except (TypeError, json.JSONDecodeError):
        return None, _lifecycle_error(f"cannot resolve live task identity: {task_id}", 1)
    if not isinstance(card, dict) or card.get("task_id") != task_id:
        return None, _lifecycle_error(f"cannot resolve live task identity: {task_id}", 1)
    return card, None


def manager_bootstrap() -> dict[str, Any]:
    """Compact, model-readable manager contract for a newly attached chat."""
    identity = _claude_manager_identity() or _codex_manager_identity()
    provider = str((identity or {}).get("provider") or _current_chat_provider())
    return {
        "ok": True,
        "schema_id": "aiworkhub.manager_bootstrap.v1",
        "role": "manager" if identity else "worker_or_unverified_client",
        "provider": provider,
        "repo": str(repo_root()),
        "manager_route": identity or {},
        "workflow": [
            "aiworkhub_task_create",
            "aiworkhub_task_auto_pickup or aiworkhub_agent_launch_task",
            "aiworkhub_task_mark_review",
            "provider callback receipt",
            "aiworkhub_task_mark_done or aiworkhub_task_reject_review",
        ],
        "callback": {
            "codex": "pushes to the exact origin thread through the App Server mux",
            "claude": "call aiworkhub_dispatcher_ensure_started, then aiworkhub_claude_callback_wait and immediately ack",
        },
        "rules": [
            "Create tasks through aiworkhub_task_create; never require repository-local AITools/taskctl.py.",
            "Use the returned task_id exactly and never fabricate callback batch/lease ids.",
            "Workers stop at review; a verified manager finalizes or rejects review.",
            "Each repository owns its own task database, callback lanes, and runtime capability.",
        ],
    }


def create_task(
    task_id: str,
    title: str,
    runner: str,
    topic: str,
    objective: str,
    acceptance: list[str],
    allowed_writes: list[str],
    forbidden: list[str] | None = None,
    required_outputs: list[str] | None = None,
    validation: list[str] | None = None,
    priority: str = "normal",
    callback_required: bool = True,
) -> dict[str, Any]:
    """Create one new canonical task card for the verified manager chat.

    Identity, callback provider, and origin thread are derived from the live
    manager route.  They are intentionally not caller-controlled parameters.
    Existing task ids are never overwritten.
    """
    identity = _claude_manager_identity() or _codex_manager_identity()
    if identity is None:
        return _lifecycle_error("manager_identity_required:task_create", 126)
    # Creation has no pre-existing card whose runner/topic could authorize the
    # write, so the normal card-scoped allowlist is inapplicable here.  Keep
    # the write gate, then require the verified manager capability directly.
    blocked = _canonical_write_gate("add-card")
    if blocked is not None:
        return blocked
    capability_ok, capability_reason = _verify_coordinator_capability(CODEX_RUNNER)
    if not capability_ok:
        return _lifecycle_error(capability_reason, 126)

    task_id = str(task_id or "").strip()
    runner = str(runner or "").strip()
    topic = str(topic or "").strip()
    title = str(title or "").strip()
    objective = str(objective or "").strip()
    priority = str(priority or "normal").strip().lower()
    if not _TASK_ID_RE.fullmatch(task_id):
        return _lifecycle_error("invalid_task_id", 2)
    if not _TASK_IDENTITY_RE.fullmatch(runner) or not _TASK_IDENTITY_RE.fullmatch(topic):
        return _lifecycle_error("invalid_runner_or_topic", 2)
    if not title or len(title) > 300 or not objective or len(objective) > 4000:
        return _lifecycle_error("invalid_title_or_objective", 2)
    if priority not in ("low", "normal", "high", "critical"):
        return _lifecycle_error("invalid_priority", 2)

    def bounded_strings(value: list[str] | None, name: str, *, required: bool = False) -> list[str]:
        if not isinstance(value, list) or (required and not value) or len(value) > 128:
            raise ValueError(f"invalid_{name}")
        result: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if not text or len(text) > 1000:
                raise ValueError(f"invalid_{name}")
            result.append(text)
        return result

    try:
        acceptance2 = bounded_strings(acceptance, "acceptance", required=True)
        writes2 = bounded_strings(allowed_writes, "allowed_writes")
        forbidden2 = bounded_strings(forbidden or [], "forbidden")
        outputs2 = bounded_strings(required_outputs or [], "required_outputs")
        validation2 = bounded_strings(validation or [], "validation")
    except ValueError as exc:
        return _lifecycle_error(str(exc), 2)
    for item in writes2:
        path = Path(item)
        if path.is_absolute() or ".." in path.parts:
            return _lifecycle_error("invalid_allowed_write_path", 2)

    origin_thread_id = str(identity.get("thread_id") or identity.get("session_id") or "").strip()
    if not _UUID_RE.fullmatch(origin_thread_id):
        return _lifecycle_error("manager_route_has_no_valid_origin_thread", 126)
    provider = str(identity["provider"])
    now = datetime.now(timezone.utc).isoformat()
    card = {
        "schema_id": "aiworkhub.task_card.v1",
        "task_id": task_id,
        "title": title,
        "runner": runner,
        "topic": topic,
        "mode": "",
        "priority": priority,
        "status": "pending",
        "worker_status": "unclaimed",
        "objective": objective,
        "origin_thread_id": origin_thread_id,
        "coordinator_provider": provider,
        "callback_required": bool(callback_required),
        "acceptance": acceptance2,
        "allowed_writes": writes2,
        "forbidden": forbidden2,
        "required_outputs": outputs2,
        "validation": validation2,
    }
    command = ["add-card", task_id]
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        callback_store.init_db(conn)
        if conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone() is not None:
            return _canonical_result(
                ok=False, returncode=1, stderr=f"task_already_exists:{task_id}", command=command
            )
        conn.execute(
            "INSERT INTO tasks(task_id,runner,topic,mode,status,worker_status,priority,objective,card_json,created_at,updated_at,origin_thread_id,archived_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?, '')",
            (
                task_id, runner, topic, "", "pending", "unclaimed", priority,
                objective, json.dumps(card, ensure_ascii=False), now, now,
                origin_thread_id,
            ),
        )
        conn.execute(
            "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) VALUES(?,?,?,?,?)",
            (
                task_id, "created", CODEX_RUNNER,
                json.dumps({"provider": provider, "topic": topic}, ensure_ascii=False), now,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return _canonical_result(
            ok=False, returncode=1, stderr=f"task_already_exists:{task_id}", command=command
        )
    finally:
        conn.close()
    return _canonical_result(
        ok=True,
        stdout=json.dumps(card, ensure_ascii=False),
        command=command,
    )


def mark_review(task_id: str, runner: str | None = None, topic: str | None = None) -> dict[str, Any]:
    """Request review for the exact task owner recorded on the live card.

    Older wiring omitted ``--runner`` and taskctl therefore substituted its
    legacy ``deepseek_flash`` default. Resolve the live identity when needed,
    require any caller-supplied identity to match it, then pass the exact
    runner/topic through to taskctl. The live card establishes the requested
    identity, which must then pass the MCP runner/topic policy allowlist.
    """
    card, error = _live_card(task_id)
    if error:
        return error
    assert card is not None
    live_runner = card.get("claimed_by") or card.get("runner")
    live_topic = card.get("topic")
    if runner is not None and runner != live_runner:
        return _lifecycle_error(f"runner mismatch expected={live_runner} got={runner}")
    if topic is not None and topic != live_topic:
        return _lifecycle_error(f"topic mismatch expected={live_topic} got={topic}")
    if not live_runner or not live_topic:
        return _lifecycle_error("task has no exact runner/topic identity")
    command = ["review", task_id, "--runner", str(live_runner), "--topic", str(live_topic)]
    blocked = _canonical_write_gate(
        "review", runner=str(live_runner), topic=str(live_topic), task_id=task_id
    )
    if blocked is not None:
        return blocked
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        callback_store.init_db(conn)
        cur = conn.execute(
            "UPDATE tasks SET worker_status='review', status='review', updated_at=? "
            "WHERE task_id=? AND worker_status IN ('claimed','in_progress')",
            (now, task_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return _canonical_result(
                ok=False, returncode=1, stderr=f"review_not_startable:task_id={task_id}", command=command
            )
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (
                task_id,
                "review",
                str(live_runner),
                json.dumps({"runner": live_runner, "topic": live_topic}, ensure_ascii=False),
                now,
            ),
        )
        origin_thread_id = callback_store.read_origin_thread(conn, task_id)
        if not origin_thread_id:
            origin_thread_id = str(card.get("origin_thread_id") or "").strip()
        callback_provider = _current_chat_provider(card)
        claude_route = _claude_manager_identity() if callback_provider == "claude" else None
        if claude_route is not None:
            # The reviewing Claude manager's live session is the callback
            # authority. Repair a legacy/missing origin that may have been
            # stamped by the task authoring chat before provider-aware
            # routing existed.
            origin_thread_id = claude_route["session_id"]
            conn.execute(
                "UPDATE tasks SET origin_thread_id=? WHERE task_id=?",
                (origin_thread_id, task_id),
            )
        callback_enqueued = callback_store.enqueue_callback(
            conn,
            task_id,
            origin_thread_id or "",
            "review_ready",
            provider=callback_provider,
        )
        conn.commit()
    finally:
        conn.close()
    card2 = task_store.get_task(repo_root(), task_id)
    stdout = json.dumps(card2, ensure_ascii=False, default=str) if card2 else ""
    result = _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)
    result["callback_enqueued"] = callback_enqueued
    return result


def mark_done(task_id: str, runner: str | None = None, topic: str | None = None) -> dict[str, Any]:
    """Finalize one exact reviewed card with server-held coordinator authority."""
    if runner not in (None, CODEX_RUNNER):
        return _lifecycle_error(
            f"coordinator runner mismatch expected={CODEX_RUNNER} got={runner}"
        )
    card, error = _live_card(task_id)
    if error:
        return error
    assert card is not None
    live_topic = card.get("topic")
    if not live_topic:
        return _lifecycle_error("task has no exact topic identity")
    if topic is not None and topic != live_topic:
        return _lifecycle_error(f"topic mismatch expected={live_topic} got={topic}")
    command = ["done", task_id, "--runner", CODEX_RUNNER, "--topic", str(live_topic)]
    blocked = _canonical_write_gate(
        "done", runner=CODEX_RUNNER, topic=str(live_topic), coordinator_capability=True
    )
    if blocked is not None:
        return blocked
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        cur = conn.execute(
            "UPDATE tasks SET worker_status='done', status='finished', completed_at=?, updated_at=? "
            "WHERE task_id=? AND worker_status='review'",
            (now, now, task_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return _canonical_result(
                ok=False, returncode=1, stderr=f"done_not_reviewable:task_id={task_id}", command=command
            )
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (task_id, "done", CODEX_RUNNER, json.dumps({"topic": live_topic}, ensure_ascii=False), now),
        )
        conn.commit()
    finally:
        conn.close()
    card2 = task_store.get_task(repo_root(), task_id)
    stdout = json.dumps(card2, ensure_ascii=False, default=str) if card2 else ""
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)


def reject_review(task_id: str, reason: str, topic: str | None = None) -> dict[str, Any]:
    card, error = _live_card(task_id)
    if error:
        return error
    assert card is not None
    live_topic = card.get("topic")
    if not live_topic:
        return _lifecycle_error("task has no exact topic identity")
    if topic is not None and topic != live_topic:
        return _lifecycle_error(f"topic mismatch expected={live_topic} got={topic}")
    command = [
        "reject-review", task_id, "--runner", CODEX_RUNNER, "--topic", str(live_topic), "--reason", reason,
    ]
    blocked = _canonical_write_gate(
        "reject-review", runner=CODEX_RUNNER, topic=str(live_topic), coordinator_capability=True
    )
    if blocked is not None:
        return blocked
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        cur = conn.execute(
            "UPDATE tasks SET worker_status='unclaimed', status='pending', claimed_by=NULL, updated_at=? "
            "WHERE task_id=? AND worker_status='review'",
            (now, task_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return _canonical_result(
                ok=False, returncode=1, stderr=f"reject_not_reviewable:task_id={task_id}", command=command
            )
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (
                task_id,
                "reject_review",
                CODEX_RUNNER,
                json.dumps({"topic": live_topic, "reason": reason}, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    card2 = task_store.get_task(repo_root(), task_id)
    stdout = json.dumps(card2, ensure_ascii=False, default=str) if card2 else ""
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)


def release_launch(
    task_id: str,
    claimed_by: str,
    reason: str,
    topic: str | None = None,
) -> dict[str, Any]:
    card, error = _live_card(task_id)
    if error:
        return error
    assert card is not None
    live_topic = card.get("topic")
    if not live_topic:
        return _lifecycle_error("task has no exact topic identity")
    if topic is not None and topic != live_topic:
        return _lifecycle_error(f"topic mismatch expected={live_topic} got={topic}")
    command = [
        "release-launch", task_id, "--runner", CODEX_RUNNER, "--topic", str(live_topic),
        "--claimed-by", claimed_by, "--reason", reason,
    ]
    blocked = _canonical_write_gate(
        "release-launch", runner=CODEX_RUNNER, topic=str(live_topic), coordinator_capability=True
    )
    if blocked is not None:
        return blocked
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        cur = conn.execute(
            "UPDATE tasks SET worker_status='unclaimed', status='pending', claimed_by=NULL, updated_at=? "
            "WHERE task_id=? AND claimed_by=?",
            (now, task_id, claimed_by),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return _canonical_result(
                ok=False,
                returncode=1,
                stderr=f"release_claimed_by_mismatch:task_id={task_id}",
                command=command,
            )
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (
                task_id,
                "release_launch",
                CODEX_RUNNER,
                json.dumps({"topic": live_topic, "claimed_by": claimed_by, "reason": reason}, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    card2 = task_store.get_task(repo_root(), task_id)
    stdout = json.dumps(card2, ensure_ascii=False, default=str) if card2 else ""
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)


def _active_cards_for_collision_guard() -> list[dict[str, Any]]:
    """Active (pending/processing/review/blocked) cards with ``card_json``
    merged in, sourced purely from a SQL scan of the canonical ``tasks``
    table -- no taskctl/taskdb subprocess."""
    conn = _canonical_connect(readonly=True)
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
        if bucket not in {"pending", "processing", "review", "blocked"}:
            continue
        try:
            card_json = json.loads(item.get("card_json") or "{}")
        except json.JSONDecodeError:
            card_json = {}
        card = {**card_json, **{k: v for k, v in item.items() if k != "card_json"}}
        card["status"] = bucket
        active.append(card)
    return active


def collision_guard(print_json: bool = True) -> dict[str, Any]:
    command = ["collision-guard"]
    if print_json:
        command.append("--print")
    try:
        cards = _active_cards_for_collision_guard()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    if not cards:
        return _canonical_result(ok=True, returncode=0, stdout="No cards to scan.", command=command)

    root_str = str(repo_root())
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    try:
        from scripts.build_tasking_parallel_group_collision_guard_v1 import scan_collisions
    except ImportError as exc:
        return _canonical_result(
            ok=False, returncode=1, stderr=f"collision_guard_import_failed:{exc}", command=command
        )

    result = scan_collisions(cards)
    lines: list[str] = []
    if result["collision_free"]:
        lines.append(
            f"collision_free=true — no overlapping allowed_writes ({result['active_cards']} active cards)"
        )
    else:
        lines.append(
            f"COLLISION: {result['collision_count']} file collision(s) among {result['active_cards']} active cards"
        )
        for fc in result.get("file_collisions", []) or []:
            lines.append(f"   {fc['file']}: {', '.join(fc['conflicting_tasks'])}")
        for cmd_line in result.get("coordination_commands", []) or []:
            lines.append(f"   {cmd_line}")
    if print_json:
        lines.append("")
        lines.append(json.dumps(result, indent=2, ensure_ascii=False))
    ok = bool(result["collision_free"])
    return _canonical_result(ok=ok, returncode=0 if ok else 1, stdout="\n".join(lines), command=command)


def callback_outbox_status() -> dict[str, Any]:
    """Read-only, redacted-safe callback outbox health (bound/unbound task
    counts, pending/inflight/delivered/dead-letter counts). Sourced from
    ``task_store.callback_bridge_health`` -- never a full origin_thread_id,
    never taskctl/taskdb."""
    command = ["callback-outbox-status"]
    try:
        stats = task_store.callback_bridge_health(repo_root())
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    stdout = json.dumps(stats, ensure_ascii=False, sort_keys=True)
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)


def record_usage(
    task_id: str,
    *,
    runner: str,
    topic: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    source: str | None = None,
    note: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    cost_usd: float = 0.0,
) -> dict[str, Any]:
    """Append one native usage event to the canonical task store.

    This is the standalone replacement for the historical
    ``taskctl.py usage`` subprocess path used by the launcher. It preserves
    the same event shape consumed by ``usage_report`` while staying fully
    repo-local under ``.aiworkhub``.
    """
    command = ["usage", task_id]
    if runner:
        command.extend(["--runner", runner])
    if topic:
        command.extend(["--topic", topic])
    blocked = _canonical_write_gate("usage", runner=runner or None, topic=topic, task_id=task_id)
    if blocked is not None:
        return {**blocked, "command": command}
    card, error = _live_card(task_id)
    if error:
        return error
    assert card is not None
    live_topic = str(card.get("topic") or topic or "")
    payload = {
        "runner": runner,
        "topic": live_topic,
        "status": card.get("status"),
        "worker_status": card.get("worker_status"),
        "model": model or "",
        "provider": provider or "",
        "source": source or "",
        "note": note or "",
        "records": 1,
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "total_tokens": int(total_tokens or (int(input_tokens or 0) + int(output_tokens or 0))),
        "cost_usd": float(cost_usd or 0.0),
    }
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (task_id, "usage_record", runner, json.dumps(payload, ensure_ascii=False), now),
        )
        conn.commit()
    finally:
        conn.close()
    return _canonical_result(ok=True, returncode=0, stdout=f"usage recorded: {task_id}", command=command)


def usage_report(runner: str | None = None, topic: str | None = None, status: str | None = None) -> dict[str, Any]:
    """Read-only aggregate over ``task_events`` rows with ``event='usage_record'``.

    Stdout keeps ``AITools/taskctl.py::cmd_usage_report``'s exact per-task pipe
    line format (``cost_ledger.py::USAGE_LINE_RE`` regex-parses it) so existing
    callers keep working unchanged. The canonical ``tasks`` schema carries no
    token/cost columns, and no write path in this module's current scope
    appends usage-record events yet, so with zero recorded events this prints
    the same ``"No usage records."`` taskctl prints for an empty report --
    never a fabricated total, never taskctl/taskdb.
    """
    command = ["usage-report"]
    if runner:
        command.extend(["--runner", runner])
    if topic:
        command.extend(["--topic", topic])
    if status:
        command.extend(["--status", status])
    try:
        conn = _canonical_connect(readonly=True)
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        rows = conn.execute(
            "SELECT task_id, runner, payload_json, created_at FROM task_events WHERE event='usage_record'"
        ).fetchall()
    finally:
        conn.close()
    records: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        rec = {**payload, "task_id": row["task_id"], "runner": row["runner"], "created_at": row["created_at"]}
        if runner and rec.get("runner") != runner:
            continue
        if topic and rec.get("topic") != topic:
            continue
        if status and rec.get("status") != status:
            continue
        records.append(rec)

    if not records:
        return _canonical_result(ok=True, returncode=0, stdout="No usage records.", command=command)

    lines = ["=== Task Usage Report ==="]
    for rec in records:
        lines.append(
            f"{rec.get('task_id')} | runner={rec.get('runner', '?')} | topic={rec.get('topic', '?')} | "
            f"records={int(rec.get('records') or 1)} | tokens={int(rec.get('total_tokens') or 0)} | "
            f"in={int(rec.get('input_tokens') or 0)} out={int(rec.get('output_tokens') or 0)} | "
            f"cost=${float(rec.get('cost_usd') or 0.0):.4f}"
        )
    stdout = "\n".join(lines)
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)


def export_jsonl(runner: str | None = None, topic: str | None = None) -> dict[str, Any]:
    """Write-gated bounded export of the canonical queue to a JSONL file
    under this repository's canonical ``.aiworkhub/tasking`` directory
    (never a package-install or historical monorepo data path).
    ``runner``/``topic`` are optional B119 allowlist identity hints (default
    None preserves prior behavior for callers that do not supply them)."""
    command = ["export-jsonl"]
    blocked = _canonical_write_gate("export-jsonl", runner=runner, topic=topic)
    if blocked is not None:
        return blocked
    try:
        rows = task_store.list_tasks(repo_root(), status=None, limit=5000)
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    out_dir = (
        repo_root() / repository_state.HUB_DIRNAME / repository_state.DURABLE_LAYOUT["tasking"]
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "canonical_task_export.jsonl"
    lines = [json.dumps(r, ensure_ascii=False, default=str) for r in rows]
    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    stdout = f"exported {len(rows)} rows to {out_path}"
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)


def read_audit_log(
    max_entries: int = 100,
    *,
    repo: Path | None = None,
) -> dict[str, Any]:
    """Read-only: return a summary of the write-gate audit log.

    Never writes, never enables writes, never launches subprocesses.
    Returns counts by tool/action and the last N entries (sanitized —
    env var VALUES are never present in audit entries by construction,
    but this function adds no new secrets exposure).
    """
    log_path = _audit_log_path(repo)
    result: dict[str, Any] = {
        "ok": True,
        "audit_log_path": str(log_path),
        "audit_log_exists": log_path.exists(),
        "total_entries": 0,
        "entries_by_tool": {},
        "entries_by_action": {},
        "last_entries": [],
        "file_size_bytes": 0,
        "writes_allowed": writes_allowed(),
        "authority_flags": {
            "write_gate_enabled": not writes_allowed(),
            "workflow_switch": False,
            "process_launch": False,
            "agent_launch": False,
        },
    }

    if not log_path.exists():
        return result

    try:
        raw = log_path.read_text(encoding="utf-8")
        result["file_size_bytes"] = log_path.stat().st_size
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        entries: list[dict[str, Any]] = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        result["total_entries"] = len(entries)

        tool_counts: dict[str, int] = {}
        for e in entries:
            tn = e.get("tool_name", "unknown")
            tool_counts[tn] = tool_counts.get(tn, 0) + 1
        result["entries_by_tool"] = tool_counts

        action_counts: dict[str, int] = {}
        for e in entries:
            a = e.get("action", "unknown")
            action_counts[a] = action_counts.get(a, 0) + 1
        result["entries_by_action"] = action_counts

        recent = entries[-max_entries:] if max_entries > 0 else []
        result["last_entries"] = recent
    except OSError as exc:
        result["ok"] = False
        result["error"] = str(exc)

    return result


def _parse_jsonl(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


# ---------------------------------------------------------------------------
# B11 (retry of stale-claimed B10): read-only supervisor-loop STATUS derivation.
#
# Implements the 7-step derivation algorithm frozen by the supervisor-loop
# status contract
# verbatim. Pure composition of pre-existing read-only helpers already defined
# above (show_task, check_runner_topic_allowlist, collision_guard,
# _lifecycle_state) -- no NEW subprocess/exec/network/daemon-launch code is
# introduced here, and no write-mode taskctl action (allow_write=True) is ever
# invoked. show_task/collision_guard only ever call run_taskctl with
# allow_write defaulting to False, so no audit entry is ever appended by this
# function (write_audit_entry is only reached from the write-command branches
# of run_taskctl). This is a deterministic safety/derivation classifier per
# CLAUDE.md's neural-control-first carve-out (controller/verifier layers are
# valid when they are correctness/support-boundary checks) -- not a learned
# routing decision, and it introduces no new cue-router cognition.
# ---------------------------------------------------------------------------

LIFECYCLE_TO_SUPERVISOR_STATE: dict[str, str] = {
    "pending": "planned",
    "processing": "dispatched_dryrun",
    "review": "worker_review_ready",
    "finished": "codex_reviewed",
    "blocked": "blocked",
}


def _parse_show_task_card(stdout: str) -> dict[str, Any] | None:
    """Parse a task card dict out of ``show_task``'s stdout, or ``None`` if
    the task was not found.

    ``AITools/taskctl.py::cmd_show`` always exits 0: on a hit it prints one
    pretty-printed JSON object via plain ``print()``; on a miss it prints the
    literal string ``f"Task not found: {task_id}"`` (also via plain
    ``print()``, no ``sys.exit``). Miss-detection must therefore inspect
    stdout content, never returncode. Tolerant parse -- never raises.
    """
    text = (stdout or "").strip()
    if not text or "Task not found:" in text:
        return None
    try:
        card = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(card, dict):
        return None
    return card


def _extract_collision_report(stdout: str) -> dict[str, Any] | None:
    """Extract the ``scan_collisions``-shaped JSON object out of
    ``collision_guard``'s stdout.

    The real ``cmd_collision_guard --print`` output is a human-readable
    banner line, a blank line, then a pretty-printed JSON blob -- not a bare
    JSON document -- so this slices from the first ``{`` rather than parsing
    stdout directly. Tolerant parse -- never raises, returns ``None`` on any
    failure so a parse hiccup degrades to "no collision detected" instead of
    crashing this read-only tool.
    """
    text = stdout or ""
    idx = text.find("{")
    if idx == -1:
        return None
    try:
        return json.loads(text[idx:])
    except json.JSONDecodeError:
        return None


def supervisor_loop_status(
    task_id: str,
    runner: str | None = None,
    topic: str | None = None,
    supervisor_request_id: str | None = None,
    previous_snapshot: dict[str, Any] | None = None,
    reported_validation_verdict: str | None = None,
) -> dict[str, Any]:
    """READ-ONLY: derive ``supervisor_state`` + error taxonomy for a task_id.

    Pure read/derive composition of pre-existing core.py helpers
    (``show_task``, ``check_runner_topic_allowlist``, ``collision_guard``,
    ``_lifecycle_state``) implementing the 7 ``ordered_steps`` frozen by
    ``task_mcp_supervisor_loop_status_tool_b08_v1.json`` exactly:
    fetch_status -> runner_topic_mismatch_check -> missing_artifact_check ->
    collision_check -> stale_task_check -> failed_validation_check ->
    clean_mapping.

    FROZEN READ-ONLY CONTRACT -- no write-gate toggle, no mutation
    parameters. Never calls taskctl done/review/start/auto-pickup/add-card
    and never invokes ``run_taskctl`` with ``allow_write=True``. Never
    mutates queue or audit state, never launches agents/daemons/processes,
    performs no network I/O. Writes remain default-off regardless of input.
    ``previous_snapshot``/``reported_validation_verdict`` are caller-supplied
    out-of-band observations relayed for comparison/reporting only -- this
    function keeps no server-side snapshot store and never independently
    runs validation itself.
    """
    base: dict[str, Any] = {
        "task_id": task_id,
        "supervisor_request_id": supervisor_request_id,
        "process_state": "no_process_local_only_mvp",
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

    # Step 1: fetch_status
    taskctl_status = show_task(task_id)
    card = _parse_show_task_card(taskctl_status.get("stdout", ""))
    if card is None:
        base["state"] = "not_found"
        base["taskctl_status"] = None
        base["supervisor_state"] = None
        base["error"] = None
        return base

    base["taskctl_status"] = taskctl_status

    # Step 2: runner_topic_mismatch_check
    if runner is not None or topic is not None:
        decision = check_runner_topic_allowlist(runner, topic, "review")
        if not decision["allowed"]:
            base["state"] = "blocked"
            base["supervisor_state"] = "blocked"
            base["error"] = {
                "code": "runner_topic_mismatch",
                "reason": f"runner_topic_mismatch:{decision['reason']}",
                "detected_at_leg": 1,
                "blocked": True,
            }
            return base

    # Step 3: missing_artifact_check (pure filesystem existence check only,
    # never a read of file contents)
    root = repo_root()
    candidate_paths = list(card.get("read_first") or []) + list(card.get("allowed_writes") or [])
    for rel_path in candidate_paths:
        if not (root / str(rel_path)).exists():
            base["state"] = "blocked"
            base["supervisor_state"] = "blocked"
            base["error"] = {
                "code": "missing_artifact",
                "reason": f"missing_artifact:{rel_path}",
                "detected_at_leg": 1,
                "blocked": True,
            }
            return base

    # Step 4: collision_check
    guard = collision_guard(print_json=True)
    report = _extract_collision_report(guard.get("stdout", ""))
    if report is not None and not report.get("collision_free", True):
        for fc in report.get("file_collisions", []) or []:
            if task_id in (fc.get("conflicting_tasks") or []):
                base["state"] = "blocked"
                base["supervisor_state"] = "blocked"
                base["error"] = {
                    "code": "collision",
                    "reason": f"collision:{fc.get('file')}:{task_id}",
                    "detected_at_leg": 1,
                    "blocked": True,
                }
                return base

    # Step 5: stale_task_check
    if previous_snapshot is not None:
        cur_snapshot = {"status": card.get("status"), "worker_status": card.get("worker_status")}
        if cur_snapshot != previous_snapshot:
            base["state"] = "blocked"
            base["supervisor_state"] = "blocked"
            base["error"] = {
                "code": "stale_task",
                "reason": (
                    f"stale_task:worker_status_changed:{task_id}:"
                    f"{previous_snapshot}->{cur_snapshot}"
                ),
                "detected_at_leg": 4,
                "blocked": True,
            }
            return base

    # Step 6: failed_validation_check (schema-reserved relay-only signal --
    # this tool never runs validation itself, only relays a caller-reported
    # out-of-band verdict, per B07 leg-3 reality_in_this_contract)
    if reported_validation_verdict == "failed":
        base["state"] = "blocked"
        base["supervisor_state"] = "blocked"
        base["error"] = {
            "code": "failed_validation",
            "reason": f"failed_validation:reported_by_caller:{task_id}",
            "detected_at_leg": 4,
            "blocked": True,
        }
        return base

    # Step 7: clean_mapping
    base["state"] = "planned"
    base["error"] = None
    base["supervisor_state"] = LIFECYCLE_TO_SUPERVISOR_STATE[_lifecycle_state(card)]
    return base


# ---------------------------------------------------------------------------
# B857: repository-bound callback dispatcher lifecycle wiring.
#
# This section is the in-process bridge between (a) the VS Code extension's
# already-idempotent one-process-per-repository lifecycle (activate/reload/
# tab-deserialize/workspace-switch/deactivate -- see McpStdioClient in
# vscode-extension/extension.js) and (b) callback_bridge.py's dispatcher
# registry. It only ever reads the per-repo canonical
# ``.aiworkhub/tasking/task_queue.sqlite`` (via task_store.storage_readiness)
# and the per-repo ``.aiworkhub/config/routing/coordinator-targets.json``
# route-selection file the extension already writes
# (readCoordinatorTargets/setCoordinatorTarget) -- never
# AITools/taskctl.py or AITools/taskdb.py directly. The actual delivery
# engine (callback_bridge.CallbackBridge) keeps using its own existing,
# already-durable AITools/taskdb.py outbox unchanged; only THIS status/
# lifecycle surface is bound to the canonical in-process task_store per the
# B852 doctrine above.
# ---------------------------------------------------------------------------

COORDINATOR_ROUTE_STATE_REL = Path("config") / "routing" / "coordinator-targets.json"


def _callback_bridge_module():
    from . import callback_bridge as _callback_bridge

    return _callback_bridge


def _coordinator_route_state_path(root: Path | None = None) -> Path:
    return (root or repo_root()) / ".aiworkhub" / COORDINATOR_ROUTE_STATE_REL


def read_selected_coordinator_target(root: Path | None = None) -> dict[str, Any]:
    """Read the explicitly selected coordinator provider ("codex"/"claude"/
    "copilot") from the same file the VS Code extension writes
    (``.aiworkhub/config/routing/coordinator-targets.json``). Never invokes
    AITools; missing/corrupt/unrecognized state fails safe to the same
    "codex" default the extension's own ``defaultCoordinatorTargets()``
    uses -- never a guess at a different provider."""
    path = _coordinator_route_state_path(root)
    default = {"schema_id": "aiworkhub.coordinator_targets.v1", "selected_provider": "codex"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    if not isinstance(payload, dict):
        return default
    provider = str(payload.get("selected_provider") or "").strip().lower()
    if provider not in ("codex", "claude", "copilot"):
        return default
    return {**default, **payload, "selected_provider": provider}


def dispatcher_ensure_started() -> dict[str, Any]:
    """Idempotently ensure exactly one callback dispatcher is running for
    the active repository, bound to the currently-selected coordinator
    target. Safe to call repeatedly (activation, tab-deserialization,
    reload, explicit coordinator-target switch) -- never starts a second
    thread and never fabricates a running dispatcher for an uninitialized
    repository."""
    root = repo_root()
    readiness = task_store.storage_readiness(root)
    if not readiness.ready:
        return {
            "ok": True,
            "status": "uninitialized",
            "dispatcher_started": False,
            "reason": readiness.reason,
            "repo": str(root),
        }
    target = read_selected_coordinator_target(root)
    claude_identity = _claude_manager_identity()
    if os.environ.get("AIWORKHUB_CALLBACK_TRANSPORT", "").strip().lower() == "sideband":
        provider = "codex"
        claude_identity = None
    elif claude_identity is not None:
        provider = "claude"
    else:
        provider = target["selected_provider"]
    window_id = os.environ.get("AIWORKHUB_WINDOW_ID", "").strip()
    if not window_id and claude_identity is not None:
        window_id = claude_identity["window_id"]
    if claude_identity is not None and provider == "claude":
        # A second ``claude --resume --print`` process cannot wake an already
        # open Claude Code webview.  The verified manager instead owns a
        # two-phase MCP long-poll inbox (callback_wait/callback_ack).  Do not
        # start the old CLI dispatcher and do not consume its retry budget.
        bridge = _callback_bridge_module()
        bridge.stop_dispatcher(root)
        conn = _canonical_connect()
        try:
            rebound_count = callback_store.rebind_pending_callbacks(
                conn,
                provider="claude",
                origin_thread_id=claude_identity["session_id"],
            )
        finally:
            conn.close()
        return {
            "ok": True,
            "status": "manager_inbox",
            "dispatcher_started": False,
            "repo": str(root),
            "provider": "claude",
            "manager_route": claude_identity,
            "reason": "claude_manager_uses_mcp_callback_wait",
            "rebound_callback_count": rebound_count,
        }
    if not window_id:
        # Headless worker MCP processes never own callback dispatch. They
        # may claim work and mark it for review, but only the repository-
        # bound VS Code extension child has the window identity and lifecycle
        # needed to wake a coordinator UI. Report the role boundary as a
        # normal state instead of attempting a doomed transport launch.
        return {
            "ok": True,
            "status": "headless_worker",
            "dispatcher_started": False,
            "reason": "dispatcher_owned_by_vscode_extension",
            "repo": str(root),
            "provider": provider,
        }
    bridge = _callback_bridge_module()
    bridge_kwargs: dict[str, Any] = {}
    if provider == "codex":
        transport = os.environ.get("AIWORKHUB_CALLBACK_TRANSPORT", "").strip().lower()
        if transport:
            bridge_kwargs["transport"] = transport
    dispatcher = bridge.ensure_dispatcher(
        root,
        provider,
        repo_id=readiness.repo_id,
        window_id=window_id,
        bridge_kwargs=bridge_kwargs,
    )
    health = dispatcher.health()
    return {
        "ok": True,
        "status": "started" if health.get("dispatcher_running") else "start_failed",
        "dispatcher_started": bool(health.get("dispatcher_running")),
        "repo": str(root),
        "manager_route": claude_identity or {},
        **health,
    }


def claude_callback_wait(timeout_seconds: int = 240) -> dict[str, Any]:
    """Wait for one callback batch belonging to this exact Claude manager.

    Returning the batch wakes the already-open Claude turn through its own
    MCP request.  Delivery remains inflight until ``claude_callback_ack``;
    if the tool response is lost, the normal lease reclaim path retries it.
    """
    identity = _claude_manager_identity()
    if identity is None:
        return {"ok": False, "reason": "verified_claude_manager_required"}
    timeout = max(1, min(int(timeout_seconds), 300))
    deadline = time.monotonic() + timeout
    while True:
        conn = _canonical_connect()
        try:
            batch = callback_store.claim_pending_callback_batch(
                conn, lease_seconds=max(120, timeout + 30), provider="claude"
            )
        finally:
            conn.close()
        if batch is not None:
            if batch.get("origin_thread_id") != identity["session_id"]:
                conn = _canonical_connect()
                try:
                    callback_store.defer_batch_busy(
                        conn,
                        str(batch["batch_id"]),
                        "callback_belongs_to_different_claude_session",
                        delay_seconds=30.0,
                    )
                finally:
                    conn.close()
            else:
                members = [
                    {
                        "task_id": str(member.get("task_id") or ""),
                        "state": str(member.get("transition") or ""),
                        "event_id": str(member.get("event_id") or ""),
                        "request_id": str(member.get("request_id") or ""),
                    }
                    for member in batch.get("members", [])
                ]
                return {
                    "ok": True,
                    "status": "callback_ready",
                    "provider": "claude",
                    "batch_id": batch["batch_id"],
                    "lease_id": batch["lease_id"],
                    "origin_thread_id": batch["origin_thread_id"],
                    "members": members,
                }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "ok": True,
                "status": "timeout_no_callback",
                "provider": "claude",
                "waited_seconds": timeout,
            }
        time.sleep(min(0.5, remaining))


def claude_callback_ack(batch_id: str, lease_id: str) -> dict[str, Any]:
    """Durably acknowledge a callback returned by ``claude_callback_wait``."""
    identity = _claude_manager_identity()
    if identity is None:
        return {"ok": False, "reason": "verified_claude_manager_required"}
    conn = _canonical_connect()
    try:
        acknowledged = callback_store.acknowledge_callback_batch(
            conn,
            batch_id,
            lease_id,
            provider="claude",
            origin_thread_id=identity["session_id"],
        )
    finally:
        conn.close()
    return {
        "ok": acknowledged,
        "status": "delivered" if acknowledged else "ack_rejected",
        "provider": "claude",
        "batch_id": batch_id,
    }


def dispatcher_health() -> dict[str, Any]:
    """Read-only dispatcher health for the active repository. An
    uninitialized repository is reported as a normal (non-degraded)
    "uninitialized" status, never as an error."""
    root = repo_root()
    readiness = task_store.storage_readiness(root)
    if not readiness.ready:
        return {
            "ok": True,
            "status": "uninitialized",
            "dispatcher_running": False,
            "reason": readiness.reason,
            "repo": str(root),
        }
    bridge = _callback_bridge_module()
    health = bridge.dispatcher_health(root)
    target = read_selected_coordinator_target(root)
    return {
        "ok": True,
        "status": "running" if health.get("dispatcher_running") else "stopped",
        "repo": str(root),
        "repo_id": readiness.repo_id,
        "selected_provider": target["selected_provider"],
        **health,
    }


def dispatcher_stop() -> dict[str, Any]:
    """Stop and unregister the active repository's dispatcher, if any.
    Used on workspace/repository switch and extension deactivation so no
    cross-repository read or delivery can happen afterward."""
    root = repo_root()
    bridge = _callback_bridge_module()
    stopped = bridge.stop_dispatcher(root)
    return {"ok": True, "stopped": stopped, "repo": str(root)}
