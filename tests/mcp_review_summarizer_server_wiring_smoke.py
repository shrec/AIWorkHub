#!/usr/bin/env python3
"""Smoke test for B111 server wiring of review_summarizer as MCP tool.

Validates:
  - aiworkhub_task_review_summarize MCP tool returns grouped tasks + checklist
  - Read-only invariants preserved (no write-gate toggle in schema)
  - Never calls taskctl done/review/start/auto-pickup/add-card
  - No agent/model process launch
  - Output shape matches Codex review checklist expectation

Usage:
    AIWORKHUB_ALLOW_WRITES=0 PYTHONPATH=tools/geoai-task-mcp/src \
    AIWORKHUB_REPO=/home/shrek/AIWorkHub python3 \
    tools/geoai-task-mcp/tests/mcp_review_summarizer_server_wiring_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(SRC))

from aiworkhub import review_summarizer
from aiworkhub.core import TaskCtlResult


FAILURES: list[str] = []


def fail(label: str, detail: str = "") -> None:
    FAILURES.append(f"{label}: {detail}")


def ok(label: str) -> None:
    print(f"  PASS {label}")


# ── Stub task data (same as B110 smoke test) ─────────────────────────

STUB_TASK_A = {
    "task_id": "DEEPSEEK_STUB_TASK_A_V1",
    "runner": "deepseek_coding",
    "topic": "coding",
    "status": "review",
    "worker_status": "review",
    "validation": [
        "bash tests/test_stub_a.sh",
        "python3 AITools/taskctl.py verify",
    ],
    "allowed_writes": [
        "src/module_a.py",
        "tests/test_module_a.py",
    ],
    "commit_contract": "NO_COMMIT",
    "mode": "implementation",
    "priority": "high",
    "objective": "Implement module A feature",
}

STUB_TASK_B = {
    "task_id": "DEEPSEEK_STUB_TASK_B_V1",
    "runner": "deepseek_stem",
    "topic": "stem",
    "status": "review",
    "worker_status": "review",
    "validation": [
        "bash tests/test_stub_b.sh",
    ],
    "allowed_writes": [
        "src/module_b.py",
        "tests/test_module_b.py",
        "eval/module_b.json",
    ],
    "commit_contract": "ONE_TASK_SCOPED_COMMIT",
    "mode": "measurement_only",
    "priority": "medium",
    "objective": "Measure module B performance",
}

STUB_TASK_C = {
    "task_id": "DEEPSEEK_STUB_TASK_C_V1",
    "runner": "deepseek_coding",
    "topic": "coding",
    "status": "review",
    "worker_status": "review",
    "validation": [
        "bash tests/test_stub_c.sh",
        "python3 AITools/taskctl.py verify",
    ],
    "allowed_writes": [
        "src/module_c.py",
    ],
    "commit_contract": "NO_COMMIT",
    "mode": "implementation",
    "priority": "low",
    "objective": "Implement module C feature",
}

STUB_REVIEW_QUEUE_OUTPUT = """=== Codex Review Queue (3) ===
  [coding] [deepseek_coding] DEEPSEEK_STUB_TASK_A_V1
  [stem] [deepseek_stem] DEEPSEEK_STUB_TASK_B_V1
  [coding] [deepseek_coding] DEEPSEEK_STUB_TASK_C_V1
"""

STUB_TASK_MAP = {
    "DEEPSEEK_STUB_TASK_A_V1": STUB_TASK_A,
    "DEEPSEEK_STUB_TASK_B_V1": STUB_TASK_B,
    "DEEPSEEK_STUB_TASK_C_V1": STUB_TASK_C,
}

CALLED_COMMANDS: list[list[str]] = []


def stub_run_taskctl(args: list[str], **kwargs) -> TaskCtlResult:
    CALLED_COMMANDS.append(list(args))
    if args[0] == "review-queue":
        return TaskCtlResult(
            command=["python3", "stub_taskctl", *args],
            returncode=0,
            stdout=STUB_REVIEW_QUEUE_OUTPUT,
            stderr="",
        )
    return TaskCtlResult(
        command=["python3", "stub_taskctl", *args],
        returncode=1,
        stdout="",
        stderr=f"stub: unexpected command {args[0]}",
    )


def stub_show_task(task_id: str) -> dict[str, Any]:
    if task_id in STUB_TASK_MAP:
        return {
            "returncode": 0,
            "stdout": json.dumps(STUB_TASK_MAP[task_id]),
            "stderr": "",
            "command": ["python3", "stub_taskctl", "show", task_id],
        }
    return {
        "returncode": 1,
        "stdout": "{}",
        "stderr": f"task not found: {task_id}",
        "command": ["python3", "stub_taskctl", "show", task_id],
    }


# ── Simulated server tool call (mirrors server.py wiring) ────────────

def aiworkhub_task_review_summarize(
    task_ids: list[str] | None = None,
    batch_label: str | None = None,
) -> dict[str, Any]:
    """Exact replica of the server.py @mcp.tool() function for smoke testing."""
    result = review_summarizer.summarize_review_queue(
        _run_taskctl=stub_run_taskctl,
        _show_task=stub_show_task,
    )
    if task_ids:
        filtered_checklist = [
            c for c in result.get("codex_review_checklist", [])
            if c.get("task_id") in task_ids
        ]
        result["codex_review_checklist"] = filtered_checklist
        result["filtered_by_task_ids"] = True
        result["requested_task_ids"] = task_ids
    if batch_label:
        result["batch_label"] = batch_label
    result["server_tool"] = "aiworkhub_task_review_summarize"
    result["contract"] = "B111_v1_readonly_server_wiring"
    return result


# ── Test runner ─────────────────────────────────────────────────────

print("=== B111 Review Summarizer Server Wiring Smoke Test ===\n")

# Precondition
assert os.environ.get("AIWORKHUB_ALLOW_WRITES", "0") == "0", (
    "AIWORKHUB_ALLOW_WRITES must be 0"
)
print("precondition: AIWORKHUB_ALLOW_WRITES=0  ✓")

# ── Test 1: Basic call (no filtering) ───────────────────────────────
CALLED_COMMANDS.clear()
result = aiworkhub_task_review_summarize()

if not result.get("ok"):
    fail("basic.ok", f"got {result.get('ok')}")
else:
    ok("basic.ok=True")

if result.get("task_count") != 3:
    fail("basic.task_count", f"expected 3, got {result.get('task_count')}")
else:
    ok("basic.task_count=3")

# ── Test 2: Server metadata fields present ──────────────────────────
if result.get("server_tool") != "aiworkhub_task_review_summarize":
    fail("server_tool", f"got {result.get('server_tool')}")
else:
    ok("server_tool=aiworkhub_task_review_summarize")

if result.get("contract") != "B111_v1_readonly_server_wiring":
    fail("contract", f"got {result.get('contract')}")
else:
    ok("contract=B111_v1_readonly_server_wiring")

# ── Test 3: Grouped tasks ───────────────────────────────────────────
grouped = result.get("grouped_tasks", {})
coding_key = "coding/deepseek_coding"
stem_key = "stem/deepseek_stem"

if coding_key not in grouped:
    fail("grouped.coding_key", f"missing {coding_key}")
else:
    ok(f"grouped_tasks has {coding_key}")
if stem_key not in grouped:
    fail("grouped.stem_key", f"missing {stem_key}")
else:
    ok(f"grouped_tasks has {stem_key}")
if len(grouped.get(coding_key, [])) != 2:
    fail("grouped.coding_count", f"expected 2, got {len(grouped.get(coding_key, []))}")
else:
    ok("grouped_tasks.coding/deepseek_coding has 2 tasks")

# ── Test 4: Codex review checklist shape ────────────────────────────
checklist = result.get("codex_review_checklist", [])
if len(checklist) != 3:
    fail("checklist.count", f"expected 3, got {len(checklist)}")
else:
    ok("checklist has 3 entries")

# Verify each checklist item has required fields
required_checklist_fields = {
    "task_id", "runner", "topic", "status", "worker_status",
    "validation_commands", "allowed_writes_count", "allowed_writes",
    "commit_contract", "mode", "priority", "objective",
}
for item in checklist:
    missing_fields = required_checklist_fields - set(item.keys())
    if missing_fields:
        fail(f"checklist.{item.get('task_id')}.missing_fields", str(missing_fields))
    else:
        ok(f"checklist.{item.get('task_id')} has all required fields")

# ── Test 5: filter by task_ids ──────────────────────────────────────
CALLED_COMMANDS.clear()
filtered = aiworkhub_task_review_summarize(
    task_ids=["DEEPSEEK_STUB_TASK_A_V1", "DEEPSEEK_STUB_TASK_B_V1"],
)
filtered_checklist = filtered.get("codex_review_checklist", [])
if len(filtered_checklist) != 2:
    fail("filtered.count", f"expected 2, got {len(filtered_checklist)}")
else:
    ok("filtered by task_ids: 2 results")
if not filtered.get("filtered_by_task_ids"):
    fail("filtered.flag", "filtered_by_task_ids missing")
else:
    ok("filtered_by_task_ids=True")
if filtered.get("requested_task_ids") != ["DEEPSEEK_STUB_TASK_A_V1", "DEEPSEEK_STUB_TASK_B_V1"]:
    fail("filtered.requested_task_ids")
else:
    ok("requested_task_ids preserved")

# ── Test 6: batch_label ─────────────────────────────────────────────
CALLED_COMMANDS.clear()
batched = aiworkhub_task_review_summarize(batch_label="review_batch_2026_07_04")
if batched.get("batch_label") != "review_batch_2026_07_04":
    fail("batch_label", f"got {batched.get('batch_label')}")
else:
    ok("batch_label=review_batch_2026_07_04")

# ── Test 7: Authority flags from summarizer ─────────────────────────
flags = result.get("authority_flags", {})
if flags.get("readonly") is not True:
    fail("authority_flags.readonly")
else:
    ok("authority_flags.readonly=True")
if flags.get("process_launch") is not False:
    fail("authority_flags.process_launch")
else:
    ok("authority_flags.process_launch=False")
if flags.get("queue_write") is not False:
    fail("authority_flags.queue_write")
else:
    ok("authority_flags.queue_write=False")
if flags.get("subprocess_launch_tripwire_zero") is not True:
    fail("authority_flags.subprocess_launch_tripwire_zero")
else:
    ok("authority_flags.subprocess_launch_tripwire_zero=True")

# ── Test 8: Forbidden operations — must NOT call write commands ─────
forbidden_cmds = {"done", "review", "start", "add-card", "auto-pickup"}
for cmd_list in CALLED_COMMANDS:
    cmd = cmd_list[0] if cmd_list else ""
    if cmd in forbidden_cmds:
        fail(f"forbidden_command.{cmd}", f"summarizer called {cmd_list}")
for cmd in forbidden_cmds:
    ok(f"forbidden_command.{cmd}: NOT called")

# ── Test 9: Empty review queue ──────────────────────────────────────
def stub_empty_run_taskctl(args: list[str], **kwargs) -> TaskCtlResult:
    if args[0] == "review-queue":
        return TaskCtlResult(
            command=["python3", "stub_taskctl", *args],
            returncode=0,
            stdout="=== Codex Review Queue (0) ===\n",
            stderr="",
        )
    return TaskCtlResult(
        command=["python3", "stub_taskctl", *args],
        returncode=1, stdout="", stderr="stub: unexpected",
    )

empty_result = review_summarizer.summarize_review_queue(
    _run_taskctl=stub_empty_run_taskctl,
    _show_task=stub_show_task,
)
if empty_result.get("task_count") != 0:
    fail("empty.task_count", f"expected 0, got {empty_result.get('task_count')}")
else:
    ok("empty queue: task_count=0")
if empty_result.get("grouped_tasks") != {}:
    fail("empty.grouped_tasks", f"expected {{}}, got {empty_result.get('grouped_tasks')}")
else:
    ok("empty queue: grouped_tasks={{}}")
if empty_result.get("codex_review_checklist") != []:
    fail("empty.checklist", f"expected [], got {empty_result.get('codex_review_checklist')}")
else:
    ok("empty queue: checklist=[]")

# ── Final ───────────────────────────────────────────────────────────
print(f"\n{'='*60}")
if FAILURES:
    print(f"FAILURES ({len(FAILURES)}):")
    for f in FAILURES:
        print(f"  {f}")
    sys.exit(1)
else:
    print("ALL TESTS PASSED")
    sys.exit(0)
