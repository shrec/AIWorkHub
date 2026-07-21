#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_agent_launcher_design_v1.sh
# Validates the CLAUDE_TASK_MCP_AGENT_LAUNCHER_DESIGN_V1 design packet.
#
# Design-only guarantees enforced here:
#   - the 3 design artifacts exist and parse;
#   - launch is DISABLED (launch_actions_enabled == false) and NO launcher
#     code was added (launcher_code_added == false, verdict == PASS);
#   - phases are all enables_launch==false except the explicitly-gated
#     enablement phase, which must reference the safety gates;
#   - exactly 3 separated adapters {claude_cli, codex_cli,
#     manual_copilot_deepseek}; manual is kind manual_external;
#   - every safety gate is blocking==true;
#   - the live MCP server registers NO launch/spawn/exec/process tool and
#     server.py wires NO AIWORKHUB_ALLOW_LAUNCH handling.
#
# Isolation-safe / parallel-safe: reads only committed artifacts + imports the
# module read-only; any scratch goes under a per-run mktemp -d; mutates no
# shared on-disk state. Prints a final PASS and exits 0 on success.
# ---------------------------------------------------------------------------

MCPROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$MCPROOT/../.." && pwd)"

export PYTHONPATH="$MCPROOT/src"
export AIWORKHUB_REPO="$ROOT"
# Never enable writes or launch inside the test.
export AIWORKHUB_ALLOW_WRITES=0

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

EVAL_JSON="$MCPROOT/eval/agent_launcher_design_v1.json"
ROWS_JSONL="$MCPROOT/eval/agent_launcher_design_rows_v1.jsonl"
NEXT_WAVE="$MCPROOT/data/tasking/agent_launcher_design_next_wave_v1.json"
SERVER_PY="$MCPROOT/src/aiworkhub/server.py"

echo "=== Agent Launcher Design v1 — validation ==="
echo "MCPROOT=$MCPROOT"

# ------------------------------------------------------------------
# 1. server.py must not wire ALLOW_LAUNCH (raw source grep, no false pass)
# ------------------------------------------------------------------
if grep -q "ALLOW_LAUNCH" "$SERVER_PY"; then
    echo "FAIL: AIWORKHUB_ALLOW_LAUNCH is wired in server.py (must be design-only)"
    exit 1
fi
echo "server.py: no ALLOW_LAUNCH wiring (ok)"

# ------------------------------------------------------------------
# 2. Python validation of artifacts + live server tool registry
# ------------------------------------------------------------------
python3 - "$EVAL_JSON" "$ROWS_JSONL" "$NEXT_WAVE" > "$SCRATCH/result.txt" <<'PY'
import json
import sys

eval_json, rows_jsonl, next_wave = sys.argv[1], sys.argv[2], sys.argv[3]

def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)

# --- load & parse all three artifacts ---
with open(eval_json, encoding="utf-8") as fh:
    design = json.load(fh)
with open(next_wave, encoding="utf-8") as fh:
    nw = json.load(fh)
rows = []
with open(rows_jsonl, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
if not rows:
    fail("rows jsonl empty")

# --- invariant + verdict ---
inv = design.get("invariant", {})
if inv.get("launch_actions_enabled") is not False:
    fail("invariant.launch_actions_enabled must be false")
if inv.get("launcher_code_added") is not False:
    fail("invariant.launcher_code_added must be false")
if inv.get("proposed_enable_flag") != "AIWORKHUB_ALLOW_LAUNCH":
    fail("invariant.proposed_enable_flag mismatch")
if inv.get("default") != "0":
    fail("invariant.default must be '0'")
if "AIWORKHUB_ALLOW_WRITES" not in str(inv.get("requires_also", "")):
    fail("invariant.requires_also must require ALLOW_WRITES")
if design.get("verdict") != "PASS":
    fail("verdict must be PASS")

# --- phases ---
phases = design.get("phases", [])
if not phases:
    fail("phases must be non-empty")
gate_ids = {g["gate_id"] for g in design.get("safety_gates", [])}
enablement_phases = 0
for p in phases:
    for k in ("phase_id", "title", "deliverable", "enables_launch", "exit_gate"):
        if k not in p:
            fail(f"phase {p.get('phase_id')} missing {k}")
    if p["enables_launch"] is True:
        enablement_phases += 1
        gb = p.get("gated_by", [])
        if not gb:
            fail(f"enablement phase {p['phase_id']} must have non-empty gated_by")
        if not set(gb).issubset(gate_ids):
            fail(f"enablement phase {p['phase_id']} gated_by references unknown gate ids")
    elif p["enables_launch"] is not False:
        fail(f"phase {p['phase_id']} enables_launch must be bool")
# at least: adapter spec, dry-run, launch-queue/state, gated enablement
if len(phases) < 4:
    fail("expected at least 4 launcher MVP phases")

# --- adapters ---
adapters = design.get("adapters", [])
ids = [a.get("id") for a in adapters]
if len(adapters) != 3:
    fail("adapters must have exactly 3 entries")
if len(set(ids)) != 3:
    fail("adapter ids must be distinct")
if set(ids) != {"claude_cli", "codex_cli", "manual_copilot_deepseek"}:
    fail(f"adapter ids mismatch: {ids}")
by_id = {a["id"]: a for a in adapters}
if by_id["claude_cli"]["kind"] != "local_cli":
    fail("claude_cli.kind must be local_cli")
if by_id["codex_cli"]["kind"] != "local_cli":
    fail("codex_cli.kind must be local_cli")
if by_id["manual_copilot_deepseek"]["kind"] != "manual_external":
    fail("manual_copilot_deepseek.kind must be manual_external")
if by_id["manual_copilot_deepseek"].get("human_in_the_loop") is not True:
    fail("manual_copilot_deepseek must be human_in_the_loop")
for a in adapters:
    if a.get("separate_model") is not True:
        fail(f"adapter {a['id']} must be separate_model")
    if a.get("disabled_by_default") is not True:
        fail(f"adapter {a['id']} must be disabled_by_default")

# --- safety gates ---
sgs = design.get("safety_gates", [])
if not sgs:
    fail("safety_gates must be non-empty")
required_gates = {
    "write_gate_parity", "runner_topic_allowlist", "audit_log",
    "collision_guard_preflight", "dry_run_default",
    "cwd_process_sandbox_pin", "concurrency_cap",
}
if not required_gates.issubset(gate_ids):
    fail(f"missing required safety gates: {required_gates - gate_ids}")
for g in sgs:
    if g.get("blocking") is not True:
        fail(f"gate {g.get('gate_id')} must be blocking==true")
    if not g.get("requirement"):
        fail(f"gate {g.get('gate_id')} must have a requirement")

# --- neural bridge note (doctrine: learned routing, no cue router) ---
nbn = design.get("neural_bridge_note", "")
if not isinstance(nbn, str) or not nbn:
    fail("neural_bridge_note must be a non-empty string")

# --- acceptance ---
acc = design.get("acceptance", {})
for k in ("phases_defined", "three_adapters_separated", "safety_gates_defined", "no_launcher_code"):
    if acc.get(k) != "pass":
        fail(f"acceptance.{k} must be 'pass'")

# --- rows cover phases + adapters + gates ---
kinds = {}
for r in rows:
    kinds.setdefault(r.get("kind"), 0)
    kinds[r["kind"]] += 1
if kinds.get("phase", 0) != len(phases):
    fail("rows: one phase row per phase expected")
if kinds.get("adapter", 0) != len(adapters):
    fail("rows: one adapter row per adapter expected")
if kinds.get("gate", 0) != len(sgs):
    fail("rows: one gate row per safety gate expected")

# --- next-wave is a proposal (not enqueued) with proper task cards ---
if nw.get("status") != "proposal":
    fail("next_wave.status must be 'proposal'")
fu = nw.get("follow_up_tasks", [])
if not fu:
    fail("next_wave.follow_up_tasks must be non-empty")
for t in fu:
    for k in ("task_id", "goal", "mode", "allowed_writes", "acceptance"):
        if k not in t:
            fail(f"follow_up task {t.get('task_id')} missing {k}")
    if not isinstance(t["allowed_writes"], list) or not t["allowed_writes"]:
        fail(f"follow_up {t['task_id']} allowed_writes must be non-empty list")
    if not isinstance(t["acceptance"], list) or not t["acceptance"]:
        fail(f"follow_up {t['task_id']} acceptance must be non-empty list")

# --- LIVE server registry: NO launch/spawn/exec/process tool ---
import aiworkhub.server as srv
tm = getattr(srv.mcp, "_tool_manager", None)
if tm is None:
    fail("could not access FastMCP tool manager")
tool_names = list(getattr(tm, "_tools", {}).keys())
if not tool_names:
    # fall back to list_tools()
    tool_names = [t.name for t in tm.list_tools()]
banned = ("launch", "spawn", "exec", "process")
for name in tool_names:
    low = name.lower()
    for b in banned:
        if b in low:
            fail(f"server registers a forbidden tool '{name}' (contains '{b}')")

print("OK phases=%d adapters=%d gates=%d rows=%d tools=%d enablement_phases=%d"
      % (len(phases), len(adapters), len(sgs), len(rows), len(tool_names), enablement_phases))
PY

cat "$SCRATCH/result.txt"
if ! grep -q '^OK ' "$SCRATCH/result.txt"; then
    echo "FAIL: python validation did not report OK"
    exit 1
fi

echo ""
echo "PASS"
exit 0
