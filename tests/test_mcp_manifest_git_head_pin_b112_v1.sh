#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_manifest_git_head_pin_b112_v1.sh
# CLAUDE_TASK_MCP_MANIFEST_GIT_HEAD_PIN_B112_V1
#
# Regression guard: now that the tools/geoai-task-mcp subsystem is COMMITTED
# (nested git repo, B110 manifest in HEAD), the B111 manifest-regeneration
# self-test MUST measure against the git_head blob, NOT a working_tree_snapshot.
# This locks in committed_source=git_head so the comparison target can never
# silently regress to a concurrently-dirty working-tree read.
#
# What it proves (all read-only, no launch, no mutation of any tracked file):
#   1. PRECONDITION  the subsystem is a git repo with the B110 manifest in HEAD.
#   2. HEAD-PIN      the same resolution the B111 .sh uses yields git_head, and
#                    the B111 self-test regenerates BYTE-IDENTICAL to the HEAD
#                    blob with committed_source=git_head recorded in the verdict.
#   3. IMMUNITY TEETH in a private mktemp git repo, committing the manifest then
#                    DIRTYING its working copy leaves `git show HEAD:<rel>` (what
#                    the pin reads) byte-clean == committed, while the on-disk
#                    working copy is the tampered bytes -> the pin measures HEAD,
#                    not the working tree.
#   4. REGRESSION TEETH the git_head assertion itself has teeth: a simulated
#                    broken resolver that returns working_tree_snapshot while the
#                    manifest IS committed in HEAD is REJECTED (non-zero).
#   5. READ-ONLY     before/after sha256 of the tracked manifest + tracked eval
#                    artifacts are unchanged; parent taskctl queue intact.
#
# Isolation: all scratch in a private mktemp dir, cleaned in a trap. Single
# process is sufficient (the one freeze-driven self-test is fast, run once); no
# shared fixed-path writes, so this test is parallel-safe as-is.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
B111_SH="$MCPROOT/tests/test_mcp_unified_contract_manifest_selftest_b111_v1.sh"
SELFTEST="$MCPROOT/tests/mcp_unified_contract_manifest_selftest.py"
B108="$MCPROOT/tests/mcp_client_smoke_contract_freeze.py"
B109="$MCPROOT/tests/mcp_readonly_result_schema_freeze.py"
MANIFEST_MCP_REL="eval/mcp_unified_contract_freeze_manifest_b110_v1.json"
MANIFEST_REL="tools/geoai-task-mcp/$MANIFEST_MCP_REL"
MANIFEST="$ROOT/$MANIFEST_REL"
B111_EVAL="$MCPROOT/eval/mcp_unified_contract_manifest_selftest_b111_v1.json"
B112_EVAL="$MCPROOT/eval/mcp_manifest_git_head_pin_b112_v1.json"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/aiworkhub_b112_head_pin.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

export PYTHONPATH="$MCPROOT/src"
export AIWORKHUB_REPO="$ROOT"

sha256_of () { sha256sum "$1" | awk '{print $1}'; }

echo "=== MCP Manifest Git-HEAD-Pin Regression B112 v1 ==="
echo "AIWORKHUB_REPO=$AIWORKHUB_REPO"
echo "TMP=$TMP"

# --- read-only proof: snapshot tracked artifacts BEFORE anything -------------
MANIFEST_SHA_BEFORE="$(sha256_of "$MANIFEST")"
B111_EVAL_SHA_BEFORE="none"; [ -f "$B111_EVAL" ] && B111_EVAL_SHA_BEFORE="$(sha256_of "$B111_EVAL")"
B112_EVAL_SHA_BEFORE="none"; [ -f "$B112_EVAL" ] && B112_EVAL_SHA_BEFORE="$(sha256_of "$B112_EVAL")"

# --- 1. PRECONDITION: subsystem is a git repo with manifest committed in HEAD -
if ! git -C "$MCPROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "FAIL: $MCPROOT is not inside a git work tree (subsystem not tracked)"
    exit 1
fi
if ! git -C "$MCPROOT" cat-file -e "HEAD:$MANIFEST_MCP_REL" 2>/dev/null; then
    echo "FAIL: B110 manifest is NOT committed in the subsystem HEAD"
    exit 1
fi
HEAD_COMMIT="$(git -C "$MCPROOT" rev-parse HEAD)"
echo "precondition: subsystem git repo with B110 manifest in HEAD ($HEAD_COMMIT): OK"

# --- 2. HEAD-PIN: replicate the B111 resolution -> must be git_head ----------
# (nested-repo HEAD first, then parent-repo HEAD, else working_tree_snapshot)
resolve_csrc () {
    # echoes: "<csrc> <path-to-committed-copy>"
    local out="$1"
    if git -C "$MCPROOT" cat-file -e "HEAD:$MANIFEST_MCP_REL" 2>/dev/null; then
        git -C "$MCPROOT" show "HEAD:$MANIFEST_MCP_REL" > "$out"; echo "git_head"
    elif git -C "$ROOT" cat-file -e "HEAD:$MANIFEST_REL" 2>/dev/null; then
        git -C "$ROOT" show "HEAD:$MANIFEST_REL" > "$out"; echo "git_head"
    else
        cp "$MANIFEST" "$out"; echo "working_tree_snapshot"
    fi
}
HEAD_COPY="$TMP/committed_head.json"
CSRC="$(resolve_csrc "$HEAD_COPY")"
HEAD_BLOB_SHA="$(sha256_of "$HEAD_COPY")"
if [ "$CSRC" != "git_head" ]; then
    echo "FAIL: resolved committed_source=$CSRC (expected git_head)"
    exit 1
fi
echo "head-pin: committed_source=git_head (HEAD blob sha256=$HEAD_BLOB_SHA): OK"

# regenerate against the HEAD-pinned blob -> must be byte-identical -----------
R108="$TMP/b108.json"; R109="$TMP/b109.json"; VERDICT="$TMP/verdict_head.json"
python3 "$B108" --out "$R108" >"$TMP/b108.log" 2>&1 || { echo "FAIL: B108 harness"; cat "$TMP/b108.log"; exit 1; }
python3 "$B109" --out "$R109" >"$TMP/b109.log" 2>&1 || { echo "FAIL: B109 harness"; cat "$TMP/b109.log"; exit 1; }
python3 "$SELFTEST" --b108 "$R108" --b109 "$R109" \
    --committed "$HEAD_COPY" --committed-source "$CSRC" --out "$VERDICT"

python3 - "$VERDICT" "git_head" <<'PYEOF'
import json, sys
v = json.load(open(sys.argv[1]))
want_src = sys.argv[2]
assert v["ok"] is True, v.get("failing_check")
assert v["byte_identical"] is True, "regenerated manifest not byte-identical to HEAD blob"
assert v["manifest_fingerprint_matches"] is True, "committed fp does not recompute"
assert v["drift_count"] == 0, v["drift"]
assert v["committed_sha256"] == v["regenerated_sha256"], "sha256 mismatch vs HEAD blob"
assert v["committed_manifest_fingerprint"] == v["regenerated_manifest_fingerprint"], "fp mismatch"
assert v["committed_manifest_fingerprint"] == v["committed_binding_recomputed_fingerprint"], "binding fp mismatch"
assert v["source_provenance"]["committed_source"] == want_src, \
    ("committed_source", v["source_provenance"]["committed_source"], want_src)
for k, val in v["authority_flags"].items():
    assert val is False, f"authority flag {k} not false"
print("PASS: regenerated == HEAD blob (byte-identical), committed_source=git_head")
PYEOF

# --- 3. IMMUNITY TEETH: HEAD blob is immune to a dirty working tree ----------
# Build a private git repo, commit the manifest, then DIRTY the working copy.
# The pin reads `git show HEAD:<rel>` -> must still be the clean committed bytes.
IMM="$TMP/immune_repo"
mkdir -p "$IMM/eval"
git -C "$IMM" init -q
git -C "$IMM" config user.email "b112@local"
git -C "$IMM" config user.name "b112"
cp "$HEAD_COPY" "$IMM/$MANIFEST_MCP_REL"
git -C "$IMM" add "$MANIFEST_MCP_REL"
git -C "$IMM" commit -q -m "commit manifest for immunity teeth"
COMMITTED_CLEAN_SHA="$(git -C "$IMM" show "HEAD:$MANIFEST_MCP_REL" | sha256sum | awk '{print $1}')"
# now DIRTY the working-tree copy (append junk) -- HEAD is untouched
printf '\nTAMPERED_WORKING_TREE_LINE\n' >> "$IMM/$MANIFEST_MCP_REL"
DIRTY_WT_SHA="$(sha256_of "$IMM/$MANIFEST_MCP_REL")"
HEAD_AFTER_DIRTY_SHA="$(git -C "$IMM" show "HEAD:$MANIFEST_MCP_REL" | sha256sum | awk '{print $1}')"
if [ "$HEAD_AFTER_DIRTY_SHA" != "$COMMITTED_CLEAN_SHA" ]; then
    echo "FAIL: HEAD blob changed after dirtying the working tree (pin not immune)"
    exit 1
fi
if [ "$HEAD_AFTER_DIRTY_SHA" != "$HEAD_BLOB_SHA" ]; then
    echo "FAIL: committed HEAD blob sha differs from the real subsystem HEAD blob"
    exit 1
fi
if [ "$DIRTY_WT_SHA" = "$HEAD_AFTER_DIRTY_SHA" ]; then
    echo "FAIL: dirtied working tree matched HEAD blob (dirtying had no effect)"
    exit 1
fi
echo "immunity teeth: dirty working tree ($DIRTY_WT_SHA) but HEAD-pin blob stays clean ($HEAD_AFTER_DIRTY_SHA): OK"

# --- 4. REGRESSION TEETH: the git_head assertion rejects a downgrade ---------
# assert_head_pin: when the manifest IS committed in HEAD, committed_source MUST
# be git_head; a resolver that returns working_tree_snapshot is a regression.
assert_head_pin () {
    local repo="$1" rel="$2" csrc="$3"
    if git -C "$repo" cat-file -e "HEAD:$rel" 2>/dev/null; then
        [ "$csrc" = "git_head" ] || return 1
    fi
    return 0
}
# real subsystem, correctly resolved git_head -> assertion passes
if ! assert_head_pin "$MCPROOT" "$MANIFEST_MCP_REL" "git_head"; then
    echo "FAIL: assert_head_pin rejected the correct git_head resolution"
    exit 1
fi
# simulated broken resolver: manifest committed in HEAD but downgraded to
# working_tree_snapshot -> assertion MUST reject (return non-zero)
rc=0
assert_head_pin "$IMM" "$MANIFEST_MCP_REL" "working_tree_snapshot" || rc=$?
if [ "$rc" -eq 0 ]; then
    echo "FAIL: assert_head_pin ACCEPTED a working_tree_snapshot downgrade (no teeth)"
    exit 1
fi
echo "regression teeth: git_head assertion rejects a working_tree_snapshot downgrade (rc=$rc): OK"

# --- 5. READ-ONLY proof: tracked artifacts byte-unchanged -------------------
MANIFEST_SHA_AFTER="$(sha256_of "$MANIFEST")"
B111_EVAL_SHA_AFTER="none"; [ -f "$B111_EVAL" ] && B111_EVAL_SHA_AFTER="$(sha256_of "$B111_EVAL")"
B112_EVAL_SHA_AFTER="none"; [ -f "$B112_EVAL" ] && B112_EVAL_SHA_AFTER="$(sha256_of "$B112_EVAL")"
[ "$MANIFEST_SHA_BEFORE" = "$MANIFEST_SHA_AFTER" ] || { echo "FAIL: tracked manifest modified (read-only violated)"; exit 1; }
[ "$B111_EVAL_SHA_BEFORE" = "$B111_EVAL_SHA_AFTER" ] || { echo "FAIL: B111 eval artifact modified"; exit 1; }
[ "$B112_EVAL_SHA_BEFORE" = "$B112_EVAL_SHA_AFTER" ] || { echo "FAIL: B112 eval artifact modified"; exit 1; }
echo "read-only proof: manifest + B111/B112 eval artifacts byte-unchanged: OK"

# --- 6. cross-check the committed B112 eval evidence (if present) ------------
if [ -f "$B112_EVAL" ]; then
    python3 - "$B112_EVAL" "$HEAD_BLOB_SHA" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
head_sha = sys.argv[2]
assert d["committed_source"] == "git_head", d["committed_source"]
assert d["byte_identical"] is True, d
assert d["manifest_fingerprint_matches"] is True, d
assert d["committed_sha256"] == head_sha, ("eval committed_sha256 stale vs HEAD blob", d["committed_sha256"], head_sha)
for k, val in d["authority_flags"].items():
    assert val is False, f"authority flag {k} not false"
print("eval evidence: committed_source=git_head, sha256 matches live HEAD blob: OK")
PYEOF
fi

# --- 8. B113 STALE-COMMIT GUARD: committed eval evidence must track HEAD blob -
# CLAUDE_TASK_MCP_MANIFEST_HEAD_PIN_CI_STALE_COMMIT_GUARD_B113_V1
# If the B110 manifest is re-committed (HEAD blob sha changes) but the committed
# B112/B113 head-pin eval evidence is NOT refreshed, the recorded committed_sha256
# goes STALE and would otherwise pass silently. This guard fails LOUDLY on that
# divergence. Crucially it compares the eval's recorded sha against the git-HEAD
# blob sha (`git show HEAD:<rel>`), NOT the working-tree file, so a concurrently
# additively-patched (dirty) working tree does NOT cause a false FAIL -- the
# manifest/registry-guard git-clean trap. All scratch stays in $TMP (isolated).
echo ""
echo "=== B113 stale-commit guard (eval evidence tracks live HEAD blob) ==="
B113_EVAL="$MCPROOT/eval/mcp_manifest_head_pin_ci_stale_commit_guard_b113_v1.json"

# git-HEAD blob sha of a manifest rel in a repo (fails if not committed in HEAD)
head_blob_sha () {
    local repo="$1" rel="$2"
    git -C "$repo" cat-file -e "HEAD:$rel" 2>/dev/null || return 1
    git -C "$repo" show "HEAD:$rel" | sha256sum | awk '{print $1}'
}

# stale-guard: assert an eval JSON's committed_sha256 == the given HEAD blob sha
# (plus head-pin invariants). Exits non-zero on ANY divergence / stale evidence.
assert_eval_tracks_head () {
    local eval_json="$1" expect_head_sha="$2"
    python3 - "$eval_json" "$expect_head_sha" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
head_sha = sys.argv[2]
assert d.get("committed_source") == "git_head", ("committed_source", d.get("committed_source"))
assert d.get("byte_identical") is True, "eval not byte_identical"
assert d.get("manifest_fingerprint_matches") is True, "eval fp does not match"
cs = d.get("committed_sha256")
assert cs == head_sha, ("STALE: eval committed_sha256 != live HEAD blob", cs, head_sha)
for k, val in d.get("authority_flags", {}).items():
    assert val is False, f"authority flag {k} not false"
PYEOF
}

# live HEAD blob sha the guard measures against (git_head, working-tree-immune)
LIVE_HEAD_SHA="$(head_blob_sha "$MCPROOT" "$MANIFEST_MCP_REL")" \
    || { echo "FAIL: B110 manifest not committed in nested HEAD (cannot pin)"; exit 1; }
[ "$LIVE_HEAD_SHA" = "$HEAD_BLOB_SHA" ] || { echo "FAIL: live HEAD blob sha drift within run"; exit 1; }

# 8a. REAL non-stale evidence tracks the live HEAD blob -> PASS
if ! assert_eval_tracks_head "$B112_EVAL" "$LIVE_HEAD_SHA"; then
    echo "FAIL: committed B112 eval evidence is STALE vs live HEAD blob"
    exit 1
fi
echo "8a: committed B112 eval committed_sha256 == live HEAD blob ($LIVE_HEAD_SHA): OK"
if [ -f "$B113_EVAL" ]; then
    if ! assert_eval_tracks_head "$B113_EVAL" "$LIVE_HEAD_SHA"; then
        echo "FAIL: committed B113 eval evidence is STALE vs live HEAD blob"
        exit 1
    fi
    echo "8a: committed B113 eval committed_sha256 == live HEAD blob: OK"
fi

# 8b. DIVERGENCE TEETH (controlled temp-copy): a stale eval whose committed_sha256
# is frozen to an old commit while HEAD advanced MUST be rejected non-zero.
STALE_EVAL="$TMP/stale_eval.json"
python3 - "$B112_EVAL" "$STALE_EVAL" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
d["committed_sha256"] = "0" * 64  # simulate a manifest recommit not reflected here
json.dump(d, open(sys.argv[2], "w"), indent=2)
PYEOF
rc=0
assert_eval_tracks_head "$STALE_EVAL" "$LIVE_HEAD_SHA" >/dev/null 2>&1 || rc=$?
if [ "$rc" -eq 0 ]; then
    echo "FAIL: stale-guard ACCEPTED a stale eval (sha frozen != HEAD blob) -> no teeth"
    exit 1
fi
echo "8b (divergence teeth): stale eval (committed_sha256 != HEAD blob) rejected (rc=$rc): OK"

# 8c. GIT-CLEAN FALSE-FAIL TRAP: an additively-patched (dirty) working tree must
# NOT cause a false FAIL. The guard reads `git show HEAD:<rel>`, so the HEAD blob
# sha (== eval evidence) is unchanged even though the working-tree file differs.
TRAP="$TMP/trap_repo"
mkdir -p "$TRAP/eval"
git -C "$TRAP" init -q
git -C "$TRAP" config user.email "b113@local"
git -C "$TRAP" config user.name "b113"
cp "$HEAD_COPY" "$TRAP/$MANIFEST_MCP_REL"          # exact clean committed bytes
git -C "$TRAP" add "$MANIFEST_MCP_REL"
git -C "$TRAP" commit -q -m "commit manifest for stale-guard trap"
TRAP_HEAD_SHA="$(head_blob_sha "$TRAP" "$MANIFEST_MCP_REL")"
[ "$TRAP_HEAD_SHA" = "$LIVE_HEAD_SHA" ] || { echo "FAIL: trap repo HEAD blob != live HEAD blob"; exit 1; }
# committed eval evidence for this repo records the clean HEAD blob sha
TRAP_EVAL="$TMP/trap_eval.json"
python3 - "$B112_EVAL" "$TRAP_EVAL" "$TRAP_HEAD_SHA" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
d["committed_sha256"] = sys.argv[3]
json.dump(d, open(sys.argv[2], "w"), indent=2)
PYEOF
# ADDITIVELY patch (dirty) the working-tree manifest -- like a coordinator patch
printf '\nADDITIVE_COORDINATOR_PATCH_LINE\n' >> "$TRAP/$MANIFEST_MCP_REL"
DIRTY_WT_SHA="$(sha256_of "$TRAP/$MANIFEST_MCP_REL")"
[ "$DIRTY_WT_SHA" != "$TRAP_HEAD_SHA" ] || { echo "FAIL: additive patch did not change working tree"; exit 1; }
# HEAD blob (what the guard reads) is still clean == eval evidence -> PASS
if ! assert_eval_tracks_head "$TRAP_EVAL" "$(head_blob_sha "$TRAP" "$MANIFEST_MCP_REL")"; then
    echo "FAIL: stale-guard FALSE-FAILED on an additively-patched dirty working tree"
    exit 1
fi
echo "8c (git-clean false-FAIL trap): dirty working tree ($DIRTY_WT_SHA) but HEAD-blob guard PASSES (a naive git-clean check would false-FAIL): OK"

# 8d. read-only re-assert after B113 sections (all scratch was in $TMP)
MANIFEST_SHA_B113="$(sha256_of "$MANIFEST")"
[ "$MANIFEST_SHA_BEFORE" = "$MANIFEST_SHA_B113" ] || { echo "FAIL: tracked manifest modified during B113 guard"; exit 1; }
B112_EVAL_SHA_B113="none"; [ -f "$B112_EVAL" ] && B112_EVAL_SHA_B113="$(sha256_of "$B112_EVAL")"
[ "$B112_EVAL_SHA_BEFORE" = "$B112_EVAL_SHA_B113" ] || { echo "FAIL: B112 eval modified during B113 guard"; exit 1; }
echo "8d: tracked manifest + B112 eval byte-unchanged after B113 guard: OK"

# --- 9. B114 AUTOREFRESH ATTESTATION: whole head-pin eval SET shares one sha -
# CLAUDE_TASK_MCP_MANIFEST_HEAD_PIN_AUTOREFRESH_ATTEST_B114_V1
# B113 checked each committed eval individually. B114 attests the SET invariant:
# when the B110 manifest HEAD blob legitimately advances, a single canonical
# refresh path must update ALL committed head-pin evals (B112 + B113 + B114) in
# one atomic step so NO committed eval can lag the others. This attests that the
# whole set is UNANIMOUS on one committed_sha256 == the live HEAD blob, recomputes
# a deterministic attestation signature over the set, and proves that a set in
# which any single eval lags is rejected non-zero. The manifest blob is measured
# via `git show HEAD:<rel>` (working-tree-immune); all scratch stays in $TMP.
echo ""
echo "=== B114 autorefresh attestation (head-pin eval SET shares one live HEAD sha) ==="
B114_EVAL="$MCPROOT/eval/mcp_manifest_head_pin_autorefresh_attest_b114_v1.json"
B114_EVAL_SHA_BEFORE="none"; [ -f "$B114_EVAL" ] && B114_EVAL_SHA_BEFORE="$(sha256_of "$B114_EVAL")"

# canonical head-pin eval set (rel paths within the MCP subsystem)
HEADPIN_EVALS=(
    "eval/mcp_manifest_git_head_pin_b112_v1.json"
    "eval/mcp_manifest_head_pin_ci_stale_commit_guard_b113_v1.json"
    "eval/mcp_manifest_head_pin_autorefresh_attest_b114_v1.json"
)

# attest_eval_set <expected_head_sha> <basedir>
# Reads committed_sha256 from every head-pin eval under <basedir>, asserts the
# whole set is UNANIMOUS and equals <expected_head_sha> (the live HEAD blob),
# recomputes the deterministic attestation signature, and asserts any eval's
# recorded attestation_signature round-trips. Exits non-zero on ANY divergence.
attest_eval_set () {
    local head_sha="$1" base="$2"
    local args=()
    local rel
    for rel in "${HEADPIN_EVALS[@]}"; do
        args+=("$rel=$base/$rel")
    done
    python3 - "$head_sha" "${args[@]}" <<'PYEOF'
import json, sys, hashlib
head_sha = sys.argv[1]
pairs = []
recorded_sigs = {}
for spec in sys.argv[2:]:
    rel, path = spec.split("=", 1)
    d = json.load(open(path))
    cs = d.get("committed_sha256")
    assert cs is not None, ("missing committed_sha256", rel)
    pairs.append((rel, cs))
    if "attestation_signature" in d:
        recorded_sigs[rel] = d["attestation_signature"]
# 1. SET UNANIMITY: no committed eval may lag the others
shas = {sha for _, sha in pairs}
assert len(shas) == 1, ("head-pin eval set NOT unanimous (an eval lags)", dict(pairs))
# 2. SET == live HEAD blob (autorefresh actually tracked the new manifest)
only = next(iter(shas))
assert only == head_sha, ("eval set committed_sha256 != live HEAD blob", only, head_sha)
# 3. deterministic attestation signature over the whole set (round-trip)
sig_input = "\n".join(f"{rel}:{sha}" for rel, sha in sorted(pairs))
sig = hashlib.sha256(sig_input.encode()).hexdigest()
for rel, rec in recorded_sigs.items():
    assert rec == sig, ("recorded attestation_signature != recomputed", rel, rec, sig)
print(sig)
PYEOF
}

# 9a. REAL committed eval set (working-tree copies) is unanimous == live HEAD ---
SIG1="$(attest_eval_set "$LIVE_HEAD_SHA" "$MCPROOT")" \
    || { echo "FAIL: head-pin eval SET is not unanimous / not == live HEAD blob"; exit 1; }
echo "9a: all head-pin evals (B112+B113+B114) share committed_sha256 == live HEAD blob; signature=$SIG1: OK"

# 9b. IDEMPOTENCE: re-run the attestation -> identical stable signature --------
SIG2="$(attest_eval_set "$LIVE_HEAD_SHA" "$MCPROOT")" \
    || { echo "FAIL: attestation failed on idempotent re-run"; exit 1; }
[ "$SIG1" = "$SIG2" ] || { echo "FAIL: attestation signature not stable across re-runs ($SIG1 != $SIG2)"; exit 1; }
echo "9b (idempotence): attestation re-run yields identical signature ($SIG2): OK"

# 9c. LAGGING-EVAL TEETH (controlled temp-copy): copy the whole eval set, then
# freeze exactly ONE eval's committed_sha256 to a stale value (simulating a
# manifest recommit whose autorefresh missed that eval). The set is no longer
# unanimous / no longer == live HEAD blob -> MUST be rejected non-zero.
SETDIR="$TMP/eval_set"; mkdir -p "$SETDIR/eval"
for rel in "${HEADPIN_EVALS[@]}"; do cp "$MCPROOT/$rel" "$SETDIR/$rel"; done
# sanity: the untouched temp copy still attests
attest_eval_set "$LIVE_HEAD_SHA" "$SETDIR" >/dev/null \
    || { echo "FAIL: untouched temp-copy eval set failed attestation"; exit 1; }
LAG_REL="eval/mcp_manifest_head_pin_ci_stale_commit_guard_b113_v1.json"
python3 - "$SETDIR/$LAG_REL" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
d["committed_sha256"] = "0" * 64  # this eval did NOT get refreshed -> it lags
json.dump(d, open(sys.argv[1], "w"), indent=2)
PYEOF
rc=0
attest_eval_set "$LIVE_HEAD_SHA" "$SETDIR" >/dev/null 2>&1 || rc=$?
if [ "$rc" -eq 0 ]; then
    echo "FAIL: attestation ACCEPTED a lagging eval set (one eval stale) -> no teeth"
    exit 1
fi
echo "9c (lagging-eval teeth): eval set with one lagging eval rejected (rc=$rc): OK"

# 9d. GIT-CLEAN FALSE-FAIL TRAP: the manifest working tree is additively patched
# (dirty) in a private repo, but the attestation measures `git show HEAD:<rel>`,
# so the live HEAD blob sha (== the unanimous eval set) is unchanged -> PASSES.
ATT="$TMP/attest_repo"; mkdir -p "$ATT/eval"
git -C "$ATT" init -q
git -C "$ATT" config user.email "b114@local"
git -C "$ATT" config user.name "b114"
cp "$HEAD_COPY" "$ATT/$MANIFEST_MCP_REL"
git -C "$ATT" add "$MANIFEST_MCP_REL"
git -C "$ATT" commit -q -m "commit manifest for autorefresh-attest trap"
ATT_HEAD_SHA="$(head_blob_sha "$ATT" "$MANIFEST_MCP_REL")"
[ "$ATT_HEAD_SHA" = "$LIVE_HEAD_SHA" ] || { echo "FAIL: attest trap repo HEAD blob != live HEAD blob"; exit 1; }
printf '\nADDITIVE_COORDINATOR_PATCH_LINE\n' >> "$ATT/$MANIFEST_MCP_REL"
ATT_DIRTY_WT_SHA="$(sha256_of "$ATT/$MANIFEST_MCP_REL")"
[ "$ATT_DIRTY_WT_SHA" != "$ATT_HEAD_SHA" ] || { echo "FAIL: additive patch did not change working tree"; exit 1; }
# the eval set still records the clean HEAD blob; attestation reads git-HEAD -> PASS
attest_eval_set "$(head_blob_sha "$ATT" "$MANIFEST_MCP_REL")" "$MCPROOT" >/dev/null \
    || { echo "FAIL: attestation FALSE-FAILED on an additively-patched dirty manifest working tree"; exit 1; }
echo "9d (git-clean false-FAIL trap): dirty manifest working tree ($ATT_DIRTY_WT_SHA) but HEAD-blob attestation PASSES: OK"

# 9e. read-only re-assert after B114 attestation (all scratch was in $TMP)
MANIFEST_SHA_B114="$(sha256_of "$MANIFEST")"
[ "$MANIFEST_SHA_BEFORE" = "$MANIFEST_SHA_B114" ] || { echo "FAIL: tracked manifest modified during B114 attestation"; exit 1; }
B114_EVAL_SHA_AFTER="none"; [ -f "$B114_EVAL" ] && B114_EVAL_SHA_AFTER="$(sha256_of "$B114_EVAL")"
[ "$B114_EVAL_SHA_BEFORE" = "$B114_EVAL_SHA_AFTER" ] || { echo "FAIL: B114 eval modified during B114 attestation"; exit 1; }
echo "9e: tracked manifest + B114 eval byte-unchanged after B114 attestation: OK"

# 9f. cross-check the committed B114 eval evidence against the recomputed set ---
if [ -f "$B114_EVAL" ]; then
    python3 - "$B114_EVAL" "$LIVE_HEAD_SHA" "$SIG1" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
head_sha, sig = sys.argv[2], sys.argv[3]
assert d["committed_source"] == "git_head", d["committed_source"]
assert d["byte_identical"] is True, d
assert d["manifest_fingerprint_matches"] is True, d
assert d["committed_sha256"] == head_sha, ("B114 committed_sha256 stale vs HEAD blob", d["committed_sha256"], head_sha)
assert d["eval_set_unanimous"] is True, d
assert d["eval_set_matches_head"] is True, d
assert d["attestation_signature"] == sig, ("B114 recorded attestation_signature != recomputed", d["attestation_signature"], sig)
# recorded per-eval map must itself be unanimous == HEAD blob
per = d.get("per_eval_committed_sha256", {})
assert per and set(per.values()) == {head_sha}, ("per_eval map not unanimous == HEAD", per, head_sha)
for k, val in d["authority_flags"].items():
    assert val is False, f"authority flag {k} not false"
print("9f: committed B114 eval evidence matches the recomputed attestation (signature + set): OK")
PYEOF
fi

# --- 7. parent task queue intact (no queue mutation) ------------------------
echo ""
echo "=== Parent task queue integrity ==="
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
