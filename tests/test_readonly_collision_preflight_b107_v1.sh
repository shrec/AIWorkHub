#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_readonly_collision_preflight_b107_v1.sh
# Verifies the B107 READ-ONLY collision preflight added to plan_command_readonly.
#
# Checks:
#   A. would_collide=true when a temp collision-guard state already holds an
#      active claim overlapping this task_id/runner; collision_preflight is
#      populated with matching detail.
#   B. would_collide=false on a fresh/empty temp state.
#   C. Running the plan mutates NO state: the temp cards/audit dir file list
#      and file hashes/mtimes are identical before and after.
#   D. Write gate stays default-off; no launch/subprocess/exec/fork string in
#      the module source.
#   E. Secret redaction is preserved (known secret env name stays redacted).
#
# Isolation: everything lives under a per-run mktemp dir; the collision-guard
# card source is redirected via GEOAI_TASK_MCP_COLLISION_CARDS_PATH and the
# audit log via GEOAI_TASK_MCP_AUDIT_LOG_PATH. No shared repo artifact is ever
# touched. Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
MODULE="$MCPROOT/src/geoai_task_mcp/cli_adapter_readonly_tool.py"

TMPDIR_RUN="$(mktemp -d "${TMPDIR:-/tmp}/geoai_readonly_collision_preflight_sh.XXXXXX")"
trap 'rm -rf "$TMPDIR_RUN"' EXIT

STATE_DIR="$TMPDIR_RUN/state"
mkdir -p "$STATE_DIR"

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES=0
export GEOAI_TASK_MCP_AUDIT_LOG_PATH="$STATE_DIR/audit.jsonl"

echo "=== B107 Read-Only Collision Preflight Test ==="
echo "GEOAI_REPO=$GEOAI_REPO"
echo "STATE_DIR=$STATE_DIR"

TASK_ID="CLAUDE_TASK_MCP_READONLY_COLLISION_PREFLIGHT_B107_V1"
RUNNER="claude_task_mcp_collision_preflight_b107"
TOPIC="task_mcp"

# ---------------------------------------------------------------------------
# Case A: seed a temp collision-guard state that already holds the target
# runner with a DIFFERENT active task_id -> would_collide=true.
# ---------------------------------------------------------------------------
CARDS_A="$STATE_DIR/cards_case_a.jsonl"
python3 -c "
import json
card = {
    'task_id': 'SOME_OTHER_ACTIVE_TASK_B999_V1',
    'runner': '$RUNNER',
    'topic': '$TOPIC',
    'status': 'processing',
    'worker_status': 'claimed',
    'allowed_writes': ['some/other/file.py'],
}
with open('$CARDS_A', 'w', encoding='utf-8') as f:
    f.write(json.dumps(card) + chr(10))
"

RESULT_A="$(GEOAI_TASK_MCP_COLLISION_CARDS_PATH="$CARDS_A" python3 -c '
import json, sys
sys.path.insert(0, "'"$MCPROOT"'/src")
from geoai_task_mcp import cli_adapter_readonly_tool as m
plan = m.plan_command_readonly(
    task_id="'"$TASK_ID"'", runner="'"$RUNNER"'", topic="'"$TOPIC"'",
    adapter_id="claude_cli", argv=["python3", "AITools/taskctl.py", "verify"],
)
print(json.dumps({
    "would_collide": plan["would_collide"],
    "cp": plan["collision_preflight"],
}))
')"

echo "Case A result: $RESULT_A"
WOULD_COLLIDE_A="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['would_collide'])" "$RESULT_A")"
CP_REASONS_A="$(python3 -c "import json,sys; d=json.loads(sys.argv[1])['cp']; print(len(d['collision_reasons']))" "$RESULT_A")"
CP_MUTATED_A="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['cp']['mutated_state'])" "$RESULT_A")"
CP_LOCK_A="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['cp']['lock_acquired'])" "$RESULT_A")"

if [ "$WOULD_COLLIDE_A" != "True" ]; then
    echo "FAIL: Case A expected would_collide=True, got $WOULD_COLLIDE_A"
    exit 1
fi
if [ "$CP_REASONS_A" = "0" ]; then
    echo "FAIL: Case A collision_preflight.collision_reasons is empty"
    exit 1
fi
if [ "$CP_MUTATED_A" != "False" ] || [ "$CP_LOCK_A" != "False" ]; then
    echo "FAIL: Case A collision_preflight claims mutation or lock (mutated=$CP_MUTATED_A lock=$CP_LOCK_A)"
    exit 1
fi
echo "Case A (would_collide=true, populated collision_preflight): OK"

# ---------------------------------------------------------------------------
# Case B: fresh/empty temp state -> would_collide=false.
# ---------------------------------------------------------------------------
CARDS_B="$STATE_DIR/cards_case_b.jsonl"
: > "$CARDS_B"  # empty but existing file
CARDS_B_MISSING="$STATE_DIR/cards_case_b_missing.jsonl"  # never created; exercises not-exists path

RESULT_B="$(GEOAI_TASK_MCP_COLLISION_CARDS_PATH="$CARDS_B" python3 -c '
import json, sys
sys.path.insert(0, "'"$MCPROOT"'/src")
from geoai_task_mcp import cli_adapter_readonly_tool as m
plan = m.plan_command_readonly(
    task_id="'"$TASK_ID"'", runner="'"$RUNNER"'", topic="'"$TOPIC"'",
    adapter_id="claude_cli", argv=["python3", "AITools/taskctl.py", "verify"],
)
print(json.dumps({"would_collide": plan["would_collide"]}))
')"

echo "Case B result: $RESULT_B"
WOULD_COLLIDE_B="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['would_collide'])" "$RESULT_B")"
if [ "$WOULD_COLLIDE_B" != "False" ]; then
    echo "FAIL: Case B expected would_collide=False, got $WOULD_COLLIDE_B"
    exit 1
fi
echo "Case B (would_collide=false on fresh/empty state): OK"

# Also exercise the not-yet-created cards path (missing file, not just empty).
WOULD_COLLIDE_B_MISSING="$(GEOAI_TASK_MCP_COLLISION_CARDS_PATH="$CARDS_B_MISSING" python3 -c '
import sys
sys.path.insert(0, "'"$MCPROOT"'/src")
from geoai_task_mcp import cli_adapter_readonly_tool as m
plan = m.plan_command_readonly(
    task_id="'"$TASK_ID"'", runner="'"$RUNNER"'", topic="'"$TOPIC"'",
    adapter_id="claude_cli", argv=["python3", "AITools/taskctl.py", "verify"],
)
print(plan["would_collide"])
')"
if [ "$WOULD_COLLIDE_B_MISSING" != "False" ]; then
    echo "FAIL: Case B (missing cards file) expected would_collide=False, got $WOULD_COLLIDE_B_MISSING"
    exit 1
fi
echo "Case B (would_collide=false, cards file does not exist yet): OK"

# ---------------------------------------------------------------------------
# Case C: running the plan mutates NO state. Snapshot the temp state dir
# (file list + sha256 + mtime) before and after a plan call, assert identical.
# ---------------------------------------------------------------------------
snapshot() {
    find "$STATE_DIR" -type f | sort | while read -r f; do
        printf '%s %s %s\n' "$f" "$(sha256sum "$f" | cut -d' ' -f1)" "$(stat -c %Y "$f")"
    done
}

SNAP_BEFORE="$(snapshot)"

GEOAI_TASK_MCP_COLLISION_CARDS_PATH="$CARDS_A" python3 -c '
import sys
sys.path.insert(0, "'"$MCPROOT"'/src")
from geoai_task_mcp import cli_adapter_readonly_tool as m
m.plan_command_readonly(
    task_id="'"$TASK_ID"'", runner="'"$RUNNER"'", topic="'"$TOPIC"'",
    adapter_id="claude_cli", argv=["python3", "AITools/taskctl.py", "verify"],
    env={"SOME_API_KEY": "super-secret-value"},
)
m.audit_summary_readonly()
' > /dev/null

SNAP_AFTER="$(snapshot)"

if [ "$SNAP_BEFORE" != "$SNAP_AFTER" ]; then
    echo "FAIL: state dir snapshot changed after running plan (mutation detected)"
    echo "--- before ---"; echo "$SNAP_BEFORE"
    echo "--- after ---"; echo "$SNAP_AFTER"
    exit 1
fi
echo "Case C (no state mutation; snapshot identical before/after): OK"

# ---------------------------------------------------------------------------
# Case D: write gate default-off; no launch/subprocess/exec/fork strings in
# the module source.
# ---------------------------------------------------------------------------
GATE_OFF="$(python3 -c '
import sys
sys.path.insert(0, "'"$MCPROOT"'/src")
from geoai_task_mcp import cli_adapter_readonly_tool as m
print(m.writes_allowed())
')"
if [ "$GATE_OFF" != "False" ]; then
    echo "FAIL: write gate not default-off (writes_allowed()=$GATE_OFF)"
    exit 1
fi
echo "Write gate default-off: OK ($GATE_OFF)"

FORBIDDEN_PATTERNS=("import subprocess" "subprocess\." "os\.exec" "os\.fork" "Popen\(" "shell=True")
for pat in "${FORBIDDEN_PATTERNS[@]}"; do
    if grep -Eq "$pat" "$MODULE"; then
        echo "FAIL: forbidden pattern '$pat' found in $MODULE"
        exit 1
    fi
done
echo "No launch/subprocess/exec/fork pattern in module source: OK"

LAUNCH_STATE="$(python3 -c '
import sys
sys.path.insert(0, "'"$MCPROOT"'/src")
from geoai_task_mcp import cli_adapter_readonly_tool as m
print(str(m.launch_enabled()) + "," + str(m.LAUNCH_IMPLEMENTED))
')"
if [ "$LAUNCH_STATE" != "False,False" ]; then
    echo "FAIL: launch state wrong (got $LAUNCH_STATE)"
    exit 1
fi
echo "Launch impossible: OK ($LAUNCH_STATE)"

# ---------------------------------------------------------------------------
# Case E: secret redaction preserved.
# ---------------------------------------------------------------------------
REDACTED="$(GEOAI_TASK_MCP_COLLISION_CARDS_PATH="$CARDS_B_MISSING" python3 -c '
import sys
sys.path.insert(0, "'"$MCPROOT"'/src")
from geoai_task_mcp import cli_adapter_readonly_tool as m
plan = m.plan_command_readonly(
    task_id="'"$TASK_ID"'", runner="'"$RUNNER"'", topic="'"$TOPIC"'",
    adapter_id="claude_cli", argv=["python3", "AITools/taskctl.py", "verify"],
    env={"MY_SECRET_TOKEN": "abc123-should-never-appear"},
)
print(plan["redacted_env"]["MY_SECRET_TOKEN"])
')"
if [ "$REDACTED" != "<redacted-secret>" ]; then
    echo "FAIL: secret redaction not preserved (got '$REDACTED')"
    exit 1
fi
echo "Secret redaction preserved: OK ($REDACTED)"

# ---------------------------------------------------------------------------
# Parent queue / repo state sanity.
# ---------------------------------------------------------------------------
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
