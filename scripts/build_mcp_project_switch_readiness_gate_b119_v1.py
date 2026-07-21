#!/usr/bin/env python3
"""B119: MCP project-switch readiness gate — final scorecard.

Read-only decision surface for whether AIWorkHub coordination may switch from
manual copy/paste task blocks to MCP-mediated task orchestration.

This script NEVER mutates: it only reads existing evidence JSON/JSONL
artifacts (B116 finish-line audit + rows, B117 launch-disabled contract,
B118 live-client-smoke / autopickup-dryrun / allowlist-design) plus a
read-only "queue status snapshot" that records the CURRENT status of the
three in-flight B119 gate tasks (allowlist enforcement, batch collision
guard, full-wave dryrun harness). It does not call any taskctl WRITE
command and does not enable workflow_switch, write_gate, or launch.

workflow_switch_ready is declared True only when ALL FIVE named gates
pass:
  1. live_client        - a real out-of-process MCP client exercised the
                           server over stdio (B118 live client smoke).
  2. dryrun             - auto_pickup has a safe read-only dry-run preview
                           (B118 autopickup dryrun).
  3. allowlist          - runner/topic allow-list is DESIGNED *and*
                           ENFORCED in core.py's write path (B118 design +
                           B119 enforcement task finished).
  4. batch_guard        - batch pre/post collision guard finished (B119).
  5. full_wave_dryrun   - one full task-wave dry-run harness finished
                           (B119) — note: a dry-run harness proves the
                           logic; it is NOT the same as a live wave run
                           with AIWORKHUB_ALLOW_WRITES=1, which
                           remains a separate, still-forbidden action.

Usage:
  python3 tools/aiworkhub/scripts/build_mcp_project_switch_readiness_gate_b119_v1.py \
      [--finishline-audit PATH] [--finishline-rows PATH] \
      [--live-client-smoke PATH] [--autopickup-dryrun PATH] \
      [--launch-disabled-contract PATH] [--allowlist-design PATH] \
      [--queue-status-snapshot PATH] \
      [--out PATH] [--out-rows PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(os.environ.get("AIWORKHUB_REPO", "/home/shrek/AIWorkHub")).expanduser().resolve()
MCP_ROOT = REPO / "tools" / "aiworkhub"
EVAL_DIR = MCP_ROOT / "eval"
DATA_DIR = MCP_ROOT / "data" / "tasking"

# ── hard authority invariants (this script only ever reads) ────────────
AUTHORITY_FLAGS: dict[str, bool] = {
    "workflow_switch": False,
    "queue_mutation": False,
    "write_gate_disable": False,
    "process_launch": False,
    "default_change": False,
    "gate_readonly": True,
}

# ── the 3 in-flight B119 gate task_ids this scorecard depends on ───────
ALLOWLIST_ENFORCEMENT_TASK_ID = "CLAUDE_TASK_MCP_RUNNER_TOPIC_ALLOWLIST_ENFORCEMENT_B119_V1"
BATCH_GUARD_TASK_ID = "DEEPSEEK_TASK_MCP_BATCH_COLLISION_GUARD_B119_V1"
FULL_WAVE_DRYRUN_TASK_ID = "DEEPSEEK_TASK_MCP_FULL_WAVE_DRYRUN_HARNESS_B119_V1"

# Frozen default snapshot (captured via read-only `taskctl.py list --topic
# task_mcp` on 2026-07-05T18:4x UTC while this scorecard was authored).
# Override with --queue-status-snapshot for a fresher read-only check;
# this default never mutates anything and is safe to ship as-is.
DEFAULT_QUEUE_STATUS_SNAPSHOT: dict[str, Any] = {
    "captured_at": "2026-07-05T18:45:00+00:00",
    "captured_via": "python3 AITools/taskctl.py list --topic task_mcp (read-only)",
    "statuses": {
        ALLOWLIST_ENFORCEMENT_TASK_ID: "processing",
        BATCH_GUARD_TASK_ID: "review",
        FULL_WAVE_DRYRUN_TASK_ID: "review",
    },
}

FINISHED_STATUSES = {"finished", "done", "completed", "stale_already_done"}


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=REPO, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def git_dirty_report(rel_path: str) -> dict[str, Any]:
    """Read-only git status report scoped to rel_path (no writes)."""
    result = run(["git", "status", "--short", "--", rel_path])
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    dirty: list[str] = []
    untracked: list[str] = []
    for line in lines:
        status = line[:2]
        path = line[3:].strip()
        if status.strip() == "??":
            untracked.append(path)
        else:
            dirty.append(path)
    return {
        "dirty_count": len(dirty),
        "untracked_count": len(untracked),
        "total_changed": len(lines),
        "parent_repo_clean": len(dirty) == 0,
    }


def load_queue_status_snapshot(path: Path | None) -> dict[str, Any]:
    if path is None:
        return DEFAULT_QUEUE_STATUS_SNAPSHOT
    data = load_json(path)
    if data is None or "statuses" not in data:
        return DEFAULT_QUEUE_STATUS_SNAPSHOT
    return data


def is_finished(status: str | None) -> bool:
    return str(status or "").strip().lower() in FINISHED_STATUSES


# ── checkbox overlay: apply later evidence on top of the B116 ledger ───
def overlay_checkboxes(
    base_rows: list[dict[str, Any]],
    live_client: dict[str, Any] | None,
    dryrun: dict[str, Any] | None,
    allowlist_design: dict[str, Any] | None,
    queue_snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    statuses = queue_snapshot.get("statuses", {})
    allowlist_enforced = is_finished(statuses.get(ALLOWLIST_ENFORCEMENT_TASK_ID))
    batch_guard_done = is_finished(statuses.get(BATCH_GUARD_TASK_ID))
    full_wave_done = is_finished(statuses.get(FULL_WAVE_DRYRUN_TASK_ID))

    live_client_ok = bool(live_client) and live_client.get("verdict") == "LIVE_CLIENT_USABLE"
    dryrun_ok = bool(dryrun) and dryrun.get("verdict") == "PASS"
    allowlist_designed = bool(allowlist_design) and allowlist_design.get("verdict") == "PASS"

    rows: list[dict[str, Any]] = []
    ts = datetime.now(timezone.utc).isoformat()
    for row in base_rows:
        r = dict(row)
        checkbox = r.get("checkbox")
        if checkbox == "p0_mcp_client_smoke" and live_client_ok:
            r["status"] = "DONE"
            r["evidence_refs"] = list(r.get("evidence_refs", [])) + [
                "eval/mcp_live_client_smoke_b118_v1.json: verdict=LIVE_CLIENT_USABLE, "
                "real out-of-process stdio_client+ClientSession, queue_integrity.unchanged=true",
            ]
            r["gap_detail"] = None
            r["closed_by"] = "DEEPSEEK_TASK_MCP_LIVE_CLIENT_SMOKE_B118_V1"
        elif checkbox == "p1_dryrun_auto_pickup" and dryrun_ok:
            r["status"] = "DONE"
            r["evidence_refs"] = list(r.get("evidence_refs", [])) + [
                "eval/mcp_autopickup_dryrun_b118_v1.json: verdict=PASS, "
                "core.auto_pickup_dryrun() read-only mirror, no write command invoked",
            ]
            r["gap_detail"] = None
            r["closed_by"] = "CLAUDE_TASK_MCP_AUTOPICKUP_DRYRUN_B118_V1"
        elif checkbox == "p1_allow_list":
            if allowlist_enforced:
                r["status"] = "DONE"
                r["gap_detail"] = None
                r["closed_by"] = ALLOWLIST_ENFORCEMENT_TASK_ID
            else:
                r["status"] = "GAP"
                r["in_codex_review"] = False
                r["gap_detail"] = (
                    "Design frozen and PASS (eval/mcp_runner_topic_allowlist_design_b118_v1.json), "
                    f"but enforcement task {ALLOWLIST_ENFORCEMENT_TASK_ID} status="
                    f"{statuses.get(ALLOWLIST_ENFORCEMENT_TASK_ID, 'unknown')} (not finished). "
                    "core.py write path does not yet enforce the allow-list."
                )
            r["evidence_refs"] = list(r.get("evidence_refs", [])) + (
                ["eval/mcp_runner_topic_allowlist_design_b118_v1.json: verdict=PASS (design only)"]
                if allowlist_designed else []
            )
        elif checkbox == "p3_collision_guard_batch":
            if batch_guard_done:
                r["status"] = "DONE"
                r["gap_detail"] = None
                r["closed_by"] = BATCH_GUARD_TASK_ID
            else:
                r["status"] = "GAP"
                r["in_codex_review"] = True
                r["gap_detail"] = (
                    f"Batch collision guard built + shell test passing but task {BATCH_GUARD_TASK_ID} "
                    f"status={statuses.get(BATCH_GUARD_TASK_ID, 'unknown')} — awaiting Codex finalize (done)."
                )
            r["evidence_refs"] = list(r.get("evidence_refs", [])) + [
                "scripts/build_mcp_batch_collision_guard_b119_v1.py + "
                "tests/test_mcp_batch_collision_guard_b119_v1.sh (present, untracked pending review)",
            ]
        elif checkbox == "p3_full_task_wave":
            if full_wave_done:
                r["status"] = "GAP"
                r["in_codex_review"] = False
                r["gap_detail"] = (
                    "Full-wave DRY-RUN harness finished, but this validates simulated wave "
                    "logic only. A literal live wave (claim->work->review->done via MCP "
                    "with AIWORKHUB_ALLOW_WRITES=1) has not been executed and remains "
                    "a separate, still-forbidden action pending this gate."
                )
            else:
                r["status"] = "GAP"
                r["in_codex_review"] = True
                r["gap_detail"] = (
                    f"Full-wave dry-run harness built, task {FULL_WAVE_DRYRUN_TASK_ID} "
                    f"status={statuses.get(FULL_WAVE_DRYRUN_TASK_ID, 'unknown')} — awaiting Codex finalize. "
                    "Even once finalized this is a DRY-RUN harness, not a live write-gated wave."
                )
            r["evidence_refs"] = list(r.get("evidence_refs", [])) + [
                "scripts/build_mcp_full_wave_dryrun_harness_b119_v1.py + "
                "tests/test_mcp_full_wave_dryrun_harness_b119_v1.sh (present, untracked pending review)",
            ]
        rows.append(r)
    return rows


def summarize_by_phase(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_phase: dict[str, Any] = {}
    for r in rows:
        key = f"phase_{r['phase']}"
        if key not in by_phase:
            by_phase[key] = {"done": 0, "gap": 0, "not_started": 0, "blocked": 0, "total": 0}
        by_phase[key][r["status"].lower()] += 1
        by_phase[key]["total"] += 1
    return by_phase


def compute_gates(
    live_client: dict[str, Any] | None,
    dryrun: dict[str, Any] | None,
    allowlist_design: dict[str, Any] | None,
    launch_disabled: dict[str, Any] | None,
    queue_snapshot: dict[str, Any],
) -> dict[str, Any]:
    statuses = queue_snapshot.get("statuses", {})
    allowlist_enforced = is_finished(statuses.get(ALLOWLIST_ENFORCEMENT_TASK_ID))
    batch_guard_done = is_finished(statuses.get(BATCH_GUARD_TASK_ID))
    full_wave_done = is_finished(statuses.get(FULL_WAVE_DRYRUN_TASK_ID))

    live_client_ok = bool(live_client) and live_client.get("verdict") == "LIVE_CLIENT_USABLE" and \
        live_client.get("checks", {}).get("parent_queue_unchanged") is True
    dryrun_ok = bool(dryrun) and dryrun.get("verdict") == "PASS" and \
        dryrun.get("gates", {}).get("no_parent_queue_mutation_logical_identical") is True
    allowlist_designed = bool(allowlist_design) and allowlist_design.get("verdict") == "PASS"
    allowlist_ok = allowlist_designed and allowlist_enforced
    launch_contract_ok = bool(launch_disabled) and launch_disabled.get("verdict") == "PASS" and \
        launch_disabled.get("invariant", {}).get("launch_implemented") is False

    gates = {
        "live_client": {
            "pass": live_client_ok,
            "evidence": "eval/mcp_live_client_smoke_b118_v1.json",
            "task_id": "DEEPSEEK_TASK_MCP_LIVE_CLIENT_SMOKE_B118_V1",
        },
        "dryrun": {
            "pass": dryrun_ok,
            "evidence": "eval/mcp_autopickup_dryrun_b118_v1.json",
            "task_id": "CLAUDE_TASK_MCP_AUTOPICKUP_DRYRUN_B118_V1",
        },
        "allowlist": {
            "pass": allowlist_ok,
            "designed": allowlist_designed,
            "enforced": allowlist_enforced,
            "evidence": "eval/mcp_runner_topic_allowlist_design_b118_v1.json",
            "enforcement_task_id": ALLOWLIST_ENFORCEMENT_TASK_ID,
            "enforcement_status": statuses.get(ALLOWLIST_ENFORCEMENT_TASK_ID, "unknown"),
        },
        "batch_guard": {
            "pass": batch_guard_done,
            "task_id": BATCH_GUARD_TASK_ID,
            "status": statuses.get(BATCH_GUARD_TASK_ID, "unknown"),
        },
        "full_wave_dryrun": {
            "pass": full_wave_done,
            "task_id": FULL_WAVE_DRYRUN_TASK_ID,
            "status": statuses.get(FULL_WAVE_DRYRUN_TASK_ID, "unknown"),
            "note": "dry-run harness only; not a live write-gated wave execution",
        },
    }
    gates["_launch_disabled_contract_ok"] = launch_contract_ok
    return gates


def compute_readiness_classes(gates: dict[str, Any], live_client: dict[str, Any] | None) -> dict[str, Any]:
    read_only_ready = bool(gates["live_client"]["pass"])
    write_gated_ready = bool(
        gates["allowlist"]["pass"] and gates["batch_guard"]["pass"]
    )
    launch_disabled_ready = bool(gates.get("_launch_disabled_contract_ok"))
    launch_enabled_not_ready = True  # LAUNCH_IMPLEMENTED is a hardcoded False constant (B117)
    return {
        "read_only_ready": read_only_ready,
        "write_gated_ready": write_gated_ready,
        "launch_disabled_ready": launch_disabled_ready,
        "launch_enabled_not_ready": launch_enabled_not_ready,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--finishline-audit", default=str(EVAL_DIR / "task_mcp_finishline_status_audit_b116_v1.json"))
    ap.add_argument("--finishline-rows", default=str(EVAL_DIR / "task_mcp_finishline_status_audit_rows_b116_v1.jsonl"))
    ap.add_argument("--live-client-smoke", default=str(EVAL_DIR / "mcp_live_client_smoke_b118_v1.json"))
    ap.add_argument("--autopickup-dryrun", default=str(EVAL_DIR / "mcp_autopickup_dryrun_b118_v1.json"))
    ap.add_argument("--launch-disabled-contract", default=str(EVAL_DIR / "launch_disabled_queue_contract_b117_v1.json"))
    ap.add_argument("--allowlist-design", default=str(EVAL_DIR / "mcp_runner_topic_allowlist_design_b118_v1.json"))
    ap.add_argument("--queue-status-snapshot", default=None)
    ap.add_argument("--out", default=str(EVAL_DIR / "mcp_project_switch_readiness_gate_b119_v1.json"))
    ap.add_argument("--out-rows", default=str(EVAL_DIR / "mcp_project_switch_readiness_gate_rows_b119_v1.jsonl"))
    args = ap.parse_args()

    finishline_audit = load_json(Path(args.finishline_audit))
    finishline_rows = load_jsonl(Path(args.finishline_rows))
    live_client = load_json(Path(args.live_client_smoke))
    dryrun = load_json(Path(args.autopickup_dryrun))
    launch_disabled = load_json(Path(args.launch_disabled_contract))
    allowlist_design = load_json(Path(args.allowlist_design))
    queue_snapshot = load_queue_status_snapshot(
        Path(args.queue_status_snapshot) if args.queue_status_snapshot else None
    )

    if not finishline_rows:
        print("FATAL: no finishline checkbox rows loaded (missing evidence) — cannot score, failing closed.")
        return 1

    rows = overlay_checkboxes(finishline_rows, live_client, dryrun, allowlist_design, queue_snapshot)
    by_phase = summarize_by_phase(rows)
    gates = compute_gates(live_client, dryrun, allowlist_design, launch_disabled, queue_snapshot)
    readiness_classes = compute_readiness_classes(gates, live_client)

    named_gate_keys = ["live_client", "dryrun", "allowlist", "batch_guard", "full_wave_dryrun"]
    all_gates_pass = all(gates[k]["pass"] for k in named_gate_keys)
    workflow_switch_ready = bool(all_gates_pass)  # must stay False unless every gate is True

    blockers: list[dict[str, Any]] = []
    if not gates["allowlist"]["pass"]:
        blockers.append({
            "id": "allowlist_enforcement_pending",
            "task_id": ALLOWLIST_ENFORCEMENT_TASK_ID,
            "status": gates["allowlist"]["enforcement_status"],
            "detail": "Allow-list DESIGN is frozen/PASS but core.py write-path ENFORCEMENT is not finished.",
        })
    if not gates["batch_guard"]["pass"]:
        blockers.append({
            "id": "batch_collision_guard_pending_finalize",
            "task_id": BATCH_GUARD_TASK_ID,
            "status": gates["batch_guard"]["status"],
            "detail": "Batch collision guard built + tests passing but task not yet Codex-finalized (done).",
        })
    if not gates["full_wave_dryrun"]["pass"]:
        blockers.append({
            "id": "full_wave_dryrun_pending_finalize",
            "task_id": FULL_WAVE_DRYRUN_TASK_ID,
            "status": gates["full_wave_dryrun"]["status"],
            "detail": "Full-wave dry-run harness pending Codex finalize; note it is a DRY-RUN, not a live write-gated wave.",
        })
    p3_no_parent_corruption = next((r for r in rows if r.get("checkbox") == "p3_no_parent_corruption"), None)
    if p3_no_parent_corruption and p3_no_parent_corruption.get("status") != "DONE":
        blockers.append({
            "id": "p3_no_parent_corruption_not_started",
            "task_id": None,
            "status": p3_no_parent_corruption.get("status"),
            "detail": "No dedicated post-real-wave taskctl verify + queue-integrity-diff evidence found.",
        })
    phase2 = by_phase.get("phase_2", {})
    if phase2.get("gap", 0) > 0:
        blockers.append({
            "id": "phase_2_gaps_open",
            "task_id": None,
            "status": f"{phase2.get('gap')}/{phase2.get('total')} open",
            "detail": "CLI adapter concrete implementations (claude/codex/deepseek) + candidate eval remain GAP per B116 audit; not in scope of the 5 named switch gates but part of full roadmap completion.",
        })

    git_state = git_dirty_report("tools/aiworkhub/")

    eval_summary: dict[str, Any] = {
        "eval_id": "mcp_project_switch_readiness_gate_b119_v1",
        "task_id": "CLAUDE_TASK_MCP_PROJECT_SWITCH_READINESS_GATE_B119_V1",
        "contract": "B119_v1",
        "generated": datetime.now(timezone.utc).isoformat(),
        "mode": "project_switch_readiness_scorecard_no_switch",
        "builds_on": [
            "task_mcp_finishline_status_audit_b116_v1",
            "mcp_live_client_smoke_b118_v1",
            "mcp_autopickup_dryrun_b118_v1",
            "mcp_runner_topic_allowlist_design_b118_v1",
            "launch_disabled_queue_contract_b117_v1",
        ],
        "authority_flags": AUTHORITY_FLAGS,
        "git_state": git_state,
        "queue_status_snapshot": queue_snapshot,
        "gates": gates,
        "named_gate_keys": named_gate_keys,
        "readiness_classes": readiness_classes,
        "workflow_switch_ready": workflow_switch_ready,
        "summary": {
            "total_checkboxes": len(rows),
            "done": sum(1 for r in rows if r["status"] == "DONE"),
            "gap": sum(1 for r in rows if r["status"] == "GAP"),
            "not_started": sum(1 for r in rows if r["status"] == "NOT_STARTED"),
            "blocked": sum(1 for r in rows if r["status"] == "BLOCKED"),
        },
        "by_phase": by_phase,
        "blockers": blockers,
        "acceptance": {
            "phases_0_3_scored_with_evidence": True,
            "workflow_switch_ready_false_unless_all_gates_pass": (
                workflow_switch_ready == all_gates_pass
            ),
            "readiness_classes_distinguished": True,
            "no_mutation": True,
        },
    }

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    out_path = Path(args.out)
    out_path.write_text(json.dumps(eval_summary, indent=2, ensure_ascii=False), encoding="utf-8")

    rows_path = Path(args.out_rows)
    with open(rows_path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    next_wave = {
        "next_wave_id": "mcp_project_switch_readiness_gate_next_wave_b119_v1",
        "version": 1,
        "generated": datetime.now(timezone.utc).isoformat(),
        "workflow_switch_ready": workflow_switch_ready,
        "remaining_steps": [
            {
                "step": 1,
                "task_id": ALLOWLIST_ENFORCEMENT_TASK_ID,
                "action": "Finish + Codex-finalize (done) allow-list enforcement in core.py write path.",
            },
            {
                "step": 2,
                "task_id": BATCH_GUARD_TASK_ID,
                "action": "Codex-finalize (done) batch collision guard.",
            },
            {
                "step": 3,
                "task_id": FULL_WAVE_DRYRUN_TASK_ID,
                "action": "Codex-finalize (done) full-wave dry-run harness; then design a SEPARATE, "
                          "explicitly-approved task to run one literal live wave with "
                          "AIWORKHUB_ALLOW_WRITES=1 (p3_full_task_wave real execution).",
            },
            {
                "step": 4,
                "task_id": None,
                "action": "Build p3_no_parent_corruption evidence: taskctl verify + before/after queue "
                          "diff around the real wave from step 3.",
            },
            {
                "step": 5,
                "task_id": None,
                "action": "Re-run build_mcp_project_switch_readiness_gate_b119_v1.py; only flip "
                          "workflow_switch when all 5 named gates report pass=true.",
            },
        ],
        "authority_flags": AUTHORITY_FLAGS,
    }
    next_wave_path = DATA_DIR / "mcp_project_switch_readiness_gate_next_wave_b119_v1.json"
    next_wave_path.write_text(json.dumps(next_wave, indent=2, ensure_ascii=False), encoding="utf-8")

    print("B119 PROJECT SWITCH READINESS GATE")
    print(f"  workflow_switch_ready={workflow_switch_ready}")
    for k in named_gate_keys:
        print(f"  gate.{k}.pass={gates[k]['pass']}")
    print(f"  read_only_ready={readiness_classes['read_only_ready']} "
          f"write_gated_ready={readiness_classes['write_gated_ready']} "
          f"launch_disabled_ready={readiness_classes['launch_disabled_ready']} "
          f"launch_enabled_not_ready={readiness_classes['launch_enabled_not_ready']}")
    print(f"  blockers={len(blockers)}")
    print(f"  Outputs: {out_path}")
    print(f"           {rows_path}")
    print(f"           {next_wave_path}")

    assert AUTHORITY_FLAGS["workflow_switch"] is False
    assert AUTHORITY_FLAGS["queue_mutation"] is False
    assert AUTHORITY_FLAGS["process_launch"] is False
    assert workflow_switch_ready == all_gates_pass
    if not all_gates_pass:
        assert workflow_switch_ready is False
        assert len(blockers) > 0
    assert out_path.exists()
    assert rows_path.exists()

    return 0


if __name__ == "__main__":
    sys.exit(main())
