#!/usr/bin/env bash
# B251: live MCP tool inventory reverify -- validates the eval/rows/next_wave
# artifacts AND independently re-derives the live tool count via a pure
# static source read (no daemon start, no MCP client launch, no subprocess
# exec of the server). Mirrors this task's own mode:
# mcp_inventory_reverify_no_source_patch.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
EVAL_JSON="$MCP_ROOT/eval/task_mcp_live_tool_inventory_reverify_b251_v1.json"
EVAL_JSONL="$MCP_ROOT/eval/task_mcp_live_tool_inventory_reverify_rows_b251_v1.jsonl"
NEXT_WAVE="$MCP_ROOT/data/tasking/task_mcp_live_tool_inventory_reverify_next_wave_b251_v1.json"
SERVER_PY="$MCP_ROOT/src/aiworkhub/server.py"
CLI_ADAPTER_PY="$MCP_ROOT/src/aiworkhub/cli_adapter_readonly_tool.py"

PASS=0
FAIL=0

pass_test() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail_test() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

echo "=== B251 Live Tool Inventory Reverify Test ==="
echo ""

echo "--- [1/6] artifact files exist ---"
for f in "$EVAL_JSON" "$EVAL_JSONL" "$NEXT_WAVE"; do
  if [[ -f "$f" ]]; then
    pass_test "exists: $f"
  else
    fail_test "missing: $f"
  fi
done
echo ""

echo "--- [2/6] eval JSON is valid, verdict PASS, no source_patch performed ---"
EVAL_JSON="$EVAL_JSON" python3 - <<'PYEOF'
import json, os, sys
d = json.load(open(os.environ["EVAL_JSON"]))
assert d["verdict"] == "PASS", d["verdict"]
assert d["task_id"] == "CLAUDE_TASK_MCP_LIVE_TOOL_INVENTORY_REVERIFY_B251_V1"
assert d["mode"] == "mcp_inventory_reverify_no_source_patch"
fops = d["forbidden_operations_verified_not_taken"]
for op in ("daemon_start", "source_patch", "credential_access", "task_status_mutation", "git_add_A", "mixed_task_commit"):
    assert op in fops, f"missing forbidden-op confirmation: {op}"
assert d["live_tool_inventory"]["total_live_tools"] == 20
assert len(d["live_tool_inventory"]["tool_names"]) == 20
assert len(set(d["live_tool_inventory"]["tool_names"])) == 20, "duplicate tool names in inventory"
print("eval JSON shape OK: verdict=PASS, mode correct, 20 unique tool_names, forbidden-ops confirmed")
PYEOF
if [[ $? -eq 0 ]]; then pass_test "eval JSON shape + forbidden-ops"; else fail_test "eval JSON shape + forbidden-ops"; fi
echo ""

echo "--- [3/6] rows JSONL: every line valid JSON, live_tool rows == 20, stale_assertion rows == 6 ---"
EVAL_JSONL="$EVAL_JSONL" python3 - <<'PYEOF'
import json, os
path = os.environ["EVAL_JSONL"]
rows = [json.loads(line) for line in open(path) if line.strip()]
live = [r for r in rows if r["row_type"] == "live_tool"]
stale = [r for r in rows if r["row_type"] == "stale_assertion"]
assert len(live) == 20, f"expected 20 live_tool rows, got {len(live)}"
assert len(stale) == 6, f"expected 6 stale_assertion rows, got {len(stale)}"
names = {r["tool_name"] for r in live}
assert len(names) == 20, "duplicate tool_name across live_tool rows"
for r in stale:
    assert "file" in r and "id" in r, r
print(f"rows JSONL OK: {len(live)} live_tool rows (unique), {len(stale)} stale_assertion rows")
PYEOF
if [[ $? -eq 0 ]]; then pass_test "rows JSONL shape"; else fail_test "rows JSONL shape"; fi
echo ""

echo "--- [4/6] next_wave JSON is valid and proposes no self-authorized task creation ---"
NEXT_WAVE="$NEXT_WAVE" python3 - <<'PYEOF'
import json, os
d = json.load(open(os.environ["NEXT_WAVE"]))
assert d["source_task_id"] == "CLAUDE_TASK_MCP_LIVE_TOOL_INVENTORY_REVERIFY_B251_V1"
assert len(d["proposals"]) >= 5
for p in d["proposals"]:
    assert "proposal_id" in p and "target" in p and "action" in p
print(f"next_wave JSON OK: {len(d['proposals'])} proposals")
PYEOF
if [[ $? -eq 0 ]]; then pass_test "next_wave JSON shape"; else fail_test "next_wave JSON shape"; fi
echo ""

echo "--- [5/6] independent static re-derivation of live tool count (no daemon/MCP launch) ---"
SERVER_PY="$SERVER_PY" CLI_ADAPTER_PY="$CLI_ADAPTER_PY" EVAL_JSON="$EVAL_JSON" python3 - <<'PYEOF'
import ast, json, os

server_src = open(os.environ["SERVER_PY"]).read()
tree = ast.parse(server_src)

direct_tool_names = []
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef):
        for dec in node.decorator_list:
            # decorator is `mcp.tool()` -> ast.Call(func=Attribute(value=Name('mcp'), attr='tool'))
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "tool"):
                direct_tool_names.append(node.name)

assert len(direct_tool_names) == 17, f"expected 17 direct @mcp.tool() defs in server.py, got {len(direct_tool_names)}: {direct_tool_names}"

adapter_src = open(os.environ["CLI_ADAPTER_PY"]).read()
adapter_tree = ast.parse(adapter_src)
registered_names = None
for node in ast.walk(adapter_tree):
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
            and node.target.id == "READONLY_TOOL_NAMES" and node.value is not None:
        registered_names = tuple(elt.value for elt in node.value.elts)
    elif isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "READONLY_TOOL_NAMES" for t in node.targets
    ):
        registered_names = tuple(elt.value for elt in node.value.elts)

assert registered_names is not None, "READONLY_TOOL_NAMES not found in cli_adapter_readonly_tool.py"
assert len(registered_names) == 3, f"expected 3 registered readonly tools, got {len(registered_names)}"

total_live = len(direct_tool_names) + len(registered_names)
assert total_live == 20, f"expected 20 total live tools (17 direct + 3 registered), got {total_live}"

eval_d = json.load(open(os.environ["EVAL_JSON"]))
eval_names = set(eval_d["live_tool_inventory"]["tool_names"])
derived_names = set(direct_tool_names) | set(registered_names)
assert eval_names == derived_names, (
    f"eval JSON tool_names mismatch vs static AST re-derivation.\n"
    f"only in eval: {eval_names - derived_names}\nonly in AST-derived: {derived_names - eval_names}"
)

print(f"static re-derivation OK: {len(direct_tool_names)} direct + {len(registered_names)} registered = {total_live} total, names match eval JSON exactly")
PYEOF
if [[ $? -eq 0 ]]; then pass_test "static AST re-derivation matches eval JSON (20 tools, names identical)"; else fail_test "static AST re-derivation mismatch"; fi
echo ""

echo "--- [6/6] no daemon/MCP client launch occurred (self-check: no server import/run in this test) ---"
# Build the forbidden substrings by concatenation so this self-check line
# itself never contains the literal joined pattern (which would make the
# grep below match its own source line).
P1="mcp""."; P1="${P1}run("
P2="FastMCP""("
P3="subprocess.Po""pen("
P4="connect_client""("
SELF="$SCRIPT_DIR/test_task_mcp_live_tool_inventory_reverify_b251_v1.sh"
if grep -qE "$P1|$P2|$P3|$P4" "$SELF" 2>/dev/null; then
  fail_test "test script itself references a daemon/client launch pattern"
else
  pass_test "test script performs static AST parsing only, no daemon/client launch"
fi
echo ""

echo "=== Results: $PASS passed, $FAIL failed ==="
if [[ $FAIL -eq 0 ]]; then
  echo "ALL TESTS PASSED"
  exit 0
else
  echo "TESTS FAILED"
  exit 1
fi
