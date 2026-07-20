#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_unified_contract_manifest_selftest_b111_v1.sh
# CLAUDE_TASK_MCP_UNIFIED_CONTRACT_MANIFEST_SELFTEST_B111_V1
#
# Read-only manifest-regeneration self-test for the B110 unified MCP contract
# freeze manifest. It runs the two freeze harnesses FRESH into a mktemp dir,
# hands their outputs to mcp_unified_contract_manifest_selftest.py, which
# REGENERATES the whole manifest (fresh fingerprints + frozen scaffold) and
# asserts it is BYTE-IDENTICAL to the committed B110 manifest and that the
# committed manifest_fingerprint recomputes from its canonical binding.
#
# Teeth: after the positive pass, TWO negative injections tamper a copy of the
# committed manifest IN mktemp and assert the self-test exits non-zero:
#   (neg1) a binding fingerprint  -> caught by byte-drift AND fp mismatch;
#   (neg2) a scaffold field (fp-uncovered) -> caught by byte-drift ALONE,
#          proving byte-identity has teeth beyond the binding-only fingerprint.
#
# MEASUREMENT HONESTY: tools/geoai-task-mcp is its OWN (nested) git repo, so the
# B110 manifest is committed in THAT repo's HEAD (not the parent repo's). The
# comparison target is pinned to the committed HEAD blob (immune to a
# concurrently-dirty working tree): nested-repo HEAD first, then parent-repo
# HEAD, and only if neither has it a ONE-TIME working-tree snapshot copied into
# private tmp (committed_source=working_tree_snapshot). A before/after sha256 of
# the on-disk manifest proves the test never mutates it.
#
# READ-ONLY: regenerates ONLY inside mktemp; mutates no tracked file; launches
# no agent/model/server; makes no network call. Isolation: all scratch in a
# private mktemp dir, cleaned in a trap. Single-process is sufficient here (the
# two freeze harnesses are fast and run sequentially); no internal parallel
# fan-out is needed, so no JOBS pool.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
SELFTEST="$MCPROOT/tests/mcp_unified_contract_manifest_selftest.py"
B108="$MCPROOT/tests/mcp_client_smoke_contract_freeze.py"
B109="$MCPROOT/tests/mcp_readonly_result_schema_freeze.py"
MANIFEST_REL="tools/geoai-task-mcp/eval/mcp_unified_contract_freeze_manifest_b110_v1.json"
MANIFEST="$ROOT/$MANIFEST_REL"
EVAL_ARTIFACT="$MCPROOT/eval/mcp_unified_contract_manifest_selftest_b111_v1.json"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/geoai_b111_manifest_selftest.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"

echo "=== MCP Unified Contract Manifest Self-Test B111 v1 ==="
echo "GEOAI_REPO=$GEOAI_REPO"
echo "TMP=$TMP"

# --- 0. self-test script holds no launch/exec/shell code (defense in depth) --
for pat in "subprocess" "os.system" "os.popen" "os.exec" "os.fork" "os.spawn" "Popen(" "shell=True" "pty.spawn"; do
    if grep -Fq -- "$pat" "$SELFTEST"; then
        echo "FAIL: forbidden launch pattern '$pat' found in self-test script"
        exit 1
    fi
done
echo "self-test script: no launch/exec/shell code: OK"

# --- 1. read-only proof: snapshot on-disk manifest + tracked eval sha256 -----
sha256_of () { sha256sum "$1" | awk '{print $1}'; }
MANIFEST_SHA_BEFORE="$(sha256_of "$MANIFEST")"
EVAL_SHA_BEFORE="none"; [ -f "$EVAL_ARTIFACT" ] && EVAL_SHA_BEFORE="$(sha256_of "$EVAL_ARTIFACT")"

# --- 2. pin the comparison target to the committed HEAD blob -----------------
# tools/geoai-task-mcp is a nested git repo -> pin to ITS HEAD first; then the
# parent repo (in case the subsystem is later vendored in); else a one-time
# working-tree snapshot. Pinning to a committed blob is immune to a
# concurrently-dirty working tree.
COMMITTED="$TMP/committed_manifest.json"
MANIFEST_MCP_REL="eval/mcp_unified_contract_freeze_manifest_b110_v1.json"
if git -C "$MCPROOT" cat-file -e "HEAD:$MANIFEST_MCP_REL" 2>/dev/null; then
    git -C "$MCPROOT" show "HEAD:$MANIFEST_MCP_REL" > "$COMMITTED"
    CSRC="git_head"
elif git -C "$ROOT" cat-file -e "HEAD:$MANIFEST_REL" 2>/dev/null; then
    git -C "$ROOT" show "HEAD:$MANIFEST_REL" > "$COMMITTED"
    CSRC="git_head"
else
    cp "$MANIFEST" "$COMMITTED"
    CSRC="working_tree_snapshot"
fi
echo "committed source: $CSRC (sha256=$(sha256_of "$COMMITTED"))"

# --- 2b. REGRESSION (B112 head-pin): once the tools/geoai-task-mcp subsystem is
# tracked with the B110 manifest committed in HEAD, the comparison target MUST be
# pinned to the committed HEAD blob (committed_source=git_head), NEVER a
# working_tree_snapshot. This asserts the head-pin does not silently regress to a
# concurrently-dirty working-tree read now that the subsystem is committed. It is
# a no-op (skipped) only while the subsystem is still untracked in both repos.
if git -C "$MCPROOT" cat-file -e "HEAD:$MANIFEST_MCP_REL" 2>/dev/null \
   || git -C "$ROOT" cat-file -e "HEAD:$MANIFEST_REL" 2>/dev/null; then
    if [ "$CSRC" != "git_head" ]; then
        echo "FAIL[b112]: manifest committed in HEAD but committed_source=$CSRC (expected git_head)"
        exit 1
    fi
    echo "regression[b112]: manifest committed in HEAD -> committed_source pinned to git_head: OK"
fi

# --- 3. run BOTH freeze harnesses FRESH -------------------------------------
R108="$TMP/b108.json"
R109="$TMP/b109.json"
python3 "$B108" --out "$R108" >"$TMP/b108.log" 2>&1 || { echo "FAIL: B108 harness"; cat "$TMP/b108.log"; exit 1; }
python3 "$B109" --out "$R109" >"$TMP/b109.log" 2>&1 || { echo "FAIL: B109 harness"; cat "$TMP/b109.log"; exit 1; }
echo "both freeze harnesses ran fresh: OK"

# --- 4. POSITIVE: regenerated manifest must be byte-identical (exit 0) --------
VERDICT="$TMP/verdict_positive.json"
python3 "$SELFTEST" --b108 "$R108" --b109 "$R109" \
    --committed "$COMMITTED" --committed-source "$CSRC" --out "$VERDICT"
echo "self-test positive pass: OK"

python3 - "$VERDICT" <<'PYEOF'
import json, sys
v = json.load(open(sys.argv[1]))
assert v["ok"] is True, v.get("failing_check")
assert v["byte_identical"] is True, "regenerated manifest not byte-identical"
assert v["manifest_fingerprint_matches"] is True, "committed fp does not recompute"
assert v["failing_check"] is None, v["failing_check"]
assert v["drift_count"] == 0, v["drift"]
assert v["readonly_tool_count"] == 11, v["readonly_tool_count"]
assert v["write_gated_tool_count"] == 4, v["write_gated_tool_count"]
assert v["committed_manifest_fingerprint"] == v["regenerated_manifest_fingerprint"], "fp mismatch"
assert v["committed_manifest_fingerprint"] == v["committed_binding_recomputed_fingerprint"], "binding fp mismatch"
assert v["committed_sha256"] == v["regenerated_sha256"], "sha256 mismatch"
for k, val in v["authority_flags"].items():
    assert val is False, f"authority flag {k} not false"
print("PASS: regenerated == committed (byte-identical), fp recomputes")
print("  readonly tools bound :", v["readonly_tool_count"])
print("  manifest fingerprint :", v["committed_manifest_fingerprint"][:16], "...")
print("  committed source     :", v["source_provenance"]["committed_source"])
PYEOF

# --- 5. NEGATIVE: tamper a copy in tmp; self-test must FAIL non-zero ----------
tamper_and_expect_fail () {
    local label="$1"; local py="$2"; local expect_byte="$3"; local expect_fp="$4"
    local m="$TMP/tamper_${label}.json"
    cp "$COMMITTED" "$m"
    python3 - "$m" <<PYEOF
import json, sys
p = sys.argv[1]
d = json.load(open(p))
$py
json.dump(d, open(p, "w"), indent=2, sort_keys=True)
open(p, "a").write("\n")
PYEOF
    local mv="$TMP/verdict_${label}.json"
    local rc=0
    python3 "$SELFTEST" --b108 "$R108" --b109 "$R109" \
        --committed "$m" --committed-source "working_tree_snapshot" --out "$mv" || rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "FAIL[$label]: self-test passed on tampered manifest (expected non-zero exit)"
        exit 1
    fi
    python3 - "$mv" "$expect_byte" "$expect_fp" <<'PYEOF'
import json, sys
v = json.load(open(sys.argv[1]))
want_byte = sys.argv[2] == "1"
want_fp = sys.argv[3] == "1"
assert v["ok"] is False, "verdict.ok must be False on tamper"
assert v["byte_identical"] is want_byte, ("byte_identical", v["byte_identical"], want_byte)
assert v["manifest_fingerprint_matches"] is want_fp, ("fp_matches", v["manifest_fingerprint_matches"], want_fp)
assert v["drift_count"] >= 1, v
PYEOF
    echo "negative[$label]: self-test correctly FAILED (exit $rc, byte_id=$expect_byte, fp_ok=$expect_fp): OK"
}

# neg1: binding fingerprint tamper -> byte drift AND fp mismatch (both teeth)
tamper_and_expect_fail "binding_fp" \
    'd["per_readonly_tool"]["geoai_task_health"]["input_schema_fp"] = "deadbeef"' \
    0 0

# neg2: scaffold field tamper (NOT covered by the binding fingerprint)
#       -> byte drift ONLY; fp still recomputes. Proves byte-identity adds teeth
#       BEYOND the fingerprint (a stale/hand-edited scaffold is still caught).
tamper_and_expect_fail "scaffold" \
    'd["purpose"] = "TAMPERED purpose string"' \
    0 1

echo "all negative tamper injections correctly rejected: OK"

# --- 6. read-only proof: on-disk manifest + tracked eval unchanged -----------
MANIFEST_SHA_AFTER="$(sha256_of "$MANIFEST")"
EVAL_SHA_AFTER="none"; [ -f "$EVAL_ARTIFACT" ] && EVAL_SHA_AFTER="$(sha256_of "$EVAL_ARTIFACT")"
if [ "$MANIFEST_SHA_BEFORE" != "$MANIFEST_SHA_AFTER" ]; then
    echo "FAIL: tracked manifest was modified during the test (read-only violated)"
    exit 1
fi
if [ "$EVAL_SHA_BEFORE" != "$EVAL_SHA_AFTER" ]; then
    echo "FAIL: tracked eval artifact was rewritten during the test"
    exit 1
fi
echo "read-only proof: committed manifest + eval artifact byte-unchanged: OK"

# --- 7. parent task queue intact (no queue mutation) -------------------------
echo ""
echo "=== Parent task queue integrity ==="
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
