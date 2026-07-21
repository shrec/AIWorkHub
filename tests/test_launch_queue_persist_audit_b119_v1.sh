#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_launch_queue_persist_audit_b119_v1.sh
# Harness for append-only launch-queue persistence + audit (no process launch).
#
# Verifies:
#   1. persist_intent()/persist_transition() append a scrubbed ProcessLogEntry
#      record per call and NEVER truncate/rewrite the log (strict line-count
#      growth, byte-for-byte prefix preserved across appends);
#   2. with both env gates unset (default), every persisted record's
#      decision/to_state == "blocked_launch_disabled";
#   3. launch stays disabled + records stay blocked_launch_disabled even with
#      BOTH env gates forced on (defense in depth -- no launcher implemented);
#   4. env values never leak into the log -- only NAME->status tokens for the
#      two explicit gate names, even when an unrelated secret-looking env var
#      is set;
#   5. read_persisted_log() reports accurate counts and all_blocked_launch_disabled;
#   6. _scrub_entry drops any stray top-level key outside the frozen schema;
#   7. resolve_log_path() precedence: explicit arg > env override > default;
#   8. no process-spawn / shell / network code exists in the module (static
#      source scan) and none fires at runtime (forbidden-call guards);
#   9. parent task queue is not mutated (taskctl verify).
#
# Isolation: per-run mktemp working dir + mktemp log file; the log path is
# passed via AIWORKHUB_LAUNCH_QUEUE_LOG_PATH so concurrent test runs
# never share a file. Both launch_queue_persist.py and its sibling
# launch_queue_contract.py are loaded by FILE PATH (never via package
# __init__), so a concurrent worker editing __init__.py/server.py cannot
# affect this test. Parallel-safe, no shared repo artifact written.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
MODULE="$MCPROOT/src/aiworkhub/launch_queue_persist.py"

TMPWORK="$(mktemp -d "${TMPDIR:-/tmp}/aiworkhub_launch_persist_b119.XXXXXX")"
trap 'rm -rf "$TMPWORK"' EXIT

LOGFILE="$TMPWORK/launch_queue_audit.jsonl"

# Ensure gates are unset for the primary run (do not inherit an enabled ambient).
unset AIWORKHUB_ALLOW_LAUNCH || true
unset AIWORKHUB_ALLOW_WRITES || true
unset AIWORKHUB_LAUNCH_QUEUE_LOG_PATH || true
export AIWORKHUB_REPO="$ROOT"

echo "=== Launch-Queue Persist + Audit Test B119 v1 ==="
echo "AIWORKHUB_REPO=$AIWORKHUB_REPO"
echo "MODULE=$MODULE"
echo "TMPWORK=$TMPWORK"
echo "LOGFILE=$LOGFILE"

CHECK_PY="$TMPWORK/check.py"
cat > "$CHECK_PY" <<'PY'
import importlib.util
import json
import os
import socket
import subprocess
import sys

MODULE_PATH = sys.argv[1]
LOGFILE = sys.argv[2]

# --- forbidden-call sentinels: any real spawn/network call fails the test ---
_HITS = []


def _boom(name):
    def _f(*_a, **_k):
        _HITS.append(name)
        raise AssertionError(f"FORBIDDEN call invoked: {name}")
    return _f


def _load_by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot build import spec for {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load BEFORE installing guards (module import itself must stay guard-free-safe).
m = _load_by_path("launch_queue_persist_b119_check", MODULE_PATH)

for attr in ("Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
    if hasattr(subprocess, attr):
        setattr(subprocess, attr, _boom(f"subprocess.{attr}"))
for attr in (
    "system", "popen", "fork", "forkpty",
    "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp",
    "spawnv", "spawnve", "spawnvp", "spawnl", "spawnle", "spawnlp",
    "posix_spawn", "posix_spawnp",
):
    if hasattr(os, attr):
        setattr(os, attr, _boom(f"os.{attr}"))
try:
    socket.socket.connect = _boom("socket.socket.connect")
except (AttributeError, TypeError):
    pass
socket.create_connection = _boom("socket.create_connection")


def _with_env(**kv):
    saved = {k: os.environ.get(k) for k in kv}

    def _apply():
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    _apply()
    return _restore


BLOCKED = "blocked_launch_disabled"

# ---------------------------------------------------------------- (1)+(2)
# Gates unset (default): persist two intents, verify blocked + append-only.
restore = _with_env(AIWORKHUB_ALLOW_LAUNCH=None, AIWORKHUB_ALLOW_WRITES=None)
try:
    assert not os.path.exists(LOGFILE), "log file must not pre-exist"

    r1 = m.persist_intent(
        task_id="T_B119_1", runner="claude_x", topic="task_mcp",
        adapter_id="claude_cli", argv_template=["claude", "-p", "<prompt>"],
        log_path=LOGFILE,
    )
    assert r1["decision"]["permitted"] is False
    assert r1["decision"]["state"] == BLOCKED
    assert r1["persisted_entry"]["decision"] == BLOCKED
    assert r1["persisted_entry"]["to_state"] == BLOCKED
    assert set(r1["persisted_entry"]) == set(m._PROCESS_LOG_KEYS)

    with open(LOGFILE, "r", encoding="utf-8") as fh:
        lines_after_1 = fh.readlines()
    assert len(lines_after_1) == 1, f"expected 1 line, got {len(lines_after_1)}"
    prefix_after_1 = lines_after_1[:]

    r2 = m.persist_intent(
        task_id="T_B119_2", runner="codex_x", topic="task_mcp",
        adapter_id="codex_cli", log_path=LOGFILE,
    )
    assert r2["persisted_entry"]["decision"] == BLOCKED

    with open(LOGFILE, "r", encoding="utf-8") as fh:
        lines_after_2 = fh.readlines()
    assert len(lines_after_2) == 2, f"expected 2 lines (append-only), got {len(lines_after_2)}"
    # Byte-for-byte prefix preserved: append never rewrites prior lines.
    assert lines_after_2[:1] == prefix_after_1, "append-only violated: prior line changed"
finally:
    restore()

# ---------------------------------------------------------------- (3)
# BOTH gates forced ON -> record STILL blocked_launch_disabled (defense in depth).
restore = _with_env(AIWORKHUB_ALLOW_LAUNCH="1", AIWORKHUB_ALLOW_WRITES="1")
try:
    assert m.lqc.env_gates_open() is True
    assert m.lqc.launch_enabled() is False
    r3 = m.persist_intent(
        task_id="T_B119_3", runner="r", topic="task_mcp",
        adapter_id="claude_cli", log_path=LOGFILE,
    )
    assert r3["decision"]["permitted"] is False
    assert r3["persisted_entry"]["decision"] == BLOCKED
    assert r3["persisted_entry"]["to_state"] == BLOCKED
    with open(LOGFILE, "r", encoding="utf-8") as fh:
        lines_after_3 = fh.readlines()
    assert len(lines_after_3) == 3, f"expected 3 lines, got {len(lines_after_3)}"
    assert lines_after_3[:2] == lines_after_2, "append-only violated across gate-on call"
finally:
    restore()

# ---------------------------------------------------------------- (4)
# Secret env value must never leak into the persisted log.
restore = _with_env(
    AIWORKHUB_ALLOW_LAUNCH="1",
    AIWORKHUB_ALLOW_WRITES="1",
    AIWORKHUB_API_TOKEN="super-secret-value-should-never-appear",
)
try:
    r4 = m.persist_intent(
        task_id="T_B119_4", runner="r", topic="task_mcp",
        adapter_id="deepseek_manual", log_path=LOGFILE,
    )
    assert set(r4["persisted_entry"]["env_gate_status"]) == {
        m.lqc.ALLOW_LAUNCH_ENV, m.lqc.ALLOW_WRITES_ENV,
    }
    assert set(r4["persisted_entry"]["env_gate_status"].values()) <= {"<set>", "<unset>"}
    with open(LOGFILE, "r", encoding="utf-8") as fh:
        blob = fh.read()
    assert "super-secret-value-should-never-appear" not in blob
    assert "AIWORKHUB_API_TOKEN" not in blob
finally:
    restore()

# ---------------------------------------------------------------- (5)
# read_persisted_log(): accurate counts, all_blocked_launch_disabled True.
restore = _with_env(AIWORKHUB_ALLOW_LAUNCH=None, AIWORKHUB_ALLOW_WRITES=None)
try:
    summary = m.read_persisted_log(log_path=LOGFILE)
    assert summary["ok"] is True
    assert summary["log_exists"] is True
    assert summary["total_entries"] == 4, summary["total_entries"]
    assert summary["all_blocked_launch_disabled"] is True
    assert summary["entries_by_decision"].get(BLOCKED) == 4
    assert summary["entries_by_to_state"].get(BLOCKED) == 4
    assert len(summary["last_entries"]) == 4
finally:
    restore()

# ---------------------------------------------------------------- (6)
# _scrub_entry drops any stray top-level key outside the frozen schema.
dirty = {
    "request_id": "x", "task_id": "x", "runner": "x", "topic": "x",
    "adapter_id": "x", "from_state": "queued", "to_state": BLOCKED,
    "decision": BLOCKED, "blocked_reason": "", "env_gate_status": {},
    "ts": None, "SECRET_LEAK": "should-not-survive-scrub",
}
scrubbed = m._scrub_entry(dirty)
assert "SECRET_LEAK" not in scrubbed
assert set(scrubbed) == set(m._PROCESS_LOG_KEYS)

# ---------------------------------------------------------------- (7)
# resolve_log_path precedence: explicit arg > env override > repo default.
explicit = m.resolve_log_path(log_path="/tmp/explicit_wins.jsonl")
assert str(explicit) == "/tmp/explicit_wins.jsonl"

restore = _with_env(AIWORKHUB_LAUNCH_QUEUE_LOG_PATH="/tmp/env_wins.jsonl")
try:
    via_env = m.resolve_log_path()
    assert str(via_env) == "/tmp/env_wins.jsonl"
finally:
    restore()

restore = _with_env(AIWORKHUB_LAUNCH_QUEUE_LOG_PATH=None)
try:
    default_path = m.resolve_log_path()
    assert str(default_path).endswith(".aiworkhub/runtime/process_logs/launch_queue_audit.jsonl")
finally:
    restore()

# ---------------------------------------------------------------- (8)
# Static source scan: no process-spawn / shell / network code in the module.
FORBIDDEN_SOURCE = (
    "import subprocess", "subprocess.", "os.system(", "os.popen(", "os.fork(",
    "os.exec", "os.spawn", "posix_spawn(", "pty.spawn", "Popen(",
    "import socket", "socket.socket(", "urllib", "http.client", "httpx",
    "requests.get(", "requests.post(", "shell=True",
)
with open(MODULE_PATH, "r", encoding="utf-8") as fh:
    src = fh.read()
found = [tok for tok in FORBIDDEN_SOURCE if tok in src]
assert not found, f"forbidden process/network tokens in module source: {found}"

cs = m.contract_summary()
assert cs["launch_implemented"] is False
assert cs["launch_enabled"] is False
assert cs["append_only"] is True
assert cs["env_recorded_as_status_tokens_only"] is True

assert _HITS == [], f"forbidden calls were invoked: {_HITS}"

print("PASS: launch-queue persistence is append-only, blocked_launch_disabled "
      "under both gate states, no secret leak, no process/network call, "
      "schema-scrubbed, precedence-correct")
PY

python3 "$CHECK_PY" "$MODULE" "$LOGFILE"

echo ""
echo "=== Parent queue integrity ==="
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
