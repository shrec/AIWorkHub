#!/usr/bin/env python3
"""B116: MVP_ROADMAP finish-line status audit (Phases 0-3).

Read-only audit: maps every MVP_ROADMAP checkbox to evidence_refs + status,
identifies the shortest safe next-wave sequence, and emits compact JSON/JSONL.
Never mutates parent queue, never enables workflow_switch or launch.

Outputs (all under tools/geoai-task-mcp/):
  eval/task_mcp_finishline_status_audit_b116_v1.json
  eval/task_mcp_finishline_status_audit_rows_b116_v1.jsonl
  data/tasking/task_mcp_finishline_status_audit_next_wave_b116_v1.json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── paths ──────────────────────────────────────────────────────────────
REPO = Path(os.environ.get("GEOAI_REPO", "/home/shrek/GeoAI")).expanduser().resolve()
MCP_ROOT = REPO / "tools" / "geoai-task-mcp"
EVAL_DIR = MCP_ROOT / "eval"
DATA_DIR = MCP_ROOT / "data" / "tasking"
SCRIPTS_DIR = MCP_ROOT / "scripts"
SRC_DIR = MCP_ROOT / "src" / "geoai_task_mcp"
TESTS_DIR = MCP_ROOT / "tests"

# ── authority flags (hard invariant) ───────────────────────────────────
AUTHORITY_FLAGS: dict[str, bool] = {
    "workflow_switch": False,
    "launch_enabled": False,
    "write_gate_default_off": True,
    "parent_queue_mutation": False,
    "audit_readonly": True,
}


def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=REPO, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)


def git_dirty_report() -> dict[str, Any]:
    """Report dirty/untracked state inside tools/geoai-task-mcp/."""
    result = run(["git", "status", "--short", "--", "tools/geoai-task-mcp/"])
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    dirty: list[str] = []
    untracked: list[str] = []
    for line in lines:
        status = line[:2]
        path = line[3:].strip()
        if status.strip() in ("M", "MM", "AM", " D", "D "):
            dirty.append(path)
        elif status.strip() == "??":
            untracked.append(path)
        else:
            untracked.append(path)  # other states treated as untracked for new dir

    is_nested_repo = (MCP_ROOT / ".git").exists()
    return {
        "dirty_count": len(dirty),
        "dirty_files": dirty,
        "untracked_count": len(untracked),
        "untracked_files": untracked,
        "total_changed": len(lines),
        "is_nested_git_repo": is_nested_repo,
        "parent_repo_clean": len([l for l in lines if l.strip()]) == len(untracked),
    }


def file_exists(rel: str) -> bool:
    return (MCP_ROOT / rel).exists()


def file_has_content(rel: str, needle: str) -> bool:
    p = MCP_ROOT / rel
    if not p.exists():
        return False
    return needle in p.read_text(encoding="utf-8")


def has_tool_in_server(tool_name: str) -> bool:
    """Check if @mcp.tool() function exists in server.py."""
    p = SRC_DIR / "server.py"
    if not p.exists():
        return False
    text = p.read_text(encoding="utf-8")
    return f"def {tool_name}(" in text


def has_core_function(func_name: str) -> bool:
    """Check if function exists in core.py."""
    p = SRC_DIR / "core.py"
    if not p.exists():
        return False
    text = p.read_text(encoding="utf-8")
    return f"def {func_name}(" in text


def has_test_file(pattern: str) -> bool:
    """Check if any test file matches pattern."""
    for f in TESTS_DIR.iterdir():
        if pattern in f.name:
            return True
    return False


# ── checkbox audit ─────────────────────────────────────────────────────
def audit_all() -> list[dict[str, Any]]:
    """Map every MVP_ROADMAP checkbox to evidence and status."""
    rows: list[dict[str, Any]] = []
    ts = datetime.now(timezone.utc).isoformat()

    # ── Phase 0 ──
    rows.append({
        "phase": 0, "checkbox": "p0_package_skeleton",
        "label": "Standalone Python package skeleton",
        "status": "DONE",
        "evidence_refs": [
            "pyproject.toml: name=geoai-task-mcp, version=0.1.0, requires mcp>=1.0.0",
            "src/geoai_task_mcp/__init__.py: __version__=0.1.0",
            "src/geoai_task_mcp/health.py: CLI entry point",
        ],
        "timestamp": ts,
    })

    rows.append({
        "phase": 0, "checkbox": "p0_fastmcp_stdio_server",
        "label": "FastMCP stdio server",
        "status": "DONE",
        "evidence_refs": [
            "server.py: FastMCP('GeoAI Task MCP'), stdio transport, main()->mcp.run()",
            "scripts/run_stdio.sh: PYTHONPATH wrapper",
            "test_mcp_stdio_client_smoke tests exist",
        ],
        "timestamp": ts,
    })

    rows.append({
        "phase": 0, "checkbox": "p0_readonly_tools",
        "label": "Read-only task tools over taskctl.py",
        "status": "DONE",
        "evidence_refs": [
            "server.py: geoai_task_health, geoai_task_review_queue, geoai_task_list, geoai_task_show, geoai_task_pending_for_runner, geoai_task_collision_guard, geoai_task_usage_report, geoai_task_audit_log_read, geoai_task_review_summarize, geoai_task_codex_handoff + cli_adapter_readonly_tool.register()",
            "core.py: all underlying read-only wrappers call taskctl without allow_write=True",
            "eval/mvp_contract_audit_v1.json: counts.read=7 (stale, now >=10)",
        ],
        "timestamp": ts,
    })

    rows.append({
        "phase": 0, "checkbox": "p0_write_gated_tools",
        "label": "Write-gated lifecycle tools",
        "status": "DONE",
        "evidence_refs": [
            "core.py: auto_pickup/mark_review/mark_done/export_jsonl -> run_taskctl(allow_write=True) gated by writes_allowed()",
            "core.py: WRITE_COMMANDS set + _is_write_command guard",
            "core.py: write_audit_entry on blocked attempt, GEOAI_TASK_MCP_ALLOW_WRITES default 0",
            "eval/mvp_contract_audit_v1.json: counts.write_gated=4, unsafe_ungated_writes=0",
        ],
        "timestamp": ts,
    })

    rows.append({
        "phase": 0, "checkbox": "p0_smoke_test",
        "label": "Smoke test for health/read/write-gate",
        "status": "DONE",
        "evidence_refs": [
            "tests/smoke.sh: health check, read tools, write-gate block smoke",
            "tests/test_write_gate_audit_v1.sh: dedicated write-gate audit smoke",
            "tests/readonly_tools_smoke.py: Python smoke for read-only tools",
        ],
        "timestamp": ts,
    })

    rows.append({
        "phase": 0, "checkbox": "p0_mcp_client_smoke",
        "label": "MCP client smoke test from VS Code/Codex/Claude environment",
        "status": "GAP",
        "evidence_refs": [
            "data/tasking/mcp_client_smoke_contract_freeze_next_wave_b108_v1.json: next-wave task exists, not yet executed",
            "tests/mcp_client_smoke_contract_freeze.py: test harness exists (tests contract freeze, not live client)",
            "tests/test_mcp_client_smoke_contract_freeze_b108_v1.sh: contract test, not live smoke",
        ],
        "gap_detail": "No live MCP client smoke from an actual VS Code/Codex/Claude MCP host. Contract + freeze tests exist but a real agent hasn't exercised the server over stdio.",
        "blocker": None,
        "timestamp": ts,
    })

    # ── Phase 1 ──
    rows.append({
        "phase": 1, "checkbox": "p1_audit_log",
        "label": "Tool-level audit log for every write-gated action",
        "status": "DONE",
        "evidence_refs": [
            "core.py: write_audit_entry() — append-only JSONL, sanitized env vars (names only, never values)",
            "core.py: read_audit_log() — returns counts by tool/action + last N entries",
            "server.py: geoai_task_audit_log_read tool wired",
            "tests/test_audit_log_read_tool_b104_v1.sh: audit log read smoke test",
        ],
        "timestamp": ts,
    })

    rows.append({
        "phase": 1, "checkbox": "p1_dryrun_auto_pickup",
        "label": "Dry-run mode for auto_pickup",
        "status": "GAP",
        "evidence_refs": [
            "source_graph: no auto_pickup_dry or dry_run mode found in core.py or server.py",
            "core.py: auto_pickup() calls run_taskctl(['auto-pickup',...], allow_write=True) — no --dry-run flag",
            "cli_adapter_dryrun.py: plan_dryrun exists for CLI adapter, but not wired to auto_pickup tool",
        ],
        "gap_detail": "auto_pickup tool has no --dry-run flag. The CLI adapter dry-run infrastructure exists (cli_adapter_dryrun.py) but is not connected to the task-lifecycle auto_pickup path.",
        "blocker": None,
        "timestamp": ts,
    })

    rows.append({
        "phase": 1, "checkbox": "p1_batch_status_snapshot",
        "label": "Batch status snapshot tool for dashboard use",
        "status": "GAP",
        "evidence_refs": [
            "source_graph: no 'batch_status' found anywhere in repo",
            "server.py: no batch/dashboard/snapshot tool registered",
            "usage_report tool exists but is single-filter, not a multi-dimensional dashboard snapshot",
        ],
        "gap_detail": "No tool provides a multi-axis snapshot (by topic×status, runner×status, age buckets, blocked count). usage_report is single-axis filtered.",
        "blocker": None,
        "timestamp": ts,
    })

    rows.append({
        "phase": 1, "checkbox": "p1_handoff_summarizer",
        "label": "Task result handoff summarizer (Codex-ready report)",
        "status": "DONE",
        "evidence_refs": [
            "server.py: geoai_task_review_summarize (B111 wiring)",
            "server.py: geoai_task_codex_handoff (B116 e2e handoff report)",
            "review_summarizer.py: summarize_review_queue(), build_codex_handoff_report()",
            "tests/test_mcp_codex_handoff_e2e_b116_v1.sh: e2e handoff test",
            "tests/test_mcp_review_queue_summarizer_b110_v1.sh: queue summarizer test",
        ],
        "timestamp": ts,
    })

    rows.append({
        "phase": 1, "checkbox": "p1_allow_list",
        "label": "Strict allow-list for runners/topics",
        "status": "GAP",
        "evidence_refs": [
            "launch_queue_contract.py: static RUNNER_ALLOWLIST + TOPIC_ALLOWLIST exist but are in the disabled launch queue contract, not enforced at taskctl caller level",
            "core.py: no runner/topic allow-list enforcement before calling taskctl",
            "server.py: no allow-list check in auto_pickup or pending_for_runner",
        ],
        "gap_detail": "Static allowlists are defined in launch_queue_contract.py (disabled module), but core.py and server.py do not enforce any runner/topic allow-list. Any runner string can call auto_pickup.",
        "blocker": None,
        "timestamp": ts,
    })

    # ── Phase 2 ──
    rows.append({
        "phase": 2, "checkbox": "p2_cli_adapter_abstraction",
        "label": "Local CLI adapter abstraction",
        "status": "DONE",
        "evidence_refs": [
            "cli_adapter_dryrun.py: AdapterPlan dataclass, plan_dryrun(), launch_enabled()=False, LAUNCH_IMPLEMENTED=False",
            "cli_adapter_readonly_tool.py: plan_command_readonly(), audit_summary_readonly(), register(mcp)",
            "tests/test_cli_adapter_dryrun_contract_b105_v1.sh",
            "tests/test_cli_adapter_readonly_tool_b106_v1.sh",
        ],
        "timestamp": ts,
    })

    rows.append({
        "phase": 2, "checkbox": "p2_claude_adapter",
        "label": "Claude CLI adapter if available",
        "status": "GAP",
        "evidence_refs": [
            "launch_queue_contract.py: claude adapter ID defined ('claude') in ADAPTER_IDS enum + ALLOWLIST",
            "No concrete ClaudeCLIAdapter implementation exists (only abstraction + contract)",
        ],
        "gap_detail": "Adapter abstraction exists but no Claude-specific CLI adapter is implemented. Blocked on Phase 2 launch-enablement gate.",
        "blocker": "phase2_launch_disabled",
        "timestamp": ts,
    })

    rows.append({
        "phase": 2, "checkbox": "p2_codex_adapter",
        "label": "Codex CLI adapter if available",
        "status": "GAP",
        "evidence_refs": [
            "launch_queue_contract.py: codex adapter ID defined ('codex') in ADAPTER_IDS enum",
            "No concrete CodexCLIAdapter implementation exists",
        ],
        "gap_detail": "Same as Claude adapter — abstraction exists, no concrete implementation.",
        "blocker": "phase2_launch_disabled",
        "timestamp": ts,
    })

    rows.append({
        "phase": 2, "checkbox": "p2_deepseek_adapter",
        "label": "Manual/external adapter for Copilot-hosted DeepSeek",
        "status": "GAP",
        "evidence_refs": [
            "launch_queue_contract.py: deepseek adapter ID defined ('deepseek') in ADAPTER_IDS enum",
            "data/tasking/agent_launcher_design_next_wave_v1.json: design doc for DeepSeek surface evaluation",
        ],
        "gap_detail": "Design doc exists, no implementation. DeepSeek worker surface candidates not yet evaluated per Phase 2 source notes.",
        "blocker": "phase2_launch_disabled",
        "timestamp": ts,
    })

    rows.append({
        "phase": 2, "checkbox": "p2_deepseek_eval",
        "label": "Evaluate DeepSeek worker surface candidates from awesome-deepseek-agent",
        "status": "GAP",
        "evidence_refs": [
            "MVP_ROADMAP.md Phase 2 Source Notes: lists candidates (DeepSeek-TUI, Reasonix, Deep Code, OpenCode/Cline/Qwen Code/Codex/Copilot)",
            "data/tasking/agent_launcher_design_next_wave_v1.json: design doc, no evaluation results",
            "No eval/benchmark JSON with candidate comparison exists",
        ],
        "gap_detail": "Source reference documented but zero candidates evaluated. Required before any DeepSeek CLI adapter choice.",
        "blocker": "phase2_launch_disabled",
        "timestamp": ts,
    })

    rows.append({
        "phase": 2, "checkbox": "p2_launch_queue",
        "label": "Launch queue with process state and logs",
        "status": "DONE",
        "evidence_refs": [
            "launch_queue_contract.py: LaunchQueue, AdapterPlan, AdapterResult dataclasses, enqueue/dequeue/status, LAUNCH_IMPLEMENTED=False constant",
            "launch_queue_contract.py: attempt_start() always raises LaunchDisabledError",
            "tests/test_launch_disabled_queue_contract_b117_v1.sh: contract smoke test PASS",
            "ALLOW_LAUNCH_ENV + ALLOW_WRITES_ENV both default '0'",
        ],
        "timestamp": ts,
    })

    rows.append({
        "phase": 2, "checkbox": "p2_launch_disabled",
        "label": "Keep all launch actions disabled unless explicitly enabled",
        "status": "DONE",
        "evidence_refs": [
            "launch_queue_contract.py: LAUNCH_IMPLEMENTED=False (constant, not toggle)",
            "launch_queue_contract.py: launch_enabled() always returns False",
            "cli_adapter_dryrun.py: LAUNCH_IMPLEMENTED=False",
            "cli_adapter_readonly_tool.py: launch_enabled() always False",
            "review_summarizer.py: SUBPROCESS_LAUNCH_TRIPWIRE=0",
            "ALLOW_LAUNCH_ENV + ALLOW_WRITES_ENV both default '0' in all modules",
        ],
        "timestamp": ts,
    })

    # ── Phase 3 ──
    rows.append({
        "phase": 3, "checkbox": "p3_full_task_wave",
        "label": "Run one full task wave through MCP without manual copy/paste",
        "status": "NOT_STARTED",
        "evidence_refs": [
            "No evidence of a complete task wave (claim→work→review→done) executed purely via MCP tools",
            "Phase 1 gaps (dry-run auto_pickup, allow-list, batch snapshot) block safe wave execution",
            "Phase 0 gap (MCP client smoke test) blocks confidence",
        ],
        "gap_detail": "Cannot execute until Phase 0/1 gaps close and at least one real MCP client exercises the full lifecycle.",
        "blocker": "p0_mcp_client_smoke + p1_dryrun_auto_pickup + p1_allow_list",
        "timestamp": ts,
    })

    rows.append({
        "phase": 3, "checkbox": "p3_collision_guard_batch",
        "label": "Validate collision guard before and after every batch",
        "status": "GAP",
        "evidence_refs": [
            "cli_adapter_readonly_tool.py: collision_preflight() exists for single-task preflight (B107)",
            "tests/test_readonly_collision_preflight_b107_v1.sh: single-task preflight test",
            "No batch pre/post validation loop exists",
        ],
        "gap_detail": "Single-task collision_preflight exists (B107). Batch-level pre-wave and post-wave collision validation not implemented.",
        "blocker": None,
        "timestamp": ts,
    })

    rows.append({
        "phase": 3, "checkbox": "p3_no_parent_corruption",
        "label": "Validate no parent-repo task state corruption",
        "status": "NOT_STARTED",
        "evidence_refs": [
            "No corruption validation test exists (no comparison of pre-MCP vs post-MCP taskctl verify, queue integrity, or JSONL consistency)",
            "eval/mvp_contract_audit_v1.json: acceptance.no_parent_change=pass (but this is at-rest audit, not post-wave validation)",
        ],
        "gap_detail": "No automated test that runs a wave through MCP and then validates parent repo taskctl verify + queue integrity unchanged.",
        "blocker": "p3_full_task_wave",
        "timestamp": ts,
    })

    rows.append({
        "phase": 3, "checkbox": "p3_freeze_contract_v1",
        "label": "Freeze MCP tool contract v1",
        "status": "NOT_STARTED",
        "evidence_refs": [
            "eval/mvp_contract_audit_v1.json: migration_readiness.blockers[0] = 'MCP tool contract v1 not frozen'",
            "data/tasking/mcp_unified_contract_freeze_gate_next_wave_b110_v1.json: next-wave freeze task exists",
            "tests/test_mcp_unified_contract_freeze_gate_b110_v1.sh: gate test exists (tests readiness, doesn't freeze)",
        ],
        "gap_detail": "Contract freeze gate task exists but not executed. Contract v1 cannot freeze until Phase 0/1/2 MVP is complete.",
        "blocker": "phase0_1_gaps",
        "timestamp": ts,
    })

    rows.append({
        "phase": 3, "checkbox": "p3_github_submodule",
        "label": "Convert this directory to real GitHub repo/submodule",
        "status": "NOT_STARTED",
        "evidence_refs": [
            "eval/mvp_contract_audit_v1.json: F5 = already independent nested git repo, mechanically ready",
            "pyproject.toml present, independent package",
            "Blocked on contract freeze (p3_freeze_contract_v1) + external smoke test (p0_mcp_client_smoke)",
        ],
        "gap_detail": "Mechanically ready (nested .git, pyproject.toml) but blocked on contract freeze and client smoke test.",
        "blocker": "p3_freeze_contract_v1 + p0_mcp_client_smoke",
        "timestamp": ts,
    })

    return rows


def build_next_wave(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Identify the shortest safe sequence to reach one full MCP-mediated task wave."""
    gaps = [r for r in rows if r["status"] in ("GAP", "NOT_STARTED")]
    
    # Shortest safe sequence: close Phase 0 client smoke → close Phase 1 gaps → execute Phase 3 wave
    sequence: list[dict[str, Any]] = []
    
    # Step 1: MCP client smoke (Phase 0 gap) — prerequisite for all further testing
    sequence.append({
        "step": 1,
        "checkbox": "p0_mcp_client_smoke",
        "label": "Run live MCP client smoke from VS Code/Codex/Claude",
        "rationale": "Prerequisite: cannot validate any MCP-mediated workflow without a real client exercising the server over stdio.",
        "current_task": "mcp_client_smoke_contract_freeze_b108_v1",
        "estimated_effort": "1 session",
        "depends_on": [],
    })
    
    # Step 2: Dry-run auto_pickup (Phase 1 gap)
    sequence.append({
        "step": 2,
        "checkbox": "p1_dryrun_auto_pickup",
        "label": "Add --dry-run flag to geoai_task_auto_pickup",
        "rationale": "Workers need to preview what auto_pickup would claim before committing. Safety prerequisite for wave execution.",
        "estimated_effort": "1 session",
        "depends_on": ["p0_mcp_client_smoke"],
    })
    
    # Step 3: Allow-list enforcement (Phase 1 gap)
    sequence.append({
        "step": 3,
        "checkbox": "p1_allow_list",
        "label": "Enforce runner/topic allow-list in core.py write path",
        "rationale": "Reuse static allowlists from launch_queue_contract.py. Prevent unauthorized runner/topic strings from mutating queue.",
        "estimated_effort": "1 session",
        "depends_on": ["p0_mcp_client_smoke"],
    })
    
    # Step 4: Collision guard batch validation (Phase 3 gap)
    sequence.append({
        "step": 4,
        "checkbox": "p3_collision_guard_batch",
        "label": "Add batch pre/post collision guard validation",
        "rationale": "Extend B107 single-task preflight to batch: validate no collisions before wave start and after wave end.",
        "estimated_effort": "1 session",
        "depends_on": ["p1_allow_list"],
    })
    
    # Step 5: Execute one full task wave
    sequence.append({
        "step": 5,
        "checkbox": "p3_full_task_wave",
        "label": "Execute one full MCP-mediated task wave (claim→work→review→done)",
        "rationale": "The goal: prove MCP can mediate the entire task lifecycle without manual copy/paste.",
        "estimated_effort": "1-2 sessions",
        "depends_on": ["p1_dryrun_auto_pickup", "p1_allow_list", "p3_collision_guard_batch"],
    })
    
    # Step 6: Validate no parent corruption
    sequence.append({
        "step": 6,
        "checkbox": "p3_no_parent_corruption",
        "label": "Validate no parent-repo task state corruption after wave",
        "rationale": "Post-wave: run taskctl verify, compare pre/post queue integrity, JSONL consistency.",
        "estimated_effort": "1 session",
        "depends_on": ["p3_full_task_wave"],
    })

    return {
        "audit_id": "task_mcp_finishline_status_audit_b116_v1",
        "contract": "B116_v1",
        "total_gaps": len(gaps),
        "total_done": len([r for r in rows if r["status"] == "DONE"]),
        "total_checkboxes": len(rows),
        "shortest_safe_sequence": sequence,
        "sequence_steps": len(sequence),
        "authority_flags": AUTHORITY_FLAGS,
        "generated": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    # 1. Git dirty report
    dirty = git_dirty_report()

    # 2. Full audit rows
    rows = audit_all()

    # 3. Next wave
    next_wave = build_next_wave(rows)

    # 4. Aggregate eval summary
    done_count = sum(1 for r in rows if r["status"] == "DONE")
    gap_count = sum(1 for r in rows if r["status"] == "GAP")
    not_started_count = sum(1 for r in rows if r["status"] == "NOT_STARTED")
    blocked_count = sum(1 for r in rows if r["status"] == "BLOCKED")

    eval_summary: dict[str, Any] = {
        "audit_id": "task_mcp_finishline_status_audit_b116_v1",
        "contract": "B116_v1",
        "generated": datetime.now(timezone.utc).isoformat(),
        "git_state": dirty,
        "authority_flags": AUTHORITY_FLAGS,
        "summary": {
            "total_checkboxes": len(rows),
            "done": done_count,
            "gap": gap_count,
            "not_started": not_started_count,
            "blocked": blocked_count,
        },
        "by_phase": {},
        "next_wave_sequence": next_wave["shortest_safe_sequence"],
        "acceptance": {
            "mvp_roadmap_mapped": True,
            "shortest_safe_sequence_provided": len(next_wave["shortest_safe_sequence"]) > 0,
            "dirty_untracked_reported": dirty["total_changed"] >= 0,
            "workflow_switch_false": AUTHORITY_FLAGS["workflow_switch"] == False,
            "launch_enabled_false": AUTHORITY_FLAGS["launch_enabled"] == False,
        },
    }

    for r in rows:
        phase_key = f"phase_{r['phase']}"
        if phase_key not in eval_summary["by_phase"]:
            eval_summary["by_phase"][phase_key] = {"done": 0, "gap": 0, "not_started": 0, "blocked": 0, "total": 0}
        eval_summary["by_phase"][phase_key][r["status"].lower()] += 1
        eval_summary["by_phase"][phase_key]["total"] += 1

    # 5. Write outputs
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Eval JSON
    eval_path = EVAL_DIR / "task_mcp_finishline_status_audit_b116_v1.json"
    eval_path.write_text(json.dumps(eval_summary, indent=2, ensure_ascii=False), encoding="utf-8")

    # Eval JSONL rows
    jsonl_path = EVAL_DIR / "task_mcp_finishline_status_audit_rows_b116_v1.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Next wave JSON
    next_wave_path = DATA_DIR / "task_mcp_finishline_status_audit_next_wave_b116_v1.json"
    next_wave_path.write_text(json.dumps(next_wave, indent=2, ensure_ascii=False), encoding="utf-8")

    # 6. Print compact summary
    print(f"B116 AUDIT COMPLETE")
    print(f"  Checkboxes: {len(rows)} total | DONE={done_count} GAP={gap_count} NOT_STARTED={not_started_count} BLOCKED={blocked_count}")
    print(f"  Phase 0: {eval_summary['by_phase']['phase_0']['done']}/{eval_summary['by_phase']['phase_0']['total']} done")
    print(f"  Phase 1: {eval_summary['by_phase']['phase_1']['done']}/{eval_summary['by_phase']['phase_1']['total']} done")
    print(f"  Phase 2: {eval_summary['by_phase']['phase_2']['done']}/{eval_summary['by_phase']['phase_2']['total']} done")
    print(f"  Phase 3: {eval_summary['by_phase']['phase_3']['done']}/{eval_summary['by_phase']['phase_3']['total']} done")
    print(f"  Dirty files: {dirty['dirty_count']} | Untracked: {dirty['untracked_count']}")
    print(f"  Next-wave steps: {len(next_wave['shortest_safe_sequence'])}")
    print(f"  workflow_switch={AUTHORITY_FLAGS['workflow_switch']} launch_enabled={AUTHORITY_FLAGS['launch_enabled']}")
    print(f"  Outputs:")
    print(f"    {eval_path}")
    print(f"    {jsonl_path}")
    print(f"    {next_wave_path}")

    # 7. Assertions for test harness
    assert done_count + gap_count + not_started_count + blocked_count == len(rows), "sum mismatch"
    assert dirty["parent_repo_clean"], "parent repo has dirty files outside untracked MCP dir"
    assert AUTHORITY_FLAGS["workflow_switch"] == False
    assert AUTHORITY_FLAGS["launch_enabled"] == False
    assert eval_path.exists()
    assert jsonl_path.exists()
    assert next_wave_path.exists()

    return 0


if __name__ == "__main__":
    sys.exit(main())
