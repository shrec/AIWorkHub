#!/usr/bin/env python3
"""Smoke test for review_summarizer (B110 contract).

Exercises the read-only review queue summarizer with a stubbed taskctl
backend to confirm:
  - Parse review-queue output lines correctly
  - Group tasks by topic/runner
  - Build Codex review checklist with validation commands + allowed_writes
  - Return subprocess_launch_tripwire=0
  - Never call done/review/start/add-card
  - Never mutate queue/audit state

Usage:
    AIWORKHUB_ALLOW_WRITES=0 PYTHONPATH=tools/geoai-task-mcp/src \
    AIWORKHUB_REPO=/home/shrek/AIWorkHub python3 \
    tools/geoai-task-mcp/tests/mcp_review_queue_summarizer_smoke.py
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


# ── Stub task data ──────────────────────────────────────────────────

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

# Review queue output format: "  [topic] [runner] task_id"
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

# Track called commands for forbidden-operation checks
CALLED_COMMANDS: list[list[str]] = []


def stub_run_taskctl(args: list[str], **kwargs) -> TaskCtlResult:
    """Stub taskctl runner — records commands, returns canned data."""
    CALLED_COMMANDS.append(list(args))

    if args[0] == "review-queue":
        return TaskCtlResult(
            command=["python3", "stub_taskctl", *args],
            returncode=0,
            stdout=STUB_REVIEW_QUEUE_OUTPUT,
            stderr="",
        )
    # All other commands should not be called by the summarizer
    return TaskCtlResult(
        command=["python3", "stub_taskctl", *args],
        returncode=1,
        stdout="",
        stderr=f"stub: unexpected command {args[0]}",
    )


def stub_show_task(task_id: str) -> dict[str, Any]:
    """Stub show_task — returns canned task cards."""
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


# ── Test runner ─────────────────────────────────────────────────────

print("=== B110 Review Queue Summarizer Smoke Test ===\n")

# Precondition
assert os.environ.get("AIWORKHUB_ALLOW_WRITES", "0") == "0", (
    "AIWORKHUB_ALLOW_WRITES must be 0"
)
print("precondition: AIWORKHUB_ALLOW_WRITES=0  ✓")

# 1. Module invariants
if review_summarizer.READONLY is not True:
    fail("READONLY", f"expected True, got {review_summarizer.READONLY}")
else:
    ok("READONLY=True")
if review_summarizer.SUBPROCESS_LAUNCH_TRIPWIRE != 0:
    fail("SUBPROCESS_LAUNCH_TRIPWIRE", f"expected 0, got {review_summarizer.SUBPROCESS_LAUNCH_TRIPWIRE}")
else:
    ok("SUBPROCESS_LAUNCH_TRIPWIRE=0")
if review_summarizer.LAUNCH_IMPLEMENTED is not False:
    fail("LAUNCH_IMPLEMENTED", f"expected False, got {review_summarizer.LAUNCH_IMPLEMENTED}")
else:
    ok("LAUNCH_IMPLEMENTED=False")

# 2. Authority flags
flags = review_summarizer._authority_flags()
if flags.get("readonly") is not True:
    fail("authority_flags.readonly")
else:
    ok("authority_flags.readonly=True")
if flags.get("process_launch") is not False:
    fail("authority_flags.process_launch")
else:
    ok("authority_flags.process_launch=False")
if flags.get("agent_launch") is not False:
    fail("authority_flags.agent_launch")
else:
    ok("authority_flags.agent_launch=False")
if flags.get("queue_write") is not False:
    fail("authority_flags.queue_write")
else:
    ok("authority_flags.queue_write=False")
if flags.get("subprocess_launch_tripwire_zero") is not True:
    fail("authority_flags.subprocess_launch_tripwire_zero")
else:
    ok("authority_flags.subprocess_launch_tripwire_zero=True")

# 3. Parse review queue lines
parsed = review_summarizer._parse_review_queue_lines(STUB_REVIEW_QUEUE_OUTPUT)
if len(parsed) != 3:
    fail("parse_review_queue_lines.count", f"expected 3, got {len(parsed)}")
else:
    ok("parse_review_queue_lines: 3 entries")
if parsed[0]["task_id"] != "DEEPSEEK_STUB_TASK_A_V1":
    fail("parse_review_queue_lines[0]", f"wrong task_id: {parsed[0]}")
else:
    ok("parse_review_queue_lines[0].task_id correct")
if parsed[1]["topic"] != "stem":
    fail("parse_review_queue_lines[1].topic", f"expected stem, got {parsed[1]['topic']}")
else:
    ok("parse_review_queue_lines[1].topic=stem")

# 4. Summarize with stubs
CALLED_COMMANDS.clear()
result = review_summarizer.summarize_review_queue(
    _run_taskctl=stub_run_taskctl,
    _show_task=stub_show_task,
)

if not result.get("ok"):
    fail("summarize.ok", f"got {result.get('ok')}")
else:
    ok("summarize.ok=True")

if result.get("task_count") != 3:
    fail("summarize.task_count", f"expected 3, got {result.get('task_count')}")
else:
    ok("summarize.task_count=3")

# 5. Grouped tasks
grouped = result.get("grouped_tasks", {})
coding_key = "coding/deepseek_coding"
stem_key = "stem/deepseek_stem"
if coding_key not in grouped:
    fail("grouped_tasks.coding", f"missing key {coding_key}")
else:
    ok(f"grouped_tasks has {coding_key}")
if stem_key not in grouped:
    fail("grouped_tasks.stem", f"missing key {stem_key}")
else:
    ok(f"grouped_tasks has {stem_key}")
if len(grouped.get(coding_key, [])) != 2:
    fail("grouped_tasks.coding.count", f"expected 2, got {len(grouped.get(coding_key, []))}")
else:
    ok("grouped_tasks.coding/deepseek_coding has 2 tasks")

# 6. Codex review checklist
checklist = result.get("codex_review_checklist", [])
if len(checklist) != 3:
    fail("checklist.count", f"expected 3, got {len(checklist)}")
else:
    ok("checklist has 3 entries")

task_a_check = [c for c in checklist if c["task_id"] == "DEEPSEEK_STUB_TASK_A_V1"]
if not task_a_check:
    fail("checklist.task_a", "missing")
else:
    c = task_a_check[0]
    if c.get("allowed_writes_count") != 2:
        fail("checklist.task_a.allowed_writes_count", f"expected 2, got {c.get('allowed_writes_count')}")
    else:
        ok("checklist.task_a.allowed_writes_count=2")
    if len(c.get("validation_commands", [])) != 2:
        fail("checklist.task_a.validation_commands", f"expected 2, got {len(c.get('validation_commands', []))}")
    else:
        ok("checklist.task_a.validation_commands: 2")
    if c.get("runner") != "deepseek_coding":
        fail("checklist.task_a.runner")
    else:
        ok("checklist.task_a.runner=deepseek_coding")
    if c.get("topic") != "coding":
        fail("checklist.task_a.topic")
    else:
        ok("checklist.task_a.topic=coding")
    if c.get("commit_contract") != "NO_COMMIT":
        fail("checklist.task_a.commit_contract")
    else:
        ok("checklist.task_a.commit_contract=NO_COMMIT")

# 7. Summary statistics
summary = result.get("summary", {})
if summary.get("total_tasks") != 3:
    fail("summary.total_tasks", f"expected 3, got {summary.get('total_tasks')}")
else:
    ok("summary.total_tasks=3")
if summary.get("topics", {}).get("coding") != 2:
    fail("summary.topics.coding", f"expected 2, got {summary.get('topics', {}).get('coding')}")
else:
    ok("summary.topics.coding=2")
if summary.get("topics", {}).get("stem") != 1:
    fail("summary.topics.stem")
else:
    ok("summary.topics.stem=1")
if summary.get("fetch_error_count") != 0:
    fail("summary.fetch_error_count", f"expected 0, got {summary.get('fetch_error_count')}")
else:
    ok("summary.fetch_error_count=0")

# 8. Forbidden operations — must NOT call done/review/start/add-card
forbidden_cmds = {"done", "review", "start", "add-card", "auto-pickup"}
for cmd_list in CALLED_COMMANDS:
    cmd = cmd_list[0] if cmd_list else ""
    if cmd in forbidden_cmds:
        fail(f"forbidden_command.{cmd}", f"summarizer called {cmd_list}")
for cmd in forbidden_cmds:
    ok(f"forbidden_command.{cmd}: NOT called")

# Only review-queue + show calls allowed
allowed_calls = {"review-queue"}
for cmd_list in CALLED_COMMANDS:
    cmd = cmd_list[0] if cmd_list else ""
    if cmd not in allowed_calls:
        fail(f"unexpected_command.{cmd}", f"summarizer called {cmd_list}")
ok("only review-queue called (show is stubbed separately)")

# 9. Empty queue
CALLED_COMMANDS.clear()
empty_result = review_summarizer.summarize_review_queue(
    _run_taskctl=lambda args, **kw: TaskCtlResult(
        command=["python3", "stub_taskctl", *args],
        returncode=0,
        stdout="=== Codex Review Queue (0) ===",
        stderr="",
    ),
    _show_task=stub_show_task,
)
if empty_result.get("task_count") != 0:
    fail("empty.task_count", f"expected 0, got {empty_result.get('task_count')}")
else:
    ok("empty queue: task_count=0")
if empty_result.get("codex_review_checklist") != []:
    fail("empty.checklist", "expected empty list")
else:
    ok("empty queue: checklist=[]")

# 10. Authority flags in result
result_flags = result.get("authority_flags", {})
if result_flags.get("readonly") is not True:
    fail("result.authority_flags.readonly")
else:
    ok("result.authority_flags.readonly=True")
if result_flags.get("subprocess_launch_tripwire_zero") is not True:
    fail("result.authority_flags.subprocess_launch_tripwire_zero")
else:
    ok("result.authority_flags.subprocess_launch_tripwire_zero=True")

# ── Summary ─────────────────────────────────────────────────────────
print(f"\n{'='*60}")
if FAILURES:
    print(f"FAILURES ({len(FAILURES)}):")
    for f in FAILURES:
        print(f"  {f}")
    sys.exit(1)
else:
    print("ALL TESTS PASSED")
    sys.exit(0)
