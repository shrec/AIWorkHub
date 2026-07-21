#!/usr/bin/env python3
"""B118 smoke: MCP auto-pickup DRY-RUN preview.

Proves the read-only ``core.auto_pickup_dryrun`` path:
  1. reports the task ``auto_pickup`` WOULD claim,
  2. mutates NOTHING (queue byte-identical + logical-state-identical
     before/after — snapshot proven),
  3. respects the ``--runner``/``--topic`` filters (visible in the report),
  4. does NOT bypass the write gate — the real ``auto_pickup`` claim path stays
     blocked with ``AIWORKHUB_ALLOW_WRITES=0``, and the blocked attempt
     also leaves the queue unmutated.

Isolation: operates on a per-run ``mktemp`` copy of the task queue via the
``BITNN_TASK_QUEUE_DB`` / ``BITNN_TASK_CARDS_PATH`` / ``BITNN_TASK_CARDS_MANIFEST``
env overrides. The REAL parent queue is never addressed. Deterministic and
parallel-safe (unique temp dir, process-local env).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]                      # tests -> aiworkhub -> tools -> AIWorkHub
SRC = HERE.parents[1] / "src"
TASKCTL = REPO / "AITools" / "taskctl.py"
REAL_QUEUE_DB = REPO / "bitnnv2" / "data" / "tasking" / "task_queue_v1.sqlite"

sys.path.insert(0, str(SRC))
from aiworkhub import core  # noqa: E402

RUNNER = "claude_b118_dryrun_probe"
OTHER_RUNNER = "claude_b118_other_probe"
TOPIC_X = "b118_topic_x"
TOPIC_Y = "b118_topic_y"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _card(task_id: str, runner: str, topic: str, *, worker_status: str = "unclaimed",
          status: str = "pending", queue_order: int = 1000) -> dict:
    return {
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "mode": "write_gate_safe_dryrun_no_workflow_switch",
        "status": status,
        "worker_status": worker_status,
        "priority": "normal",
        "queue_order": queue_order,
        "objective": f"synthetic probe card {task_id} for B118 dry-run isolation test",
    }


def _seed(env: dict, cards: list[dict]) -> None:
    """Seed each card into the temp queue DB via `taskctl add-card`."""
    for c in cards:
        cp = Path(env["BITNN_TASK_CARDS_MANIFEST"]).parent / f"seed_{c['task_id']}.json"
        cp.write_text(json.dumps(c), encoding="utf-8")
        r = subprocess.run(
            [sys.executable, str(TASKCTL), "add-card", str(cp)],
            cwd=str(REPO), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        assert r.returncode == 0, f"seed add-card failed: {r.stdout}\n{r.stderr}"
        assert "UPSERTED" in r.stdout, r.stdout


def _checkpoint(db: Path) -> None:
    """Force a clean, WAL-merged state so main-file byte hashes are stable."""
    if not db.exists():
        return
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()


def _sha256(path: Path) -> str:
    if not path.exists():
        return "<absent>"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _logical(db: Path) -> list[tuple]:
    """Authoritative queue state: sorted per-task lifecycle tuples."""
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT task_id, status, worker_status, "
            "IFNULL(claimed_by,''), IFNULL(started_at,'') "
            "FROM tasks ORDER BY task_id"
        ).fetchall()
    finally:
        conn.close()
    return [tuple(r) for r in rows]


def _snapshot(db: Path) -> tuple[str, list[tuple]]:
    # byte hash FIRST (pure file read), then logical (SQLite) — ordering keeps
    # the byte hash reflecting the file before any of our own SQLite opens.
    return _sha256(db), _logical(db)


# --------------------------------------------------------------------------
# 1. pure selection unit test (runner/topic/claim/lifecycle filtering + order)
# --------------------------------------------------------------------------
def test_eligible_pure() -> None:
    A = _card("B118_A", RUNNER, TOPIC_X, queue_order=1)
    B = _card("B118_B", RUNNER, TOPIC_X, worker_status="claimed", queue_order=2)
    C = _card("B118_C", RUNNER, TOPIC_Y, queue_order=3)
    D = _card("B118_D", OTHER_RUNNER, TOPIC_X, queue_order=4)
    E = _card("B118_E", RUNNER, TOPIC_X, status="finished", queue_order=5)
    F = _card("B118_F", RUNNER, TOPIC_X, queue_order=6)
    rows = [A, B, C, D, E, F]

    ids = lambda cs: [c["task_id"] for c in cs]

    # runner+topic filter: claimed(B), other-topic(C), other-runner(D),
    # finished(E) all excluded; order preserved -> [A, F]
    assert ids(core.eligible_dryrun_candidates(rows, RUNNER, TOPIC_X)) == ["B118_A", "B118_F"]
    # topic=None -> any topic for RUNNER (still excludes claimed/finished/other-runner)
    assert ids(core.eligible_dryrun_candidates(rows, RUNNER, None)) == ["B118_A", "B118_C", "B118_F"]
    # different topic changes the result
    assert ids(core.eligible_dryrun_candidates(rows, RUNNER, TOPIC_Y)) == ["B118_C"]
    # unknown runner -> nothing
    assert core.eligible_dryrun_candidates(rows, "NOBODY", None) == []
    print("PASS test_eligible_pure (runner/topic/claim/lifecycle filter + order)")


# --------------------------------------------------------------------------
# 2. integration: dry-run against an isolated temp queue, prove no mutation
# --------------------------------------------------------------------------
def test_dryrun_no_mutation_isolated() -> None:
    saved = {k: os.environ.get(k) for k in (
        "BITNN_TASK_QUEUE_DB", "BITNN_TASK_CARDS_PATH", "BITNN_TASK_CARDS_MANIFEST",
        "AIWORKHUB_REPO", "AIWORKHUB_ALLOW_WRITES",
    )}
    tmp = Path(tempfile.mkdtemp(prefix="aiworkhub_b118_dryrun_"))
    try:
        db = tmp / "task_queue.sqlite"
        os.environ["BITNN_TASK_QUEUE_DB"] = str(db)
        os.environ["BITNN_TASK_CARDS_PATH"] = str(tmp / "cards.jsonl")
        os.environ["BITNN_TASK_CARDS_MANIFEST"] = str(tmp / "manifest.json")
        os.environ["AIWORKHUB_REPO"] = str(REPO)
        os.environ["AIWORKHUB_ALLOW_WRITES"] = "0"   # write gate OFF

        # isolation sanity: temp DB is NOT the real parent queue
        assert db.resolve() != REAL_QUEUE_DB.resolve(), "temp DB collides with real queue"

        # Seed: T1 (R,X) should be the pick; T2 (R,Y) other topic; T3 (OTHER,X)
        # other runner. First-by-queue-order pick within a runner/topic group.
        _seed(os.environ.copy(), [
            _card("B118_T1", RUNNER, TOPIC_X, queue_order=1),
            _card("B118_T2", RUNNER, TOPIC_Y, queue_order=2),
            _card("B118_T3", OTHER_RUNNER, TOPIC_X, queue_order=3),
        ])
        _checkpoint(db)

        before = _snapshot(db)
        assert before[1], "seed produced empty queue"

        # --- dry-run for (RUNNER, TOPIC_X) ---
        res = core.auto_pickup_dryrun(RUNNER, TOPIC_X)
        assert res["ok"] is True, res
        assert res["dry_run"] is True
        assert res["would_claim_task_id"] == "B118_T1", res
        assert res["candidate"]["task_id"] == "B118_T1"
        # runner/topic filtering is visible in the report
        assert res["filtering"]["runner_filter"] == RUNNER
        assert res["filtering"]["topic_filter"] == TOPIC_X
        assert res["filtering"]["eligible_unclaimed"] == 1, res["filtering"]
        # mutation + write-gate authority flags
        assert res["mutation"]["queue_mutated"] is False
        assert res["mutation"]["write_gate_bypassed"] is False
        assert res["mutation"]["write_command_invoked"] is False
        assert res["mutation"]["real_auto_pickup_write_gated"] is True
        assert res["mutation"]["writes_allowed_env"] is False
        # only the read-only `export` command was used
        assert "export" in (res["source_command"] or []), res["source_command"]
        assert "auto-pickup" not in (res["source_command"] or [])

        after = _snapshot(db)
        assert after == before, f"dry-run MUTATED the queue!\nbefore={before}\nafter={after}"

        # --- topic filtering actually changes the candidate ---
        res_y = core.auto_pickup_dryrun(RUNNER, TOPIC_Y)
        assert res_y["would_claim_task_id"] == "B118_T2", res_y
        res_other = core.auto_pickup_dryrun(OTHER_RUNNER, TOPIC_X)
        assert res_other["would_claim_task_id"] == "B118_T3", res_other
        # runner with no eligible task -> no candidate, still ok, still no mutation
        res_none = core.auto_pickup_dryrun("NOBODY_RUNNER", TOPIC_X)
        assert res_none["ok"] is True and res_none["would_claim_task_id"] is None, res_none
        assert res_none["candidate"] is None
        assert _snapshot(db) == before, "dry-run (empty/other filters) mutated queue"

        # --- real auto_pickup stays write-gated (NOT bypassed by dry-run) ---
        blocked = core.auto_pickup(RUNNER, TOPIC_X)
        assert blocked["ok"] is False, blocked
        assert blocked["returncode"] == 126, blocked
        assert "write command blocked" in (blocked.get("stderr") or ""), blocked
        assert _snapshot(db) == before, "blocked auto_pickup MUTATED the queue!"

        print("PASS test_dryrun_no_mutation_isolated "
              "(byte+logical identical; runner/topic filtering; gate intact)")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    test_eligible_pure()
    test_dryrun_no_mutation_isolated()
    print("ALL B118 DRYRUN SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
