#!/usr/bin/env bash
set -euo pipefail

# ── test_mcp_review_summarizer_startup_smoke_b114_v1.sh ─────────────
# B114 startup smoke test: MCP server module surface exposes
# aiworkhub_task_review_summarize with readonly flags, write gate default-off,
# and no queue mutation on tool listing.
#
# This test imports the server module directly (no process launch, no
# network, no agent/model start). It does NOT call the tool — it only
# verifies tool registration and authority flag invariants.
#
# Usage:
#   AIWORKHUB_ALLOW_WRITES=0 bash \
#     tools/geoai-task-mcp/tests/test_mcp_review_summarizer_startup_smoke_b114_v1.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

export PYTHONPATH="$MCPROOT/src"
export AIWORKHUB_REPO="$ROOT"
export AIWORKHUB_ALLOW_WRITES="${AIWORKHUB_ALLOW_WRITES:-0}"

echo "=== B114 MCP Review Summarizer Startup Smoke Test ==="
echo "ROOT=$ROOT"
echo "PYTHONPATH=$PYTHONPATH"
echo "AIWORKHUB_ALLOW_WRITES=$AIWORKHUB_ALLOW_WRITES"
echo ""

# ── 1. Validate ALLOW_WRITES is off ─────────────────────────────────
if [ "$AIWORKHUB_ALLOW_WRITES" != "0" ]; then
    echo "FATAL: AIWORKHUB_ALLOW_WRITES must be 0, got '$AIWORKHUB_ALLOW_WRITES'"
    exit 2
fi

# ── 2. Tool registration check ──────────────────────────────────────
echo "--- Tool registration ---"
python3 -c "
import sys, os
sys.path.insert(0, os.path.join('$MCPROOT', 'src'))

import aiworkhub.server as server_mod
from aiworkhub import review_summarizer

mcp = server_mod.mcp
tm = mcp._tool_manager
tools = tm._tools if hasattr(tm, '_tools') else {}

REQUIRED_TOOL = 'aiworkhub_task_review_summarize'
assert REQUIRED_TOOL in tools, \
    f'TOOL_MISSING: {REQUIRED_TOOL} not registered. Tools: {sorted(tools.keys())}'
print(f'  PASS tool_registered: {REQUIRED_TOOL} found in tool list')

# Verify the tool object has expected attributes
tool_obj = tools[REQUIRED_TOOL]
assert hasattr(tool_obj, 'fn'), f'{REQUIRED_TOOL} has no fn attribute'
assert callable(tool_obj.fn), f'{REQUIRED_TOOL}.fn is not callable'
print(f'  PASS tool_callable: {REQUIRED_TOOL}.fn is callable')

assert hasattr(tool_obj, 'name'), f'{REQUIRED_TOOL} has no name'
assert tool_obj.name == REQUIRED_TOOL, f'name mismatch: {tool_obj.name}'
print(f'  PASS tool_name: {tool_obj.name}')

# Check description contains READ-ONLY invariants
desc = (tool_obj.description or '').lower()
assert 'read-only' in desc or 'readonly' in desc, \
    'TOOL_DESCRIPTION_MISSING_READONLY'
print(f'  PASS tool_description_readonly: description contains read-only')

assert 'never mutates' in desc, \
    'TOOL_DESCRIPTION_MISSING_NEVER_MUTATES'
print(f'  PASS tool_description_no_mutation: description states never mutates')

assert 'never calls taskctl' in desc, \
    'TOOL_DESCRIPTION_MISSING_NEVER_CALLS_TASKCTL'
print(f'  PASS tool_description_no_taskctl_mutation: description states never calls taskctl done/review/start')

assert 'never launches' in desc, \
    'TOOL_DESCRIPTION_MISSING_NEVER_LAUNCHES'
print(f'  PASS tool_description_no_launch: description states never launches agents/models')

# Verify the function docstring as well
fn_doc = (tool_obj.fn.__doc__ or '').lower()
assert 'read-only' in fn_doc or 'readonly' in fn_doc, \
    'FN_DOCSTRING_MISSING_READONLY'
print(f'  PASS fn_docstring_readonly: fn docstring contains read-only')

print('')
print('  ALL tool_registration: PASS')
"
RC_TOOL=$?
if [ $RC_TOOL -ne 0 ]; then
    echo "FAIL: tool registration check failed"
    exit 1
fi

# ── 3. Readonly flags check ─────────────────────────────────────────
echo "--- Readonly flags ---"
python3 -c "
import sys, os
sys.path.insert(0, os.path.join('$MCPROOT', 'src'))
from aiworkhub import review_summarizer

flags = review_summarizer._authority_flags()
print(f'  authority_flags: {flags}')

# B114 required flags
assert flags.get('readonly') == True, f'readonly must be True, got {flags.get(\"readonly\")}'
print('  PASS readonly_flag: True')

assert flags.get('process_launch') == False, f'process_launch must be False, got {flags.get(\"process_launch\")}'
print('  PASS process_launch: False')

assert flags.get('agent_launch') == False, f'agent_launch must be False, got {flags.get(\"agent_launch\")}'
print('  PASS agent_launch: False')

assert flags.get('shell_invocation') == False, f'shell_invocation must be False'
print('  PASS shell_invocation: False')

assert flags.get('queue_write') == False, f'queue_write must be False'
print('  PASS queue_write: False')

assert flags.get('audit_write') == False, f'audit_write must be False'
print('  PASS audit_write: False')

assert flags.get('subprocess_launch_tripwire_zero') == True, \
    f'subprocess_launch_tripwire_zero must be True, got {flags.get(\"subprocess_launch_tripwire_zero\")}'
print('  PASS subprocess_launch_tripwire_zero: True')

print('')
print('  ALL readonly_flags: PASS')
"
RC_FLAGS=$?
if [ $RC_FLAGS -ne 0 ]; then
    echo "FAIL: readonly flags check failed"
    exit 1
fi

# ── 4. Write gate default-off check ─────────────────────────────────
echo "--- Write gate default-off ---"
python3 -c "
import sys, os
sys.path.insert(0, os.path.join('$MCPROOT', 'src'))
from aiworkhub import core, review_summarizer

# write gate is default-off: ALLOW_WRITES=0 in env
write_gate_on = core.writes_allowed()
assert write_gate_on == False, \
    f'Write gate must be default-off, but writes_allowed()={write_gate_on}'
print(f'  PASS write_gate_default_off: writes_allowed()={write_gate_on}')

# Verify authority flags: write_gate_enabled=True means gate is active
# (blocking writes), which is the correct default-off state.
flags = review_summarizer._authority_flags()
wg_flag = flags.get('write_gate_enabled')
assert wg_flag == True, \
    f'write_gate_enabled must be True (gate active = writes blocked), got {wg_flag}'
print(f'  PASS write_gate_enabled_flag: {wg_flag} (gate active, writes blocked)')

print('')
print('  ALL write_gate: PASS')
"
RC_GATE=$?
if [ $RC_GATE -ne 0 ]; then
    echo "FAIL: write gate check failed"
    exit 1
fi

# ── 5. No queue mutation on list-tools (module surface import) ──────
echo "--- No queue mutation on list-tools ---"
python3 -c "
import sys, os, hashlib, json
sys.path.insert(0, os.path.join('$MCPROOT', 'src'))

QUEUE_PATH = os.path.join('$ROOT', 'tools/geoai-task-mcp/data/task_queue_v1.sqlite')

# Snapshot queue sha256 before
if os.path.exists(QUEUE_PATH):
    with open(QUEUE_PATH, 'rb') as f:
        before_sha = hashlib.sha256(f.read()).hexdigest()
else:
    before_sha = 'FILE_NOT_FOUND'
print(f'  queue sha256 before: {before_sha}')

# Import server module (this triggers tool registration, not execution)
import aiworkhub.server as _server_mod

# Access tool list (no tool execution)
mcp = _server_mod.mcp
tm = mcp._tool_manager
tools = list((tm._tools if hasattr(tm, '_tools') else {}).keys())
print(f'  tools listed: {len(tools)} tools')
assert 'aiworkhub_task_review_summarize' in tools, 'tool missing after import'

# Snapshot queue sha256 after
if os.path.exists(QUEUE_PATH):
    with open(QUEUE_PATH, 'rb') as f:
        after_sha = hashlib.sha256(f.read()).hexdigest()
else:
    after_sha = 'FILE_NOT_FOUND'
print(f'  queue sha256 after:  {after_sha}')

assert before_sha == after_sha, \
    f'QUEUE_MUTATION_DETECTED: sha256 changed from {before_sha} to {after_sha}'
print('  PASS queue_unchanged: sha256 identical before/after list-tools')

# Also verify no audit log mutation
AUDIT_PATH = os.path.join('$ROOT', 'tools/geoai-task-mcp/logs/audit.jsonl')
if os.path.exists(AUDIT_PATH):
    with open(AUDIT_PATH, 'rb') as f:
        audit_before = hashlib.sha256(f.read()).hexdigest()
else:
    audit_before = 'FILE_NOT_FOUND'
print(f'  audit sha256 before: {audit_before}')

# Re-import is idempotent but let's re-access tool list for completeness
tools2 = list((tm._tools if hasattr(tm, '_tools') else {}).keys())

if os.path.exists(AUDIT_PATH):
    with open(AUDIT_PATH, 'rb') as f:
        audit_after = hashlib.sha256(f.read()).hexdigest()
    assert audit_before == audit_after, \
        f'AUDIT_MUTATION_DETECTED: sha256 changed from {audit_before} to {audit_after}'
    print(f'  PASS audit_unchanged: sha256 identical')
else:
    print(f'  PASS audit_unchanged: no audit log file (OK)')

print('')
print('  ALL queue_mutation: PASS')
"
RC_QM=$?
if [ $RC_QM -ne 0 ]; then
    echo "FAIL: queue mutation check failed"
    exit 1
fi

# ── 6. No agent/model launch verification ───────────────────────────
echo "--- No agent/model launch ---"
python3 -c "
import sys, os
sys.path.insert(0, os.path.join('$MCPROOT', 'src'))

# Import the server module — must not launch any subprocess during import
import aiworkhub.server as server_mod
from aiworkhub import review_summarizer

# Verify SUBPROCESS_LAUNCH_TRIPWIRE is 0
assert review_summarizer.SUBPROCESS_LAUNCH_TRIPWIRE == 0, \
    f'SUBPROCESS_LAUNCH_TRIPWIRE must be 0, got {review_summarizer.SUBPROCESS_LAUNCH_TRIPWIRE}'
print('  PASS subprocess_launch_tripwire: 0')

# Verify LAUNCH_IMPLEMENTED is False
assert review_summarizer.LAUNCH_IMPLEMENTED == False, \
    f'LAUNCH_IMPLEMENTED must be False, got {review_summarizer.LAUNCH_IMPLEMENTED}'
print('  PASS launch_implemented: False')

# Verify no process-spawn machinery imported by review_summarizer
# (it only imports core, which imports subprocess — but core.run_taskctl is
#  not called during import)
assert 'multiprocessing' not in dir(review_summarizer), 'multiprocessing imported'
print('  PASS no_multiprocessing_import: review_summarizer does not import multiprocessing')

# Verify the core module does not auto-launch
from aiworkhub import core
assert hasattr(core, 'run_taskctl'), 'core.run_taskctl missing'
assert callable(core.run_taskctl), 'core.run_taskctl not callable'
print('  PASS core.run_taskctl_callable: core can run taskctl but does not auto-launch')

print('')
print('  ALL no_agent_launch: PASS')
"
RC_LAUNCH=$?
if [ $RC_LAUNCH -ne 0 ]; then
    echo "FAIL: no-agent-launch check failed"
    exit 1
fi

# ── 7. Eval JSON validity ───────────────────────────────────────────
echo "--- Eval JSON validity ---"
python3 -m json.tool "$MCPROOT/eval/mcp_review_summarizer_startup_smoke_b114_v1.json" >/dev/null
echo "  PASS eval_json_valid"
echo ""

echo "=== test_mcp_review_summarizer_startup_smoke_b114_v1.sh: PASS ==="
