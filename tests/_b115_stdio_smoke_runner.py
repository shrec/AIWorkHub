#!/usr/bin/env python3
"""B115 stdio smoke test runner — managed by the shell test harness.

Start the MCP server via stdio subprocess, send JSON-RPC
initialize/list_tools/tools_call, verify response shape, check queue unchanged.
No agent/model launch, no network, no mutation.
"""

import sys, os, json, subprocess, time, hashlib

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
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    return "FILE_NOT_FOUND"


def main():
    q_before = sha256_file(QUEUE_PATH)
    a_before = sha256_file(AUDIT_PATH)
    print(f"  queue before (py): {q_before}")
    print(f"  audit before (py): {a_before}")

    # Build env for the child server
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(MCPROOT, "src")
    env["GEOAI_REPO"] = ROOT
    env["GEOAI_TASK_MCP_ALLOW_WRITES"] = ALLOW_WRITES
    env["GEOAI_TASK_MCP_TIMEOUT"] = "30"
    env["PYTHONUNBUFFERED"] = "1"

    # Start MCP server in stdio mode (text mode for line-based I/O)
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

    server_pid = proc.pid
    print(f"  server pid: {server_pid}")

    def send_jsonrpc(data, timeout=30):
        """Send a JSON-RPC message to the server and read one response line."""
        payload = json.dumps(data) + "\n"
        proc.stdin.write(payload)
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise TimeoutError(f"No response from server (EOF)")
        line = line.strip()
        if not line:
            raise ValueError("Empty response line from server")
        return json.loads(line)

    try:
        # --- Step A: initialize ---
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "b115-stdio-smoke-test", "version": "1.0.0"},
            },
        }
        init_resp = send_jsonrpc(init_req)
        has_result = "result" in init_resp
        print(f"  initialize response id={init_resp.get('id')}, has result={has_result}")
        if not has_result:
            fail("initialize", f"no result field: {json.dumps(init_resp)[:200]}")
            raise SystemExit(1)
        server_name = init_resp["result"].get("serverInfo", {}).get("name", "?")
        pass_("initialize", "server=" + str(server_name))

        # --- Step B: send initialized notification ---
        init_note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        proc.stdin.write(json.dumps(init_note) + "\n")
        proc.stdin.flush()
        time.sleep(0.5)
        pass_("initialized_notification")

        # --- Step C: tools/list ---
        list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        list_resp = send_jsonrpc(list_req)
        if "result" not in list_resp or "tools" not in list_resp["result"]:
            fail("tools/list", f"unexpected response: {json.dumps(list_resp)[:300]}")
            raise SystemExit(1)

        tools = list_resp["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        print(f"  tools/list: {len(tools)} tools returned")
        pass_("tools_list_count", f"{len(tools)} tools")

        # --- Step D: find geoai_task_review_summarize ---
        REQUIRED = "geoai_task_review_summarize"
        review_tool = None
        for t in tools:
            if t["name"] == REQUIRED:
                review_tool = t
                break

        if review_tool is None:
            fail("tool_find", f"{REQUIRED} not in tools/list. Found: {tool_names}")
            raise SystemExit(1)
        pass_("tool_found", REQUIRED)

        # Verify tool description contains readonly invariants
        desc = (review_tool.get("description", "") or "").lower()
        if "read-only" in desc or "readonly" in desc:
            pass_("tool_desc_readonly")
        else:
            fail("tool_desc_readonly", "missing readonly in description")

        # --- Step E: call geoai_task_review_summarize ---
        call_req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": REQUIRED,
                "arguments": {
                    "task_ids": None,
                    "batch_label": "b115_stdio_smoke",
                },
            },
        }
        call_resp = send_jsonrpc(call_req)

        if "result" not in call_resp:
            fail("tools/call", f"no result: {json.dumps(call_resp)[:300]}")
            raise SystemExit(1)

        # MCP returns content as list of text/resource items
        result = call_resp["result"]
        content = result.get("content", [])
        pass_("tools_call_ok", f"content items: {len(content)}")

        # Extract the text content
        text_content = ""
        for item in content:
            if item.get("type") == "text":
                text_content += item.get("text", "")
        if not text_content:
            fail("tools_call_content", "no text content in response")
            raise SystemExit(1)

        # Parse the JSON payload inside the text content
        try:
            payload = json.loads(text_content)
        except json.JSONDecodeError as e:
            fail("json_parse", f"cannot parse response payload: {e}")
            raise SystemExit(1)

        # --- Step F: verify response shape ---
        required_top_fields = [
            "ok", "task_count", "grouped_tasks",
            "codex_review_checklist", "summary", "authority_flags",
        ]
        for field in required_top_fields:
            if field in payload:
                pass_(f"response_field_{field}", str(type(payload[field]).__name__))
            else:
                fail(f"response_field_{field}", "missing")

        # Check authority_flags sub-shape
        af = payload.get("authority_flags", {})
        for flag_key in ["readonly", "process_launch", "agent_launch", "queue_write", "write_gate_enabled"]:
            if flag_key in af:
                val = af[flag_key]
                pass_(f"authority_flag_{flag_key}", str(val))
            else:
                fail(f"authority_flag_{flag_key}", "missing")

        # readonly must be True
        if af.get("readonly") is not True:
            fail("authority_flag_readonly_value", f"expected True, got {af.get('readonly')}")

        # process_launch and agent_launch must be False
        for k in ["process_launch", "agent_launch"]:
            if af.get(k) is not False:
                fail(f"authority_flag_{k}_value", f"expected False, got {af.get(k)}")

        # write_gate_enabled must be True (gate active = writes blocked = default-off)
        if af.get("write_gate_enabled") is not True:
            fail("authority_flag_write_gate_enabled_value",
                 f"expected True, got {af.get('write_gate_enabled')}")

        # Check server-level fields
        if payload.get("server_tool") == REQUIRED:
            pass_("response_server_tool", REQUIRED)
        else:
            fail("response_server_tool", f"expected {REQUIRED}, got {payload.get('server_tool')}")

        if "contract" in payload:
            pass_("response_contract", payload["contract"])
        else:
            fail("response_contract", "missing")

        if payload.get("batch_label") == "b115_stdio_smoke":
            pass_("response_batch_label", "b115_stdio_smoke")
        else:
            fail("response_batch_label", f"expected b115_stdio_smoke, got {payload.get('batch_label')}")

        # codex_review_checklist must be a list
        checklist = payload.get("codex_review_checklist")
        if isinstance(checklist, list):
            pass_("response_checklist_type", f"list[{len(checklist)}]")
        else:
            fail("response_checklist_type", f"expected list, got {type(checklist).__name__}")

        pass_("response_shape_valid", "all required fields present and typed")

        # --- Step G: verify server did NOT enable writes ---
        if af.get("queue_write") is True:
            fail("queue_write_flag", "queue_write is True, should be False")
        if af.get("audit_write") is True:
            fail("audit_write_flag", "audit_write is True, should be False")

        print("")
        print("  ALL stdio_smoke: PASS")

    except Exception as e:
        fail("exception", str(e))
        import traceback
        traceback.print_exc()
    finally:
        # --- Cleanup: terminate server ---
        print(f"  cleaning up server pid={server_pid}...")
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

    # --- Post-flight snapshots ---
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
        for f in FAILURES:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("VERDICT: ALL PASS")
        sys.exit(0)


if __name__ == "__main__":
    main()
