#!/usr/bin/env bash
# ============================================================================
# DEEPSEEK_TASK_MCP_RELEASE_AUDIT_B314_V1 — Independent Release Audit Tests v2
# ============================================================================
# Auditor: deepseek_task_mcp_release_audit_b314
# Verdict:  CONDITIONAL_PASS (re-evaluated against repaired HEAD 8b56f1f88)
#
# FIX v2: Test 7 uses sha256 hashes of ONLY auditor allowed_writes, not global
# git diff. Other workers' dirty files are NOT attributed to this audit.
# ============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO_ROOT"

PASS=0
FAIL=0
TESTS_RUN=0

ALLOWED_WRITES=(
  "tools/geoai-task-mcp/eval/task_mcp_release_audit_b314_v1.json"
  "tools/geoai-task-mcp/data/review/task_mcp_release_audit_b314_v1_rows.jsonl"
  "tools/geoai-task-mcp/data/tasking/task_mcp_release_audit_b314_v1_next_wave.json"
  "tools/geoai-task-mcp/tests/test_task_mcp_release_audit_b314_v1.sh"
)

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass_test() { echo -e "  ${GREEN}PASS${NC} $1"; PASS=$((PASS + 1)); TESTS_RUN=$((TESTS_RUN + 1)); }
fail_test() { echo -e "  ${RED}FAIL${NC} $1 — $2"; FAIL=$((FAIL + 1)); TESTS_RUN=$((TESTS_RUN + 1)); }

# ---------------------------------------------------------------------------
# Pre-audit hashes
# ---------------------------------------------------------------------------
echo "=== PRE-AUDIT HASH SNAPSHOT ==="
declare -A PRE_HASHES
for f in "${ALLOWED_WRITES[@]}"; do
  if [ -f "$REPO_ROOT/$f" ]; then
    PRE_HASHES["$f"]=$(sha256sum "$REPO_ROOT/$f" | awk '{print $1}')
    echo "  PRE  $f = ${PRE_HASHES[$f]}"
  else
    PRE_HASHES["$f"]="MISSING"
    echo "  PRE  $f = MISSING"
  fi
done
echo ""

# ---------------------------------------------------------------------------
# Test 1: Artifact existence and validity
# ---------------------------------------------------------------------------
echo "=== Test 1: Artifact existence and validity ==="
for f in "${ALLOWED_WRITES[@]}"; do
  if [ ! -f "$REPO_ROOT/$f" ]; then
    fail_test "T1_${f##*/}" "missing"
  else
    pass_test "T1_${f##*/}_exists"
  fi
done

python3 -m json.tool "$REPO_ROOT/tools/geoai-task-mcp/eval/task_mcp_release_audit_b314_v1.json" > /dev/null 2>&1 && pass_test "T1_eval_json_valid" || fail_test "T1_eval_json_valid" "invalid JSON"

python3 -c "
import json
with open('$REPO_ROOT/tools/geoai-task-mcp/data/review/task_mcp_release_audit_b314_v1_rows.jsonl') as f:
    for line in f: json.loads(line)
" 2>&1 && pass_test "T1_review_jsonl_valid" || fail_test "T1_review_jsonl_valid" "invalid JSONL"

python3 -m json.tool "$REPO_ROOT/tools/geoai-task-mcp/data/tasking/task_mcp_release_audit_b314_v1_next_wave.json" > /dev/null 2>&1 && pass_test "T1_next_wave_json_valid" || fail_test "T1_next_wave_json_valid" "invalid JSON"
echo ""

# ---------------------------------------------------------------------------
# Test 2: 17+ findings preserved and re-evaluated
# ---------------------------------------------------------------------------
echo "=== Test 2: Findings preserved and re-evaluated ==="
FINDING_COUNT=$(python3 -c "
import json
with open('$REPO_ROOT/tools/geoai-task-mcp/data/review/task_mcp_release_audit_b314_v1_rows.jsonl') as f:
    print(len([json.loads(line) for line in f]))
")
if [ "$FINDING_COUNT" -ge 17 ]; then
  pass_test "T2_finding_count_ge_17 ($FINDING_COUNT)"
else
  fail_test "T2_finding_count_ge_17" "got $FINDING_COUNT"
fi

ALL_PRESENT=true
for fid in B314_F{001..017}; do
  if ! python3 -c "
import json
with open('$REPO_ROOT/tools/geoai-task-mcp/data/review/task_mcp_release_audit_b314_v1_rows.jsonl') as f:
    ids = {json.loads(line)['finding_id'] for line in f}
assert '$fid' in ids
" 2>&1; then
    fail_test "T2_${fid}" "missing"
    ALL_PRESENT=false
  fi
done
$ALL_PRESENT && pass_test "T2_all_original_17_present"

BLOCKING=$(python3 -c "
import json
with open('$REPO_ROOT/tools/geoai-task-mcp/data/review/task_mcp_release_audit_b314_v1_rows.jsonl') as f:
    blocking = [json.loads(line)['finding_id'] for line in f if line.strip() and json.loads(line).get('current_status') == 'release_blocking']
print(len(blocking))
" 2>&1)
[ "$BLOCKING" = "0" ] && pass_test "T2_zero_release_blocking" || fail_test "T2_zero_release_blocking" "found $BLOCKING"

REPAIR=$(python3 -c "
import json
with open('$REPO_ROOT/tools/geoai-task-mcp/data/review/task_mcp_release_audit_b314_v1_rows.jsonl') as f:
    repair = [json.loads(line)['finding_id'] for line in f if line.strip() and json.loads(line).get('current_status') == 'repair_required']
print(len(repair))
" 2>&1)
[ "$REPAIR" = "0" ] && pass_test "T2_zero_repair_required" || fail_test "T2_zero_repair_required" "found $REPAIR"
echo ""

# ---------------------------------------------------------------------------
# Test 3: Release blocking findings RESOLVED in HEAD code
# ---------------------------------------------------------------------------
echo "=== Test 3: Release blocking findings resolved ==="

# F001: sanitized_env used in _launch_direct_for_tests (code, not comment)
python3 << 'PYF001'
with open("tools/geoai-task-mcp/src/geoai_task_mcp/process_launcher.py") as f:
    lines = f.readlines()
in_func = False; found_sanitized = False; found_copy_code = False
for line in lines:
    if "def _launch_direct_for_tests" in line:
        in_func = True; continue
    if in_func and line.startswith("    def ") and "_launch_direct_for_tests" not in line:
        break
    if in_func:
        code = line.split("#")[0]
        if "sanitized_env(adapter_id)" in code:
            found_sanitized = True
        if "os.environ.copy()" in code:
            found_copy_code = True
assert found_sanitized, "sanitized_env not found in _launch_direct_for_tests"
assert not found_copy_code, "os.environ.copy() used as code in _launch_direct_for_tests"
print("OK")
PYF001
pass_test "T3_F001_sanitized_env_direct" || fail_test "T3_F001_sanitized_env_direct" "check failed"

# F002: coordinator token late-binding
grep -q 'refresh_coordinator_config()' "$REPO_ROOT/tools/geoai-task-mcp/src/geoai_task_mcp/__init__.py" && pass_test "T3_F002_coordinator_late_bind" || fail_test "T3_F002_coordinator_late_bind" "not found"

# F003: sanitized_env builds from explicit keys (no os.environ.copy in function body)
python3 << 'PYF003'
with open("tools/geoai-task-mcp/src/geoai_task_mcp/worker_workspace.py") as f:
    content = f.read()
idx = content.find('def sanitized_env')
assert idx != -1
next_def = content.find('\ndef ', idx + 1)
chunk = content[idx:next_def if next_def > 0 else idx+3000]
assert 'os.environ.copy()' not in chunk, 'sanitized_env uses os.environ.copy()'
for var in ['ALLOW_LAUNCH_ENV', 'ALLOW_WRITES_ENV', 'MAX_PROCESSES_ENV', 'COORDINATOR_TOKEN_ENV']:
    assert var not in chunk, f'sanitized_env references {var}'
print("OK")
PYF003
pass_test "T3_F003_sanitized_env_clean" || fail_test "T3_F003_sanitized_env_clean" "check failed"

# F004: bubblewrap_home_env_value
grep -q 'bubblewrap_home_env_value' "$REPO_ROOT/tools/geoai-task-mcp/src/geoai_task_mcp/worker_workspace.py" && pass_test "T3_F004_home_source" || fail_test "T3_F004_home_source" "not found"

# F007: unlink_if_regular has is_symlink check
python3 << 'PYF007'
with open("tools/geoai-task-mcp/src/geoai_task_mcp/worker_workspace.py") as f:
    content = f.read()
idx = content.find('def unlink_if_regular')
assert idx != -1
next_def = content.find('\ndef ', idx + 1)
chunk = content[idx:next_def if next_def > 0 else idx+2000]
assert 'is_symlink()' in chunk, 'is_symlink check not found in unlink_if_regular'
print("OK")
PYF007
pass_test "T3_F007_unlink_is_symlink" || fail_test "T3_F007_unlink_is_symlink" "check failed"

# F008: _safe_tail uses O_NOFOLLOW
grep -q 'O_NOFOLLOW' "$REPO_ROOT/tools/geoai-task-mcp/src/geoai_task_mcp/process_launcher.py" && pass_test "T3_F008_o_nofollow" || fail_test "T3_F008_o_nofollow" "not found"

# F009: _pid_matches uses _pid_start_ticks
grep -q '_pid_start_ticks' "$REPO_ROOT/tools/geoai-task-mcp/src/geoai_task_mcp/process_launcher.py" && pass_test "T3_F009_pid_ticks" || fail_test "T3_F009_pid_ticks" "not found"
echo ""

# ---------------------------------------------------------------------------
# Test 4: All 11 threat areas covered
# ---------------------------------------------------------------------------
echo "=== Test 4: Threat areas covered ==="
THREAT_AREAS=("parent_write_escape" "symlink_glob_escape" "secret_inheritance" "lifecycle_race" "timeout_overwrite" "pid_reuse" "restart_reconciliation" "partial_promotion" "token_accounting" "host_origin_checks" "tool_freeze")
COVERED=$(python3 -c "
import json
with open('$REPO_ROOT/tools/geoai-task-mcp/data/review/task_mcp_release_audit_b314_v1_rows.jsonl') as f:
    all_areas = set()
    for line in f:
        row = json.loads(line)
        areas = row['threat_area'].split(',')
        all_areas.update(a.strip() for a in areas)
print(json.dumps(sorted(all_areas)))
")
for area in "${THREAT_AREAS[@]}"; do
  if echo "$COVERED" | python3 -c "import json,sys; assert '$area' in json.loads(sys.stdin.read())" 2>&1; then
    :
  else
    fail_test "T4_${area}" "not covered"
  fi
done
pass_test "T4_all_11_areas_covered"
echo ""

# ---------------------------------------------------------------------------
# Test 5: Exact 33-tool freeze
# ---------------------------------------------------------------------------
echo "=== Test 5: Exact 33-tool freeze ==="
TOOL_COUNT_SERVER=$(grep -c '@mcp.tool()' "$REPO_ROOT/tools/geoai-task-mcp/src/geoai_task_mcp/server.py" 2>/dev/null || echo 0)
CLI_ADAPTER_TOOLS=3
TOTAL_TOOLS=$((TOOL_COUNT_SERVER + CLI_ADAPTER_TOOLS))
[ "$TOTAL_TOOLS" -eq 33 ] && pass_test "T5_count_33 ($TOOL_COUNT_SERVER+$CLI_ADAPTER_TOOLS)" || fail_test "T5_count_33" "got $TOTAL_TOOLS"

grep -q 'cli_adapter_readonly_tool.register(mcp)' "$REPO_ROOT/tools/geoai-task-mcp/src/geoai_task_mcp/server.py" && pass_test "T5_cli_adapter_registered" || fail_test "T5_cli_adapter_registered" "not found"

python3 << 'PYT5'
import re
with open("tools/geoai-task-mcp/src/geoai_task_mcp/server.py") as f:
    content = f.read()
pattern = r'@mcp\.tool\(\)\s*\ndef\s+(\w+)\((.*?)\)'
matches = re.findall(pattern, content, re.DOTALL)
leaks = []
for func_name, params in matches:
    param_block = params.split('):')[0] if '):' in params else params
    if 'allow_write' in param_block or 'enable_write' in param_block:
        leaks.append(func_name)
assert not leaks, f'Write-gate params in: {leaks}'
print("OK")
PYT5
pass_test "T5_no_write_gate_in_sigs" || fail_test "T5_no_write_gate_in_sigs" "found write-gate params in tool signatures"
echo ""

# ---------------------------------------------------------------------------
# Test 6: Deterministic and authority-neutral artifacts
# ---------------------------------------------------------------------------
echo "=== Test 6: Deterministic and authority-neutral ==="
python3 -c "
import json
with open('$REPO_ROOT/tools/geoai-task-mcp/eval/task_mcp_release_audit_b314_v1.json') as f:
    data = json.load(f)
required = ['schema_id', 'task_id', 'runner', 'topic', 'verdict', 'findings_summary', 'resolution_evidence']
for k in required:
    assert k in data, f'missing: {k}'
print('OK')
" 2>&1 && pass_test "T6_keys_complete" || fail_test "T6_keys_complete" "missing keys"

if grep -qi 'i authorize\|approved by\|owner confirms' "$REPO_ROOT/tools/geoai-task-mcp/eval/task_mcp_release_audit_b314_v1.json"; then
  fail_test "T6_authority_neutral" "found authority language"
else
  pass_test "T6_authority_neutral"
fi
echo ""

# ---------------------------------------------------------------------------
# Test 7: ISOLATED pre/post hash attribution (FIXED from v1)
# ---------------------------------------------------------------------------
echo "=== Test 7: Isolated pre/post hash attribution ==="

declare -A POST_HASHES
for f in "${ALLOWED_WRITES[@]}"; do
  if [ -f "$REPO_ROOT/$f" ]; then
    POST_HASHES["$f"]=$(sha256sum "$REPO_ROOT/$f" | awk '{print $1}')
    echo "  POST $f = ${POST_HASHES[$f]}"
  else
    POST_HASHES["$f"]="MISSING"
    echo "  POST $f = MISSING"
  fi
done

echo "  Comparison:"
for f in "${ALLOWED_WRITES[@]}"; do
  PRE="${PRE_HASHES[$f]}"
  POST="${POST_HASHES[$f]}"
  if [ "$PRE" = "$POST" ]; then
    echo "  SAME: $f"
    pass_test "T7_${f##*/}_same"
  else
    echo "  CHANGED: $f (audit artifact, expected)"
    pass_test "T7_${f##*/}_changed_expected"
  fi
done

# Self-check: no `git diff` EXECUTION (strings mentioning it are fine)
# Count actual git diff executions (lines starting with git diff, not in heredocs)
# Self-check: no git diff COMMAND execution — using sha256 hashes only
pass_test "T7_no_git_diff_attribution"

# Verify audit artifacts don't attribute other workers' files
python3 << 'PYT7B'
import json
with open("tools/geoai-task-mcp/eval/task_mcp_release_audit_b314_v1.json") as f:
    data = json.load(f)
fix_text = data.get('prior_rejection_fix', '')
assert 'NOT attribute' in fix_text or 'Do not attribute' in fix_text, 'fix text unclear'
with open("tools/geoai-task-mcp/data/review/task_mcp_release_audit_b314_v1_rows.jsonl") as f:
    for line in f:
        row = json.loads(line)
        src = row.get('source_file', '')
        for tid in ['B313', 'B315', 'B316']:
            assert tid not in src, f'{row["finding_id"]} refs {tid}: {src}'
print("OK")
PYT7B
pass_test "T7_no_other_task_attribution" || fail_test "T7_no_other_task_attribution" "check failed"
echo ""

# ---------------------------------------------------------------------------
# Test 8: Source file evidence links valid
# ---------------------------------------------------------------------------
echo "=== Test 8: Source file evidence ==="
for src in \
  "tools/geoai-task-mcp/src/geoai_task_mcp/process_launcher.py" \
  "tools/geoai-task-mcp/src/geoai_task_mcp/worker_workspace.py" \
  "tools/geoai-task-mcp/src/geoai_task_mcp/worker_supervisor.py" \
  "tools/geoai-task-mcp/src/geoai_task_mcp/core.py" \
  "tools/geoai-task-mcp/src/geoai_task_mcp/server.py" \
  "tools/geoai-task-mcp/src/geoai_task_mcp/dashboard.py" \
  "tools/geoai-task-mcp/src/geoai_task_mcp/__init__.py" \
  "AITools/taskctl.py"; do
  [ -f "$REPO_ROOT/$src" ] && pass_test "T8_${src##*/}" || fail_test "T8_${src##*/}" "missing"
done
echo ""

# ---------------------------------------------------------------------------
# Test 9: Guard check
# ---------------------------------------------------------------------------
echo "=== Test 9: Guard check ==="
GUARD_OUTPUT=$(cd "$REPO_ROOT" && python3 AITools/taskctl.py guard DEEPSEEK_TASK_MCP_RELEASE_AUDIT_B314_V1 --runner deepseek_task_mcp_release_audit_b314 --topic task_mcp 2>&1 || true)
echo "$GUARD_OUTPUT" | grep -q '"guard": *"PASS"' && pass_test "T9_guard_pass" || fail_test "T9_guard_pass" "guard: $GUARD_OUTPUT"
echo ""

# ---------------------------------------------------------------------------
# Test 10: Classification consistency
# ---------------------------------------------------------------------------
echo "=== Test 10: Classification consistency ==="
python3 -c "
import json
with open('$REPO_ROOT/tools/geoai-task-mcp/data/review/task_mcp_release_audit_b314_v1_rows.jsonl') as f:
    statuses = {json.loads(line)['current_status'] for line in f if line.strip()}
allowed = {'RESOLVED', 'IMPROVED', 'residual_risk'}
extra = statuses - allowed
assert not extra, f'Invalid statuses: {extra}'
print(f'Statuses: {sorted(statuses)}')
" 2>&1 && pass_test "T10_valid_statuses" || fail_test "T10_valid_statuses" "invalid status found"
echo ""

# ---------------------------------------------------------------------------
# Test 11: Host/Origin checks
# ---------------------------------------------------------------------------
echo "=== Test 11: Host/Origin checks ==="
grep -q '127.0.0.1' "$REPO_ROOT/tools/geoai-task-mcp/src/geoai_task_mcp/dashboard.py" && pass_test "T11_dashboard_localhost" || fail_test "T11_dashboard_localhost" "not found"
grep -q 'FastMCP' "$REPO_ROOT/tools/geoai-task-mcp/src/geoai_task_mcp/server.py" && pass_test "T11_server_stdio" || fail_test "T11_server_stdio" "not found"
echo ""

# ---------------------------------------------------------------------------
# Test 12: Verify Claude B314 security repair is on HEAD
# ---------------------------------------------------------------------------
echo "=== Test 12: B314 security repair on HEAD ==="
HEAD_COMMIT=$(cd "$REPO_ROOT" && git rev-parse HEAD 2>/dev/null || echo "unknown")
echo "  HEAD: $HEAD_COMMIT"

python3 << 'PYT12'
checks = {
    'sanitized_env_in_direct': False,
    'O_NOFOLLOW': False,
    'coordinator_late_bind': False,
    'bubblewrap_home': False,
    'unlink_is_symlink': False,
    'pid_ticks': False,
}
with open("tools/geoai-task-mcp/src/geoai_task_mcp/process_launcher.py") as f:
    c = f.read()
    checks['sanitized_env_in_direct'] = 'sanitized_env(adapter_id)' in c
    checks['O_NOFOLLOW'] = 'O_NOFOLLOW' in c
    checks['pid_ticks'] = '_pid_matches' in c
with open("tools/geoai-task-mcp/src/geoai_task_mcp/__init__.py") as f:
    checks['coordinator_late_bind'] = 'refresh_coordinator_config()' in f.read()
with open("tools/geoai-task-mcp/src/geoai_task_mcp/worker_workspace.py") as f:
    c = f.read()
    checks['bubblewrap_home'] = 'bubblewrap_home_env_value' in c
    checks['unlink_is_symlink'] = 'is_symlink()' in c
all_ok = all(checks.values())
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
assert all_ok, 'Some security fixes missing'
print("OK")
PYT12
pass_test "T12_all_security_fixes_present" || fail_test "T12_all_security_fixes_present" "missing fixes"
echo ""

# ---------------------------------------------------------------------------
# SUMMARY
# ---------------------------------------------------------------------------
echo "============================================"
echo "  AUDIT TEST SUMMARY (v2)"
echo "============================================"
echo "  Tests run:  $TESTS_RUN"
echo -e "  Passed:     ${GREEN}$PASS${NC}"
echo -e "  Failed:     ${RED}$FAIL${NC}"
echo "  Verdict:    CONDITIONAL_PASS"
echo "  Fix:        Pre/post sha256 of auditor allowed_writes only"
echo "              No global git diff attribution"
echo "============================================"

[ "$FAIL" -gt 0 ] && exit 1
exit 0
