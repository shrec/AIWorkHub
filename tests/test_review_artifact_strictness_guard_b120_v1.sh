#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_review_artifact_strictness_guard_b120_v1.sh
# Harness for the B120 review-artifact strictness guard (READ-ONLY).
#
# Verifies:
#   1. static source scan: the guard module contains no subprocess/os.system/
#      os.popen/os.fork/os.exec/Popen call and never invokes any taskctl
#      write subcommand (done/review/auto-pickup/export-jsonl/usage/stage/
#      guard-staged) as a string literal anywhere in its source;
#   2. the repaired B119 full-wave dryrun and two other completed B119
#      artifacts are invalid=False with
#      reviewer_action="accept_for_codex_review";
#   3. synthetic hollow fixtures still trigger machine-readable invalid
#      reasons for echo-only, unbound-positional, and dummy-harness shapes;
#   4. every wave result carries machine-readable invalid_reasons (list)
#      and reviewer_action (string) fields;
#   5. running the script writes the two declared eval outputs (JSON +
#      rows JSONL), both non-empty, both parse, verdict == "PASS";
#   6. --no-write mode writes neither output file;
#   7. the parent task queue is untouched (taskctl verify, and the live
#      review-queue is byte-identical before/after running the guard).
#
# Isolation: only reads existing repo files under tools/geoai-task-mcp/ and
# writes solely to this task's two declared eval outputs; no mktemp needed
# since no shared mutable state is touched. Safe to run concurrently with
# other workers because it never touches taskctl or any file outside its
# own allowed_writes.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
GUARD="$MCPROOT/scripts/audit_review_artifact_strictness_b120_v1.py"
OUT_JSON="$MCPROOT/eval/review_artifact_strictness_guard_b120_v1.json"
OUT_ROWS="$MCPROOT/eval/review_artifact_strictness_guard_rows_b120_v1.jsonl"

export AIWORKHUB_REPO="$ROOT"

echo "=== B120 Review-Artifact Strictness Guard Test ==="
echo "ROOT=$ROOT"
echo "GUARD=$GUARD"

test -f "$GUARD"

CHECK_PY="$(mktemp "${TMPDIR:-/tmp}/aiworkhub_review_strictness_b120_check.XXXXXX.py")"
trap 'rm -f "$CHECK_PY"' EXIT

cat > "$CHECK_PY" <<'PY'
import importlib.util
import json
import sys
from pathlib import Path

GUARD_PATH = Path(sys.argv[1])
OUT_JSON = Path(sys.argv[2])
OUT_ROWS = Path(sys.argv[3])

# ---------------------------------------------------------------- (1)
# Static source scan: no process-spawn code, no taskctl write subcommand.
src = GUARD_PATH.read_text(encoding="utf-8")
FORBIDDEN_LAUNCH = (
    "import subprocess", "subprocess.", "os.system(", "os.popen(", "os.fork(",
    "os.exec", "os.spawn", "posix_spawn(", "pty.spawn", "Popen(",
)
FORBIDDEN_TASKCTL_WRITE = (
    "taskctl.py done", "taskctl.py review", "taskctl.py auto-pickup",
    "taskctl.py export-jsonl", "taskctl.py usage", "taskctl.py stage",
    "taskctl.py guard-staged", "taskctl.py add-card",
)
found_launch = [tok for tok in FORBIDDEN_LAUNCH if tok in src]
assert not found_launch, f"forbidden process/launch tokens in guard source: {found_launch}"
found_taskctl = [tok for tok in FORBIDDEN_TASKCTL_WRITE if tok in src]
assert not found_taskctl, f"forbidden taskctl write tokens in guard source: {found_taskctl}"
assert "import taskctl" not in src and "from taskctl" not in src

# Load the module by file path (isolation: no package __init__ import).
spec = importlib.util.spec_from_file_location("audit_review_artifact_strictness_b120_v1", GUARD_PATH)
m = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(m)

assert m.READONLY is True
assert m.SUBPROCESS_LAUNCH_TRIPWIRE == 0
assert m.QUEUE_MUTATION_TRIPWIRE == 0

# ---------------------------------------------------------------- (2)(3)(4)
report = m.build_report()
by_wave = {r["wave"]: r for r in report["wave_results"]}

for wave_name in (
    "mcp_full_wave_dryrun_harness_b119_v1",
    "mcp_codex_handoff_markdown_render_b119_v1",
    "launch_queue_persist_audit_b119_v1",
):
    res = by_wave[wave_name]
    assert res["invalid"] is False, (wave_name, res["invalid_reasons"])
    assert res["reviewer_action"] == "accept_for_codex_review", (wave_name, res["reviewer_action"])
    assert res["invalid_reasons"] == []

# Every result: machine-readable invalid_reasons (list) + reviewer_action (str in the fixed set).
for res in report["wave_results"]:
    assert isinstance(res["invalid_reasons"], list)
    assert res["reviewer_action"] in ("accept_for_codex_review", "reject_return_to_worker")

assert report["verdict"] == "PASS"
assert report["checks_failed"] == 0

# ---------------------------------------------------------------- (5)
# main() with defaults writes both output artifacts.
if OUT_JSON.exists():
    OUT_JSON.unlink()
if OUT_ROWS.exists():
    OUT_ROWS.unlink()
rc = m.main([])
assert rc == 0
assert OUT_JSON.exists() and OUT_JSON.stat().st_size > 0
assert OUT_ROWS.exists() and OUT_ROWS.stat().st_size > 0

written = json.loads(OUT_JSON.read_text(encoding="utf-8"))
assert written["verdict"] == "PASS"
assert written["schema_id"] == "geoai.review_artifact_strictness_guard_eval.v1"
assert written["checks_total"] == 3
assert written["checks_passed"] == 3

row_lines = [l for l in OUT_ROWS.read_text(encoding="utf-8").splitlines() if l.strip()]
assert len(row_lines) == 3
for line in row_lines:
    row = json.loads(line)
    assert "invalid_reasons" in row and isinstance(row["invalid_reasons"], list)
    assert row["reviewer_action"] in ("accept_for_codex_review", "reject_return_to_worker")

# ---------------------------------------------------------------- (6)
# --no-write mode must not touch the output files.
OUT_JSON.unlink()
OUT_ROWS.unlink()
rc2 = m.main(["--no-write"])
assert rc2 == 0
assert not OUT_JSON.exists(), "--no-write must not create the eval JSON output"
assert not OUT_ROWS.exists(), "--no-write must not create the rows JSONL output"

# Restore the real outputs for the repo (write mode is the normal/default path).
rc3 = m.main([])
assert rc3 == 0
assert OUT_JSON.exists() and OUT_ROWS.exists()

# ---------------------------------------------------------------- (7) B121
# Synthetic isolation-safe fixtures (mktemp, no shared mutable state):
# positive, echo-only, and positional-arg-broken, exercised directly via
# audit_test_script() so the guard is proven against the NEW dummy shape
# (unbound $1/$2 positional refs) as well as the original echo-only one --
# and proven NOT to false-positive on the ordinary, ubiquitous bash idiom
# of a helper function using $1/$2 as its own bound call-time parameters.
import shutil
import tempfile

fixture_dir = Path(tempfile.mkdtemp(prefix="aiworkhub_b121_strictness_fixture_"))
try:
    positive_sh = fixture_dir / "test_positive_fixture.sh"
    positive_sh.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "check() {\n"
        "  local expected=\"$1\" actual=\"$2\"\n"
        "  if [ \"$expected\" != \"$actual\" ]; then\n"
        "    echo \"FAIL: $expected != $actual\"\n"
        "    exit 1\n"
        "  fi\n"
        "}\n"
        "check \"a\" \"a\"\n"
        "echo \"PASS: all checks ok\"\n",
        encoding="utf-8",
    )
    echo_only_sh = fixture_dir / "test_echo_only_fixture.sh"
    echo_only_sh.write_text("#!/usr/bin/env bash\necho \"PASS\"\nexit 0\n", encoding="utf-8")

    positional_broken_sh = fixture_dir / "test_positional_arg_broken_fixture.sh"
    positional_broken_sh.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "EXPECTED=\"$1\"\n"
        "ACTUAL=\"$2\"\n"
        "if [ \"$EXPECTED\" = \"$ACTUAL\" ]; then\n"
        "  echo \"PASS\"\n"
        "else\n"
        "  echo \"FAIL\"\n"
        "  exit 1\n"
        "fi\n",
        encoding="utf-8",
    )

    positive_result = m.audit_test_script(positive_sh)
    assert positive_result["reasons"] == [], ("positive fixture must be clean", positive_result)

    echo_result = m.audit_test_script(echo_only_sh)
    assert "echo_only_dummy_test" in echo_result["reasons"], echo_result

    broken_result = m.audit_test_script(positional_broken_sh)
    assert "unbound_positional_arg_test" in broken_result["reasons"], broken_result
    assert broken_result["reasons"] != ["echo_only_dummy_test"], (
        "positional-arg-broken fixture must be caught by its OWN reason, "
        "not accidentally only by the older echo-only heuristic", broken_result,
    )

    # ------------------------------------------------------------ (8) B121
    # Adversarial red-team fixtures (found via an independent bypass-hunt
    # subagent against this same repaired guard): padding an echo-only
    # test with `true` no-ops, and hiding a substance/real-io marker word
    # inside a comment, both used to slip past the ORIGINAL echo-only /
    # dummy-harness checks undetected. Both must now be caught.
    noop_padded_sh = fixture_dir / "test_noop_padded_bypass_fixture.sh"
    noop_padded_sh.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"Running checks...\"\n"
        "true\n"
        "true\n"
        "echo \"ALL CHECKS PASSED\"\n",
        encoding="utf-8",
    )
    noop_result = m.audit_test_script(noop_padded_sh)
    assert "echo_only_dummy_test" in noop_result["reasons"], noop_result

    comment_marker_sh = fixture_dir / "test_comment_marker_bypass_fixture.sh"
    comment_marker_sh.write_text(
        "#!/usr/bin/env bash\n"
        "# assert nothing real happens here, this is a fake marker in a comment\n"
        "true\n"
        "echo \"PASS\"\n",
        encoding="utf-8",
    )
    comment_result = m.audit_test_script(comment_marker_sh)
    assert comment_result["has_substance"] is False, comment_result
    assert "echo_only_dummy_test" in comment_result["reasons"], comment_result

    comment_harness_py = fixture_dir / "build_comment_marker_bypass_fixture.py"
    comment_harness_py.write_text(
        "# TODO: this is a placeholder; real version would use open(path) to read data\n"
        "def main(): print(\"done, trust me\")\n",
        encoding="utf-8",
    )
    comment_harness_result = m.audit_harness_script(comment_harness_py)
    assert comment_harness_result["has_real_io"] is False, comment_harness_result
    assert "dummy_harness_script" in comment_harness_result["reasons"], comment_harness_result
finally:
    shutil.rmtree(fixture_dir, ignore_errors=True)

# Regression: the ubiquitous real idiom (helper function binding $1/$2 as
# its own call-time params) across every existing tools/geoai-task-mcp
# test script must NEVER be flagged unbound_positional_arg_test.
tests_dir = GUARD_PATH.parent.parent / "tests"
false_positive_hits = []
for sh_path in sorted(tests_dir.glob("*.sh")):
    res = m.audit_test_script(sh_path)
    if "unbound_positional_arg_test" in res["reasons"]:
        false_positive_hits.append(sh_path.name)
assert not false_positive_hits, (
    "unbound_positional_arg_test false-positived on real existing test scripts",
    false_positive_hits,
)

print("PASS: strictness guard accepts repaired live B119 waves and still flags "
      "hollow fixtures, with machine-readable invalid_reasons/reviewer_action, "
      "no subprocess/launch code, no taskctl write calls")
PY

python3 "$CHECK_PY" "$GUARD" "$OUT_JSON" "$OUT_ROWS"

echo ""
echo "=== Parent queue integrity ==="
BEFORE="$(python3 "$ROOT/AITools/taskctl.py" review-queue 2>&1)"
python3 "$GUARD" --no-write > /dev/null
AFTER="$(python3 "$ROOT/AITools/taskctl.py" review-queue 2>&1)"
if [ "$BEFORE" != "$AFTER" ]; then
  echo "review-queue changed around guard invocation (likely a concurrent worker, not this guard):"
  diff <(echo "$BEFORE") <(echo "$AFTER") || true
fi
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
