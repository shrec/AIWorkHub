#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_phase2_orchestration_contract_b104_v1.sh
# Validates the CLAUDE_TASK_MCP_PHASE2_ORCHESTRATION_CONTRACT_B104_V1 packet.
#
# Contract-only guarantees enforced here:
#   - the 3 artifacts (contract json, rows jsonl, next-wave json) parse;
#   - command_allowlist is present and non-empty;
#   - all 3 adapter boundaries present {claude_cli, codex_cli, deepseek_manual};
#   - token/cost reporting fields present (incl input/output tokens + cost);
#   - launch_enabled_by_default == false (disabled-by-default preserved);
#   - launch_preconditions is non-empty;
#   - rows: exactly one row per adapter + one per precondition;
#   - next-wave is a proposal with proper follow-up task cards;
#   - NO process-launch code anywhere in the 3 data artifacts (raw grep).
#
# Isolation-safe / parallel-safe: reads only the committed artifacts; any
# scratch goes under a per-run mktemp -d; mutates no shared on-disk state.
# Prints a final PASS and exits 0 on success.
# ---------------------------------------------------------------------------

MCPROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$MCPROOT/../.." && pwd)"

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

CONTRACT_JSON="$MCPROOT/eval/phase2_orchestration_contract_b104_v1.json"
ROWS_JSONL="$MCPROOT/eval/phase2_orchestration_contract_rows_b104_v1.jsonl"
NEXT_WAVE="$MCPROOT/data/tasking/phase2_orchestration_contract_next_wave_b104_v1.json"

echo "=== Phase-2 Orchestration Contract b104 v1 - validation ==="
echo "MCPROOT=$MCPROOT"

# ------------------------------------------------------------------
# 1. NO process-launch code anywhere in the 3 data artifacts.
#    Raw grep so a false pass is impossible. We scan only the data
#    artifacts, never this test (which necessarily names the tokens).
# ------------------------------------------------------------------
BANNED=(
  "subprocess"
  "Popen"
  "os.exec"
  "os.fork"
  "os.spawn"
  "pty.spawn"
  "shell=True"
  "os.system"
  "check_output"
  "check_call"
  "multiprocessing.Process"
)
for f in "$CONTRACT_JSON" "$ROWS_JSONL" "$NEXT_WAVE"; do
  for tok in "${BANNED[@]}"; do
    if grep -F -q "$tok" "$f"; then
      echo "FAIL: process-launch code token '$tok' found in $f (contract must add no launch code)"
      exit 1
    fi
  done
done
echo "no process-launch code tokens in data artifacts (ok)"

# ------------------------------------------------------------------
# 2. Structural validation of the artifacts.
# ------------------------------------------------------------------
python3 - "$CONTRACT_JSON" "$ROWS_JSONL" "$NEXT_WAVE" > "$SCRATCH/result.txt" <<'PY'
import json
import sys

contract_json, rows_jsonl, next_wave = sys.argv[1], sys.argv[2], sys.argv[3]

def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)

with open(contract_json, encoding="utf-8") as fh:
    c = json.load(fh)
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

# --- command allowlist present + non-empty ---
allow = c.get("command_allowlist")
if not isinstance(allow, list) or not allow:
    fail("command_allowlist must be a non-empty list")

# --- disabled-by-default preserved ---
if c.get("launch_enabled_by_default") is not False:
    fail("launch_enabled_by_default must be false")
inv = c.get("invariant", {})
if inv.get("launch_code_added") is not False:
    fail("invariant.launch_code_added must be false")
if inv.get("write_gate_disabled") is not False:
    fail("invariant.write_gate_disabled must be false")
if inv.get("proposed_enable_flag") != "AIWORKHUB_ALLOW_LAUNCH":
    fail("invariant.proposed_enable_flag mismatch")
if inv.get("default") != "0":
    fail("invariant.default must be '0'")
if "AIWORKHUB_ALLOW_WRITES" not in str(inv.get("requires_also", "")):
    fail("invariant.requires_also must require ALLOW_WRITES")
if c.get("verdict") != "PASS":
    fail("verdict must be PASS")

# --- all 3 adapter boundaries present ---
adapters = c.get("adapters", {})
required_adapters = {"claude_cli", "codex_cli", "deepseek_manual"}
if set(adapters.keys()) != required_adapters:
    fail("adapters must be exactly {claude_cli, codex_cli, deepseek_manual}, got %s" % sorted(adapters.keys()))
for aid, a in adapters.items():
    if not a.get("boundary"):
        fail("adapter %s must have a boundary" % aid)
    if a.get("disabled_by_default") is not True:
        fail("adapter %s must be disabled_by_default" % aid)
    if a.get("separate_model") is not True:
        fail("adapter %s must be separate_model" % aid)
if adapters["claude_cli"]["kind"] != "local_cli":
    fail("claude_cli.kind must be local_cli")
if adapters["codex_cli"]["kind"] != "local_cli":
    fail("codex_cli.kind must be local_cli")
if adapters["deepseek_manual"]["kind"] != "manual_external":
    fail("deepseek_manual.kind must be manual_external")
if adapters["deepseek_manual"].get("human_in_the_loop") is not True:
    fail("deepseek_manual must be human_in_the_loop")
if adapters["codex_cli"].get("review_role") != "reviewer_finalizer":
    fail("codex_cli must be the reviewer_finalizer")

# --- token / cost reporting fields present ---
tcf = c.get("token_cost_reporting_fields")
if not isinstance(tcf, list) or not tcf:
    fail("token_cost_reporting_fields must be a non-empty list")
for req in ("task_id", "input_tokens", "output_tokens", "cost_usd"):
    if req not in tcf:
        fail("token_cost_reporting_fields missing %s" % req)
pol = c.get("token_cost_reporting_policy", {})
if pol.get("per_task") is not True:
    fail("token_cost_reporting_policy.per_task must be true")

# --- sandbox + logging present ---
sb = c.get("sandbox", {})
for k in ("cwd_pin", "argv_list_only", "no_shell", "env_scrub", "concurrency_cap_default"):
    if k not in sb:
        fail("sandbox missing %s" % k)
if sb.get("no_shell") is not True or sb.get("argv_list_only") is not True:
    fail("sandbox must be argv_list_only + no_shell")
lg = c.get("logging", {})
for k in ("audit_log_path_default", "append_only", "never_logs_secret_values", "per_launch_fields"):
    if k not in lg:
        fail("logging missing %s" % k)
if lg.get("never_logs_secret_values") is not True:
    fail("logging.never_logs_secret_values must be true")

# --- launch preconditions non-empty ---
lp = c.get("launch_preconditions")
if not isinstance(lp, list) or not lp:
    fail("launch_preconditions must be a non-empty list")
pre_ids = []
for p in lp:
    if not isinstance(p, dict) or "id" not in p or "requirement" not in p:
        fail("each launch_precondition needs id + requirement")
    pre_ids.append(p["id"])
for must in ("allow_launch_flag", "allow_writes_flag", "dry_run_default", "cwd_env_sandbox_pin"):
    if must not in pre_ids:
        fail("launch_preconditions missing %s" % must)

# --- codex review handoff ---
crh = c.get("codex_review_handoff", {})
if crh.get("finalizer") != "codex":
    fail("codex_review_handoff.finalizer must be codex")
if "review" not in str(crh.get("command", "")):
    fail("codex_review_handoff.command must invoke taskctl review")
if not crh.get("separation_of_duties"):
    fail("codex_review_handoff must state separation_of_duties")

# --- neural bridge note (learned routing, no cue router) ---
if not isinstance(c.get("neural_bridge_note"), str) or not c.get("neural_bridge_note"):
    fail("neural_bridge_note must be a non-empty string")

# --- acceptance all pass ---
acc = c.get("acceptance", {})
for k in ("launch_preconditions_defined_no_launch_code", "three_adapter_boundaries",
          "token_cost_reporting_fields", "disabled_by_default_preserved",
          "codex_review_handoff_defined"):
    if acc.get(k) != "pass":
        fail("acceptance.%s must be 'pass'" % k)

# --- rows: one per adapter + one per precondition ---
n_adapter = sum(1 for r in rows if r.get("kind") == "adapter")
n_pre = sum(1 for r in rows if r.get("kind") == "precondition")
if n_adapter != len(adapters):
    fail("rows: expected one adapter row per adapter (%d), got %d" % (len(adapters), n_adapter))
if n_pre != len(lp):
    fail("rows: expected one precondition row per precondition (%d), got %d" % (len(lp), n_pre))
row_adapter_ids = {r["id"] for r in rows if r.get("kind") == "adapter"}
if row_adapter_ids != required_adapters:
    fail("adapter rows must cover exactly the 3 adapters")
row_pre_ids = {r["id"] for r in rows if r.get("kind") == "precondition"}
if row_pre_ids != set(pre_ids):
    fail("precondition rows must cover exactly the contract preconditions")

# --- next-wave proposal (not enqueued) with proper follow-up cards ---
if nw.get("status") != "proposal":
    fail("next_wave.status must be 'proposal'")
fu = nw.get("follow_up_tasks", [])
if not fu:
    fail("next_wave.follow_up_tasks must be non-empty")
saw_gated_final = False
for t in fu:
    for k in ("task_id", "goal", "mode", "allowed_writes", "acceptance"):
        if k not in t:
            fail("follow_up %s missing %s" % (t.get("task_id"), k))
    if not isinstance(t["allowed_writes"], list) or not t["allowed_writes"]:
        fail("follow_up %s allowed_writes must be a non-empty list" % t["task_id"])
    if not isinstance(t["acceptance"], list) or not t["acceptance"]:
        fail("follow_up %s acceptance must be a non-empty list" % t["task_id"])
    if "GATED_ENABLEMENT" in t["task_id"]:
        saw_gated_final = True
if not saw_gated_final:
    fail("next_wave must include a final gated-enablement follow-up task")

print("OK allowlist=%d adapters=%d preconditions=%d rows=%d token_fields=%d follow_ups=%d"
      % (len(allow), len(adapters), len(lp), len(rows), len(tcf), len(fu)))
PY

cat "$SCRATCH/result.txt"
if ! grep -q '^OK ' "$SCRATCH/result.txt"; then
    echo "FAIL: python validation did not report OK"
    exit 1
fi

echo ""
echo "PASS"
exit 0
