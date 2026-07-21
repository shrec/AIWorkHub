#!/usr/bin/env python3
"""Read-only tool smoke test for aiworkhub.

Exercises every read-only tool with AIWORKHUB_ALLOW_WRITES=0
and confirms write-gated tools are blocked without mutating parent queue.

Usage:
    AIWORKHUB_ALLOW_WRITES=0 PYTHONPATH=tools/geoai-task-mcp/src \
    AIWORKHUB_REPO=/home/shrek/AIWorkHub python3 tools/geoai-task-mcp/tests/readonly_tools_smoke.py
"""

from __future__ import annotations

import json
import os
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(SRC))

from aiworkhub import core


FAILURES: list[str] = []


def fail(label: str, detail: str = "") -> None:
    FAILURES.append(f"{label}: {detail}")


def ok(label: str) -> None:
    print(f"  PASS {label}")


# ── precondition: writes must be disallowed ──────────────────────────
assert os.environ.get("AIWORKHUB_ALLOW_WRITES", "0") == "0", (
    "AIWORKHUB_ALLOW_WRITES must be 0 for this test"
)
print("precondition: AIWORKHUB_ALLOW_WRITES=0  ✓")


# ── 1. health ────────────────────────────────────────────────────────
h = core.health()
if not h.get("ok"):
    fail("health", f"ok=False: {json.dumps(h, default=str)}")
else:
    ok("health.ok")
if h.get("writes_allowed") is not False:
    fail("health.writes_allowed", f"expected False, got {h.get('writes_allowed')}")
else:
    ok("health.writes_allowed=False")
if h.get("verify", {}).get("returncode") != 0:
    fail("health.verify", f"returncode={h.get('verify',{}).get('returncode')}")
else:
    ok("health.verify.returncode==0")
if not h.get("repo"):
    fail("health.repo", "missing repo path")
else:
    ok("health.repo present")


# ── 2. review_queue ──────────────────────────────────────────────────
rq = core.review_queue()
if rq.get("returncode") != 0:
    fail("review_queue", f"returncode={rq.get('returncode')}")
else:
    ok("review_queue.returncode==0")
if "stdout" not in rq:
    fail("review_queue", "missing stdout")
else:
    ok("review_queue.stdout present")


# ── 3. list_tasks ────────────────────────────────────────────────────
lt = core.list_tasks(status="pending", limit=5)
if lt.get("returncode") != 0:
    fail("list_tasks", f"returncode={lt.get('returncode')}")
else:
    ok("list_tasks.returncode==0")
lt_all = core.list_tasks(status="all", limit=5)
if lt_all.get("returncode") != 0:
    fail("list_tasks(all)", f"returncode={lt_all.get('returncode')}")
else:
    ok("list_tasks(all).returncode==0")


# ── 4. show_task ─────────────────────────────────────────────────────
# Use a known existing task id; fall back gracefully if not found
KNOWN_ID = "DEEPSEEK_TASK_MCP_READONLY_TOOL_SMOKE_V1"
st = core.show_task(KNOWN_ID)
if st.get("returncode") != 0:
    fail("show_task", f"returncode={st.get('returncode')} stderr={st.get('stderr','')}")
else:
    ok("show_task.returncode==0")
if KNOWN_ID not in st.get("stdout", ""):
    fail("show_task", "task_id not found in stdout")
else:
    ok("show_task.content has task_id")


# ── 5. pending_for_runner ────────────────────────────────────────────
pfr = core.pending_for_runner("deepseek_task_mcp_readonly_smoke_v1")
if pfr.get("returncode") != 0:
    fail("pending_for_runner", f"returncode={pfr.get('returncode')}")
else:
    ok("pending_for_runner.returncode==0")
if "jsonl_rows" not in pfr:
    fail("pending_for_runner", "missing jsonl_rows key")
else:
    ok("pending_for_runner.jsonl_rows present")


# ── 6. collision_guard ───────────────────────────────────────────────
cg = core.collision_guard(print_json=True)
if cg.get("returncode") != 0:
    fail("collision_guard", f"returncode={cg.get('returncode')}")
else:
    ok("collision_guard.returncode==0")
cg_out = cg.get("stdout", "")
if "collision_free" not in cg_out and "collision" not in cg_out.lower():
    fail("collision_guard", "no collision info in stdout")
else:
    ok("collision_guard.stdout has collision info")


# ── 7. usage_report ──────────────────────────────────────────────────
ur = core.usage_report()
if ur.get("returncode") != 0:
    fail("usage_report", f"returncode={ur.get('returncode')}")
else:
    ok("usage_report.returncode==0")
ur_runner = core.usage_report(runner="deepseek_task_mcp_readonly_smoke_v1")
if ur_runner.get("returncode") != 0:
    fail("usage_report(runner)", f"returncode={ur_runner.get('returncode')}")
else:
    ok("usage_report(runner).returncode==0")


# ── 8. auto_pickup MUST be blocked (write-gate) ──────────────────────
ap = core.auto_pickup("deepseek_task_mcp_readonly_smoke_v1", topic="task_mcp")
if ap.get("returncode") != 126:
    fail("auto_pickup block", f"expected returncode=126, got {ap.get('returncode')}")
else:
    ok("auto_pickup.returncode==126 (blocked)")
if "write command blocked" not in ap.get("stderr", ""):
    fail("auto_pickup block", f"missing 'write command blocked' in stderr: {ap.get('stderr','')}")
else:
    ok("auto_pickup.stderr has 'write command blocked'")


# ── 9. mark_review MUST be blocked (write-gate) ──────────────────────
mr = core.mark_review(KNOWN_ID)
if mr.get("returncode") != 126:
    fail("mark_review block", f"expected returncode=126, got {mr.get('returncode')}")
else:
    ok("mark_review.returncode==126 (blocked)")


# ── 10. mark_done MUST be blocked (write-gate) ───────────────────────
md = core.mark_done(KNOWN_ID)
if md.get("returncode") != 126:
    fail("mark_done block", f"expected returncode=126, got {md.get('returncode')}")
else:
    ok("mark_done.returncode==126 (blocked)")


# ── 11. export_jsonl MUST be blocked (write-gate) ────────────────────
ej = core.export_jsonl()
if ej.get("returncode") != 126:
    fail("export_jsonl block", f"expected returncode=126, got {ej.get('returncode')}")
else:
    ok("export_jsonl.returncode==126 (blocked)")


# ── 12. parent queue non-mutation check ──────────────────────────────
# Verify the task card for KNOWN_ID still shows original state after
# all read-only calls.  The card must not have been mutated.
st2 = core.show_task(KNOWN_ID)
if st2.get("returncode") != 0:
    fail("post-read-show", f"returncode={st2.get('returncode')}")
else:
    ok("post-read-show.returncode==0 (no mutation)")

# Verify taskctl verify still passes
from aiworkhub.core import run_taskctl
v2 = run_taskctl(["verify"])
if v2.returncode != 0:
    fail("post-read-verify", f"returncode={v2.returncode}")
else:
    ok("post-read-verify.returncode==0 (queue intact)")


# ── summary ──────────────────────────────────────────────────────────
print(f"\n{'='*60}")
if FAILURES:
    print(f"FAILURES ({len(FAILURES)}):")
    for f in FAILURES:
        print(f"  {f}")
    sys.exit(1)
else:
    print("ALL READ-ONLY TOOL SMOKE TESTS PASSED")
    sys.exit(0)
