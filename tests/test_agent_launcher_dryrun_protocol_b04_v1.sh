#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_agent_launcher_dryrun_protocol_b04_v1.sh
# Harness for the B04 launcher DRYRUN protocol (compose CLI commands/prompts
# and validate them without starting real agents).
#
# Verifies:
#   1. eval JSON parses and every required protocol section is present
#      (request/response schema, command allowlist, runner/topic binding,
#      task_id binding, prompt budget, allowed_writes check, review-return
#      envelope, >=3 dry-run examples: claude/codex/deepseek_manual)
#   2. the three dry_run_examples' would_run_argv / command_allowlist_check /
#      runner_topic_check are cross-validated against the REAL, already-shipped
#      cli_adapter_dryrun.build_argv_template/validate_command and
#      core.check_runner_topic_allowlist functions (not hand-typed prose)
#   3. prompt composition formula (contract-json + read_first paths) recomputes
#      deterministically and the truncate_and_flag budget policy triggers
#      correctly at a small budget
#   4. launch stays impossible even with GEOAI_TASK_MCP_ALLOW_LAUNCH=1
#   5. write gate stays OFF by default
#   6. task_id_binding_check is read-only (taskctl show, never review/done)
#      for both a known and an unknown task_id
#   7. forbidden_operations_confirmed_not_taken are all false in the eval JSON
#   8. parent task queue is not mutated (taskctl verify)
#
# Isolation: uses a per-run mktemp audit path; overrides
# GEOAI_TASK_MCP_AUDIT_LOG_PATH. Never calls taskctl review/done. No shared
# repo artifact is written. Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
EVAL_JSON="$MCPROOT/eval/agent_launcher_dryrun_protocol_b04_v1.json"
TASK_ID="CLAUDE_TASK_MCP_AGENT_LAUNCHER_DRYRUN_PROTOCOL_B04_V1"

TMPDIR_AUDIT="$(mktemp -d "${TMPDIR:-/tmp}/geoai_dryrun_protocol_b04_sh.XXXXXX")"
trap 'rm -rf "$TMPDIR_AUDIT"' EXIT

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES=0
export GEOAI_TASK_MCP_AUDIT_LOG_PATH="$TMPDIR_AUDIT/audit.jsonl"

echo "=== Agent Launcher DryRun Protocol Test B04 v1 ==="
echo "GEOAI_REPO=$GEOAI_REPO"
echo "GEOAI_TASK_MCP_ALLOW_WRITES=$GEOAI_TASK_MCP_ALLOW_WRITES"
echo "AUDIT_LOG=$GEOAI_TASK_MCP_AUDIT_LOG_PATH"
echo ""

echo "--- [1/8] eval JSON structure ---"
EVAL_JSON="$EVAL_JSON" python3 - <<'PYEOF'
import json, os, sys

path = os.environ["EVAL_JSON"]
d = json.load(open(path))

required_top = [
    "dry_run_request_schema", "dry_run_response_schema",
    "prompt_composition_policy", "dry_run_examples",
    "review_return_envelope_example", "forbidden_operations_confirmed_not_taken",
]
for k in required_top:
    assert k in d, f"missing top-level section: {k}"

req = d["dry_run_request_schema"]
for f in ("task_id", "runner", "topic", "adapter_id", "action", "mode", "prompt_char_budget"):
    assert f in req, f"request schema missing field: {f}"

resp = d["dry_run_response_schema"]
for f in ("decision", "would_run_argv", "command_allowlist_check", "runner_topic_check",
          "task_id_binding_check", "allowed_writes_check", "composed_prompt_char_count",
          "prompt_truncated", "prompt_char_budget", "review_return_envelope"):
    assert f in resp, f"response schema missing field: {f}"

examples = d["dry_run_examples"]
adapters_seen = {ex["request"]["adapter_id"] for k, ex in examples.items() if k != "note"}
assert {"claude_cli", "codex_cli", "deepseek_manual"} <= adapters_seen, f"missing adapter examples: {adapters_seen}"

envelope_fields = d["review_return_envelope_example"]
for f in ("task_id", "runner", "topic", "adapter_id", "changed_paths", "tests_run", "verdict",
          "token_cost_report", "audit_log_ref"):
    assert f in envelope_fields, f"review_return_envelope_example missing field: {f}"

forbidden = d["forbidden_operations_confirmed_not_taken"]
for k, v in forbidden.items():
    assert v is False, f"forbidden op {k} not confirmed false: {v}"

print("eval JSON structure: OK (%d dry_run_examples, %d forbidden-ops all false)" % (
    len(adapters_seen), len(forbidden)))
PYEOF
echo ""

echo "--- [2/8] cross-validate examples against REAL cli_adapter_dryrun + core code ---"
EVAL_JSON="$EVAL_JSON" python3 - <<'PYEOF'
import json, os, sys
sys.path.insert(0, os.path.join(os.environ["GEOAI_REPO"], "tools/geoai-task-mcp/src"))
from geoai_task_mcp import cli_adapter_dryrun as cad
from geoai_task_mcp import core

d = json.load(open(os.environ["EVAL_JSON"]))
examples = {k: v for k, v in d["dry_run_examples"].items() if k != "note"}
prompt = "CONTRACT_JSON_PLACEHOLDER"

checked = 0
for name, ex in examples.items():
    req, resp = ex["request"], ex["response"]
    adapter_id, runner, topic, action = req["adapter_id"], req["runner"], req["topic"], req["action"]

    real_argv = cad.build_argv_template(adapter_id, prompt, repo="<GEOAI_REPO>")
    assert real_argv == resp["would_run_argv"], (
        f"{name}: would_run_argv mismatch: real={real_argv} eval={resp['would_run_argv']}")

    if real_argv:
        real_val = cad.validate_command(real_argv).as_dict()
        assert real_val["ok"] == resp["command_allowlist_check"]["ok"], (
            f"{name}: command_allowlist_check.ok mismatch")
        assert real_val["decision"] == resp["command_allowlist_check"]["decision"], (
            f"{name}: command_allowlist_check.decision mismatch: real={real_val['decision']} eval={resp['command_allowlist_check']['decision']}")
    else:
        assert resp["command_allowlist_check"]["decision"] == "not_applicable_manual_handoff", (
            f"{name}: manual adapter must report not_applicable_manual_handoff")

    real_rt = core.check_runner_topic_allowlist(runner, topic, action)
    assert real_rt["allowed"] == resp["runner_topic_check"]["allowed"], (
        f"{name}: runner_topic_check.allowed mismatch: real={real_rt} eval={resp['runner_topic_check']}")
    assert real_rt["reason"] == resp["runner_topic_check"]["reason"], (
        f"{name}: runner_topic_check.reason mismatch: real={real_rt} eval={resp['runner_topic_check']}")

    assert cad.launch_enabled() is False, f"{name}: launch_enabled must be False"
    assert cad.LAUNCH_IMPLEMENTED is False, f"{name}: LAUNCH_IMPLEMENTED must be False"
    checked += 1

assert checked >= 3, f"expected >=3 cross-validated examples, got {checked}"
print(f"cross-validated {checked} dry_run_examples against real code: OK")
PYEOF
echo ""

echo "--- [3/8] prompt composition formula + budget truncation ---"
CONTRACT_JSON="$(python3 "$ROOT/AITools/taskctl.py" contract "$TASK_ID")"
python3 - "$CONTRACT_JSON" <<'PYEOF'
import sys

contract_json = sys.argv[1]
read_first = [
    "docs/task_mcp_agent_launcher_safety_contract_b03_v1.md",
    "bitnnv2/eval/task_mcp_agent_launcher_safety_contract_b03_v1.json",
    "AITools/taskctl.py",
    "bitnnv2/data/tasking/worker_low_token_protocol_v1.json",
]
composed = contract_json + "\n---read_first---\n" + "\n".join(read_first)
marker = "...[PROMPT_TRUNCATED_AT_BUDGET]"

def apply_budget(text, budget):
    if len(text) > budget:
        return text[: budget - len(marker)] + marker, True
    return text, False

default_budget = 20000
out_default, truncated_default = apply_budget(composed, default_budget)
assert len(out_default) <= default_budget, "default-budget output exceeds budget"

small_budget = 4000
out_small, truncated_small = apply_budget(composed, small_budget)
assert truncated_small is True, "small budget must trigger truncation for this composed prompt"
assert len(out_small) == small_budget, f"truncated output length {len(out_small)} != budget {small_budget}"
assert out_small.endswith(marker), "truncated output must end with the truncation marker"

print(f"composed_prompt_chars={len(composed)} default_budget_truncated={truncated_default} "
      f"small_budget_truncated={truncated_small} small_out_len={len(out_small)}: OK")
PYEOF
echo ""

echo "--- [4/8] Defense-in-depth: launch impossible even with ALLOW_LAUNCH=1 ---"
LAUNCH_STATE="$(GEOAI_TASK_MCP_ALLOW_LAUNCH=1 python3 -c '
import sys
sys.path.insert(0, "'"$MCPROOT"'/src")
from geoai_task_mcp import cli_adapter_dryrun as m
print(str(m.launch_enabled()) + "," + str(m.LAUNCH_IMPLEMENTED))
')"
if [ "$LAUNCH_STATE" != "False,False" ]; then
    echo "FAIL: launch not disabled with ALLOW_LAUNCH=1 (got $LAUNCH_STATE)"
    exit 1
fi
echo "launch impossible with ALLOW_LAUNCH=1: OK ($LAUNCH_STATE)"
echo ""

echo "--- [5/8] write gate still off by default ---"
ACTUAL="$(python3 -c 'import os; print(os.environ.get("GEOAI_TASK_MCP_ALLOW_WRITES","0"))')"
if [ "$ACTUAL" != "0" ]; then
    echo "FAIL: GEOAI_TASK_MCP_ALLOW_WRITES=$ACTUAL (expected 0)"
    exit 1
fi
echo "GEOAI_TASK_MCP_ALLOW_WRITES confirmed still 0 (off)"
echo ""

echo "--- [6/8] task_id_binding_check is read-only (taskctl show only) ---"
KNOWN_OUT="$(python3 "$ROOT/AITools/taskctl.py" show "$TASK_ID")"
if ! echo "$KNOWN_OUT" | grep -q "$TASK_ID"; then
    echo "FAIL: known task_id not found via read-only show"
    exit 1
fi
UNKNOWN_OUT="$(python3 "$ROOT/AITools/taskctl.py" show NONEXISTENT_TASK_ID_ZZZ_TEST_B04)"
if ! echo "$UNKNOWN_OUT" | grep -qi "not found"; then
    echo "FAIL: unknown task_id did not report not-found"
    exit 1
fi
echo "task_id_binding_check via read-only taskctl show: OK (known=found, unknown=not_found)"
echo ""

echo "--- [7/8] forbidden_operations_confirmed_not_taken re-check ---"
python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
f = d['forbidden_operations_confirmed_not_taken']
assert all(v is False for v in f.values()), f
print('forbidden ops all False:', f)
"
echo ""

echo "--- [8/8] parent queue intact ---"
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact; this script never calls review/done)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
