#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_claude_runner_orchestration_review_b251_v1.sh
# Validates the B251 Claude-runner orchestration review artifacts: the eval
# JSON, the next-wave JSON, and the disabled-by-default dispatch-chain
# invariants (server.py + launch_queue_contract.py + launch_queue_persist.py
# + cli_adapter_dryrun.py + cli_adapter_readonly_tool.py) the review depends
# on. This is a REVIEW/DESIGN task: no process launch, no server behavior
# change, no write-gate toggle. Read-only against the repo tree.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
SERVER_SRC="$MCPROOT/src/aiworkhub/server.py"
LAUNCH_CONTRACT_SRC="$MCPROOT/src/aiworkhub/launch_queue_contract.py"
LAUNCH_PERSIST_SRC="$MCPROOT/src/aiworkhub/launch_queue_persist.py"
DRYRUN_SRC="$MCPROOT/src/aiworkhub/cli_adapter_dryrun.py"
READONLY_TOOL_SRC="$MCPROOT/src/aiworkhub/cli_adapter_readonly_tool.py"

EVAL_JSON="$MCPROOT/eval/mcp_claude_runner_orchestration_review_b251_v1.json"
NEXT_WAVE_JSON="$MCPROOT/data/tasking/mcp_claude_runner_orchestration_review_next_wave_b251_v1.json"

echo "=== MCP Claude-Runner Orchestration Review Test B251 v1 ==="

# --- 0. all reviewed source files exist -------------------------------------
for f in "$SERVER_SRC" "$LAUNCH_CONTRACT_SRC" "$LAUNCH_PERSIST_SRC" "$DRYRUN_SRC" "$READONLY_TOOL_SRC"; do
    if [ ! -f "$f" ]; then
        echo "FAIL: expected source file missing: $f"
        exit 1
    fi
done
echo "reviewed source files present: OK"

# --- 1. no process-launch CODE pattern anywhere in the dispatch-chain
# sources (docstrings are allowed to DESCRIBE the invariant in prose --
# e.g. "no subprocess module" -- so patterns below target actual code
# shapes: an import statement or an attribute/call, never a bare noun).
for f in "$SERVER_SRC" "$LAUNCH_CONTRACT_SRC" "$LAUNCH_PERSIST_SRC" "$DRYRUN_SRC" "$READONLY_TOOL_SRC"; do
    for pat in "import subprocess" "subprocess." "os.system(" "os.popen(" "os.exec" "os.fork(" "os.spawn" "Popen(" "shell=True" "pty.spawn"; do
        if grep -Fq -- "$pat" "$f"; then
            echo "FAIL: forbidden launch pattern '$pat' found in $f"
            exit 1
        fi
    done
done
echo "no launch/exec/shell code in any reviewed dispatch-chain source: OK"

# --- 2. hard-coded disabled constants still present in launch_queue_contract -
for pat in "LAUNCH_IMPLEMENTED: bool = False" "WORKFLOW_SWITCH_ENABLED: bool = False" "PARENT_QUEUE_MUTATION_ENABLED: bool = False"; do
    if ! grep -Fq -- "$pat" "$LAUNCH_CONTRACT_SRC"; then
        echo "FAIL: launch_queue_contract.py missing disabled constant '$pat'"
        exit 1
    fi
done
echo "launch_queue_contract.py disabled constants intact: OK"

if ! grep -Fq -- "READONLY: bool = True" "$READONLY_TOOL_SRC"; then
    echo "FAIL: cli_adapter_readonly_tool.py missing READONLY=True constant"
    exit 1
fi
echo "cli_adapter_readonly_tool.py READONLY constant intact: OK"

# --- 3. launch_queue_contract/persist are NOT yet wired into server.py -----
# (documents the current gap this review identifies; this assertion should be
# UPDATED, not silently loosened, the day a future task wires them.)
if grep -Fq -- "launch_queue_contract" "$SERVER_SRC"; then
    echo "FAIL: server.py now imports launch_queue_contract -- review artifact is stale, update B251 eval + this test together"
    exit 1
fi
if grep -Fq -- "launch_queue_persist" "$SERVER_SRC"; then
    echo "FAIL: server.py now imports launch_queue_persist -- review artifact is stale, update B251 eval + this test together"
    exit 1
fi
echo "launch_queue_contract/persist confirmed unwired to server.py (matches review finding): OK"

# --- 4. eval JSON structural + invariant checks -----------------------------
EVAL_JSON="$EVAL_JSON" NEXT_WAVE_JSON="$NEXT_WAVE_JSON" python3 - <<'PYEOF'
import json, os

eval_p = os.environ["EVAL_JSON"]
nw_p = os.environ["NEXT_WAVE_JSON"]

d = json.load(open(eval_p, encoding="utf-8"))

assert d["task_id"] == "CLAUDE_TASK_MCP_CLAUDE_RUNNER_ORCHESTRATION_REVIEW_B251_V1", d["task_id"]
assert d["runner"] == "claude_task_mcp_claude_runner_orchestration_review_b251", d["runner"]
assert d["topic"] == "task_mcp", d["topic"]
assert d["verdict"] == "PASS", d["verdict"]
assert d["mode"] == "mcp_orchestration_review_no_process_launch", d["mode"]
assert d["commit_contract"] == "NO_COMMIT", d["commit_contract"]

af = d["authority_flags"]
for k in ("process_launch_authority", "write_gate_enabled", "workflow_switch", "runtime_authority"):
    assert af[k] is False, f"authority flag {k} not false"

c7 = d["context7_usage"]
assert c7["context7_used"] is False, c7
assert c7["local_docs_sufficient"] is True, c7

inv = d["current_capability_inventory"]
assert inv["total_live_tools"] == 20, inv["total_live_tools"]
assert inv["read_only_tools"] == 16, inv["read_only_tools"]
assert inv["write_gated_tools"] == 4, inv["write_gated_tools"]
assert len(inv["write_gated_tool_names"]) == 4, inv["write_gated_tool_names"]
assert len(inv["read_only_tool_names"]) == 16, inv["read_only_tool_names"]
assert len(set(inv["write_gated_tool_names"]) & set(inv["read_only_tool_names"])) == 0

vi = d["verified_invariants"]
for k in (
    "server_py_no_launch_code",
    "launch_queue_contract_launch_implemented_false",
    "launch_queue_contract_not_imported_by_server",
    "cli_adapter_dryrun_launch_implemented_false",
    "cli_adapter_readonly_tool_readonly_true",
):
    assert vi[k] is True, f"verified invariant {k} not true"

chain = d["existing_dispatch_design_chain"]["layers"]
assert len(chain) == 5, len(chain)
wired = {layer["layer"]: layer["wired_to_mcp"] for layer in chain}
assert wired["B106"] is True, wired
assert wired["B119"] is False, wired

sa = d["sufficiency_assessment"]
assert len(sa["sufficient_today_for"]) >= 5
assert len(sa["missing_before_any_process_launch_can_be_enabled"]) >= 2

nw = json.load(open(nw_p, encoding="utf-8"))
assert nw["parent_task_id"] == d["task_id"]
assert nw["status"] == "completed"
assert nw["verdict"] == "PASS"
cards = nw["next_wave_tasks"]
assert len(cards) == 3, len(cards)
task_ids = [c["task_id"] for c in cards]
assert len(task_ids) == len(set(task_ids)), "duplicate next-wave task_ids"
for c in cards:
    assert c["topic"] == "task_mcp", c
    assert d["task_id"] in c["depends_on"], c
gated = next(c for c in cards if c["task_id"] == "CLAUDE_TASK_MCP_ORCH_GATED_ENABLEMENT_DESIGN_B253_V1")
assert gated["ready"] is False, "gated-enablement design card must NOT be auto-ready"
assert "blocked_reason" in gated

print("eval + next_wave JSON structural and invariant checks: PASS")
print(f"  total_live_tools={inv['total_live_tools']} read_only={inv['read_only_tools']} write_gated={inv['write_gated_tools']}")
print(f"  next_wave cards={len(cards)} gated_design_ready={gated['ready']}")
PYEOF

# --- 5. parent task queue intact --------------------------------------------
echo ""
echo "=== Parent task queue integrity ==="
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
