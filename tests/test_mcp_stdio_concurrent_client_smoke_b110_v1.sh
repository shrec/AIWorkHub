#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_stdio_concurrent_client_smoke_b110_v1.sh
# Proves the frozen read-only tool contract v1 holds under CONCURRENCY: two
# independent mcp.client.stdio.stdio_client subprocess sessions are opened to
# the aiworkhub server AT THE SAME TIME (overlap forced by an
# asyncio.Barrier(2)) and driven through real mcp.ClientSession over OS pipes.
# Asserts: both concurrent sessions see the SAME B108 frozen inputSchema
# fingerprints (15 tools: 11 read-only + 4 write-gated), the two sessions agree
# (no drift under concurrency), NO cross-session state bleed (each session's
# pending_for_runner echoes ONLY its own runner; each isolated audit dir stays
# empty + byte-identical), NO queue/audit writes with AIWORKHUB_ALLOW_WRITES
# unset AND =1, ONLY the MCP server subprocess is launched (no agent/model, no
# shell=True), and server.py holds no process-launch code. Determinism is
# proven by running the smoke TWICE and asserting the path-free run_signature is
# byte-identical across runs.
#
# Isolation: each smoke run owns private mktemp audit dirs (per session) and a
# per-run mktemp result path. Parallel-safe; no shared file is mutated.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
SMOKE="$MCPROOT/tests/mcp_stdio_concurrent_client_smoke.py"
SERVER_SRC="$MCPROOT/src/aiworkhub/server.py"

TMPDIR_STATE="$(mktemp -d "${TMPDIR:-/tmp}/aiworkhub_b110_concurrent_smoke.XXXXXX")"
trap 'rm -rf "$TMPDIR_STATE"' EXIT

export PYTHONPATH="$MCPROOT/src"
export AIWORKHUB_REPO="$ROOT"

echo "=== MCP STDIO Concurrent Client Smoke Test B110 v1 ==="
echo "AIWORKHUB_REPO=$AIWORKHUB_REPO"
echo "TMP=$TMPDIR_STATE"

# --- 0. server.py holds no process-launch code (defense in depth) ----------
for pat in "subprocess" "os.system" "os.popen" "os.exec" "os.fork" "os.spawn" "Popen(" "shell=True" "pty.spawn"; do
    if grep -Fq -- "$pat" "$SERVER_SRC"; then
        echo "FAIL: forbidden launch pattern '$pat' found in server.py"
        exit 1
    fi
done
echo "server.py: no launch/exec/shell code: OK"

# --- 0b. smoke drives REAL concurrent stdio transport, no shell -------------
for req in "StdioServerParameters" "stdio_client" "ClientSession" "asyncio.Barrier" "asyncio.gather"; do
    if ! grep -Fq -- "$req" "$SMOKE"; then
        echo "FAIL: smoke missing concurrent-stdio marker '$req'"
        exit 1
    fi
done
if grep -Fq -- "shell=True" "$SMOKE"; then
    echo "FAIL: smoke uses shell=True (forbidden)"
    exit 1
fi
# two distinct concurrent-session runner identities present
for runner in "claude_task_mcp_concurrent_alpha_b110" "claude_task_mcp_concurrent_bravo_b110"; do
    if ! grep -Fq -- "$runner" "$SMOKE"; then
        echo "FAIL: smoke missing concurrent-session runner '$runner'"
        exit 1
    fi
done
echo "smoke: real concurrent stdio transport, two runners, no shell=True: OK"

# --- 1. run the concurrent smoke TWICE (determinism proof) -----------------
R1="$TMPDIR_STATE/result1.json"
R2="$TMPDIR_STATE/result2.json"
python3 "$SMOKE" --out "$R1" >/dev/null 2>"$TMPDIR_STATE/smoke1.stderr"
echo "run 1 exit 0 (concurrent_contract_v1=true): OK"
python3 "$SMOKE" --out "$R2" >/dev/null 2>"$TMPDIR_STATE/smoke2.stderr"
echo "run 2 exit 0 (concurrent_contract_v1=true): OK"

# --- 2. assert every gate in both emitted results + cross-run determinism --
python3 - "$R1" "$R2" <<'PYEOF'
import json, sys
d1 = json.load(open(sys.argv[1]))
d2 = json.load(open(sys.argv[2]))

expected = {
    "readonly_tools_visible",
    "write_gated_tools_visible",
    "schema_fingerprints_match_frozen",
    "schema_deterministic_across_concurrent_sessions",
    "concurrent_sessions_overlap",
    "no_cross_session_state_bleed",
    "no_write_allow_unset",
    "no_write_allow_set",
    "stdio_transport_used",
    "only_mcp_server_launched",
    "server_no_launch_code",
}

for tag, d in (("run1", d1), ("run2", d2)):
    assert d["concurrent_contract_v1"] is True, (tag, "contract not true", d.get("failing_check"))
    assert d["failing_check"] is None, (tag, d["failing_check"])
    assert d["mode"] == "mcp_stdio_concurrent_readonly_no_agent_launch", (tag, d["mode"])
    assert d["extends"] == "mcp_stdio_subprocess_client_smoke_b109_v1", (tag, d["extends"])
    assert d["concurrent_sessions"] == 2, (tag, d["concurrent_sessions"])

    checks = d["checks"]
    assert expected <= set(checks), (tag, sorted(expected - set(checks)))
    for name in expected:
        assert checks[name] is True, (tag, f"check failed: {name}")

    # contract completeness (live-inventory refresh B119: total 20 tools)
    assert d["readonly_tool_count"] == 11, (tag, d["readonly_tool_count"])
    assert d["write_gated_tool_count"] == 4, (tag, d["write_gated_tool_count"])
    det = d["detail"]
    assert len(det["readonly_tools_visible"]) == 11, (tag, det["readonly_tools_visible"])
    assert det["total_tools_visible"] == 20, (tag, det["total_tools_visible"])
    assert det["total_tools_visible_session_b"] == 20, (tag, det["total_tools_visible_session_b"])

    # SAME frozen fingerprints as B108, zero drift in EITHER concurrent session
    assert det["schema_fingerprint_mismatches_session_a"] == [], (tag, det["schema_fingerprint_mismatches_session_a"])
    assert det["schema_fingerprint_mismatches_session_b"] == [], (tag, det["schema_fingerprint_mismatches_session_b"])

    # concurrency + no-bleed + no-write per round (unset AND set)
    for rk in ("round_allow_unset", "round_allow_set"):
        r = det[rk]
        assert r["overlap_confirmed"] is True, (tag, rk, "no concurrent overlap")
        assert r["no_tool_errors"] is True, (tag, rk, "a tool errored")
        assert r["distinct_audit_dirs"] is True, (tag, rk, "audit dirs not distinct")
        # no cross-session bleed: each session sees ONLY its own runner
        assert r["session_a_sees_own_runner"] is True, (tag, rk)
        assert r["session_a_sees_other_runner"] is False, (tag, rk, "session A saw session B runner")
        assert r["session_b_sees_own_runner"] is True, (tag, rk)
        assert r["session_b_sees_other_runner"] is False, (tag, rk, "session B saw session A runner")
        assert r["no_cross_session_bleed"] is True, (tag, rk)
        # no queue/audit writes: byte-identical + empty isolated dirs, queue intact
        assert r["state_byte_identical"] is True, (tag, rk, "state not byte-identical")
        assert r["state_empty"] is True, (tag, rk, "isolated audit dir not empty")
        assert r["queue_verify_intact"] is True, (tag, rk, "queue verify not intact")
        assert r["ok"] is True, (tag, rk)

    # only-the-server launched: normalized python + server module, no agent tokens
    assert det["server_command_normalized"] == ["<python>", "-m", "aiworkhub.server"], \
        (tag, det["server_command_normalized"])
    assert det["launched_agent_or_model_token_hits"] == [], (tag, det["launched_agent_or_model_token_hits"])

    # server holds no launch code
    assert det["server_launch_pattern_hits"] == [], (tag, det["server_launch_pattern_hits"])
    assert det["launch_enabled"] is False, tag
    assert det["launch_implemented"] is False, tag

    # authority flags all stay false at worker scope (incl. agent launch authority)
    for k, v in d["authority_flags"].items():
        assert v is False, (tag, f"authority flag {k} not false")

# determinism across two INDEPENDENT process runs: path-free signature equal
assert d1["detail"]["run_signature"] == d2["detail"]["run_signature"], \
    "run_signature differs across two runs (non-deterministic)"

print("PASS: all concurrent stdio client-smoke contract gates satisfied")
print("  concurrent sessions    :", d1["concurrent_sessions"])
print("  readonly tools visible :", len(d1["detail"]["readonly_tools_visible"]))
print("  transport              :", d1["client_path"])
print("  run_signature          : byte-identical across 2 runs (deterministic)")
print("  concurrent_contract_v1 :", d1["concurrent_contract_v1"])
PYEOF

# --- 3. parent task queue intact ------------------------------------------
echo ""
echo "=== Parent task queue integrity ==="
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
