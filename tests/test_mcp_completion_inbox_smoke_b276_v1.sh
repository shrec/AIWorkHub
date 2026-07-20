#!/usr/bin/env bash
# B276 smoke test: validates the B275 geoai_completion_inbox wiring end to end
# WITHOUT launching any external/paid model or agent process.
#
# Sections:
#   1. module invariants (READONLY/LAUNCH_IMPLEMENTED/tripwire)
#   2. fixture-based facet correctness (review_queue, stale_processing,
#      runner_mismatch_warnings, latest_validation_facts) via injected
#      _list_tasks/_show_task stubs -- deterministic, no live DB dependency
#   3. real-call subprocess spy: only `taskctl.py list`/`show` ever issued,
#      never a write subcommand (review/done/start/auto-pickup/add-card/
#      export-jsonl/stage/guard-staged/usage)
#   4. live task_queue_v1.sqlite fingerprint (size+mtime) byte-identical
#      before/after a real geoai_completion_inbox-equivalent call
#   5. server.py additive wiring: geoai_completion_inbox registered AND
#      pre-existing sentinel tools (geoai_task_review_queue,
#      geoai_task_mark_done) still present
#
# Exit 0 = all sections PASS. Exit 1 = any FAIL.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PKG_DIR}/../.." && pwd)"
QUEUE_DB="${REPO_ROOT}/bitnnv2/data/tasking/task_queue_v1.sqlite"

cd "${PKG_DIR}" || exit 1

python3 - "$QUEUE_DB" <<'PYEOF'
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "src")

from geoai_task_mcp import completion_inbox, server

QUEUE_DB = sys.argv[1]

failures = []


def check(name, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}")
    if not cond:
        failures.append(name)


# --- Section 1: module invariants ------------------------------------------
check("readonly_flag_true", completion_inbox.READONLY is True)
check("launch_not_implemented", completion_inbox.LAUNCH_IMPLEMENTED is False)
check("subprocess_tripwire_zero", completion_inbox.SUBPROCESS_LAUNCH_TRIPWIRE == 0)

# --- Section 2: fixture-based facet correctness ----------------------------
now = datetime.now(timezone.utc)
recent_iso = (now - timedelta(hours=1)).isoformat()
stale_iso = (now - timedelta(hours=48)).isoformat()

FIXTURE_CARDS = {
    "TASK_REVIEW_OK_B276": {
        "task_id": "TASK_REVIEW_OK_B276",
        "runner": "claude_task_mcp_x_b276",
        "claimed_by": "claude_task_mcp_x_b276",
        "topic": "task_mcp",
        "priority": "high",
        "objective": "fixture review card, batch-matched",
        "allowed_writes": ["fixture_a.json"],
        "updated_at": recent_iso,
        "review_at": recent_iso,
        "validation_status": "pass",
    },
    "TASK_MISMATCH_B123_V1": {
        "task_id": "TASK_MISMATCH_B123_V1",
        "runner": "claude_task_mcp_y_b999",
        "claimed_by": "claude_task_mcp_y_b999",
        "topic": "task_mcp",
        "priority": "high",
        "objective": "fixture review card, deliberately batch-mismatched",
        "allowed_writes": [],
        "updated_at": recent_iso,
        "review_at": recent_iso,
        "validation_status": "unreported",
    },
    "TASK_STALE_PROCESSING_B276": {
        "task_id": "TASK_STALE_PROCESSING_B276",
        "runner": "claude_task_mcp_z_b276",
        "claimed_by": "claude_task_mcp_z_b276",
        "topic": "task_mcp",
        "updated_at": stale_iso,
        "validation_status": "unreported",
    },
    "TASK_FRESH_PROCESSING_B276": {
        "task_id": "TASK_FRESH_PROCESSING_B276",
        "runner": "claude_task_mcp_w_b276",
        "claimed_by": "claude_task_mcp_w_b276",
        "topic": "task_mcp",
        "updated_at": recent_iso,
        "validation_status": "unreported",
    },
}

STATUS_MAP = {
    "pending": [],
    "processing": ["TASK_STALE_PROCESSING_B276", "TASK_FRESH_PROCESSING_B276"],
    "review": ["TASK_REVIEW_OK_B276", "TASK_MISMATCH_B123_V1"],
}


def stub_list_tasks(status="pending", topic=None, limit=80):
    ids = STATUS_MAP.get(status, [])
    lines = [
        f"[{status}] [task_mcp] [{FIXTURE_CARDS[i]['runner']}] {i}" for i in ids
    ]
    return {"stdout": "\n".join(lines), "returncode": 0}


def stub_show_task(task_id):
    card = FIXTURE_CARDS.get(task_id)
    if card is None:
        return {"stdout": "", "returncode": 1, "stderr": "not_found"}
    return {"stdout": json.dumps(card), "returncode": 0}


fixture_result = completion_inbox.build_completion_inbox(
    topic="task_mcp",
    limit=50,
    stale_processing_hours=24.0,
    _list_tasks=stub_list_tasks,
    _show_task=stub_show_task,
)

review_ids = {e["task_id"] for e in fixture_result["review_queue"]}
check(
    "fixture_review_queue_has_both_review_cards",
    review_ids == {"TASK_REVIEW_OK_B276", "TASK_MISMATCH_B123_V1"},
)

mismatch_entry = next(
    (e for e in fixture_result["review_queue"] if e["task_id"] == "TASK_MISMATCH_B123_V1"),
    None,
)
ok_entry = next(
    (e for e in fixture_result["review_queue"] if e["task_id"] == "TASK_REVIEW_OK_B276"),
    None,
)
check(
    "fixture_mismatch_entry_flagged",
    bool(mismatch_entry) and mismatch_entry["runner_task_batch_mismatch"] is not None,
)
check(
    "fixture_ok_entry_not_flagged",
    bool(ok_entry) and ok_entry["runner_task_batch_mismatch"] is None,
)

stale_ids = {e["task_id"] for e in fixture_result["stale_processing"]}
check("fixture_stale_processing_only_old_card", stale_ids == {"TASK_STALE_PROCESSING_B276"})

mismatch_warning_ids = {e["task_id"] for e in fixture_result["runner_mismatch_warnings"]}
check(
    "fixture_runner_mismatch_warnings_exactly_one",
    mismatch_warning_ids == {"TASK_MISMATCH_B123_V1"},
)

check(
    "fixture_latest_validation_facts_covers_all_four_cards",
    len(fixture_result["latest_validation_facts"]) == 4,
)
check("fixture_no_fetch_errors", fixture_result["counts"]["fetch_errors"] == 0)

flags = fixture_result["authority_flags"]
check(
    "fixture_authority_flags_all_false_except_readonly",
    flags["process_launch"] is False
    and flags["agent_launch"] is False
    and flags["shell_invocation"] is False
    and flags["queue_write"] is False
    and flags["audit_write"] is False
    and flags["readonly"] is True
    and flags["subprocess_launch_tripwire_zero"] is True,
)

mutation = fixture_result["mutation"]
check(
    "fixture_mutation_all_false",
    mutation["queue_mutated"] is False
    and mutation["write_gate_bypassed"] is False
    and mutation["write_command_invoked"] is False
    and mutation["agent_or_process_launched"] is False,
)

# --- Section 3: real-call subprocess spy ------------------------------------
captured_calls = []
orig_run = subprocess.run


def spy_run(*args, **kwargs):
    captured_calls.append(args[0] if args else kwargs.get("args"))
    return orig_run(*args, **kwargs)


subprocess.run = spy_run
try:
    real_result = completion_inbox.build_completion_inbox(topic="task_mcp", limit=5)
finally:
    subprocess.run = orig_run

check("real_call_no_exception", isinstance(real_result, dict))
check("real_call_at_least_one_subprocess_issued", len(captured_calls) > 0)

FORBIDDEN_SUBCOMMANDS = {
    "review", "done", "start", "auto-pickup", "add-card", "export-jsonl",
    "stage", "guard-staged", "usage",
}
ALLOWED_SUBCOMMANDS = {"list", "show"}


def _subcommand_of(cmd):
    """The subcommand is the argv element immediately after the taskctl.py
    path -- NOT any later flag value (e.g. `--status review` must not be
    mistaken for the `review` subcommand)."""
    cmd_strs = [str(p) for p in cmd]
    for i, part in enumerate(cmd_strs):
        if part.endswith("taskctl.py") and i + 1 < len(cmd_strs):
            return cmd_strs[i + 1]
    return None


all_taskctl_calls = all(
    isinstance(cmd, list) and any("taskctl.py" in str(part) for part in cmd)
    for cmd in captured_calls
)
check("real_call_every_subprocess_is_taskctl", all_taskctl_calls)

subcommands = [_subcommand_of(cmd) for cmd in captured_calls]
no_forbidden_subcommand = all(sc not in FORBIDDEN_SUBCOMMANDS for sc in subcommands)
check("real_call_never_issues_write_subcommand", no_forbidden_subcommand)

has_list_or_show = any(sc in ALLOWED_SUBCOMMANDS for sc in subcommands)
check("real_call_issues_list_or_show", has_list_or_show)
check(
    "real_call_every_subcommand_is_list_or_show",
    all(sc in ALLOWED_SUBCOMMANDS for sc in subcommands),
)

# --- Section 4: live queue DB fingerprint -----------------------------------
import os

before = os.stat(QUEUE_DB)
_ = completion_inbox.build_completion_inbox(topic="task_mcp", limit=5)
after = os.stat(QUEUE_DB)
check(
    "queue_db_byte_identical_size_and_mtime",
    before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns,
)

# --- Section 5: server.py additive wiring -----------------------------------
import asyncio


async def _list_tool_names():
    tools = await server.mcp.list_tools()
    return {t.name for t in tools}


tool_names = asyncio.run(_list_tool_names())
check("server_registers_completion_inbox_tool", "geoai_completion_inbox" in tool_names)
check(
    "server_preexisting_sentinel_tools_intact",
    {"geoai_task_review_queue", "geoai_task_mark_done"} <= tool_names,
)

# --- Summary -----------------------------------------------------------------
print(f"TOTAL={len(failures)}_FAILURES")
if failures:
    print("FAILED_CHECKS:", ",".join(failures))
    sys.exit(1)
sys.exit(0)
PYEOF
status=$?

if [ "$status" -eq 0 ]; then
  echo "SMOKE_B276_VERDICT=PASS"
else
  echo "SMOKE_B276_VERDICT=FAIL"
fi
exit "$status"
