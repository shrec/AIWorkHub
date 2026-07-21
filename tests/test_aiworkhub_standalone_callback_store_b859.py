"""B859: extracted-VSIX/packaged-runtime black-box proof.

This is the missing closure the review flagged: everything else in the
callback suite (138/138) is accepted as-is and untouched here. This file
only adds the isolated black-box test that a *packaged runtime* -- copied
wholesale the same way ``vscode-extension/test/package-vsix.js`` stages
``runtime/aiworkhub/`` -- delivers callbacks with zero dependency on this
repository or ``AITools`` anywhere on ``sys.path``.

Method: copy only ``src/aiworkhub`` into an isolated temp directory (no
repo root, no AITools), then run a driver script in a **subprocess** whose
``PYTHONPATH``/``sys.path`` contains only that copied directory. A
subprocess is the only way to make "AITools is not importable" a real,
observed fact rather than an in-process illusion (the parent test process
already has AITools importable and cached in ``sys.modules``).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_AIWORKHUB = Path(__file__).resolve().parents[1] / "src" / "aiworkhub"

_SKIP_DIRS = {"__pycache__", ".pytest_cache"}
_SKIP_SUFFIXES = (".pyc", ".pyo")


def _copy_packaged_runtime(dest: Path) -> Path:
    """Mirror ``package-vsix.js::copyPythonRuntime`` -- copy only the real
    package source (and its data assets), never bytecode caches."""
    runtime_dir = dest / "runtime" / "aiworkhub"

    def _copy(src: Path, dst: Path) -> None:
        dst.mkdir(parents=True, exist_ok=True)
        for entry in src.iterdir():
            if entry.is_dir():
                if entry.name in _SKIP_DIRS:
                    continue
                _copy(entry, dst / entry.name)
                continue
            if entry.name.endswith(_SKIP_SUFFIXES):
                continue
            shutil.copyfile(entry, dst / entry.name)

    _copy(_SRC_AIWORKHUB, runtime_dir)
    assert (runtime_dir / "callback_store.py").is_file()
    assert (runtime_dir / "callback_bridge.py").is_file()
    return runtime_dir


_DRIVER = textwrap.dedent(
    """
    import json, sys, uuid
    from pathlib import Path

    # --- prove isolation: neither the repository root nor AITools is on
    # sys.path, and AITools/taskdb is genuinely unimportable here. ---
    banned_markers = ("AITools", str(Path({repo_root!r})))
    for entry in sys.path:
        assert "AITools" not in entry, entry
        assert entry != {repo_root!r}, entry
    try:
        import AITools  # noqa: F401
        raise SystemExit("AITools importable in isolated runtime -- FAIL")
    except ModuleNotFoundError:
        pass
    try:
        import AITools.taskdb  # noqa: F401
        raise SystemExit("AITools.taskdb importable in isolated runtime -- FAIL")
    except ModuleNotFoundError:
        pass

    from aiworkhub import callback_store
    from aiworkhub.callback_bridge import CallbackBridge, CallbackDispatcher

    repo = Path({repo!r})
    db_path = repo / "task_queue.sqlite"
    conn = callback_store.open_db(db_path)
    callback_store.init_db(conn)

    session_id = str(uuid.uuid4())
    task_id = {task_id!r}
    now = callback_store.utc_now()
    card = {{
        "schema_id": "aiworkhub.machine_task_card.v1",
        "task_id": task_id,
        "status": "review",
        "worker_status": "review",
        "runner": "r",
        "topic": "task_mcp",
        "priority": "high",
        "objective": "b859 extracted-runtime black-box",
        "origin_thread_id": session_id,
        "claim_epoch": 0,
    }}
    conn.execute(
        \"\"\"
        INSERT INTO tasks (
          task_id, runner, topic, status, worker_status, priority, objective,
          card_json, created_at, updated_at, origin_thread_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        \"\"\",
        (task_id, "r", "task_mcp", "review", "review", "high", card["objective"],
         json.dumps(card, ensure_ascii=False, sort_keys=True), now, now, session_id),
    )
    conn.commit()
    callback_store.enqueue_callback(conn, task_id, session_id, "review_ready", episode_id="0")
    conn.close()

    calls = []

    def fake_ack_transport(cmd, prompt, timeout):
        class _Completed:
            def __init__(self):
                self.returncode = 0
                self.stdout = json.dumps({{"ok": True, "event_id": "", "request_id": ""}})
                self.stderr = ""
        calls.append(cmd)
        return _Completed()

    bridge = CallbackBridge(
        repo=repo, db_path=db_path, state_path=repo / "state.json",
        transport="claude_cli", claude_repo_id="repo_b859", claude_window_id="window_b859",
        claude_cli_run_fn=fake_ack_transport,
        lease_seconds=30, app_server_timeout=5, lease_margin_seconds=1,
    )
    result = bridge.run_once()
    stats = callback_store.callback_outbox_stats(callback_store.open_db(db_path))

    out = {{
        "result": result,
        "delivered": stats["by_state"].get("delivered", 0),
        "pending": stats["by_state"].get("pending", 0),
        "dead_letter": stats["by_state"].get("dead_letter", 0),
        "calls": len(calls),
        "resume_in_call": bool(calls) and "--resume" in calls[0] and session_id in calls[0],
    }}
    print("RESULT_JSON:" + json.dumps(out))
    """
)


_UNSUPPORTED_DRIVER = textwrap.dedent(
    """
    import json, uuid
    from pathlib import Path
    from aiworkhub import callback_store
    from aiworkhub.callback_bridge import CallbackDispatcher

    repo = Path({repo!r})
    db_path = repo / "task_queue.sqlite"
    conn = callback_store.open_db(db_path)
    callback_store.init_db(conn)
    session_id = str(uuid.uuid4())
    task_id = {task_id!r}
    now = callback_store.utc_now()
    conn.execute(
        \"\"\"
        INSERT INTO tasks (
          task_id, runner, topic, status, worker_status, priority, objective,
          card_json, created_at, updated_at, origin_thread_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        \"\"\",
        (task_id, "r", "task_mcp", "review", "review", "high", "b859 unsupported transport",
         json.dumps({{"schema_id": "x"}}), now, now, session_id),
    )
    conn.commit()
    callback_store.enqueue_callback(conn, task_id, session_id, "review_ready", episode_id="0")
    conn.close()

    dispatcher = CallbackDispatcher(repo, "copilot", bridge_kwargs={{
        "db_path": db_path, "state_path": repo / "state.json",
    }})
    dispatcher.start()
    health = dispatcher.health()
    stats = callback_store.callback_outbox_stats(callback_store.open_db(db_path))
    out = {{
        "dispatcher_running": dispatcher.is_running(),
        "health_ok": health["ok"],
        "last_start_error": health["last_start_error"],
        "pending": stats["by_state"].get("pending", 0),
        "delivered": stats["by_state"].get("delivered", 0),
        "dead_letter": stats["by_state"].get("dead_letter", 0),
    }}
    print("RESULT_JSON:" + json.dumps(out))
    """
)


def _run_isolated(tmp_path: Path, driver_src: str) -> dict:
    runtime_dir = _copy_packaged_runtime(tmp_path / "extracted_vsix")
    import_root = runtime_dir.parent
    repo = tmp_path / "repo"
    repo.mkdir()
    driver_path = tmp_path / "driver.py"
    driver_path.write_text(driver_src, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(driver_path)],
        cwd=str(import_root),
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(import_root)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    line = next(l for l in proc.stdout.splitlines() if l.startswith("RESULT_JSON:"))
    return json.loads(line[len("RESULT_JSON:"):])


def test_extracted_runtime_no_aitools_on_syspath_delivers_via_fake_transport(tmp_path):
    driver_src = _DRIVER.format(
        repo_root=str(_REPO_ROOT),
        repo=str(tmp_path / "repo"),
        task_id="E2E_B859_EXTRACTED_RUNTIME",
    )
    out = _run_isolated(tmp_path, driver_src)
    assert out["result"]["ok"] is True
    assert out["result"]["action"] == "delivered"
    assert out["delivered"] == 1
    assert out["pending"] == 0
    assert out["dead_letter"] == 0
    assert out["calls"] == 1
    assert out["resume_in_call"] is True


def test_extracted_runtime_unsupported_transport_fails_closed_stays_pending(tmp_path):
    driver_src = _UNSUPPORTED_DRIVER.format(
        repo=str(tmp_path / "repo"),
        task_id="E2E_B859_UNSUPPORTED_TRANSPORT",
    )
    out = _run_isolated(tmp_path, driver_src)
    assert out["dispatcher_running"] is False
    assert out["health_ok"] is False
    assert out["last_start_error"].startswith("unsupported_provider:")
    assert out["pending"] == 1
    assert out["delivered"] == 0
    assert out["dead_letter"] == 0
