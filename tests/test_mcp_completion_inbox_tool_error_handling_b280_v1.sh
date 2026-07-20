#!/usr/bin/env bash
# B280 test: bounded read-error reporting for the MCP completion inbox.
#
# Verifies that a taskctl `list`/`show` read-tool failure never (a) silently
# reads as a genuinely-empty queue or (b) crosses build_completion_inbox()
# as an uncaught exception -- it always degrades to a bounded, scoped
# `read_errors` entry (additive facet; existing `fetch_errors`/facet shapes
# untouched).
#
# Sections:
#   1. nonzero LIST-call returncode (empty stdout) -> read_errors scope=list,
#      error_kind=nonzero_returncode -- tested SEPARATELY from SHOW failure.
#   2. LIST call raises an exception -> caught, read_errors scope=list,
#      error_kind=exception, no exception escapes build_completion_inbox.
#   3. nonzero SHOW-call returncode for one task -> read_errors scope=show,
#      error_kind=nonzero_returncode, AND fetch_errors keeps its original
#      shape (task_id + error) for backward compatibility.
#   4. SHOW call raises an exception -> caught, read_errors scope=show,
#      error_kind=exception, no exception escapes.
#   5. read-only / no-launch / no-mutation invariants hold in every scenario
#      (authority_flags all False, mutation block all False).
#   6. clean-path regression: no read tool failure -> read_errors == [],
#      existing facets (review_queue/fetch_errors/counts) unaffected in
#      shape or content vs the pre-B280 contract.
#
# Exit 0 = all sections PASS. Exit 1 = any FAIL.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PKG_DIR}" || exit 1

python3 - <<'PYEOF'
import json
import sys

sys.path.insert(0, "src")

from geoai_task_mcp import completion_inbox

failures = []


def check(name, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}")
    if not cond:
        failures.append(name)


def only_status(target_status, ok_result):
    """Return a _list_tasks stub: `target_status` gets the failure behavior
    supplied by the caller via closure; every other canonical status returns
    a clean empty list so only ONE scope is under test at a time."""
    def _stub(status="pending", topic=None, limit=80):
        if status == target_status:
            return ok_result
        return {"returncode": 0, "stdout": "", "stderr": ""}
    return _stub


def show_never_called(tid):
    raise AssertionError(f"show_task must not be called for {tid} when list yielded no rows")


# --- Section 1: nonzero LIST-call returncode, empty stdout ------------------
list_fail_stub = only_status(
    "review", {"returncode": 1, "stdout": "", "stderr": "sqlite lock: db is locked"}
)
r1 = completion_inbox.build_completion_inbox(
    topic="task_mcp", _list_tasks=list_fail_stub, _show_task=show_never_called
)
list_errs_1 = [e for e in r1["read_errors"] if e["scope"] == "list"]
check("s1_read_errors_has_list_scope_entry", len(list_errs_1) == 1)
check(
    "s1_list_entry_fields",
    bool(list_errs_1)
    and list_errs_1[0]["status"] == "review"
    and list_errs_1[0]["error_kind"] == "nonzero_returncode"
    and "db is locked" in list_errs_1[0]["error_message"],
)
check("s1_review_queue_empty_not_crashed", r1["review_queue"] == [])
check("s1_counts_read_errors_matches", r1["counts"]["read_errors"] == 1)
check("s1_no_show_scope_entries", all(e["scope"] != "show" for e in r1["read_errors"]))

# --- Section 2: LIST call raises an exception --------------------------------
def list_raises(status="pending", topic=None, limit=80):
    if status == "processing":
        raise OSError("taskctl subprocess spawn failed")
    return {"returncode": 0, "stdout": "", "stderr": ""}


r2 = completion_inbox.build_completion_inbox(
    topic="task_mcp", _list_tasks=list_raises, _show_task=show_never_called
)
list_errs_2 = [e for e in r2["read_errors"] if e["scope"] == "list"]
check("s2_no_uncaught_exception", isinstance(r2, dict))
check("s2_read_errors_has_exception_entry", len(list_errs_2) == 1)
check(
    "s2_exception_entry_fields",
    bool(list_errs_2)
    and list_errs_2[0]["status"] == "processing"
    and list_errs_2[0]["error_kind"] == "exception"
    and "spawn failed" in list_errs_2[0]["error_message"],
)

# --- Section 3: nonzero SHOW-call returncode for one task --------------------
def list_one_review_task(status="pending", topic=None, limit=80):
    if status == "review":
        return {
            "returncode": 0,
            "stdout": "[review] [task_mcp] [claude_x_b280] TASK_SHOW_FAIL_B280",
            "stderr": "",
        }
    return {"returncode": 0, "stdout": "", "stderr": ""}


def show_fails(tid):
    return {"returncode": 1, "stdout": "", "stderr": "task not found in db"}


r3 = completion_inbox.build_completion_inbox(
    topic="task_mcp", _list_tasks=list_one_review_task, _show_task=show_fails
)
check(
    "s3_fetch_errors_shape_preserved",
    r3["fetch_errors"] == [{"task_id": "TASK_SHOW_FAIL_B280", "error": "task not found in db"}],
)
show_errs_3 = [e for e in r3["read_errors"] if e["scope"] == "show"]
check("s3_read_errors_has_show_scope_entry", len(show_errs_3) == 1)
check(
    "s3_show_entry_fields",
    bool(show_errs_3)
    and show_errs_3[0]["task_id"] == "TASK_SHOW_FAIL_B280"
    and show_errs_3[0]["error_kind"] == "nonzero_returncode",
)
check("s3_no_list_scope_entries", all(e["scope"] != "list" for e in r3["read_errors"]))
check("s3_review_queue_empty_but_no_crash", r3["review_queue"] == [])

# --- Section 4: SHOW call raises an exception --------------------------------
def show_raises(tid):
    raise TimeoutError("taskctl show timed out")


r4 = completion_inbox.build_completion_inbox(
    topic="task_mcp", _list_tasks=list_one_review_task, _show_task=show_raises
)
check("s4_no_uncaught_exception", isinstance(r4, dict))
check(
    "s4_fetch_errors_shape_preserved",
    r4["fetch_errors"] == [{"task_id": "TASK_SHOW_FAIL_B280", "error": "taskctl show timed out"}],
)
show_errs_4 = [e for e in r4["read_errors"] if e["scope"] == "show"]
check(
    "s4_show_exception_entry_fields",
    len(show_errs_4) == 1
    and show_errs_4[0]["task_id"] == "TASK_SHOW_FAIL_B280"
    and show_errs_4[0]["error_kind"] == "exception"
    and "timed out" in show_errs_4[0]["error_message"],
)

# --- Section 5: read-only / no-launch / no-mutation invariants --------------
for label, result in (("s1", r1), ("s2", r2), ("s3", r3), ("s4", r4)):
    flags = result["authority_flags"]
    check(
        f"{label}_authority_flags_no_write_or_launch",
        flags["process_launch"] is False
        and flags["agent_launch"] is False
        and flags["shell_invocation"] is False
        and flags["queue_write"] is False
        and flags["audit_write"] is False
        and flags["readonly"] is True,
    )
    mutation = result["mutation"]
    check(
        f"{label}_mutation_all_false",
        mutation["queue_mutated"] is False
        and mutation["write_gate_bypassed"] is False
        and mutation["write_command_invoked"] is False
        and mutation["agent_or_process_launched"] is False,
    )

# --- Section 6: clean-path regression (no read-tool failure) ----------------
def list_clean(status="pending", topic=None, limit=80):
    return {"returncode": 0, "stdout": "", "stderr": ""}


r6 = completion_inbox.build_completion_inbox(
    topic="task_mcp", _list_tasks=list_clean, _show_task=show_never_called
)
check("s6_read_errors_empty_on_clean_path", r6["read_errors"] == [])
check("s6_fetch_errors_empty_on_clean_path", r6["fetch_errors"] == [])
check(
    "s6_existing_facets_present",
    set(["review_queue", "stale_processing", "runner_mismatch_warnings",
         "latest_validation_facts", "fetch_errors", "counts"]) <= set(r6.keys()),
)
check("s6_counts_read_errors_zero", r6["counts"]["read_errors"] == 0)
check(
    "s6_counts_backward_compatible_keys_present",
    set(["pending_scanned", "processing_scanned", "review_scanned", "review_queue",
         "stale_processing", "runner_mismatch_warnings", "latest_validation_facts",
         "fetch_errors"]) <= set(r6["counts"].keys()),
)

# --- Summary ------------------------------------------------------------------
print(f"TOTAL={len(failures)}_FAILURES")
if failures:
    print("FAILED_CHECKS:", ",".join(failures))
    sys.exit(1)
sys.exit(0)
PYEOF
status=$?

if [ "$status" -eq 0 ]; then
  echo "B280_TOOL_ERROR_HANDLING_VERDICT=PASS"
else
  echo "B280_TOOL_ERROR_HANDLING_VERDICT=FAIL"
fi
exit "$status"
