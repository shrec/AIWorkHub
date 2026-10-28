"""Lifecycle canary: one real create -> launch -> supervise -> finalize walk.

RM-2026-00015 criterion 3 (needfix-NF-2026-00252 v2).  Every step runs
production code against real stores inside ``tmp_path``: a git repository, an
initialized canonical task store, a bound Manager Chat seat, ``core.create_task``,
an isolated ``ProcessManager`` launch, the real supervisor and the real
finalizer.  The ONLY substitute is the provider CLI: ``adapter_builder``
returns a real ``runtime_adapters.RuntimeAdapterPlan`` whose argv runs
``tests/fixtures/canary_worker.py`` with the base interpreter.  No launcher,
supervisor, finalizer, task-store or callback function is monkeypatched;
``monkeypatch`` only sets environment bindings.  Every assertion reads a real
store or a sealed artifact.

Callback contract: a worker's review_ready is announced to the manager only
after the system quality review, so the launcher records
``manager_callback_deferred`` on the terminal evidence and
``task_engine.mark_terminal_review`` enqueues no callback_outbox row.  The
canary pins exactly that state at review_ready.

Steps the host cannot run skip with a named capability, never a stub.
"""

from __future__ import annotations

import json
import runpy
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    attempt_artifacts,
    callback_store,
    core,
    process_event_ledger,
    process_launcher,
    runtime_adapters,
    task_store,
    windows_appcontainer,
)

_CANARY_WORKER = Path(__file__).resolve().parent / "fixtures" / "canary_worker.py"
# The fixture owns the output path and bytes; the test reads them, never restates them.
_CANARY = runpy.run_path(str(_CANARY_WORKER))
CANARY_OUTPUT: str = _CANARY["CANARY_OUTPUT"]
CANARY_TEXT: str = _CANARY["CANARY_TEXT"]

TASK_ID = "LIFECYCLE_CANARY_GOLDEN_PATH"
RUNNER = "claude_sonnet5_lifecycle_canary"
TOPIC = "task_system"
ADAPTER_ID = "claude_cli"
SEAT_BACKEND = "claude_cli"
SEAT_MODEL = "claude-sonnet-5"
SEEDED_VALIDATION_TEST = "tests/test_canary_output.py"
VALIDATION_COMMAND = f"python -m pytest -q {SEEDED_VALIDATION_TEST}"
TOOLCHAIN_HMAC_KEY = "hex:" + "11" * 32

GIT_TIMEOUT_SECONDS = 60
WORKER_TIMEOUT_SECONDS = 300
COLLECT_DEADLINE_SECONDS = 420.0
COLLECT_POLL_SECONDS = 0.25

APPCONTAINER_SKIP = (
    "validation_unsupported_in_sandbox:windows_appcontainer:"
    "nested_worker_launch_and_preflight_key_dacl_denied"
)
GIT_MISSING_SKIP = "capability_missing:git_executable"
CLAUDE_AUTH_BLOCKER = "claude_authentication_unavailable"


def _base_python() -> str:
    """The base interpreter: the provider stand-in needs only the stdlib."""

    return str(getattr(sys, "_base_executable", "") or sys.executable)


def _git(git: str, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [git, *args],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )


def _seed_git_repo(git: str, root: Path) -> None:
    """One commit: the card's validation test."""

    root.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "lifecycle-canary@example.invalid"),
        ("config", "user.name", "Lifecycle Canary"),
        ("config", "core.autocrlf", "false"),
        # Pin off what a host's global config could inject into the seed commit.
        ("config", "commit.gpgsign", "false"),
        ("config", "core.hooksPath", ".git/hooks"),
    ):
        completed = _git(git, root, *args)
        assert completed.returncode == 0, completed.stderr
    validation_test = root / SEEDED_VALIDATION_TEST
    validation_test.parent.mkdir(parents=True)
    validation_test.write_text(
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def test_canary_output_written():\n"
        f"    output = Path(__file__).resolve().parents[1] / {CANARY_OUTPUT!r}\n"
        f"    assert output.read_bytes() == {CANARY_TEXT.encode('utf-8')!r}\n",
        encoding="utf-8",
    )
    for args in (
        ("add", SEEDED_VALIDATION_TEST),
        ("commit", "-qm", "lifecycle canary seed"),
    ):
        completed = _git(git, root, *args)
        assert completed.returncode == 0, completed.stderr


def _bind_environment(monkeypatch: pytest.MonkeyPatch, repo: Path, tmp_path: Path) -> None:
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    # The canonical binding wins over the legacy one and must agree with it.
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv("AIWORKHUB_AUDIT_LOG_PATH", str(tmp_path / "audit" / "audit.jsonl"))
    monkeypatch.setenv("AIWORKHUB_TOOLCHAIN_AUTHORITY_HMAC_KEY", TOOLCHAIN_HMAC_KEY)


def _bind_manager_seat(monkeypatch: pytest.MonkeyPatch, repo: Path) -> str:
    """A real Manager Chat seat: minted token plus a selected, active session."""

    # Imported here, after the capability skips: a sparse validation lane that
    # carries no Manager Chat backends must still collect (and skip) this file.
    from aiworkhub import manager_loop_backends

    seat_env = manager_loop_backends.provision_manager_seat_env(
        repo,
        SEAT_BACKEND,
        python_executable=sys.executable,
        package_import_root=_SRC,
    )
    for key, value in seat_env.items():
        monkeypatch.setenv(key, value)
    session_id = "mls-" + uuid.uuid4().hex
    state_dir = repo / ".aiworkhub" / "runtime" / "manager_loop"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "session_id": session_id,
        "status": "active",
        "backend_id": SEAT_BACKEND,
        "model": SEAT_MODEL,
        "created_at": now,
        "updated_at": now,
        "last_turn_at": now,
    }
    (sessions_dir / f"{session_id}.json").write_text(json.dumps(record), encoding="utf-8")
    (state_dir / "selected.json").write_text(
        json.dumps({"session_id": session_id}), encoding="utf-8"
    )
    return session_id


def _canary_plan(
    *, adapter_id: str, prompt: str, repo: Path, **_kwargs: Any
) -> runtime_adapters.RuntimeAdapterPlan:
    """The provider CLI substitute: a real plan that runs the canary worker."""

    python = _base_python()
    return runtime_adapters.RuntimeAdapterPlan(
        adapter_id=adapter_id,
        # Inline source: the sparse isolated workspace checks out only the
        # card's paths, so a seeded script file would not be there.
        argv=[python, "-c", _CANARY_WORKER.read_text(encoding="utf-8")],
        cwd=str(repo),
        executable=python,
        launchable=True,
        manual_only=False,
        validation_ok=True,
        validation_reason="",
        # The supervisor refuses a launch whose prompt was never delivered.
        stdin_text=prompt,
    )


def _collect_until_terminal(
    manager: process_launcher.ProcessManager, request_id: str
) -> dict[str, Any]:
    deadline = time.monotonic() + COLLECT_DEADLINE_SECONDS
    result = manager.collect(request_id)
    while not result.get("terminal"):
        if time.monotonic() >= deadline:
            manager.cancel(request_id, reason="lifecycle_canary_deadline")
            pytest.fail(
                f"lifecycle canary not terminal within {COLLECT_DEADLINE_SECONDS}s: "
                + json.dumps(result, sort_keys=True, default=str)[:2000]
            )
        time.sleep(COLLECT_POLL_SECONDS)
        result = manager.collect(request_id)
    return result


def _callback_outbox_rows(repo: Path, task_id: str) -> list[dict[str, Any]]:
    _readiness, db_path = task_store._require_ready(repo)
    conn = callback_store.open_db(Path(db_path))
    try:
        rows = conn.execute(
            "SELECT transition, state, provider, episode_id FROM callback_outbox "
            "WHERE task_id=?",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def test_lifecycle_canary_golden_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if windows_appcontainer.current_process_is_appcontainer():
        pytest.skip(APPCONTAINER_SKIP)
    git = shutil.which("git")
    if git is None:
        pytest.skip(GIT_MISSING_SKIP)

    # -- seed: git repository, canonical task store, environment, manager seat
    repo = (tmp_path / "repo").resolve()
    _seed_git_repo(git, repo)
    initialized = task_store.initialize_repository(repo)
    assert initialized["ok"], initialized
    _bind_environment(monkeypatch, repo, tmp_path)
    _bind_manager_seat(monkeypatch, repo)

    # -- create: the verified manager route writes the canonical card
    created = core.create_task(
        task_id=TASK_ID,
        title="Lifecycle canary golden path",
        runner=RUNNER,
        topic=TOPIC,
        objective=f"Write {CANARY_OUTPUT} containing the lifecycle canary line.",
        acceptance=[f"{CANARY_OUTPUT} holds the lifecycle canary line."],
        allowed_writes=[CANARY_OUTPUT],
        required_outputs=[CANARY_OUTPUT],
        validation=[VALIDATION_COMMAND],
        custom_template_escape="audited_custom_unclassified",
    )
    assert created.get("ok") is True, created
    card = task_store.get_task(repo, TASK_ID)
    assert card is not None
    assert card["status"] == "pending", card["status"]
    assert card["runner"] == RUNNER

    # -- launch: real isolated launch; only the provider CLI is substituted
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=repo / process_launcher.PROCESS_LOG_DEFAULT_REL,
        process_dir=repo / process_launcher.PROCESS_DIR_DEFAULT_REL,
        adapter_builder=_canary_plan,
    )
    launched = manager.launch(
        task_id=TASK_ID,
        runner=RUNNER,
        topic=TOPIC,
        adapter_id=ADAPTER_ID,
        timeout_seconds=WORKER_TIMEOUT_SECONDS,
    )
    blocked_reason = str(launched.get("blocked_reason") or "")
    if blocked_reason.startswith(CLAUDE_AUTH_BLOCKER):
        pytest.skip(f"capability_missing:{blocked_reason}")
    assert launched.get("ok") is True, json.dumps(launched, sort_keys=True, default=str)
    request_id = str(launched["request_id"])

    # -- supervise + finalize: poll the real supervisor until terminal
    result = _collect_until_terminal(manager, request_id)
    assert result["state"] == "review_ready", json.dumps(result, sort_keys=True, default=str)
    assert result["exit_code"] == 0

    # -- task card: review / review_ready
    card = task_store.get_task(repo, TASK_ID)
    assert card is not None
    assert card["status"] == "review"
    assert card["terminal_substatus"] == "review_ready"
    # The candidate is promoted only by an explicit accept, never by review_ready.
    assert not (repo / CANARY_OUTPUT).exists()

    # -- process events: one ordered chain for this request and this task
    events = process_event_ledger.events_for_requests(
        manager.process_log_path, [request_id]
    )[request_id]
    states = [str(event.get("state") or "") for event in events if event.get("state")]
    assert states, events
    assert states[0] == "starting", states
    assert "running" in states, states
    assert states[-1] == "review_ready", states
    assert {str(event.get("task_id") or "") for event in events} == {TASK_ID}, events

    # -- attempt manifest: the sealed bundle re-verifies byte for byte
    bundle = attempt_artifacts.verify_json_bundle(
        manager.process_dir / "attempt-artifacts" / request_id
    )
    assert bundle["verified"] is True
    assert bundle["attempt_id"] == request_id

    # -- callback: deferred while the system quality review is pending
    evidence = card["terminal_review"]["evidence"]
    assert evidence["manager_callback_deferred"] == {
        "reason": "system_quality_review_pending"
    }
    outbox = _callback_outbox_rows(repo, TASK_ID)
    assert [row for row in outbox if row["transition"] == "review_ready"] == [], outbox
