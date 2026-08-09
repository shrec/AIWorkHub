"""Portable fresh-install lifecycle qualification used by release CI.

No paid model is launched.  The test exercises the installed Python package,
real stdio MCP discovery, InitRepo, first multi-language Source Graph index,
repo-local context persistence, exact task claim, terminal review, callback,
manager acceptance, reload recovery and same-id two-repository isolation.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import aiworkhub
from aiworkhub import (
    callback_store,
    context_writes,
    core,
    repository_bootstrap,
    source_graph,
    source_graph_daemon,
    task_engine,
    task_store,
)


THREAD_ID = "019f5097-6dbe-7172-870a-945afc5f3bfa"
RUNNER = "qualification_worker"
TOPIC = "release_qualification"
TASK_ID = "AIWORKHUB_FRESH_INSTALL_QUALIFICATION"


def _manager_identity() -> dict[str, str]:
    return {
        "provider": "codex",
        "session_id": THREAD_ID,
        "thread_id": THREAD_ID,
        "window_id": "qualification_window",
    }


def _bind(monkeypatch, repo: Path) -> None:
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_PROCESS_REPO_ROOT_OVERRIDE", None)


def _create() -> dict:
    result = core.create_task(
        TASK_ID,
        "Fresh-install qualification",
        RUNNER,
        TOPIC,
        "Exercise the portable lifecycle without launching a paid model.",
            ["terminal review and callback are durable"],
            ["qualification-evidence.json"],
            required_outputs=["qualification-evidence.json"],
            validation=["python -c \"print('qualification')\""],
    )
    assert result["ok"] is True, result
    return json.loads(result["stdout"])


def _stdio_tool_names(repo: Path) -> set[str]:
    env = dict(os.environ)
    env["AIWORKHUB_REPO_ROOT"] = str(repo)
    env["AIWORKHUB_REPO"] = str(repo)
    package_root = str(Path(aiworkhub.__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (package_root, env.get("PYTHONPATH", "")) if value
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "aiworkhub.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    responses: queue.Queue[dict | BaseException] = queue.Queue()

    def _read_stdout() -> None:
        try:
            for line in process.stdout:
                if line.strip():
                    responses.put(json.loads(line))
        except BaseException as exc:  # pragma: no cover - diagnostic transport path
            responses.put(exc)

    reader = threading.Thread(target=_read_stdout, daemon=True)
    reader.start()

    def _write(message: dict) -> None:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def _wait_for(response_id: int, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stderr = process.stderr.read()[-2000:] if process.poll() is not None else ""
                raise AssertionError(
                    f"stdio MCP response id={response_id} timed out; "
                    f"returncode={process.poll()}; stderr={stderr}"
                )
            try:
                message = responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise AssertionError(f"stdio MCP response id={response_id} timed out") from exc
            if isinstance(message, BaseException):
                raise AssertionError(f"stdio MCP reader failed: {message!r}") from message
            if message.get("id") == response_id:
                return message

    try:
        _write({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "release-qualification", "version": "1"},
            },
        })
        initialized = _wait_for(1)
        assert "result" in initialized, initialized
        _write({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        _write({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        response = _wait_for(2)
        return {tool["name"] for tool in response["result"]["tools"]}
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)


def test_fresh_install_task_context_callback_reload_and_repo_isolation(tmp_path, monkeypatch):
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()
    # A first index is useful only if it sees real language surfaces.  Keep a
    # Python and PHP function in the portable gate (PHP was a real fresh-install
    # zero-file regression, not optional coverage). Materialize the repository
    # sources before InitRepo so this gate qualifies the first canonical
    # generation rather than racing a background scan against test-fixture
    # creation; post-init file convergence is covered by the single-file and
    # daemon refresh suites.
    (repo_a / "app.py").write_text("def python_probe():\n    return 1\n", encoding="utf-8")
    (repo_a / "app.php").write_text("<?php function php_probe() { return 1; }\n", encoding="utf-8")
    init_a = repository_bootstrap.initialize_repository_full(repo_a)
    init_b = repository_bootstrap.initialize_repository_full(repo_b)
    assert init_a["ok"] is True and init_b["ok"] is True

    # InitRepo owns automatic indexing.  Synchronize with that canonical
    # daemon instead of racing it with a second direct writer: on a fast host
    # the first build can already include both files, while on a slower host a
    # follow-up refresh indexes them.  Both outcomes are correct; the portable
    # release invariant is a ready, queryable graph containing both surfaces.
    daemon = source_graph_daemon.get_daemon(repo_a)
    assert daemon is not None
    assert daemon.wait_for_first_build(timeout=10), "initial Source Graph build never completed"
    refreshed = daemon.refresh_now()
    assert refreshed["ok"] is True, refreshed
    assert refreshed["status"] == source_graph_daemon.STATUS_READY, refreshed
    assert refreshed["last_report"]["errors"] == [], refreshed
    # Two code surfaces plus the three repository instruction documents
    # projected by InitRepo and indexed as documentation evidence.
    assert refreshed["last_report"]["files_seen"] == 5, refreshed
    graph = source_graph.connect(source_graph.resolve_db_path(repo_a))
    try:
        assert source_graph.func(graph, "python_probe")
        assert source_graph.func(graph, "php_probe")
    finally:
        graph.close()

    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", _manager_identity)
    _bind(monkeypatch, repo_a)
    card_a = _create()
    _bind(monkeypatch, repo_b)
    card_b = _create()
    assert card_a["origin_thread_id"] == THREAD_ID
    assert card_b["origin_thread_id"] == THREAD_ID

    _bind(monkeypatch, repo_a)
    actor = {
        "role": "manager",
        "actor_id": THREAD_ID,
        "task_id": "",
        "provider": "codex",
        "session_id": THREAD_ID,
    }
    checkpoint = context_writes.session_write(
        repo_a,
        actor=actor,
        action="checkpoint",
        topic="qualification",
        content="fresh install context survives reload",
        idempotency_key="qualification:session:checkpoint:0001",
        provenance="release-qualification",
    )
    assert checkpoint["ok"] is True

    claimed = task_engine.claim_start_exact(
        repo_a, TASK_ID, RUNNER, TOPIC, request_id="qualification-request"
    )
    assert claimed["ok"] is True, claimed
    reviewed = task_engine.mark_terminal_review(
        repo_a,
        TASK_ID,
        RUNNER,
        "review_ready",
        evidence={
            "request_id": "qualification-request",
            "request_identity": {"request_id": "qualification-request"},
            "validation": [{"ok": True}],
            "required_outputs": [{"ok": True}],
        },
    )
    assert reviewed["ok"] is True and reviewed["callback_enqueued"] is True

    callback_db = callback_store.open_db(callback_store.resolve_db_path(repo_a))
    try:
        callback_store.init_db(callback_db)
        batch = callback_store.claim_pending_callback_batch(callback_db, provider="codex")
        assert batch is not None
        assert [member["task_id"] for member in batch["members"]] == [TASK_ID]
        callback_store.mark_batch_delivered(callback_db, batch["batch_id"])
        callback_db.commit()
    finally:
        callback_db.close()

    accepted = task_engine.accept_review(
        repo_a,
        TASK_ID,
        runner=RUNNER,
        topic=TOPIC,
        request_id="qualification-request",
        evidence={"qualification": True},
    )
    assert accepted["ok"] is True, accepted

    # Reload/restart recovery is an idempotent InitRepo plus a fresh stdio MCP
    # child.  The accepted row and the other repository's pending same-id row
    # must remain independent.
    restarted = repository_bootstrap.initialize_repository_full(repo_a)
    assert restarted["ok"] is True
    assert task_store.get_task(repo_a, TASK_ID)["status"] == "finished"
    assert task_store.get_task(repo_b, TASK_ID)["status"] == "pending"
    names = _stdio_tool_names(repo_a)
    for required in {
        "aiworkhub_repo_current",
        "aiworkhub_manager_source_graph_query",
        "aiworkhub_manager_context_import",
        "aiworkhub_task_show",
    }:
        assert required in names


def test_remote_ssh_release_contract_uses_workspace_extension_kind():
    if os.environ.get("AIWORKHUB_QUALIFICATION_MODE") != "remote_ssh_contract":
        return
    package = Path(__file__).resolve().parents[1] / "vscode-extension" / "package.json"
    payload = json.loads(package.read_text(encoding="utf-8"))
    assert "workspace" in payload["extensionKind"]
