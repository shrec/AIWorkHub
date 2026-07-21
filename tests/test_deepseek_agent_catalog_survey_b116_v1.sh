#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_deepseek_agent_catalog_survey_b116_v1.sh
# Harness for the DeepSeek agent catalog survey (read-only, no launch).
#
# Verifies:
#   1. Survey script runs and prints PASS
#   2. eval JSON parses, verdict=PASS, gates correct
#   3. JSONL rows: 23 candidates, at least top-8 (DeepSeek-TUI, Reasonix, Deep Code, OpenCode, Cline, Qwen, Codex, Copilot)
#   4. next_wave JSON parses, next experiment disabled_by_default=true
#   5. launch_enabled=false, install_performed=false
#   6. No process launch code in survey script
#   7. Parent task queue intact (taskctl verify)
#
# Isolation: uses mktemp for scratch; no shared repo artifact written.
# Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/aiworkhub_agent_catalog_survey_sh.XXXXXX")"
trap 'rm -rf "$TMPDIR"' EXIT

export PYTHONPATH="$MCPROOT/src"
export AIWORKHUB_REPO="$ROOT"
export AIWORKHUB_ALLOW_WRITES=0

echo "=== DeepSeek Agent Catalog Survey Test B116 v1 ==="
echo "AIWORKHUB_REPO=$AIWORKHUB_REPO"
echo "AIWORKHUB_ALLOW_WRITES=$AIWORKHUB_ALLOW_WRITES"

# ── 1. Run survey script ──
echo ""
echo "--- 1. Survey script run ---"
SURVEY_OUT="$(python3 "$MCPROOT/scripts/survey_deepseek_agent_catalog_b116_v1.py" 2>&1)"
echo "$SURVEY_OUT"

if ! echo "$SURVEY_OUT" | grep -q "PASS"; then
    echo "FAIL: survey script did not print PASS"
    exit 1
fi
echo "Survey script: PASS"

# ── 2. Verify eval JSON ──
echo ""
echo "--- 2. Eval JSON checks ---"
EVAL_JSON="$MCPROOT/eval/deepseek_agent_catalog_survey_b116_v1.json"
if [ ! -f "$EVAL_JSON" ]; then
    echo "FAIL: eval JSON not found at $EVAL_JSON"
    exit 1
fi

python3 -c "
import json
with open('$EVAL_JSON') as f:
    d = json.load(f)
assert d['verdict'] == 'PASS', f'verdict not PASS: {d[\"verdict\"]}'
assert d['gates']['launch_enabled'] == False, 'launch_enabled must be False'
assert d['gates']['install_performed'] == False, 'install_performed must be False'
assert d['gates']['next_experiment_disabled_by_default'] == True, 'next_experiment must be disabled'
assert d['gates']['no_process_launch_code'] == True, 'no_process_launch_code must be True'
assert d['gates']['survey_readonly'] == True, 'survey_readonly must be True'
assert d['mode'] == 'adapter_survey_no_launch_no_install', f'bad mode: {d[\"mode\"]}'
assert d['total_candidates'] == 23, f'expected 23 candidates, got {d[\"total_candidates\"]}'
assert d['recommended_next_adapter_experiment']['disabled_by_default'] == True
assert d['recommended_next_adapter_experiment']['candidate_id'] == 'deepseek_tui'
print('eval JSON: PASS')
"
echo "eval JSON: PASS"

# ── 3. Verify JSONL rows ──
echo ""
echo "--- 3. JSONL rows checks ---"
JSONL="$MCPROOT/eval/deepseek_agent_catalog_survey_rows_b116_v1.jsonl"
if [ ! -f "$JSONL" ]; then
    echo "FAIL: JSONL not found at $JSONL"
    exit 1
fi

ROW_COUNT=$(wc -l < "$JSONL")
if [ "$ROW_COUNT" -ne 23 ]; then
    echo "FAIL: expected 23 JSONL rows, got $ROW_COUNT"
    exit 1
fi
echo "JSONL row count: $ROW_COUNT (expected 23)"

# Check required candidates are present
python3 -c "
import json
required = {'deepseek_tui', 'reasonix', 'deep_code', 'opencode', 'cline', 'qwen_code', 'codex', 'github_copilot'}
found = set()
with open('$JSONL') as f:
    for line in f:
        row = json.loads(line)
        found.add(row['candidate_id'])
        # Verify structure
        assert 'candidate_id' in row
        assert 'rank' in row
        assert 'overall_score' in row
        assert 'tier' in row
        assert 'scores' in row
        assert 'worker_suitable' in row
missing = required - found
assert not missing, f'Missing required candidates: {missing}'
print(f'All {len(required)} required candidates present: OK')
print(f'Total candidates in JSONL: {len(found)}')
"
echo "JSONL structure: PASS"

# ── 4. Verify next_wave JSON ──
echo ""
echo "--- 4. Next-wave JSON checks ---"
NW_JSON="$MCPROOT/data/tasking/deepseek_agent_catalog_survey_next_wave_b116_v1.json"
if [ ! -f "$NW_JSON" ]; then
    echo "FAIL: next_wave JSON not found at $NW_JSON"
    exit 1
fi

python3 -c "
import json
with open('$NW_JSON') as f:
    d = json.load(f)
assert d['status'] == 'proposal', f'status not proposal: {d[\"status\"]}'
assert len(d['follow_up_tasks']) >= 1, 'need at least 1 follow_up_task'
next_task = d['follow_up_tasks'][0]
assert next_task['disabled_by_default'] == True, 'first follow-up must be disabled_by_default'
assert 'NO_COMMIT' in next_task.get('mode', ''), 'mode must be NO_COMMIT'
assert 'adapter' in next_task['goal'].lower() or 'dryrun' in next_task['goal'].lower(), 'first task must be adapter/dryrun'
print('next_wave JSON: PASS')
"
echo "next_wave JSON: PASS"

# ── 5. Survey script has no process launch code ──
echo ""
echo "--- 5. No-launch-code audit ---"
SURVEY_PY="$MCPROOT/scripts/survey_deepseek_agent_catalog_b116_v1.py"
FORBIDDEN=("subprocess.Popen" "subprocess.run" "subprocess.call" "os.system" "os.exec" "os.fork" "os.spawn" "shell=True")
FOUND_ANY=0
for pattern in "${FORBIDDEN[@]}"; do
    if grep -q "$pattern" "$SURVEY_PY" 2>/dev/null; then
        echo "FAIL: forbidden process-launch pattern found in survey script: $pattern"
        FOUND_ANY=1
    fi
done
# Also check for gate-bypass patterns (looking at actual function calls, not JSON field names)
for pattern in "install_tool(" "runtime_switch_to_mcp(" "write_gate_disable(" "api_key_write("; do
    if grep -q "$pattern" "$SURVEY_PY" 2>/dev/null; then
        echo "FAIL: forbidden gate-bypass function call: $pattern"
        FOUND_ANY=1
    fi
done
if [ "$FOUND_ANY" -eq 0 ]; then
    echo "No forbidden launch/install/switch patterns found: OK"
else
    echo "FATAL: forbidden patterns found in survey script"
    exit 1
fi
echo "No-launch-code audit: PASS"

# ── 6. Verify launch_enabled and install_performed invariants ──
echo ""
echo "--- 6. Authority flag invariants ---"
python3 -c "
import json
with open('$EVAL_JSON') as f:
    d = json.load(f)
flags = d['authority_flags']
assert flags['launch_enabled'] == False
assert flags['install_performed'] == False
assert flags['workflow_switch'] == False
assert flags['write_gate_default_off'] == True
print('Authority flags: PASS')
"
echo "Authority flags: PASS"

# ── 7. JSON parse validation ──
echo ""
echo "--- 7. JSON parse validation ---"
python3 -m json.tool "$EVAL_JSON" > /dev/null
echo "eval JSON parses: OK"
python3 -m json.tool "$NW_JSON" > /dev/null
echo "next_wave JSON parses: OK"
python3 -c "
import json
with open('$JSONL') as f:
    for i, line in enumerate(f, 1):
        json.loads(line)
print(f'All {i} JSONL rows parse: OK')
"
echo "JSON parse: PASS"

# ── 8. Parent queue intact ──
echo ""
echo "--- 8. Parent queue integrity ---"
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "=== ALL CHECKS PASSED ==="
exit 0
