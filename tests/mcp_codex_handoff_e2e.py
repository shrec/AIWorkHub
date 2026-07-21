#!/usr/bin/env python3
"""E2E test: MCP Codex-ready HANDOFF report tool (B116).

Builds a TEMP fixture review queue (per-test mktemp dir), invokes the
review_summarizer handoff builder AND the registered MCP server tool, and
asserts:

  * every handoff field present per task (task_id, runner, topic,
    allowed_writes, validation_commands, risks, commit_hygiene,
    recommended_stage_commands, scoped_paths);
  * ZERO mutation of the queue — the fixture dir sha256 is identical before
    and after, AND the stub records that NO write taskctl command
    (done/review/start/auto-pickup/export-jsonl/usage) was ever issued;
  * write gate default-off; readonly / authority flags correct;
  * empty-queue and fixture-queue cases both yield a valid report;
  * task_ids filtering and batch_label pass-through work;
  * the registered server tool `aiworkhub_task_codex_handoff` is wired and reads
    the LIVE queue read-only (stdout identical before/after).

Isolation-safe / parallel-safe: mktemp dir, no fixed shared path, no writes
to the real parent queue. Run via test_mcp_codex_handoff_e2e_b116_v1.sh.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from aiworkhub import core, review_summarizer


FAILURES: list[str] = []
PASSES: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        PASSES.append(label)
        print(f"  PASS — {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL — {label}")


# ── Fixture taskctl stub (READ-ONLY over a temp dir) ────────────────────
class _ResultLike:
    """Mimics core.TaskCtlResult (has .stdout/.returncode/.stderr)."""

    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FixtureTaskctl:
    """Read-only fixture queue backed by files in a temp dir.

    `run` serves ``review-queue`` from review_queue.txt; `show` serves each
    card from ``<task_id>.json``. Records every command it is asked to run so
    the test can prove no write command was ever issued. Opens files 'r' only
    — it never writes, so the fixture dir hash cannot change.
    """

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
    import json

    (fixture_dir / f"{card['task_id']}.json").write_text(
        json.dumps(card, indent=2), encoding="utf-8"
    )


def _write_queue(fixture_dir: Path, cards: list[dict[str, Any]]) -> None:
    lines = ["=== Codex Review Queue ({}) ===".format(len(cards))]
    for c in cards:
        lines.append(f"  [{c['topic']}] [{c['runner']}] {c['task_id']}")
    (fixture_dir / "review_queue.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


# ── Fixture cards ───────────────────────────────────────────────────────
CLEAN = {
    "task_id": "CLEAN_TASK_B116_V1",
    "runner": "worker_clean",
    "topic": "coding",
    "status": "processing",
    "worker_status": "in_progress",
    "mode": "clean_mode",
    "priority": "p_clean",
    "objective": "A well-scoped task with validation and full commit guards.",
    "allowed_writes": [
        "bitnnv2/tests/test_clean_b116.py",
        "bitnnv2/eval/clean_b116_v1.json",
    ],
    "validation": ["bash bitnnv2/tests/test_clean_b116.sh", "python3 AITools/taskctl.py verify"],
    "commit_contract": "NO_COMMIT preferred; Codex finalizes explicit allowed_writes. Never git add -A / git add . / mixed-task commit.",
    "forbidden": ["git_add_A", "mixed_task_commit"],
}

RISKY = {
    "task_id": "RISKY_TASK_B116_V1",
    "runner": "worker_risky",
    "topic": "task_mcp",
    "status": "processing",
    "worker_status": "in_progress",
    "mode": "risky_mode",
    "priority": "p_risky",
    "objective": "Touches shared server.py + a manifest and declares no validation.",
    "allowed_writes": [
        "tools/geoai-task-mcp/src/aiworkhub/server.py",
        "bitnnv2/data/tasking/machine_task_cards_manifest_v1.json",
    ],
    "validation": [],  # missing validation -> high risk
    "commit_contract": "",  # empty -> hygiene warn
    "forbidden": [],  # missing guards -> low risk
}

MEDIUM = {
    "task_id": "MEDIUM_TASK_B116_V1",
    "runner": "worker_medium",
    "topic": "task_mcp",
    "status": "processing",
    "worker_status": "in_progress",
    "mode": "medium_mode",
    "priority": "p_medium",
    "objective": "Normal scoped task carrying a declared medium human-brief risk.",
    "allowed_writes": ["bitnnv2/tests/test_medium_b116.py"],
    "validation": ["bash bitnnv2/tests/test_medium_b116.sh"],
    "commit_contract": "NO_COMMIT preferred. Never git add -A / mixed-task commit.",
    "forbidden": ["git_add_A", "mixed_task_commit"],
    "human_brief": {"risk": "medium", "intent": "demo declared risk"},
}

_REQUIRED_FIELDS = (
    "task_id",
    "runner",
    "topic",
    "allowed_writes",
    "allowed_writes_count",
    "validation_commands",
    "validation_present",
    "forbidden",
    "risks",
    "risk_level",
    "commit_hygiene",
    "recommended_stage_commands",
    "scoped_paths",
)


def test_fixture_queue() -> None:
    print("[test_fixture_queue] non-empty fixture queue, all fields + zero mutation")
    fixture_dir = Path(tempfile.mkdtemp(prefix="b116_handoff_fixture_"))
    try:
        cards = [CLEAN, RISKY, MEDIUM]
        for c in cards:
            _write_card(fixture_dir, c)
        _write_queue(fixture_dir, cards)

        before = _hash_dir(fixture_dir)
        stub = FixtureTaskctl(fixture_dir)
        report = review_summarizer.build_codex_handoff_report(
            _run_taskctl=stub.run,
            _show_task=stub.show,
            batch_label="b116_e2e",
        )
        after = _hash_dir(fixture_dir)

        # top-level shape
        check(report.get("ok") is True, "report ok is True")
        check(report.get("contract") == "B116_v1_codex_handoff_e2e_readonly", "handoff contract label")
        check(report.get("readonly") is True, "report readonly True")
        check(report.get("mutation_performed") is False, "report mutation_performed False")
        check(report.get("task_count") == 3, "task_count == 3")
        check(isinstance(report.get("handoff_reports"), list) and len(report["handoff_reports"]) == 3,
              "handoff_reports has 3 entries")
        check(report.get("batch_label") == "b116_e2e", "batch_label passthrough")

        # ZERO MUTATION proofs
        check(before == after, "fixture dir sha256 identical before/after (zero mutation)")
        write_cmds = [c for c in stub.calls if c and c[0] in review_summarizer.WRITE_TASKCTL_COMMANDS]
        check(not write_cmds, "no write taskctl command ever issued")
        check(["review-queue"] in stub.calls, "review-queue read issued")
        check(all(c[0] in ("review-queue", "show") for c in stub.calls),
              "only read-only commands (review-queue/show) issued")

        # per-task fields present
        by_id = {r["task_id"]: r for r in report["handoff_reports"]}
        for tid, rep in by_id.items():
            missing = [f for f in _REQUIRED_FIELDS if f not in rep]
            check(not missing, f"{tid}: all handoff fields present")
            cmds = rep.get("recommended_stage_commands", [])
            check(any("stage " + tid in c for c in cmds), f"{tid}: stage command present")
            check(any("guard-staged " + tid in c for c in cmds), f"{tid}: guard-staged command present")
            check(any("done " + tid in c for c in cmds), f"{tid}: done command present")
            check(rep["scoped_paths"] == rep["allowed_writes"], f"{tid}: scoped_paths == allowed_writes")
            check(isinstance(rep["commit_hygiene"], dict) and "status" in rep["commit_hygiene"],
                  f"{tid}: commit_hygiene has status")

        # clean task: low risk, hygiene ok
        clean = by_id["CLEAN_TASK_B116_V1"]
        check(clean["risk_level"] == "low", "clean task risk_level low")
        check(clean["commit_hygiene"]["status"] == "ok", "clean task hygiene ok")
        check(clean["validation_present"] is True, "clean task validation present")

        # risky task: high risk (missing validation), shared-file + hygiene risks
        risky = by_id["RISKY_TASK_B116_V1"]
        codes = {r["code"] for r in risky["risks"]}
        check(risky["risk_level"] == "high", "risky task risk_level high")
        check("missing_validation" in codes, "risky task flags missing_validation")
        check("shared_file_write" in codes, "risky task flags shared_file_write (server.py/manifest)")
        check(risky["commit_hygiene"]["status"] == "warn", "risky task hygiene warn")
        check(risky["validation_present"] is False, "risky task validation absent")

        # medium task: declared human_brief risk surfaced
        medium = by_id["MEDIUM_TASK_B116_V1"]
        mcodes = {r["code"] for r in medium["risks"]}
        check("declared_risk" in mcodes, "medium task surfaces declared_risk")
        check(medium["risk_level"] in ("medium", "high"), "medium task risk_level >= medium")

        # summary rollups
        summ = report["summary"]
        check(summ["total_tasks"] == 3, "summary total_tasks 3")
        check("RISKY_TASK_B116_V1" in summ["tasks_missing_validation"], "summary lists missing-validation task")
        check("RISKY_TASK_B116_V1" in summ["commit_hygiene_warn"], "summary lists hygiene-warn task")
        check(sum(summ["risk_levels"].values()) == 3, "summary risk_levels sum to 3")

        # authority flags: read-only, gate on, no launch
        af = report["authority_flags"]
        check(af.get("readonly") is True, "authority readonly True")
        check(af.get("process_launch") is False, "authority process_launch False")
        check(af.get("agent_launch") is False, "authority agent_launch False")
        check(af.get("queue_write") is False, "authority queue_write False")
        check(af.get("audit_write") is False, "authority audit_write False")
        check(af.get("write_gate_enabled") is True, "authority write_gate_enabled True (ALLOW_WRITES=0)")
        check(af.get("subprocess_launch_tripwire_zero") is True, "authority subprocess tripwire zero")
    finally:
        shutil.rmtree(fixture_dir, ignore_errors=True)


def test_empty_queue() -> None:
    print("[test_empty_queue] empty fixture queue yields valid empty report")
    fixture_dir = Path(tempfile.mkdtemp(prefix="b116_handoff_empty_"))
    try:
        _write_queue(fixture_dir, [])
        before = _hash_dir(fixture_dir)
        stub = FixtureTaskctl(fixture_dir)
        report = review_summarizer.build_codex_handoff_report(
            _run_taskctl=stub.run, _show_task=stub.show
        )
        after = _hash_dir(fixture_dir)

        check(report.get("ok") is True, "empty: ok True")
        check(report.get("task_count") == 0, "empty: task_count 0")
        check(report.get("handoff_reports") == [], "empty: handoff_reports []")
        check(report.get("mutation_performed") is False, "empty: mutation_performed False")
        check(report["summary"]["total_tasks"] == 0, "empty: summary total 0")
        check(before == after, "empty: fixture dir hash unchanged")
        write_cmds = [c for c in stub.calls if c and c[0] in review_summarizer.WRITE_TASKCTL_COMMANDS]
        check(not write_cmds, "empty: no write command issued")
    finally:
        shutil.rmtree(fixture_dir, ignore_errors=True)


def test_task_id_filter() -> None:
    print("[test_task_id_filter] task_ids filter narrows the report")
    fixture_dir = Path(tempfile.mkdtemp(prefix="b116_handoff_filter_"))
    try:
        cards = [CLEAN, RISKY, MEDIUM]
        for c in cards:
            _write_card(fixture_dir, c)
        _write_queue(fixture_dir, cards)
        stub = FixtureTaskctl(fixture_dir)
        report = review_summarizer.build_codex_handoff_report(
            _run_taskctl=stub.run,
            _show_task=stub.show,
            task_ids=["CLEAN_TASK_B116_V1"],
        )
        check(report.get("task_count") == 1, "filter: task_count 1")
        check(report.get("filtered_by_task_ids") is True, "filter: filtered_by_task_ids flag")
        check(report["handoff_reports"][0]["task_id"] == "CLEAN_TASK_B116_V1", "filter: correct task returned")
    finally:
        shutil.rmtree(fixture_dir, ignore_errors=True)


def test_server_tool_wiring_live_readonly() -> None:
    print("[test_server_tool_wiring_live_readonly] registered MCP tool reads live queue read-only")
    try:
        from aiworkhub import server  # noqa: F401
    except Exception as exc:  # pragma: no cover - mcp SDK missing in minimal env
        check(False, f"import server (mcp SDK): {exc}")
        return

    tool = getattr(server, "aiworkhub_task_codex_handoff", None)
    check(tool is not None, "server exposes aiworkhub_task_codex_handoff")
    if tool is None:
        return

    # live review-queue stdout snapshot before/after (pure read, both sides)
    before = core.run_taskctl(["review-queue"]).stdout
    try:
        result = tool()
    except TypeError as exc:  # pragma: no cover - decorator wrapped it
        check(False, f"tool not directly callable: {exc}")
        return
    after = core.run_taskctl(["review-queue"]).stdout

    check(isinstance(result, dict) and result.get("ok") is True, "server tool returns ok report")
    check(result.get("server_tool") == "aiworkhub_task_codex_handoff", "server_tool label set")
    check(result.get("contract") == "B116_v1_codex_handoff_e2e_readonly", "server tool contract label")
    check(result.get("readonly") is True, "server tool readonly True")
    check(result.get("mutation_performed") is False, "server tool mutation_performed False")
    check(isinstance(result.get("handoff_reports"), list), "server tool handoff_reports is list")
    af = result.get("authority_flags", {})
    check(af.get("write_gate_enabled") is True, "server tool write gate enabled (ALLOW_WRITES=0)")
    check(af.get("process_launch") is False, "server tool process_launch False")
    check(before == after, "live review-queue stdout identical before/after (no mutation)")


def main() -> int:
    print("=== B116 MCP Codex Handoff E2E ===")
    test_fixture_queue()
    test_empty_queue()
    test_task_id_filter()
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


if __name__ == "__main__":
    sys.exit(main())
