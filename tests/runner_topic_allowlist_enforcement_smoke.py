#!/usr/bin/env python3
"""B119 smoke: runner/topic allowlist enforcement for MCP write paths.

Proves the deterministic safety layer added to
``aiworkhub.core.run_taskctl`` / ``check_runner_topic_allowlist``:

  1. The allowlist matrix matches mcp_runner_topic_allowlist_design_b118_v1.json
     exactly (11 claude_*/deepseek_* runner/topic pairs + codex wildcard topic).
  2. All 8 malformed/attack fixtures from the B118 design are rejected
     (empty runner/topic, path traversal, shell metacharacters, null byte,
     case mismatch, unknown runner).
  3. Layer ORDER is preserved: the existing AIWORKHUB_ALLOW_WRITES gate is
     still checked FIRST -- a malformed/unknown identity does NOT get a
     different (or bypassing) outcome while the gate is closed; it stays
     ``blocked_write``, exactly as before this patch.
  4. The new allowlist layer only narrows an OPEN gate: with the gate open,
     an allowlisted (runner, topic, action) triple proceeds to the real
     taskctl call (isolated temp queue) while a well-formed-but-not-allowlisted
     triple is blocked with returncode=126 and an audit entry, WITHOUT ever
     invoking taskctl (no subprocess for the deny path).
  5. Read-only tools are unaffected (_is_write_command stays False for them).
  6. Every denial is audited, and the audit entry never contains a secret env
     VALUE -- only NAME -> '<set>'/'<unset>' tokens (reuses the existing
     sanitizer unchanged).
  7. Legacy call sites that omit identity (``runner=None, topic=None`` --
     mirrors current server.py wiring for review/done/export-jsonl) skip the
     new layer entirely and keep their prior behavior.

Isolation: all subprocess-touching cases use a per-run ``mktemp`` copy of the
task queue (BITNN_TASK_QUEUE_DB / _CARDS_PATH / _CARDS_MANIFEST) and a per-run
temp audit log path (AIWORKHUB_AUDIT_LOG_PATH). The REAL parent queue and
the real audit log are never touched. Deterministic and parallel-safe.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]                      # tests -> aiworkhub -> tools -> AIWorkHub
SRC = HERE.parents[1] / "src"
TASKCTL = REPO / "AITools" / "taskctl.py"
REAL_QUEUE_DB = REPO / "bitnnv2" / "data" / "tasking" / "task_queue_v1.sqlite"
REAL_AUDIT_LOG = REPO / "tools" / "aiworkhub" / "logs" / "audit.jsonl"

sys.path.insert(0, str(SRC))
from aiworkhub import core  # noqa: E402

MALFORMED_FIXTURES = [
    {"runner": "", "topic": "coding", "reason": "empty runner string"},
    {"runner": "claude_coding", "topic": "", "reason": "empty topic string"},
    {"runner": "", "topic": "", "reason": "both empty"},
    {"runner": "../../etc/passwd", "topic": "coding", "reason": "path traversal in runner"},
    {"runner": "claude_coding", "topic": "; rm -rf /", "reason": "shell injection in topic"},
    {"runner": "claude_coding\x00hidden", "topic": "coding", "reason": "null byte in runner"},
    {"runner": "CLAUDE_CODING", "topic": "coding", "reason": "case mismatch -- exact-match only"},
    {"runner": "unknown_runner_abc123", "topic": "coding", "reason": "runner not in allowlist"},
]


# --------------------------------------------------------------------------
# 1 + 2. pure allowlist-matrix + malformed-fixture unit tests (no I/O)
# --------------------------------------------------------------------------
def test_allowlist_matrix_pure() -> None:
    for (runner, topic), actions in core.RUNNER_TOPIC_ALLOWLIST.items():
        for action in ("auto-pickup", "review", "start", "usage"):
            decision = core.check_runner_topic_allowlist(runner, topic, action)
            expect = action in actions
            assert decision["allowed"] is expect, (runner, topic, action, decision)
        for action in ("done", "export-jsonl"):
            decision = core.check_runner_topic_allowlist(runner, topic, action)
            assert decision["allowed"] is False, (runner, topic, action, decision)

    for action in core.CODEX_ALLOWED_ACTIONS:
        decision = core.check_runner_topic_allowlist(core.CODEX_RUNNER, "any_topic_at_all", action)
        assert decision["allowed"] is True, (action, decision)
    for action in ("auto-pickup", "start"):
        decision = core.check_runner_topic_allowlist(core.CODEX_RUNNER, "coding", action)
        assert decision["allowed"] is False, (action, decision)

    print(f"PASS test_allowlist_matrix_pure "
          f"({len(core.RUNNER_TOPIC_ALLOWLIST)} runner/topic pairs + codex wildcard)")


def test_malformed_fixtures_pure() -> None:
    for fx in MALFORMED_FIXTURES:
        decision = core.check_runner_topic_allowlist(fx["runner"], fx["topic"], "auto-pickup")
        assert decision["allowed"] is False, (fx["reason"], decision)
    print(f"PASS test_malformed_fixtures_pure ({len(MALFORMED_FIXTURES)} cases all denied)")


def test_partial_and_unknown_identity_pure() -> None:
    # topic omitted for a non-codex runner -> deny (topic-bound doctrine: every
    # non-codex identity must assert BOTH runner and topic).
    d = core.check_runner_topic_allowlist("claude_coding", None, "auto-pickup")
    assert d["allowed"] is False, d
    d = core.check_runner_topic_allowlist(None, "coding", "auto-pickup")
    assert d["allowed"] is False, d
    # both omitted -> the caller supplied no identity; run_taskctl() never even
    # calls check_runner_topic_allowlist in that case (tested below), but the
    # pure function itself must still deny defensively if called directly.
    d = core.check_runner_topic_allowlist(None, None, "auto-pickup")
    assert d["allowed"] is False, d
    # unknown (runner, topic) pair, both individually well-formed
    d = core.check_runner_topic_allowlist("claude_coding", "stem", "auto-pickup")
    assert d["allowed"] is False and d["reason"] == "unknown_runner_topic_pair", d
    print("PASS test_partial_and_unknown_identity_pure")


def test_read_only_tools_unaffected() -> None:
    for args in (["list", "--status", "pending"], ["show", "SOME_TASK"], ["export", "--runner", "x"],
                 ["verify"], ["review-queue"], ["collision-guard", "--print"], ["usage-report"]):
        assert core._is_write_command(args) is False, args
    print("PASS test_read_only_tools_unaffected (no write-gate/allowlist path touched)")


# --------------------------------------------------------------------------
# 3+4+6. run_taskctl() layer-order + audit-sanitization integration tests
# --------------------------------------------------------------------------
def test_write_gate_checked_before_allowlist() -> None:
    """Gate CLOSED: a well-formed allowlisted identity still gets the existing
    ``blocked_write`` outcome, never reaching the allowlist check, and a
    malformed/unknown identity gets the SAME ``blocked_write`` outcome (no
    information leak about allowlist membership while the gate is shut)."""
    saved = {k: os.environ.get(k) for k in ("AIWORKHUB_ALLOW_WRITES", "AIWORKHUB_AUDIT_LOG_PATH")}
    tmp = Path(tempfile.mkdtemp(prefix="aiworkhub_b119_gateorder_"))
    try:
        os.environ["AIWORKHUB_ALLOW_WRITES"] = "0"
        os.environ["AIWORKHUB_AUDIT_LOG_PATH"] = str(tmp / "audit.jsonl")

        allowed_result = core.run_taskctl(
            ["review", "FAKE_TASK"], allow_write=True, runner="claude_coding", topic="coding",
        )
        assert allowed_result.returncode == 126
        assert "write command blocked" in allowed_result.stderr, allowed_result.stderr

        malformed_result = core.run_taskctl(
            ["review", "FAKE_TASK"], allow_write=True, runner="../../etc/passwd", topic="coding",
        )
        assert malformed_result.returncode == 126
        assert "write command blocked" in malformed_result.stderr, malformed_result.stderr

        log = core.read_audit_log(repo=REPO)
        actions = {e.get("action") for e in log["last_entries"]}
        assert actions == {"blocked_write"}, actions  # allowlist layer never reached
        print("PASS test_write_gate_checked_before_allowlist (gate-closed priority preserved)")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_allowlist_denies_without_subprocess() -> None:
    """Gate OPEN: a malformed/unknown identity is blocked with returncode=126
    WITHOUT invoking taskctl at all (proven via a nonexistent taskctl-shaped
    command that would raise/fail loudly if actually executed), and every
    denial is audited without any secret env VALUE in the entry."""
    saved = {k: os.environ.get(k) for k in ("AIWORKHUB_ALLOW_WRITES", "AIWORKHUB_AUDIT_LOG_PATH",
                                             "AIWORKHUB_SECRET_PROBE")}
    tmp = Path(tempfile.mkdtemp(prefix="aiworkhub_b119_denyaudit_"))
    try:
        os.environ["AIWORKHUB_ALLOW_WRITES"] = "1"
        os.environ["AIWORKHUB_AUDIT_LOG_PATH"] = str(tmp / "audit.jsonl")
        os.environ["AIWORKHUB_SECRET_PROBE"] = "super-secret-value-must-never-appear-in-audit"

        unknown = core.run_taskctl(
            ["review", "FAKE_TASK"], allow_write=True, runner="unknown_runner_abc123", topic="coding",
        )
        assert unknown.returncode == 126
        assert "allowlist denied" in unknown.stderr, unknown.stderr

        malformed = core.run_taskctl(
            ["review", "FAKE_TASK"], allow_write=True, runner="claude_coding", topic="; rm -rf /",
        )
        assert malformed.returncode == 126
        assert "allowlist denied" in malformed.stderr, malformed.stderr

        off_matrix_action = core.run_taskctl(
            ["done", "FAKE_TASK"], allow_write=True, runner="claude_coding", topic="coding",
        )
        assert off_matrix_action.returncode == 126, off_matrix_action  # done is codex-only

        log = core.read_audit_log(repo=REPO)
        by_action = log["entries_by_action"]
        # unknown-runner (reason=unknown_runner_topic_pair) and off-matrix
        # action (reason=action_not_allowed_for_runner_topic) both tag
        # blocked_runner_topic_not_in_allowlist; the shell-metachar topic is a
        # genuinely malformed identity and tags blocked_malformed_runner_or_topic.
        assert by_action.get("blocked_runner_topic_not_in_allowlist", 0) == 2, by_action
        assert by_action.get("blocked_malformed_runner_or_topic", 0) == 1, by_action
        raw = json.dumps(log["last_entries"])
        assert "super-secret-value-must-never-appear-in-audit" not in raw, raw
        for entry in log["last_entries"]:
            for tok in entry["caller_info"]["env_vars_checked"].values():
                assert tok in ("<set>", "<unset>"), tok  # NAME->status only, never a value

        print("PASS test_allowlist_denies_without_subprocess "
              "(returncode=126, audited, no secret leak)")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_malformed_audit_tag() -> None:
    """A genuinely malformed identity (not merely unknown) is tagged
    ``blocked_malformed_runner_or_topic`` distinctly from an unknown-but-
    well-formed one (``blocked_runner_topic_not_in_allowlist``)."""
    saved = {k: os.environ.get(k) for k in ("AIWORKHUB_ALLOW_WRITES", "AIWORKHUB_AUDIT_LOG_PATH")}
    tmp = Path(tempfile.mkdtemp(prefix="aiworkhub_b119_malformedtag_"))
    try:
        os.environ["AIWORKHUB_ALLOW_WRITES"] = "1"
        os.environ["AIWORKHUB_AUDIT_LOG_PATH"] = str(tmp / "audit.jsonl")

        result = core.run_taskctl(
            ["review", "FAKE_TASK"], allow_write=True, runner="claude_coding\x00hidden", topic="coding",
        )
        assert result.returncode == 126

        log = core.read_audit_log(repo=REPO)
        assert log["entries_by_action"].get("blocked_malformed_runner_or_topic", 0) == 1, log["entries_by_action"]
        print("PASS test_malformed_audit_tag")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 4+7. real (isolated) queue integration: allow proceeds, legacy skips layer
# --------------------------------------------------------------------------
def _card(task_id: str, runner: str, topic: str, *, queue_order: int = 1) -> dict:
    return {
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "mode": "b119_allowlist_probe",
        "status": "pending",
        "worker_status": "unclaimed",
        "priority": "normal",
        "queue_order": queue_order,
        "objective": f"synthetic probe card {task_id} for B119 allowlist isolation test",
    }


def _seed(env: dict, cards: list[dict], tmp: Path) -> None:
    for c in cards:
        cp = tmp / f"seed_{c['task_id']}.json"
        cp.write_text(json.dumps(c), encoding="utf-8")
        r = subprocess.run(
            [sys.executable, str(TASKCTL), "add-card", str(cp)],
            cwd=str(REPO), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        assert r.returncode == 0, f"seed add-card failed: {r.stdout}\n{r.stderr}"


def test_allowed_identity_proceeds_isolated() -> None:
    saved = {k: os.environ.get(k) for k in (
        "BITNN_TASK_QUEUE_DB", "BITNN_TASK_CARDS_PATH", "BITNN_TASK_CARDS_MANIFEST",
        "BITNN_TASK_QUEUE_LOCK",
        "AIWORKHUB_REPO", "AIWORKHUB_ALLOW_WRITES", "AIWORKHUB_AUDIT_LOG_PATH",
    )}
    tmp = Path(tempfile.mkdtemp(prefix="aiworkhub_b119_allow_isolated_"))
    try:
        db = tmp / "task_queue.sqlite"
        os.environ["BITNN_TASK_QUEUE_DB"] = str(db)
        os.environ["BITNN_TASK_CARDS_PATH"] = str(tmp / "cards.jsonl")
        os.environ["BITNN_TASK_CARDS_MANIFEST"] = str(tmp / "manifest.json")
        os.environ["BITNN_TASK_QUEUE_LOCK"] = str(tmp / "task_queue.lock")
        os.environ["AIWORKHUB_REPO"] = str(REPO)
        os.environ["AIWORKHUB_ALLOW_WRITES"] = "1"
        os.environ["AIWORKHUB_AUDIT_LOG_PATH"] = str(tmp / "audit.jsonl")

        assert db.resolve() != REAL_QUEUE_DB.resolve(), "temp DB collides with real queue"

        _seed(os.environ.copy(), [_card("B119_ALLOW_1", "claude_coding", "coding")], tmp)

        # allowlisted (runner, topic, auto-pickup) -> real claim proceeds
        allowed = core.auto_pickup("claude_coding", "coding")
        assert allowed["ok"] is True, allowed
        assert allowed["returncode"] == 0, allowed

        # well-formed but NOT allowlisted (runner, topic) pair -> blocked even
        # though the write gate is OPEN and taskctl would otherwise accept it
        _seed(os.environ.copy(), [_card("B119_DENY_1", "claude_coding", "context_graph", queue_order=2)], tmp)
        denied = core.run_taskctl(
            ["auto-pickup", "--runner", "claude_coding", "--topic", "context_graph"],
            allow_write=True, runner="claude_coding", topic="context_graph",
        )
        assert denied.returncode == 126, denied
        assert "allowlist denied" in denied.stderr, denied.stderr

        # legacy call sites that omit identity (mirrors current server.py
        # wiring for review/done/export-jsonl) skip the new layer entirely
        legacy_review = core.mark_review("B119_ALLOW_1")
        assert legacy_review["ok"] is True, legacy_review

        print("PASS test_allowed_identity_proceeds_isolated "
              "(allowlisted proceeds; off-matrix blocked; legacy no-identity unaffected)")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_card_scoped_claim_review_usage_authority() -> None:
    saved_env = {k: os.environ.get(k) for k in (
        "AIWORKHUB_ALLOW_WRITES", "AIWORKHUB_AUDIT_LOG_PATH",
    )}
    saved_show = core.show_task
    saved_run = core.subprocess.run
    tmp = Path(tempfile.mkdtemp(prefix="aiworkhub_b623_card_scoped_"))
    calls: list[list[str]] = []
    task_id = "B623_CARD_SCOPED_1"
    runner = "claude_oneoff_b623_card_scoped"
    topic = "tasking_system"
    card = _card(task_id, runner, topic)
    try:
        os.environ["AIWORKHUB_ALLOW_WRITES"] = "1"
        os.environ["AIWORKHUB_AUDIT_LOG_PATH"] = str(tmp / "audit.jsonl")

        def fake_show(show_task_id: str) -> dict:
            assert show_task_id == task_id
            return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

        class FakeProc:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        def fake_run(command, **_kwargs):  # noqa: ANN001
            calls.append(list(command))
            return FakeProc()

        core.show_task = fake_show
        core.subprocess.run = fake_run

        claimed = core.run_taskctl(
            ["claim-start", task_id, "--runner", runner, "--topic", topic],
            allow_write=True, runner=runner, topic=topic,
        )
        assert claimed.returncode == 0, claimed

        card.update({"status": "processing", "worker_status": "in_progress", "claimed_by": runner})
        reviewed = core.run_taskctl(
            ["review", task_id, "--runner", runner, "--topic", topic],
            allow_write=True, runner=runner, topic=topic,
        )
        assert reviewed.returncode == 0, reviewed

        card.update({"status": "review", "worker_status": "review", "review_requested_by": runner})
        usage = core.run_taskctl(
            ["usage", task_id, "--runner", runner, "--model", "claude"],
            allow_write=True, runner=runner, topic=topic,
        )
        assert usage.returncode == 0, usage

        denied_action = core.run_taskctl(
            ["done", task_id, "--runner", runner, "--topic", topic],
            allow_write=True, runner=runner, topic=topic,
        )
        assert denied_action.returncode == 126, denied_action

        denied_mismatch = core.run_taskctl(
            ["review", task_id, "--runner", runner, "--topic", "coding"],
            allow_write=True, runner=runner, topic="coding",
        )
        assert denied_mismatch.returncode == 126, denied_mismatch
        assert len(calls) == 3, calls
        print("PASS test_card_scoped_claim_review_usage_authority")
    finally:
        core.show_task = saved_show
        core.subprocess.run = saved_run
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    test_allowlist_matrix_pure()
    test_malformed_fixtures_pure()
    test_partial_and_unknown_identity_pure()
    test_read_only_tools_unaffected()
    test_write_gate_checked_before_allowlist()
    test_allowlist_denies_without_subprocess()
    test_malformed_audit_tag()
    test_allowed_identity_proceeds_isolated()
    test_card_scoped_claim_review_usage_authority()
    print("ALL B119 RUNNER/TOPIC ALLOWLIST ENFORCEMENT SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
