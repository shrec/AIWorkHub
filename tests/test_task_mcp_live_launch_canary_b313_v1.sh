#!/usr/bin/env bash
# Validates evidence fields for CLAUDE_TASK_MCP_LIVE_LAUNCH_CANARY_B313_V1.
# No network access. No queue mutation. Read-only JSON field checks only.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVAL="$ROOT/eval/task_mcp_live_launch_canary_b313_v1.json"

if [[ ! -f "$EVAL" ]]; then
  echo "FAIL: eval artifact missing: $EVAL"
  exit 1
fi

EVAL="$EVAL" python3 - <<'PY'
import json
import os
import sys

EVAL = os.environ["EVAL"]

with open(EVAL, encoding="utf-8") as fh:
    d = json.load(fh)

errors = []

def check(label, actual, expected):
    if actual != expected:
        errors.append(f"{label}: expected {expected!r} got {actual!r}")

# Identity fields
check("task_id", d.get("task_id"), "CLAUDE_TASK_MCP_LIVE_LAUNCH_CANARY_B313_V1")
check("runner", d.get("runner"), "claude_task_mcp_live_launch_canary_b313")
check("topic", d.get("topic"), "task_mcp")
check("mode", d.get("mode"), "task_mcp_real_cli_launch_canary_no_source_mutation")
check("verdict", d.get("verdict"), "PASS")

# Lifecycle evidence distinguishing process launch from task completion
lc = d.get("lifecycle_evidence", {})
check("exact_claim_start", lc.get("exact_claim_start"), True)
check("continued_after_claim", lc.get("continued_after_claim"), True)
check("shell_invocation", lc.get("shell_invocation"), False)
check("product_source_mutation", lc.get("product_source_mutation"), False)

# Lifecycle invariants
check("taskctl_done_called", lc.get("taskctl_done_called"), False)
check("git_add_A_used", lc.get("git_add_A_used"), False)
check("commit_made", lc.get("commit_made"), False)
check("guard_passed", lc.get("guard_passed"), True)

# Gate completeness
gates = d.get("gates", {})
required_gates = [
    "task_id_matches_contract",
    "runner_matches_contract",
    "topic_matches_contract",
    "mode_matches_contract",
    "verdict_is_PASS",
    "exact_claim_start_true",
    "continued_after_claim_true",
    "shell_invocation_false",
    "product_source_mutation_false",
    "guard_executed_before_write",
    "no_taskctl_done",
    "no_git_add_A",
    "no_commit",
    "taskctl_review_called",
]
for g in required_gates:
    if not gates.get(g):
        errors.append(f"gate {g!r} missing or false")

if errors:
    print("FAIL:")
    for e in errors:
        print(f"  {e}")
    sys.exit(1)

print("PASS: all evidence fields verified for CLAUDE_TASK_MCP_LIVE_LAUNCH_CANARY_B313_V1")
print(f"  task_id={d['task_id']}")
print(f"  runner={d['runner']}")
print(f"  topic={d['topic']}")
print(f"  mode={d['mode']}")
print(f"  verdict={d['verdict']}")
print(f"  exact_claim_start={lc['exact_claim_start']}, continued_after_claim={lc['continued_after_claim']}")
print(f"  shell_invocation={lc['shell_invocation']}, product_source_mutation={lc['product_source_mutation']}")
PY
