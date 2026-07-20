#!/usr/bin/env bash
set -euo pipefail

# ── test_mcp_review_summarizer_stdio_smoke_b115_v1.sh ───────────────────────
# B115 stdio transport smoke test: start MCP server via stdio, send
# JSON-RPC initialize + list_tools, find geoai_task_review_summarize,
# call it via tools/call, verify response shape, prove live queue
# unchanged before/after. No agent/model launch, no network, no mutation.
#
# Usage:
#   GEOAI_TASK_MCP_ALLOW_WRITES=0 bash \
#     tools/geoai-task-mcp/tests/test_mcp_review_summarizer_stdio_smoke_b115_v1.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES="${GEOAI_TASK_MCP_ALLOW_WRITES:-0}"

QUEUE_PATH="$ROOT/bitnnv2/data/tasking/task_queue_v1.sqlite"
AUDIT_PATH="$ROOT/tools/geoai-task-mcp/logs/audit.jsonl"

echo "=== B115 MCP Review Summarizer Stdio Smoke Test ==="
echo "ROOT=$ROOT"
echo "PYTHONPATH=$PYTHONPATH"
echo "GEOAI_TASK_MCP_ALLOW_WRITES=$GEOAI_TASK_MCP_ALLOW_WRITES"
echo "QUEUE_PATH=$QUEUE_PATH"
echo ""

# ── 0. Validate ALLOW_WRITES is off ─────────────────────────────────────────
if [ "$GEOAI_TASK_MCP_ALLOW_WRITES" != "0" ]; then
    echo "FATAL: GEOAI_TASK_MCP_ALLOW_WRITES must be 0, got '$GEOAI_TASK_MCP_ALLOW_WRITES'"
    exit 2
fi

# ── 1. Snapshot live queue and audit log before ─────────────────────────────
echo "--- Pre-flight snapshots ---"
if [ -f "$QUEUE_PATH" ]; then
    QUEUE_BEFORE=$(sha256sum "$QUEUE_PATH" | awk '{print $1}')
    echo "  queue sha256 before: $QUEUE_BEFORE"
else
    QUEUE_BEFORE="FILE_NOT_FOUND"
    echo "  queue sha256 before: $QUEUE_BEFORE"
fi

if [ -f "$AUDIT_PATH" ]; then
    AUDIT_BEFORE=$(sha256sum "$AUDIT_PATH" | awk '{print $1}')
    echo "  audit sha256 before: $AUDIT_BEFORE"
else
    AUDIT_BEFORE="FILE_NOT_FOUND"
    echo "  audit sha256 before: $AUDIT_BEFORE"
fi
echo ""

# ── 2. Run the stdio smoke test (single Python process managing child) ──────
echo "--- Stdio JSON-RPC smoke test ---"
PYTHONPATH="$MCPROOT/src" GEOAI_REPO="$ROOT" GEOAI_TASK_MCP_ALLOW_WRITES=0 \
  python3 - <<'PY'
#!/usr/bin/env python3
"""B115 inline stdio smoke runner.

Keep this embedded in the allowed shell test file so the task does not need an
extra helper path outside its allowed_writes contract.
"""

import hashlib
import json
import os
import subprocess
import sys
import time


ROOT = os.environ.get("GEOAI_REPO", "/home/shrek/GeoAI")
MCPROOT = os.path.join(ROOT, "tools", "geoai-task-mcp")
ALLOW_WRITES = os.environ.get("GEOAI_TASK_MCP_ALLOW_WRITES", "0")
QUEUE_PATH = os.path.join(ROOT, "bitnnv2", "data", "tasking", "task_queue_v1.sqlite")
AUDIT_PATH = os.path.join(ROOT, "tools", "geoai-task-mcp", "logs", "audit.jsonl")
FAILURES = []


def fail(tag, msg):
    FAILURES.append(f"{tag}: {msg}")
    print(f"  FAIL {tag}: {msg}")


def pass_(tag, detail=""):
    msg = f"  PASS {tag}"
    if detail:
        msg += f": {detail}"
    print(msg)


def sha256_file(path):
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    return "FILE_NOT_FOUND"


def main():
    q_before = sha256_file(QUEUE_PATH)
    a_before = sha256_file(AUDIT_PATH)
    print(f"  queue before (py): {q_before}")
    print(f"  audit before (py): {a_before}")

    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(MCPROOT, "src")
    env["GEOAI_REPO"] = ROOT
    env["GEOAI_TASK_MCP_ALLOW_WRITES"] = ALLOW_WRITES
    env["GEOAI_TASK_MCP_TIMEOUT"] = "30"
    env["PYTHONUNBUFFERED"] = "1"

    server_cmd = [sys.executable, "-u", "-m", "geoai_task_mcp.server"]
    print(f"  starting server: {server_cmd}")
    proc = subprocess.Popen(
        server_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=ROOT,
        text=True,
        bufsize=1,
    )
    print(f"  server pid: {proc.pid}")

    def send_jsonrpc(data):
        proc.stdin.write(json.dumps(data) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise TimeoutError("No response from server (EOF)")
        line = line.strip()
        if not line:
            raise ValueError("Empty response line from server")
        return json.loads(line)

    try:
        init_resp = send_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "b115-stdio-smoke-test", "version": "1.0.0"},
                },
            }
        )
        if "result" not in init_resp:
            fail("initialize", f"no result field: {json.dumps(init_resp)[:200]}")
            raise SystemExit(1)
        server_name = init_resp["result"].get("serverInfo", {}).get("name", "?")
        pass_("initialize", "server=" + str(server_name))

        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        time.sleep(0.5)
        pass_("initialized_notification")

        list_resp = send_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        if "result" not in list_resp or "tools" not in list_resp["result"]:
            fail("tools/list", f"unexpected response: {json.dumps(list_resp)[:300]}")
            raise SystemExit(1)
        tools = list_resp["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        pass_("tools_list_count", f"{len(tools)} tools")

        required = "geoai_task_review_summarize"
        review_tool = next((t for t in tools if t["name"] == required), None)
        if review_tool is None:
            fail("tool_find", f"{required} not in tools/list. Found: {tool_names}")
            raise SystemExit(1)
        pass_("tool_found", required)

        desc = (review_tool.get("description", "") or "").lower()
        if "read-only" in desc or "readonly" in desc:
            pass_("tool_desc_readonly")
        else:
            fail("tool_desc_readonly", "missing readonly in description")

        call_resp = send_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": required,
                    "arguments": {"task_ids": None, "batch_label": "b115_stdio_smoke"},
                },
            }
        )
        if "result" not in call_resp:
            fail("tools/call", f"no result: {json.dumps(call_resp)[:300]}")
            raise SystemExit(1)

        content = call_resp["result"].get("content", [])
        pass_("tools_call_ok", f"content items: {len(content)}")
        text_content = "".join(item.get("text", "") for item in content if item.get("type") == "text")
        if not text_content:
            fail("tools_call_content", "no text content in response")
            raise SystemExit(1)

        try:
            payload = json.loads(text_content)
        except json.JSONDecodeError as exc:
            fail("json_parse", f"cannot parse response payload: {exc}")
            raise SystemExit(1)

        for field in [
            "ok",
            "task_count",
            "grouped_tasks",
            "codex_review_checklist",
            "summary",
            "authority_flags",
        ]:
            if field in payload:
                pass_(f"response_field_{field}", type(payload[field]).__name__)
            else:
                fail(f"response_field_{field}", "missing")

        authority = payload.get("authority_flags", {})
        for key in ["readonly", "process_launch", "agent_launch", "queue_write", "write_gate_enabled"]:
            if key in authority:
                pass_(f"authority_flag_{key}", str(authority[key]))
            else:
                fail(f"authority_flag_{key}", "missing")

        if authority.get("readonly") is not True:
            fail("authority_flag_readonly_value", f"expected True, got {authority.get('readonly')}")
        for key in ["process_launch", "agent_launch"]:
            if authority.get(key) is not False:
                fail(f"authority_flag_{key}_value", f"expected False, got {authority.get(key)}")
        if authority.get("write_gate_enabled") is not True:
            fail(
                "authority_flag_write_gate_enabled_value",
                f"expected True, got {authority.get('write_gate_enabled')}",
            )
        if authority.get("queue_write") is True:
            fail("queue_write_flag", "queue_write is True, should be False")
        if authority.get("audit_write") is True:
            fail("audit_write_flag", "audit_write is True, should be False")

        if payload.get("server_tool") == required:
            pass_("response_server_tool", required)
        else:
            fail("response_server_tool", f"expected {required}, got {payload.get('server_tool')}")
        if "contract" in payload:
            pass_("response_contract", payload["contract"])
        else:
            fail("response_contract", "missing")
        if payload.get("batch_label") == "b115_stdio_smoke":
            pass_("response_batch_label", "b115_stdio_smoke")
        else:
            fail("response_batch_label", f"expected b115_stdio_smoke, got {payload.get('batch_label')}")

        checklist = payload.get("codex_review_checklist")
        if isinstance(checklist, list):
            pass_("response_checklist_type", f"list[{len(checklist)}]")
        else:
            fail("response_checklist_type", f"expected list, got {type(checklist).__name__}")
        pass_("response_shape_valid", "all required fields present and typed")
        print("")
        print("  ALL stdio_smoke: PASS")
    except Exception as exc:
        fail("exception", str(exc))
        import traceback

        traceback.print_exc()
    finally:
        print(f"  cleaning up server pid={proc.pid}...")
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        except Exception:
            try:
                proc.kill()
                proc.wait()
            except Exception:
                pass
        print("  server stopped")

    q_after = sha256_file(QUEUE_PATH)
    a_after = sha256_file(AUDIT_PATH)
    print(f"  queue after (py):  {q_after}")
    print(f"  audit after (py):  {a_after}")

    if q_before == q_after:
        pass_("queue_unchanged", f"sha256={q_after[:16]}...")
    else:
        fail("queue_unchanged", f"sha256 changed: {q_before[:16]}... -> {q_after[:16]}...")

    if a_before == a_after:
        detail = f"sha256={a_after[:16]}..." if a_after != "FILE_NOT_FOUND" else "no audit log"
        pass_("audit_unchanged", detail)
    else:
        fail("audit_unchanged", f"sha256 changed: {a_before[:16]}... -> {a_after[:16]}...")

    print("")
    if FAILURES:
        print(f"FAILURES: {len(FAILURES)}")
        for failure in FAILURES:
            print(f"  {failure}")
        sys.exit(1)
    print("VERDICT: ALL PASS")


if __name__ == "__main__":
    main()
PY
RC_SMOKE=$?
echo ""

# ── 3. Post-flight queue/audit verification (bash-level) ───────────────────
echo "--- Post-flight verification ---"
if [ -f "$QUEUE_PATH" ]; then
    QUEUE_AFTER=$(sha256sum "$QUEUE_PATH" | awk '{print $1}')
    echo "  queue sha256 after:  $QUEUE_AFTER"
    if [ "$QUEUE_BEFORE" != "$QUEUE_AFTER" ]; then
        echo "FAIL: queue mutated during stdio smoke test"
        echo "  before: $QUEUE_BEFORE"
        echo "  after:  $QUEUE_AFTER"
        exit 1
    fi
    echo "  PASS queue_unchanged_bash: sha256 identical"
else
    echo "  queue sha256 after:  FILE_NOT_FOUND"
    if [ "$QUEUE_BEFORE" != "FILE_NOT_FOUND" ]; then
        echo "FAIL: queue file disappeared during test"
        exit 1
    fi
    echo "  PASS queue_unchanged_bash: both FILE_NOT_FOUND"
fi

if [ -f "$AUDIT_PATH" ]; then
    AUDIT_AFTER=$(sha256sum "$AUDIT_PATH" | awk '{print $1}')
    echo "  audit sha256 after:  $AUDIT_AFTER"
    if [ "$AUDIT_BEFORE" != "$AUDIT_AFTER" ]; then
        echo "FAIL: audit log mutated during stdio smoke test"
        echo "  before: $AUDIT_BEFORE"
        echo "  after:  $AUDIT_AFTER"
        exit 1
    fi
    echo "  PASS audit_unchanged_bash: sha256 identical"
else
    echo "  audit sha256 after:  FILE_NOT_FOUND"
    if [ "$AUDIT_BEFORE" != "FILE_NOT_FOUND" ]; then
        echo "FAIL: audit log disappeared during test"
        exit 1
    fi
    echo "  PASS audit_unchanged_bash: both FILE_NOT_FOUND"
fi

echo ""

if [ $RC_SMOKE -ne 0 ]; then
    echo "FAIL: stdio smoke test failed with exit code $RC_SMOKE"
    exit 1
fi

echo "=== B115 MCP Review Summarizer Stdio Smoke Test: ALL PASS ==="
exit 0
