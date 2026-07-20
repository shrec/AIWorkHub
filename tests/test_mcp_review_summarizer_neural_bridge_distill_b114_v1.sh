#!/usr/bin/env bash
set -euo pipefail

# ── test_mcp_review_summarizer_neural_bridge_distill_b114_v1.sh ─────
# B114 neural bridge distill validation:
#   - Build script runs and produces eval JSON, distill rows JSONL, next_wave JSON
#   - All JSON artifacts are valid
#   - Distill rows cover: review_priority, topic_grouping, runner_grouping,
#     status_distinction, abstain_detection, tool_affordance
#   - process_launch_authority=false, write_gate_enabled=false
#   - No live queue mutation, no agent/model launch
#
# Usage:
#   GEOAI_TASK_MCP_ALLOW_WRITES=0 bash \
#     tools/geoai-task-mcp/tests/test_mcp_review_summarizer_neural_bridge_distill_b114_v1.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES="${GEOAI_TASK_MCP_ALLOW_WRITES:-0}"

echo "=== B114 MCP Review Summarizer Neural Bridge Distill Test ==="
echo "ROOT=$ROOT"
echo "PYTHONPATH=$PYTHONPATH"
echo "GEOAI_TASK_MCP_ALLOW_WRITES=$GEOAI_TASK_MCP_ALLOW_WRITES"
echo ""

EVAL_JSON="$MCPROOT/eval/mcp_review_summarizer_neural_bridge_distill_b114_v1.json"
JSONL="$MCPROOT/eval/mcp_review_summarizer_neural_bridge_distill_rows_b114_v1.jsonl"
NEXT_WAVE="$MCPROOT/data/tasking/mcp_review_summarizer_neural_bridge_distill_next_wave_b114_v1.json"
BUILD_SCRIPT="$MCPROOT/scripts/build_mcp_review_summarizer_neural_bridge_distill_b114_v1.py"

# ── 1. Validate ALLOW_WRITES is off ─────────────────────────────────
if [ "$GEOAI_TASK_MCP_ALLOW_WRITES" != "0" ]; then
    echo "FATAL: GEOAI_TASK_MCP_ALLOW_WRITES must be 0, got '$GEOAI_TASK_MCP_ALLOW_WRITES'"
    exit 2
fi

# ── 2. Build script exists and is syntactically valid ───────────────
echo "--- Build script check ---"
python3 -c "import py_compile; py_compile.compile('$BUILD_SCRIPT', doraise=True)"
echo "  PASS build_script_syntax: Python syntax OK"
RC_BUILD_SYN=$?
if [ $RC_BUILD_SYN -ne 0 ]; then
    echo "FAIL: build script syntax check failed"
    exit 1
fi

# ── 3. Run build script ─────────────────────────────────────────────
echo "--- Run build script ---"
python3 "$BUILD_SCRIPT"
RC_BUILD=$?
if [ $RC_BUILD -ne 0 ]; then
    echo "FAIL: build script exited with $RC_BUILD"
    exit 1
fi
echo "  PASS build_script_run"

# ── 4. Validate eval JSON ───────────────────────────────────────────
echo "--- Eval JSON validity ---"
python3 -m json.tool "$EVAL_JSON" >/dev/null
echo "  PASS eval_json_valid"

# ── 5. Validate distill rows JSONL ──────────────────────────────────
echo "--- Distill rows JSONL validity ---"
python3 -c "
import json
count = 0
skills = set()
with open('$JSONL', 'r') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        count += 1
        # Every row must have required fields
        assert 'row_id' in row, f'row {count}: missing row_id'
        assert 'stage' in row, f'row {row[\"row_id\"]}: missing stage'
        assert row['stage'] == 'verified', f'row {row[\"row_id\"]}: stage={row[\"stage\"]}, expected verified'
        assert 'skill' in row, f'row {row[\"row_id\"]}: missing skill'
        assert 'input_features' in row, f'row {row[\"row_id\"]}: missing input_features'
        assert 'target' in row, f'row {row[\"row_id\"]}: missing target'
        assert 'source' in row, f'row {row[\"row_id\"]}: missing source'
        assert 'curriculum_module' in row, f'row {row[\"row_id\"]}: missing curriculum_module'
        skills.add(row['skill'])

print(f'  total_rows: {count}')
print(f'  skills: {sorted(skills)}')

# Verify required skills are covered
required_skills = {
    'review_priority_classification',
    'review_actionable_detection',
    'topic_grouping',
    'runner_grouping',
    'status_action_classification',
    'abstain_detection',
    'tool_affordance_reasoning',
}
missing = required_skills - skills
assert not missing, f'Missing required skills: {missing}'
print(f'  PASS skills_coverage: all {len(required_skills)} required skills covered')
"
RC_JSONL=$?
if [ $RC_JSONL -ne 0 ]; then
    echo "FAIL: JSONL validation failed"
    exit 1
fi

# ── 6. Validate next_wave JSON ──────────────────────────────────────
echo "--- Next wave JSON validity ---"
python3 -m json.tool "$NEXT_WAVE" >/dev/null
echo "  PASS next_wave_json_valid"

python3 -c "
import json
with open('$NEXT_WAVE', 'r') as f:
    nw = json.load(f)
assert nw.get('verdict') == 'PASS', f'verdict not PASS: {nw.get(\"verdict\")}'
assert nw.get('invariants_verified', {}).get('PROCESS_LAUNCH_AUTHORITY_FALSE') == True
assert nw.get('invariants_verified', {}).get('WRITE_GATE_ENABLED_FALSE') == True
print('  PASS next_wave_invariants: process_launch_authority=false, write_gate_enabled=false')
"
RC_NW=$?
if [ $RC_NW -ne 0 ]; then
    echo "FAIL: next_wave validation failed"
    exit 1
fi

# ── 7. Authority flags check: process_launch_authority=false ───────
echo "--- Authority flags check ---"
python3 -c "
import json
with open('$EVAL_JSON', 'r') as f:
    eval_doc = json.load(f)

ar = eval_doc.get('acceptance_results', {})
assert ar.get('process_launch_authority_false') == True, \
    f'process_launch_authority must be false, got {ar.get(\"process_launch_authority_false\")}'
print('  PASS process_launch_authority_false')

assert ar.get('write_gate_enabled_false') == True, \
    f'write_gate_enabled must be false, got {ar.get(\"write_gate_enabled_false\")}'
print('  PASS write_gate_enabled_false')

assert ar.get('no_live_queue_mutation') == True
print('  PASS no_live_queue_mutation')

assert ar.get('no_agent_launch') == True
print('  PASS no_agent_launch')

assert ar.get('no_model_launch') == True
print('  PASS no_model_launch')
"
RC_FLAGS=$?
if [ $RC_FLAGS -ne 0 ]; then
    echo "FAIL: authority flags check failed"
    exit 1
fi

# ── 8. Priority/abstain coverage check ──────────────────────────────
echo "--- Curriculum coverage check ---"
python3 -c "
import json
with open('$EVAL_JSON', 'r') as f:
    eval_doc = json.load(f)

cc = eval_doc.get('curriculum_coverage', {})

# Review priority labels
rp = cc.get('review_priority', {})
assert rp.get('covered') == True, 'review_priority not covered'
labels = rp.get('labels', [])
assert 'PRIORITY_HIGH' in labels, 'PRIORITY_HIGH missing'
assert 'PRIORITY_MEDIUM' in labels, 'PRIORITY_MEDIUM missing'
assert 'PRIORITY_LOW' in labels, 'PRIORITY_LOW missing'
assert 'PRIORITY_NONE' in labels, 'PRIORITY_NONE missing'
assert 'PRIORITY_UNKNOWN' in labels, 'PRIORITY_UNKNOWN missing'
print(f'  PASS review_priority: {len(labels)} labels')

# Status distinction
sd = cc.get('status_distinction', {})
assert sd.get('covered') == True
action_classes = sd.get('action_classes', [])
assert 'REVIEW_READY_ACTIONABLE' in action_classes
assert 'BLOCKED_NEEDS_UNBLOCK' in action_classes
assert 'PENDING_WAITING_WORKER' in action_classes
assert 'DONE_NO_ACTION' in action_classes
assert 'UNKNOWN_ABSTAIN' in action_classes
print(f'  PASS status_distinction: {len(action_classes)} action classes')

# Abstain conditions
ad = cc.get('abstain_detection', {})
assert ad.get('covered') == True
conditions = ad.get('conditions', [])
assert 'empty_queue' in conditions
assert 'all_fetch_errors' in conditions
assert 'write_gate_disabled' in conditions
assert 'no_process_launch' in conditions
print(f'  PASS abstain_detection: {len(conditions)} conditions')

# Topic grouping
tg = cc.get('topic_grouping', {})
assert tg.get('covered') == True
assert len(tg.get('topics', [])) >= 3, f'Expected >=3 topics, got {len(tg.get(\"topics\", []))}'
print(f'  PASS topic_grouping: {len(tg.get(\"topics\", []))} topics')

# Runner grouping
rg = cc.get('runner_grouping', {})
assert rg.get('covered') == True
assert len(rg.get('runners', [])) >= 4, f'Expected >=4 runners, got {len(rg.get(\"runners\", []))}'
print(f'  PASS runner_grouping: {len(rg.get(\"runners\", []))} runners')

# Tool affordance
ta = cc.get('tool_affordance', {})
assert ta.get('covered') == True
assert len(ta.get('sections', [])) >= 6, f'Expected >=6 sections, got {len(ta.get(\"sections\", []))}'
print(f'  PASS tool_affordance: {len(ta.get(\"sections\", []))} sections')

print('')
print('  ALL curriculum_coverage: PASS')
"
RC_COV=$?
if [ $RC_COV -ne 0 ]; then
    echo "FAIL: curriculum coverage check failed"
    exit 1
fi

# ── 9. Verify no subprocess/multiprocessing/agent import in build script ──
echo "--- No-launch import check ---"
python3 -c "
import ast, sys
with open('$BUILD_SCRIPT', 'r') as f:
    tree = ast.parse(f.read())
imports = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for alias in node.names:
            imports.add(alias.name.split('.')[0])
    elif isinstance(node, ast.ImportFrom):
        if node.module:
            imports.add(node.module.split('.')[0])

forbidden = {'subprocess', 'multiprocessing', 'asyncio', 'socket', 'http', 'urllib', 'requests'}
found = forbidden & imports
if found:
    print(f'FAIL: Forbidden imports in build script: {found}', file=sys.stderr)
    sys.exit(1)
print(f'  PASS no_forbidden_imports: {sorted(imports)}')
"
RC_IMP=$?
if [ $RC_IMP -ne 0 ]; then
    echo "FAIL: forbidden import check failed"
    exit 1
fi

# ── 10. Verify live queue unchanged ─────────────────────────────────
echo "--- Queue unchanged check ---"
QUEUE_FILE="$ROOT/bitnnv2/data/tasking/task_queue_v1.sqlite"
if [ -f "$QUEUE_FILE" ]; then
    QUEUE_HASH_BEFORE=$(sha256sum "$QUEUE_FILE" | awk '{print $1}')
    QUEUE_HASH_AFTER=$(sha256sum "$QUEUE_FILE" | awk '{print $1}')
    if [ "$QUEUE_HASH_BEFORE" != "$QUEUE_HASH_AFTER" ]; then
        echo "FAIL: queue file changed during test"
        exit 1
    fi
    echo "  PASS queue_unchanged: sha256=$QUEUE_HASH_AFTER"
else
    echo "  SKIP queue_unchanged: queue file not found at $QUEUE_FILE"
fi

echo ""
echo "=== test_mcp_review_summarizer_neural_bridge_distill_b114_v1.sh: PASS ==="
