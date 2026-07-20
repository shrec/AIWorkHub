#!/usr/bin/env bash
set -euo pipefail

# ── test_mcp_codex_handoff_markdown_render_b119_v1.sh ───────────────────
# Test for the B119 Codex-ready MCP handoff MARKDOWN renderer.
#
# Runs an inline Python harness (heredoc — no extra repo file, per this
# task's allowed_writes) that:
#   - exercises render_codex_handoff_markdown as a PURE function (no
#     taskctl I/O) over a hand-built report dict, and asserts it is
#     deterministic (same input -> byte-identical output, called twice);
#   - builds a TEMP fixture review queue (per-test mktemp dir) and calls
#     build_codex_handoff_markdown_report with a stub run/show, asserting
#     the fixture dir sha256 is unchanged before/after and no write
#     taskctl command (done/review/start/auto-pickup/export-jsonl/usage)
#     was ever issued;
#   - asserts the registered MCP server tool geoai_task_codex_handoff_markdown
#     is wired, callable, and reads the LIVE review queue read-only (stdout
#     identical before/after);
#   - asserts write gate stays default-off throughout.
#
# Isolation-safe / parallel-safe: mktemp dirs only, no fixed shared path,
# no writes to the real parent queue.
#
# Usage:
#   GEOAI_TASK_MCP_ALLOW_WRITES=0 bash \
#     tools/geoai-task-mcp/tests/test_mcp_codex_handoff_markdown_render_b119_v1.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES="${GEOAI_TASK_MCP_ALLOW_WRITES:-0}"

echo "=== B119 MCP Codex Handoff Markdown Render Test ==="
echo "ROOT=$ROOT"
echo "PYTHONPATH=$PYTHONPATH"
echo "GEOAI_TASK_MCP_ALLOW_WRITES=$GEOAI_TASK_MCP_ALLOW_WRITES"
echo ""

# ── Validate ALLOW_WRITES is off (write gate must stay default-off) ─────
if [ "$GEOAI_TASK_MCP_ALLOW_WRITES" != "0" ]; then
    echo "FATAL: GEOAI_TASK_MCP_ALLOW_WRITES must be 0, got '$GEOAI_TASK_MCP_ALLOW_WRITES'"
    exit 2
fi

python3 - <<'PYEOF'
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from geoai_task_mcp import core, review_summarizer

FAILURES: list[str] = []
PASSES: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        PASSES.append(label)
        print(f"  PASS - {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL - {label}")


# ── Fixture taskctl stub (READ-ONLY over a temp dir) ────────────────────
class _ResultLike:
    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FixtureTaskctl:
    def __init__(self, fixture_dir: Path) -> None:
        self.dir = Path(fixture_dir)
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> _ResultLike:
        self.calls.append(list(args))
        if list(args) == ["review-queue"]:
            text = (self.dir / "review_queue.txt").read_text(encoding="utf-8")
            return _ResultLike(0, text, "")
        raise AssertionError(f"unexpected run_taskctl call: {args!r}")

    def show(self, task_id: str) -> dict[str, Any]:
        self.calls.append(["show", task_id])
        card_path = self.dir / f"{task_id}.json"
        if not card_path.exists():
            return {"ok": False, "returncode": 1, "stdout": "", "stderr": "not found"}
        return {
            "ok": True,
            "returncode": 0,
            "stdout": card_path.read_text(encoding="utf-8"),
            "stderr": "",
        }


def _hash_dir(path: Path) -> str:
    h = hashlib.sha256()
    for fp in sorted(Path(path).rglob("*")):
        if fp.is_file():
            h.update(fp.relative_to(path).as_posix().encode("utf-8"))
            h.update(b"\0")
            h.update(fp.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def _write_card(fixture_dir: Path, card: dict[str, Any]) -> None:
    (fixture_dir / f"{card['task_id']}.json").write_text(
        json.dumps(card, indent=2), encoding="utf-8"
    )


def _write_queue(fixture_dir: Path, cards: list[dict[str, Any]]) -> None:
    lines = ["=== Codex Review Queue ({}) ===".format(len(cards))]
    for c in cards:
        lines.append(f"  [{c['topic']}] [{c['runner']}] {c['task_id']}")
    (fixture_dir / "review_queue.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


CLEAN = {
    "task_id": "CLEAN_TASK_B119_V1",
    "runner": "worker_clean",
    "topic": "coding",
    "status": "processing",
    "worker_status": "in_progress",
    "mode": "clean_mode",
    "priority": "p_clean",
    "objective": "A well-scoped task with validation and full commit guards.",
    "allowed_writes": [
        "bitnnv2/tests/test_clean_b119.py",
        "bitnnv2/eval/clean_b119_v1.json",
    ],
    "validation": ["bash bitnnv2/tests/test_clean_b119.sh", "python3 AITools/taskctl.py verify"],
    "commit_contract": "NO_COMMIT preferred; Codex finalizes explicit allowed_writes. Never git add -A / git add . / mixed-task commit.",
    "forbidden": ["git_add_A", "mixed_task_commit"],
}

RISKY = {
    "task_id": "RISKY_TASK_B119_V1",
    "runner": "worker_risky",
    "topic": "task_mcp",
    "status": "processing",
    "worker_status": "in_progress",
    "mode": "risky_mode",
    "priority": "p_risky",
    "objective": "Touches shared server.py + a manifest and declares no validation.",
    "allowed_writes": [
        "tools/geoai-task-mcp/src/geoai_task_mcp/server.py",
        "bitnnv2/data/tasking/machine_task_cards_manifest_v1.json",
    ],
    "validation": [],
    "commit_contract": "",
    "forbidden": [],
}

_REQUIRED_MD_MARKERS = (
    "# Codex Handoff Review Packet",
    "## CLEAN_TASK_B119_V1",
    "## RISKY_TASK_B119_V1",
    "risk_level:",
    "allowed_writes (",
    "validation:",
    "risks:",
    "next:",
)


def test_pure_render_deterministic() -> None:
    print("[test_pure_render_deterministic] render_codex_handoff_markdown is a pure, deterministic function")
    fake_report = {
        "ok": True,
        "contract": "B116_v1_codex_handoff_e2e_readonly",
        "task_count": 1,
        "batch_label": "b119_pure_test",
        "summary": {
            "risk_levels": {"low": 0, "medium": 0, "high": 1},
            "commit_hygiene_ok": 0,
            "commit_hygiene_warn": ["FAKE_TASK"],
            "tasks_missing_validation": ["FAKE_TASK"],
        },
        "handoff_reports": [
            {
                "task_id": "FAKE_TASK",
                "runner": "r1",
                "topic": "t1",
                "risk_level": "high",
                "commit_hygiene": {"status": "warn"},
                "allowed_writes": ["a.py"],
                "allowed_writes_count": 1,
                "validation_commands": [],
                "risks": [{"code": "missing_validation", "severity": "high", "detail": "x"}],
                "recommended_stage_commands": ["python3 AITools/taskctl.py stage FAKE_TASK"],
            }
        ],
    }
    md1 = review_summarizer.render_codex_handoff_markdown(fake_report)
    md2 = review_summarizer.render_codex_handoff_markdown(fake_report)
    check(md1 == md2, "render is deterministic (identical output for identical input)")
    check(isinstance(md1, str) and len(md1) > 0, "render returns non-empty string")
    check("# Codex Handoff Review Packet" in md1, "markdown has top-level header")
    check("## FAKE_TASK" in md1, "markdown has per-task section")
    check("missing_validation[high]" in md1, "markdown surfaces risk code+severity")
    check("stage FAKE_TASK" in md1, "markdown surfaces recommended stage command")
    check("b119_pure_test" in md1, "markdown surfaces batch_label")
    # purity: no taskctl/core symbols touched, so calling it cannot mutate
    # anything regardless of caller — assert function takes exactly one arg.
    import inspect
    sig = inspect.signature(review_summarizer.render_codex_handoff_markdown)
    check(list(sig.parameters.keys()) == ["report"], "render function signature is pure (report) -> str")


def test_fixture_queue_render() -> None:
    print("[test_fixture_queue_render] fixture queue -> markdown report, zero mutation")
    fixture_dir = Path(tempfile.mkdtemp(prefix="b119_handoff_md_fixture_"))
    try:
        cards = [CLEAN, RISKY]
        for c in cards:
            _write_card(fixture_dir, c)
        _write_queue(fixture_dir, cards)

        before = _hash_dir(fixture_dir)
        stub = FixtureTaskctl(fixture_dir)
        report = review_summarizer.build_codex_handoff_markdown_report(
            _run_taskctl=stub.run,
            _show_task=stub.show,
            batch_label="b119_e2e",
        )
        after = _hash_dir(fixture_dir)

        check(report.get("ok") is True, "report ok True")
        check(report.get("task_count") == 2, "task_count == 2")
        check(report.get("markdown_contract") == review_summarizer.MARKDOWN_RENDER_CONTRACT,
              "markdown_contract label present")
        check(report.get("contract") == "B116_v1_codex_handoff_e2e_readonly",
              "original B116 contract field preserved unchanged")
        check(isinstance(report.get("markdown"), str) and len(report["markdown"]) > 0,
              "markdown field present and non-empty")
        for marker in _REQUIRED_MD_MARKERS:
            check(marker in report["markdown"], f"markdown contains marker: {marker!r}")

        # every original B116 JSON field must still be present (additive-only)
        check("handoff_reports" in report, "handoff_reports field preserved")
        check("authority_flags" in report, "authority_flags field preserved")
        check("recommended_stage_commands" in report["handoff_reports"][0],
              "per-task recommended_stage_commands preserved")

        # ZERO MUTATION proofs
        check(before == after, "fixture dir sha256 identical before/after (zero mutation)")
        write_cmds = [c for c in stub.calls if c and c[0] in review_summarizer.WRITE_TASKCTL_COMMANDS]
        check(not write_cmds, "no write taskctl command ever issued")
        check(["review-queue"] in stub.calls, "review-queue read issued")
        check(all(c[0] in ("review-queue", "show") for c in stub.calls),
              "only read-only commands (review-queue/show) issued")

        # rendering the same fixture report twice is byte-identical
        md_again = review_summarizer.render_codex_handoff_markdown(report)
        check(md_again == report["markdown"], "re-rendering the same report dict is byte-identical")
    finally:
        shutil.rmtree(fixture_dir, ignore_errors=True)


def test_empty_queue_render() -> None:
    print("[test_empty_queue_render] empty fixture queue yields a valid empty markdown report")
    fixture_dir = Path(tempfile.mkdtemp(prefix="b119_handoff_md_empty_"))
    try:
        _write_queue(fixture_dir, [])
        stub = FixtureTaskctl(fixture_dir)
        report = review_summarizer.build_codex_handoff_markdown_report(
            _run_taskctl=stub.run, _show_task=stub.show
        )
        check(report.get("task_count") == 0, "empty: task_count 0")
        check(isinstance(report.get("markdown"), str), "empty: markdown is a string")
        check("tasks: 0" in report["markdown"], "empty: markdown reports tasks: 0")
        write_cmds = [c for c in stub.calls if c and c[0] in review_summarizer.WRITE_TASKCTL_COMMANDS]
        check(not write_cmds, "empty: no write command issued")
    finally:
        shutil.rmtree(fixture_dir, ignore_errors=True)


def test_server_tool_wiring_live_readonly() -> None:
    print("[test_server_tool_wiring_live_readonly] registered MCP tool reads live queue read-only")
    try:
        from geoai_task_mcp import server  # noqa: F401
    except Exception as exc:  # pragma: no cover - mcp SDK missing in minimal env
        check(False, f"import server (mcp SDK): {exc}")
        return

    tool = getattr(server, "geoai_task_codex_handoff_markdown", None)
    check(tool is not None, "server exposes geoai_task_codex_handoff_markdown")
    if tool is None:
        return

    before = core.run_taskctl(["review-queue"]).stdout
    try:
        result = tool()
    except TypeError as exc:  # pragma: no cover - decorator wrapped it
        check(False, f"tool not directly callable: {exc}")
        return
    after = core.run_taskctl(["review-queue"]).stdout

    check(isinstance(result, dict) and result.get("ok") is True, "server tool returns ok report")
    check(result.get("server_tool") == "geoai_task_codex_handoff_markdown", "server_tool label set")
    check(result.get("markdown_contract") == review_summarizer.MARKDOWN_RENDER_CONTRACT,
          "server tool markdown_contract label")
    check(isinstance(result.get("markdown"), str), "server tool markdown field is a string")
    af = result.get("authority_flags", {})
    check(af.get("write_gate_enabled") is True, "server tool write gate enabled (ALLOW_WRITES=0)")
    check(af.get("process_launch") is False, "server tool process_launch False")
    check(before == after, "live review-queue stdout identical before/after (no mutation)")


def main() -> int:
    print("=== B119 MCP Codex Handoff Markdown Render ===")
    test_pure_render_deterministic()
    test_fixture_queue_render()
    test_empty_queue_render()
    test_server_tool_wiring_live_readonly()
    print("")
    print(f"PASS={len(PASSES)} FAIL={len(FAILURES)}")
    if FAILURES:
        print("FAILURES:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL PASS")
    return 0


sys.exit(main())
PYEOF
RC=$?

echo ""
if [ $RC -eq 0 ]; then
    echo "=== test_mcp_codex_handoff_markdown_render_b119_v1.sh: PASS ==="
else
    echo "=== test_mcp_codex_handoff_markdown_render_b119_v1.sh: FAIL (exit=$RC) ==="
fi
exit $RC
