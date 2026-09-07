from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

autolaunch = importlib.import_module("aiworkhub.dependency_autolaunch")


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    db_dir = repo / ".aiworkhub" / "tasking"
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "task_queue.sqlite")
    conn.execute(
        "CREATE TABLE tasks("
        "task_id TEXT PRIMARY KEY, runner TEXT, topic TEXT, mode TEXT DEFAULT '', "
        "status TEXT, worker_status TEXT, priority TEXT DEFAULT 'normal', objective TEXT DEFAULT '', "
        "card_json TEXT, created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '', "
        "completed_at TEXT DEFAULT '', claimed_by TEXT DEFAULT '', claimed_at TEXT DEFAULT '', "
        "started_at TEXT DEFAULT '', origin_thread_id TEXT DEFAULT '', archived_at TEXT DEFAULT '')"
    )
    conn.execute(
        "CREATE TABLE task_events("
        "task_id TEXT, event TEXT, runner TEXT, payload_json TEXT, created_at TEXT)"
    )
    conn.commit()
    conn.close()
    return repo


def _add(repo: Path, task_id: str, *, deps=None, status="pending", worker="unclaimed", runner=None, topic="task_mcp"):
    card = {
        "task_id": task_id,
        "runner": runner or f"runner_{task_id.lower()}",
        "topic": topic,
        "status": status,
        "worker_status": worker,
        "depends_on": list(deps or []),
        "origin_thread_id": f"thread_{task_id.lower()}",
        "coordinator_provider": "codex",
    }
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    conn.execute(
        "INSERT INTO tasks(task_id, runner, topic, status, worker_status, card_json, origin_thread_id) VALUES(?,?,?,?,?,?,?)",
        (task_id, card["runner"], topic, status, worker, json.dumps(card), card["origin_thread_id"]),
    )
    conn.commit()
    conn.close()


def _canonical_claim_start(repo: Path, calls: list[str], lock: threading.Lock | None = None):
    def launch(task_id: str, runner: str, topic: str, request_id: str):
        if lock is None:
            return _claim(repo, calls, task_id, runner, topic, request_id)
        with lock:
            return _claim(repo, calls, task_id, runner, topic, request_id)

    return launch


def _claim(repo: Path, calls: list[str], task_id: str, runner: str, topic: str, request_id: str):
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    try:
        row = conn.execute("SELECT runner, topic FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None or row[0] != runner or row[1] != topic:
            return {"ok": False, "stderr": "identity_mismatch"}
        cur = conn.execute(
            "UPDATE tasks SET status='processing', worker_status='claimed', claimed_by=? "
            "WHERE task_id=? AND status='pending' AND worker_status='unclaimed'",
            (runner, task_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return {"ok": False, "stderr": "claim_conflict"}
        conn.execute(
            "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) VALUES(?,?,?,?, '')",
            (task_id, "claim_start", runner, json.dumps({"request_id": request_id})),
        )
        conn.commit()
        calls.append(task_id)
        return {"ok": True}
    finally:
        conn.close()


def _state(repo: Path, task_id: str):
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    row = conn.execute("SELECT status, worker_status, card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    conn.close()
    return row[0], row[1], json.loads(row[2])


def test_mark_done_hook_is_production_connected_to_reconciler_and_claim_start_path():
    core_source = (SRC / "aiworkhub" / "core.py").read_text(encoding="utf-8")
    assert "dependency_autolaunch.reconcile_after_accept" in core_source
    assert "claim_start_exact" in core_source
    assert "result[\"dependency_autolaunch\"]" in core_source


def test_no_dependents_and_finished_dependency_not_returned_ready(tmp_path):
    repo = _init_repo(tmp_path)
    _add(repo, "P", status="finished", worker="done")
    calls: list[str] = []
    outcome = autolaunch.reconcile_after_accept(repo, "P", _canonical_claim_start(repo, calls))
    assert outcome["schema_id"] == "aiworkhub.dependency_autolaunch_outcome.v1"
    assert outcome["launched"] == []
    assert calls == []
    assert _state(repo, "P")[:2] == ("finished", "done")


def test_chain_fan_in_fan_out_and_diamond_graphs(tmp_path):
    repo = _init_repo(tmp_path)
    for tid in ("A", "B"):
        _add(repo, tid, status="finished", worker="done")
    _add(repo, "C", deps=["A"])
    _add(repo, "D", deps=["A", "B"])
    _add(repo, "E", deps=["A"])
    _add(repo, "F", deps=["C", "D"])
    calls: list[str] = []
    first = autolaunch.reconcile_after_accept(repo, "A", _canonical_claim_start(repo, calls))
    assert [r["task_id"] for r in first["launched"]] == ["C", "D", "E"]
    assert "F" not in calls
    for tid in ("C", "D"):
        conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
        conn.execute("UPDATE tasks SET status='finished', worker_status='done' WHERE task_id=?", (tid,))
        conn.commit()
        conn.close()
    second = autolaunch.reconcile_after_accept(repo, "D", _canonical_claim_start(repo, calls))
    assert [r["task_id"] for r in second["launched"]] == ["F"]


def test_duplicate_restart_and_concurrent_exact_once(tmp_path):
    repo = _init_repo(tmp_path)
    _add(repo, "A", status="finished", worker="done")
    _add(repo, "B", deps=["A"])
    calls: list[str] = []
    lock = threading.Lock()
    launch = _canonical_claim_start(repo, calls, lock)
    threads = [threading.Thread(target=autolaunch.reconcile_after_accept, args=(repo, "A", launch)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    again = autolaunch.reconcile_startup(repo, launch)
    assert calls == ["B"]
    assert again["launched"] == []
    assert _state(repo, "B")[:2] == ("processing", "claimed")


def test_capacity_delay_retry_and_manual_launch_preservation(tmp_path):
    repo = _init_repo(tmp_path)
    _add(repo, "A", status="finished", worker="done")
    _add(repo, "B", deps=["A"])
    _add(repo, "C", deps=["A"])
    _add(repo, "MANUAL", deps=["A"], status="processing", worker="claimed")
    calls: list[str] = []
    first = autolaunch.reconcile_startup(repo, _canonical_claim_start(repo, calls), capacity=1)
    assert [r["task_id"] for r in first["launched"]] == ["B"]
    assert any(r["task_id"] == "C" and r["reason"] == "capacity" for r in first["delayed"])
    second = autolaunch.reconcile_startup(repo, _canonical_claim_start(repo, calls))
    assert [r["task_id"] for r in second["launched"]] == ["C"]
    assert _state(repo, "MANUAL")[:2] == ("processing", "claimed")


def test_failed_and_cancelled_dependency_routes_child_to_review_visible_blocked(tmp_path):
    repo = _init_repo(tmp_path)
    _add(repo, "FAILED", status="failed", worker="worker_failed")
    _add(repo, "CANCELLED", status="cancelled", worker="cancelled")
    _add(repo, "CHILD", deps=["FAILED", "CANCELLED"])
    outcome = autolaunch.reconcile_after_accept(repo, "FAILED", _canonical_claim_start(repo, []))
    assert outcome["blocked"] == [{"task_id": "CHILD", "blocked_by": ["CANCELLED", "FAILED"]}]
    status, worker, card = _state(repo, "CHILD")
    assert (status, worker, card["substatus"]) == ("review", "review", "dependency_blocked")
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    row = conn.execute(
        "SELECT transition,state FROM callback_outbox WHERE task_id='CHILD'"
    ).fetchone()
    conn.close()
    assert row == ("blocked", "pending")


def test_adapter_families_and_two_repository_isolation(tmp_path):
    repo1 = _init_repo(tmp_path / "one")
    repo2 = _init_repo(tmp_path / "two")
    adapters = [
        ("CLAUDE_CHILD", "claude_task_mcp_family_b905"),
        ("DEEPSEEK_CHILD", "deepseek_v4pro_task_mcp_family_b905"),
        ("CODEX_CHILD", "codex_gpt55_task_mcp_family_b905"),
    ]
    for repo in (repo1, repo2):
        _add(repo, "P", status="finished", worker="done")
    for tid, runner in adapters:
        _add(repo1, tid, deps=["P"], runner=runner)
    _add(repo2, "CLAUDE_CHILD", deps=["P"], runner="claude_task_mcp_family_b905")
    calls1: list[str] = []
    calls2: list[str] = []
    out1 = autolaunch.reconcile_after_accept(repo1, "P", _canonical_claim_start(repo1, calls1))
    assert sorted(row["task_id"] for row in out1["launched"]) == sorted(tid for tid, _runner in adapters)
    assert calls2 == []
    assert _state(repo2, "CLAUDE_CHILD")[:2] == ("pending", "unclaimed")


def test_deterministic_denial_holds_until_card_changes(tmp_path):
    repo = _init_repo(tmp_path)
    _add(repo, "PARENT", status="finished", worker="done")
    _add(repo, "CHILD", deps=["PARENT"])
    calls: list[str] = []

    def denying_launch(task_id, runner, topic, request_id):
        calls.append(task_id)
        return {
            "ok": False,
            "stderr": "workforce_route_absent:runner=claude_opus-5:adapter=claude_cli",
        }

    first = autolaunch.reconcile(repo, trigger_task_id="t", launch=denying_launch)
    assert calls == ["CHILD"]
    [delayed] = first["delayed"]
    assert delayed["denial_kind"] == "deterministic"
    assert delayed["next_attempt_at"] == ""

    # An identical trigger no longer re-issues the doomed launch.
    second = autolaunch.reconcile(repo, trigger_task_id="t", launch=denying_launch)
    assert calls == ["CHILD"]
    [skipped] = [row for row in second["skipped"] if row["task_id"] == "CHILD"]
    assert skipped["reason"].startswith("deterministic_denial_hold:")

    # Any card change releases the hold for exactly one fresh attempt.
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    conn.execute(
        "UPDATE tasks SET updated_at='2026-09-01T12:00:00+00:00' WHERE task_id='CHILD'"
    )
    conn.commit()
    conn.close()
    autolaunch.reconcile(repo, trigger_task_id="t", launch=denying_launch)
    assert calls == ["CHILD", "CHILD"]


def test_transient_denial_backs_off_and_relaunches_when_due(tmp_path):
    repo = _init_repo(tmp_path)
    _add(repo, "PARENT", status="finished", worker="done")
    _add(repo, "CHILD", deps=["PARENT"])
    calls: list[str] = []

    def flaky_launch(task_id, runner, topic, request_id):
        calls.append(task_id)
        return {"ok": False, "stderr": "sqlite3.OperationalError: database is locked"}

    first = autolaunch.reconcile(repo, trigger_task_id="t", launch=flaky_launch)
    [delayed] = first["delayed"]
    assert delayed["denial_kind"] == "transient"
    assert delayed["next_attempt_at"] > autolaunch._now()[:19]

    # Before the backoff is due the identical trigger does not relaunch.
    second = autolaunch.reconcile(repo, trigger_task_id="t", launch=flaky_launch)
    assert calls == ["CHILD"]
    [held] = [row for row in second["delayed"] if row["task_id"] == "CHILD"]
    assert held["reason"] == "transient_backoff_hold"

    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    conn.execute(
        "UPDATE dependency_autolaunch_holds SET next_attempt_at='2000-01-01T00:00:00+00:00'"
    )
    conn.commit()
    conn.close()
    autolaunch.reconcile(repo, trigger_task_id="t", launch=flaky_launch)
    assert calls == ["CHILD", "CHILD"]


def test_successful_launch_clears_denial_hold(tmp_path):
    repo = _init_repo(tmp_path)
    _add(repo, "PARENT", status="finished", worker="done")
    _add(repo, "CHILD", deps=["PARENT"])
    calls: list[str] = []

    def flaky_once(task_id, runner, topic, request_id):
        calls.append(task_id)
        if len(calls) == 1:
            return {"ok": False, "stderr": "transient provider hiccup"}
        return _claim(repo, [], task_id, runner, topic, request_id)

    autolaunch.reconcile(repo, trigger_task_id="t", launch=flaky_once)
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    conn.execute(
        "UPDATE dependency_autolaunch_holds SET next_attempt_at='2000-01-01T00:00:00+00:00'"
    )
    conn.commit()
    conn.close()

    outcome = autolaunch.reconcile(repo, trigger_task_id="t", launch=flaky_once)
    assert [row["task_id"] for row in outcome["launched"]] == ["CHILD"]
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    remaining = conn.execute(
        "SELECT COUNT(*) FROM dependency_autolaunch_holds"
    ).fetchone()[0]
    conn.close()
    assert remaining == 0


# ---------------------------------------------------------------------------
# NF-2026-00549 follow-up (2026-09-07).  The hand-maintained denial vocabulary
# had drifted to 1.7% coverage of the 286 launch_blocked reasons the canonical
# store holds, and it matched by bare substring containment.  These tests pin
# the classification proven at each raise site, the identifier-boundary match
# that replaced containment, and the gate that makes the drift unrepeatable.

# The ten launch-denial reasons the canonical store actually recorded, with
# their measured counts and the class proven from the code that RAISES each.
MEASURED_PRODUCTION_DENIALS = (
    # transient: task_plan.py:668-674 calls it a point-in-time pre-claim
    # result; it clears when the OTHER card finishes, not when this card
    # changes, so holding it would strand the single largest bucket.
    ("collision_guard_failed", 62, "transient"),
    # transient: AuthoritySnapshot.available is `not missing`, and missing
    # names absent executables/modules.  repair() "cannot invoke a package
    # manager ... unresolved external requirements remain unresolved".
    ('task_contract_unwinnable:["executable:mypy"]', 18, "transient"),
    # transient: worker_workspace.py:1240-1243 stats the repo; a sibling
    # card's accept can promote the file in without touching this card.
    (
        "workspace_required_input_missing:field=immutable_inputs:index=0:path=src/a.py",
        16,
        "transient",
    ),
    # transient by construction: process_launcher.py:8569-8572 is the branch
    # for exception types that were NOT anticipated.
    ("unexpected_launch_error:KeyError:'card'", 16, "transient"),
    # transient: worker_workspace.py:5158-5165 compares retained candidate
    # workspace content, which retention/restore changes on its own.
    ("quality_review_candidate_mismatch:src/a.py,src/b.py", 16, "transient"),
    # deterministic: process_launcher.py:2812-2837 _validate_adapter_identity
    # is a total pure function of (runner, adapter_id) over literal tuples.
    (
        "runner_adapter_mismatch:runner=claude_opus:expected=vscode_lm|claude_cli:got=codex_cli",
        10,
        "deterministic",
    ),
    # deterministic: launch_replay_guard.py:50-57 compares two fields of the
    # same card against each other.
    ("validation_only_replay_predecessor_mismatch", 9, "deterministic"),
    # deterministic: process_launcher.py:4096-4097 keys off the card's topic,
    # and LaunchFn here structurally cannot supply the binding.
    ("quality_review_binding_required", 9, "deterministic"),
    # transient: worker_workspace.py:4330-4331 is a leftover directory that
    # stranded-worktree recovery removes without touching the card row.
    ("workspace_exists:dependency-autolaunch:PARENT:C08", 8, "transient"),
    # transient: process_launcher.py:8022-8035 is a live Source Graph call.
    ("vscode_lm_initial_source_graph_prefetch_failed:daemon_unavailable", 8, "transient"),
)


def _holds(repo: Path) -> dict:
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    conn.row_factory = sqlite3.Row
    rows = {
        row["task_id"]: dict(row)
        for row in conn.execute(
            "SELECT task_id, reason, kind, attempts, card_updated_at, next_attempt_at "
            "FROM dependency_autolaunch_holds"
        )
    }
    conn.close()
    return rows


def _expire_transient_holds(repo: Path) -> None:
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    conn.execute(
        "UPDATE dependency_autolaunch_holds SET next_attempt_at='2000-01-01T00:00:00+00:00' "
        "WHERE kind='transient'"
    )
    conn.commit()
    conn.close()


def test_every_launch_denial_reason_in_the_codebase_is_classified():
    """Anti-drift: a new launch-denial reason must be claimed or disclaimed.

    A hand-maintained tuple fell 1.7% behind production because nothing failed
    when a new reason appeared.  This enumerates the declared producers from
    source, so an unclassified denial is visible instead of silently absent.
    """
    package_root = SRC / "aiworkhub"
    discovered = autolaunch.discover_launch_denial_reasons(package_root)
    # Guard against a vacuous pass: a renamed producer must not turn this test
    # into an assertion over an empty scan.
    assert len(discovered) > 90, f"denial scan collapsed to {len(discovered)} reasons"
    for anchor in (
        "collision_guard_failed",
        "runner_adapter_mismatch",
        "quality_review_binding_required",
        "card_scoped_identity_mismatch",
        "validation_only_replay_predecessor_mismatch",
    ):
        assert anchor in discovered, f"{anchor} not discovered by the producer scan"

    unclassified = autolaunch.unclassified_denial_reasons(package_root)
    assert unclassified == {}, (
        "launch-denial reasons the dependency registry neither claims as "
        "deterministic nor disclaims as transient: "
        + ", ".join(
            f"{reason} ({'; '.join(sites)})" for reason, sites in unclassified.items()
        )
    )


def test_measured_production_denials_classify_as_proven_at_their_raise_sites():
    for reason, count, expected in MEASURED_PRODUCTION_DENIALS:
        assert autolaunch.classify_denial(reason) == expected, (
            f"{reason} ({count} events) classified "
            f"{autolaunch.classify_denial(reason)}, expected {expected}"
        )


def test_runner_adapter_mismatch_is_deterministic_and_not_shadowed_by_runner_mismatch():
    assert "runner_mismatch" in autolaunch.DETERMINISTIC_DENIAL_REASONS
    assert "runner_adapter_mismatch" in autolaunch.DETERMINISTIC_DENIAL_REASONS
    assert (
        autolaunch.classify_denial(
            "runner_adapter_mismatch:runner=glm_4:expected=vscode_lm:got=claude_cli"
        )
        == "deterministic"
    )


def test_identifier_boundary_matching_does_not_bleed_across_reasons():
    # `topic_mismatch` is claimed, but must not classify a longer, unrelated
    # identifier that merely contains it -- the failure mode of substring
    # containment, which would hold a transient denial forever.
    assert "topic_mismatch" in autolaunch.DETERMINISTIC_DENIAL_REASONS
    assert autolaunch.classify_denial("some_unrelated_topic_mismatch_thing") == "transient"
    # An unknown reason fails closed to transient: retried, never held.
    assert autolaunch.classify_denial("brand_new_reason_nobody_classified") == "transient"
    assert autolaunch.classify_denial("") == "transient"
    # A declared family still matches across a `_` boundary.
    assert autolaunch.classify_denial("repo_policy_rejected:forbidden") == "deterministic"


def test_wrapped_write_gate_denial_is_classified_through_its_wrapper():
    # claim_start_exact is the launch callable production wires in (core.py:4153,
    # server.py:1654).  core.py:1013 hands its write-gate verdicts back wrapped
    # as "runner/topic allowlist denied: <reason>", so the reason is embedded.
    assert (
        autolaunch.classify_denial(
            "runner/topic allowlist denied: card_scoped_identity_mismatch"
        )
        == "deterministic"
    )
    assert (
        autolaunch.classify_denial(
            "runner/topic allowlist denied: malformed_runner:leading_space"
        )
        == "deterministic"
    )
    # Its genuinely racy denials must stay retryable.
    for racy in (
        "identity_mismatch:task_id=CHILD",
        "claim_conflict:task_id=CHILD",
        "task_not_found:CHILD",
    ):
        assert autolaunch.classify_denial(racy) == "transient", racy


def test_replaying_every_measured_production_denial_fills_the_holds_table(tmp_path):
    """The holds table had 0 rows in production; prove each real reason lands."""
    repo = _init_repo(tmp_path)
    _add(repo, "PARENT", status="finished", worker="done")
    children = []
    for index, (reason, _count, _expected) in enumerate(MEASURED_PRODUCTION_DENIALS):
        task_id = f"C{index:02d}"
        _add(repo, task_id, deps=["PARENT"])
        children.append((task_id, reason))
    reason_by_task = dict(children)
    launched: list[str] = []

    def denying_launch(task_id, runner, topic, request_id):
        launched.append(task_id)
        return {"ok": False, "stderr": reason_by_task[task_id]}

    outcome = autolaunch.reconcile(repo, trigger_task_id="PARENT", launch=denying_launch)
    assert launched == [task_id for task_id, _ in children]

    rows = _holds(repo)
    assert len(rows) == len(MEASURED_PRODUCTION_DENIALS), rows
    now_prefix = autolaunch._now()[:19]
    for (task_id, reason), (_reason, _count, expected) in zip(
        children, MEASURED_PRODUCTION_DENIALS
    ):
        row = rows[task_id]
        assert row["kind"] == expected, (task_id, reason, row)
        assert row["attempts"] == 1, row
        assert row["reason"] == reason[:240]
        if expected == "deterministic":
            assert row["next_attempt_at"] == "", row
        else:
            assert row["next_attempt_at"] > now_prefix, row
    # The reconcile outcome reports the same classification it persisted.
    delayed = {row["task_id"]: row for row in outcome["delayed"]}
    for task_id, _reason in children:
        assert delayed[task_id]["denial_kind"] == rows[task_id]["kind"]
        assert delayed[task_id]["attempts"] == 1

    # Second pass: deterministic holds suppress the relaunch entirely, while a
    # due transient hold is retried and escalates its backoff.
    _expire_transient_holds(repo)
    launched.clear()
    second = autolaunch.reconcile(repo, trigger_task_id="PARENT", launch=denying_launch)
    deterministic_ids = [
        task_id
        for (task_id, _r), (_reason, _count, expected) in zip(
            children, MEASURED_PRODUCTION_DENIALS
        )
        if expected == "deterministic"
    ]
    transient_ids = [
        task_id
        for (task_id, _r), (_reason, _count, expected) in zip(
            children, MEASURED_PRODUCTION_DENIALS
        )
        if expected == "transient"
    ]
    assert sorted(launched) == sorted(transient_ids)
    skipped = {
        row["task_id"]
        for row in second["skipped"]
        if row["reason"].startswith("deterministic_denial_hold:")
    }
    assert skipped == set(deterministic_ids)

    rows = _holds(repo)
    for task_id in deterministic_ids:
        assert rows[task_id]["attempts"] == 1, rows[task_id]
    for task_id in transient_ids:
        assert rows[task_id]["attempts"] == 2, rows[task_id]
        assert rows[task_id]["next_attempt_at"] > now_prefix


def test_denial_reason_scanner_detects_a_newly_invented_reason(tmp_path):
    """Positive control: the anti-drift gate must not be able to go vacuous.

    ``unclassified_denial_reasons`` asserting ``== {}`` against the real
    package would keep passing if the scanner silently stopped finding
    anything.  Point it at a synthetic producer and require the new reason to
    surface, with its file and line.
    """
    package = tmp_path / "aiworkhub"
    package.mkdir()
    (package / "brand_new_launcher.py").write_text(
        "class LaunchRejected(RuntimeError):\n"
        "    pass\n"
        "\n"
        "\n"
        "def launch(card):\n"
        '    raise LaunchRejected("newly_invented_denial_reason:detail")\n',
        encoding="utf-8",
    )
    discovered = autolaunch.discover_launch_denial_reasons(package)
    assert "newly_invented_denial_reason" in discovered
    assert discovered["newly_invented_denial_reason"] == ("brand_new_launcher.py:6",)
    unclassified = autolaunch.unclassified_denial_reasons(package)
    assert "newly_invented_denial_reason" in unclassified

    # A reason that IS classified must not be reported as drift.
    (package / "known_launcher.py").write_text(
        "class LaunchRejected(RuntimeError):\n"
        "    pass\n"
        "\n"
        "\n"
        "def launch(card):\n"
        '    raise LaunchRejected("collision_guard_failed")\n',
        encoding="utf-8",
    )
    assert "collision_guard_failed" not in autolaunch.unclassified_denial_reasons(package)
