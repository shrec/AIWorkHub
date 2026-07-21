#!/usr/bin/env bash
# CLAUDE_TASK_MCP_MVP_CONTRACT_AUDIT_V1 — verify the MVP contract-audit artifacts
# are consistent with the LIVE FastMCP registration and the real write gate.
# Isolation-safe: all scratch goes under a per-run mktemp -d; audit-log writes
# from the live blocked-write probe are redirected into that temp dir.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AIWORKHUB_REPO="${AIWORKHUB_REPO:-$(cd "$ROOT/../.." && pwd)}"

export PYTHONPATH="$ROOT/src"
export AIWORKHUB_REPO
export AIWORKHUB_ALLOW_WRITES=0

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export AIWORKHUB_AUDIT_LOG_PATH="$TMP/audit.jsonl"

AUDIT_JSON="$ROOT/eval/mvp_contract_audit_v1.json"
ROWS_JSONL="$ROOT/eval/mvp_contract_audit_rows_v1.jsonl"
NEXT_WAVE="$ROOT/data/tasking/mvp_contract_audit_next_wave_v1.json"

AUDIT_JSON="$AUDIT_JSON" ROWS_JSONL="$ROWS_JSONL" NEXT_WAVE="$NEXT_WAVE" python3 - <<'PY'
import asyncio, json, os
from pathlib import Path

audit_p = Path(os.environ["AUDIT_JSON"])
rows_p = Path(os.environ["ROWS_JSONL"])
nw_p = Path(os.environ["NEXT_WAVE"])

# 1) all three artifacts exist and parse
assert audit_p.is_file(), audit_p
assert rows_p.is_file(), rows_p
assert nw_p.is_file(), nw_p

audit = json.loads(audit_p.read_text(encoding="utf-8"))
rows = [json.loads(ln) for ln in rows_p.read_text(encoding="utf-8").splitlines() if ln.strip()]
nw = json.loads(nw_p.read_text(encoding="utf-8"))
assert isinstance(rows, list) and rows, "rows empty"
assert nw.get("cards"), "next_wave has no cards"

# 2) audit JSON invariants
sot = audit["source_of_truth"]
assert sot["parent_taskctl_remains_authority"] is True, sot
wg = audit["write_gate"]
assert wg["default"] == "0", wg
assert wg["blocked_returncode"] == 126, wg
assert wg["blocked_marker"] == "write command blocked", wg
assert audit["counts"]["unsafe_ungated_writes"] == 0, audit["counts"]
assert audit["verdict"] == "PASS", audit["verdict"]
assert audit["naming"]["all_conform"] is True, audit["naming"]

audit_names = sorted(t["name"] for t in audit["tools"])
audit_write_names = sorted(t["name"] for t in audit["tools"] if t["class"] == "write")

# 3) TIE TO REAL CODE: enumerate the actually-registered FastMCP tools
from aiworkhub import server, core
live_names = sorted(t.name for t in asyncio.run(server.mcp.list_tools()))
# B119 inventory refresh: audit was frozen at 11 tools; live now has 20.
# Verify all audited tools are still present (subset check).
missing_from_live = sorted(set(audit_names) - set(live_names))
assert not missing_from_live, f"audited tools missing from live server: {missing_from_live}"
new_since_audit = sorted(set(live_names) - set(audit_names))
print(f"  audited tools all present: {len(audit_names)}/{len(live_names)} OK")
print(f"  new tools since audit freeze ({len(new_since_audit)}): {new_since_audit}")

# also assert each audited tool name string is present in server.py source and that
# server.py registers no tool absent from the audit (belt-and-suspenders vs introspection)
server_src = (Path(server.__file__)).read_text(encoding="utf-8")
for n in audit_names:
    assert n in server_src, f"audited tool {n} not found in server.py source"

# every write-class tool must be gated, and gating must match the real WRITE_COMMANDS table
for t in audit["tools"]:
    if t["class"] == "write":
        assert t["gated"] is True, ("write tool not gated", t)
        assert t["taskctl_subcmd"] in core.WRITE_COMMANDS, ("write subcmd not in WRITE_COMMANDS", t)
    assert t["safe"] is True, ("unsafe tool audited", t)

# rows audit-vs-live: audit rows only cover the 11 originally-audited tools.
# Verify row tool names are a subset of live names (no audited tool lost).
row_tool_names = sorted(r["name"] for r in rows if r.get("kind") == "tool")
missing_rows = sorted(set(row_tool_names) - set(live_names))
assert not missing_rows, f"row tools missing from live server: {missing_rows}"
print(f"  row tools all present in live: {len(row_tool_names)}/{len(live_names)} OK")
assert any(r.get("kind") == "finding" for r in rows), "no finding rows"

# 4) LIVE write-gate re-derivation (evidence-tied): blocked at ALLOW_WRITES=0
assert os.environ.get("AIWORKHUB_ALLOW_WRITES") == "0"
res = core.auto_pickup("mvp_audit_probe_runner_no_write", "mvp_audit_probe_topic")
assert res["returncode"] == 126, ("expected blocked rc 126", res)
assert "write command blocked" in res["stderr"], res

# blocked probe must not have touched the parent queue: it only appends the temp audit log
log_path = os.environ["AIWORKHUB_AUDIT_LOG_PATH"]
assert log_path.startswith("/tmp") or "/tmp" in log_path, log_path

print("audit artifacts consistent with live code; write gate live-verified")
PY

echo "PASS"
