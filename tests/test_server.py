from __future__ import annotations

import builtins
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sqlite3
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import server  # noqa: E402


def test_manager_review_hold_tool_forwards_the_exact_identity(monkeypatch) -> None:
    captured: dict = {}

    class Manager:
        def resolve_review_route_hold(self, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "state": "review_manager_hold_resolved"}

    monkeypatch.setattr(server.process_launcher, "default_manager", lambda: Manager())
    result = server.aiworkhub_manager_review_hold_resolve(
        target_task_id="TARGET", target_request_id="target-request",
        claim_epoch="7", candidate_sha256="b" * 64, lens="correctness",
        attempt_index=2, reviewer_task_id="QUALITY_REVIEW_EXACT",
        reviewer_request_id="review-request-exact",
        decision="retry_existing_attempt",
    )

    assert result == {"ok": True, "state": "review_manager_hold_resolved"}
    assert captured == {
        "target_task_id": "TARGET", "target_request_id": "target-request",
        "claim_epoch": "7", "candidate_sha256": "b" * 64,
        "lens": "correctness", "attempt_index": 2,
        "reviewer_task_id": "QUALITY_REVIEW_EXACT",
        "reviewer_request_id": "review-request-exact",
        "decision": "retry_existing_attempt",
    }


def test_stdlib_backend_can_be_selected_even_when_sdk_is_installed() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC)
    env["AIWORKHUB_MCP_STDIO_BACKEND"] = "stdlib"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from aiworkhub import server; print(server._MCP_SDK_AVAILABLE)",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert completed.stdout.strip() == "False"


def test_manager_archive_and_supersede_tools_are_write_gated(monkeypatch) -> None:
    monkeypatch.setattr(server.core, "writes_allowed", lambda: False)

    def unexpected_archive(*args, **kwargs):
        raise AssertionError("archive_task must not run while the write gate is closed")

    monkeypatch.setattr(server.task_engine, "archive_task", unexpected_archive)

    archive = server.aiworkhub_manager_task_archive("TASK_B891", reason="done")
    supersede = server.aiworkhub_manager_task_supersede("TASK_B891", reason="orphan")

    assert archive == {"ok": False, "error": "write_gate_closed", "task_id": "TASK_B891"}
    assert supersede == {"ok": False, "error": "write_gate_closed", "task_id": "TASK_B891"}


def test_manager_archive_and_supersede_use_verified_manager_actor(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[dict] = []
    expected_calls: list[dict] = []
    monkeypatch.setattr(server.core, "writes_allowed", lambda: True)
    monkeypatch.setattr(server.core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(server.core, "CODEX_RUNNER", "unexpected-direct-runner")

    def fake_archive(repo, task_id, *, actor, reason="", supersede=False):
        calls.append(
            {
                "repo": repo,
                "task_id": task_id,
                "actor": actor,
                "reason": reason,
                "supersede": supersede,
            }
        )
        return {"ok": True, "returncode": 0}

    monkeypatch.setattr(server.task_engine, "archive_task", fake_archive)

    for actor in ("claude", "codex"):
        monkeypatch.setattr(server.core, "_verified_manager_actor", lambda: actor)
        archive_task_id = f"TASK_ARCHIVE_{actor.upper()}"
        supersede_task_id = f"TASK_SUPERSEDE_{actor.upper()}"
        assert server.aiworkhub_manager_task_archive(archive_task_id, reason="done")["ok"] is True
        assert (
            server.aiworkhub_manager_task_supersede(supersede_task_id, reason="orphan")["ok"]
            is True
        )
        expected_calls.extend(
            [
                {
                    "repo": tmp_path,
                    "task_id": archive_task_id,
                    "actor": actor,
                    "reason": "done",
                    "supersede": False,
                },
                {
                    "repo": tmp_path,
                    "task_id": supersede_task_id,
                    "actor": actor,
                    "reason": "orphan",
                    "supersede": True,
                },
            ]
        )

    assert calls == expected_calls


def test_fallback_stdio_writer_is_binary_utf8_and_transport_safe() -> None:
    """Exercise the real fallback writer without replacing live modules."""
    import uuid

    package_path = _SRC / "aiworkhub"
    server_path = package_path / "server.py"
    package_name = f"_test_aiworkhub_{uuid.uuid4().hex}"
    module_name = f"{package_name}.server"
    original_import = builtins.__import__
    saved_stderr = sys.stderr

    def selective_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "mcp.server.fastmcp" or name.startswith("mcp.server.fastmcp."):
            raise ModuleNotFoundError(f"No module named '{name}'")
        return original_import(name, globals, locals, fromlist, level)

    try:
        builtins.__import__ = selective_import
        package_spec = importlib.util.spec_from_file_location(
            package_name,
            package_path / "__init__.py",
            submodule_search_locations=[str(package_path)],
        )
        assert package_spec is not None and package_spec.loader is not None
        package = importlib.util.module_from_spec(package_spec)
        sys.modules[package_name] = package
        package_spec.loader.exec_module(package)

        spec = importlib.util.spec_from_file_location(module_name, server_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        georgian_message = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"text": "გამარჯობა →"},
        }
        stream = io.BytesIO()
        module._stdio_write_message(stream, georgian_message)
        emitted = stream.getvalue()
        assert emitted.endswith(b"\n")
        assert emitted.count(b"\n") == 1
        assert json.loads(emitted.decode("utf-8")) == georgian_message

        ascii_message = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"text": "hello world"},
        }
        expected_ascii = (
            json.dumps(ascii_message, ensure_ascii=False, default=str).encode("utf-8")
            + b"\n"
        )
        ascii_stream = io.BytesIO()
        module._stdio_write_message(ascii_stream, ascii_message)
        assert ascii_stream.getvalue() == expected_ascii

        class BrokenStream:
            def write(self, _data):
                raise BrokenPipeError()

            def flush(self):
                return None

        stderr = io.StringIO()
        sys.stderr = stderr
        try:
            module._stdio_write_message(
                BrokenStream(),
                {"jsonrpc": "2.0", "id": 3, "result": {}},
            )
            raise AssertionError("expected _StdioTransportClosed")
        except module._StdioTransportClosed as exc:
            assert exc.code == 0

        record = json.loads(stderr.getvalue().strip())
        assert record == {
            "component": "aiworkhub.mcp_stdio",
            "event": "transport_closed",
            "request_id": 3,
            "error_type": "BrokenPipeError",
        }
    finally:
        builtins.__import__ = original_import
        sys.stderr = saved_stderr
        for name in list(sys.modules):
            if name == package_name or name.startswith(f"{package_name}."):
                sys.modules.pop(name, None)


def test_sdlc_metrics_tool_is_read_only_forwarder(monkeypatch, tmp_path: Path) -> None:
    class Readiness:
        ready = True
        reason = "ready"
        repo_id = "repo-one"

    monkeypatch.setattr(server.core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(server.task_store, "storage_readiness", lambda root: Readiness())
    monkeypatch.setattr(
        server.sdlc_outcome_metrics,
        "read_repository_metrics",
        lambda root, *, repository_id, limit: {
            "readonly": True,
            "root": root,
            "repository_id": repository_id,
            "limit": limit,
        },
    )
    assert server.aiworkhub_manager_sdlc_outcome_metrics(17) == {
        "readonly": True,
        "root": tmp_path,
        "repository_id": "repo-one",
        "limit": 17,
    }


def test_needfix_caused_by_verifies_real_canonical_accepted_task(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "task.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(server.task_store.SCHEMA)
    changed = tmp_path / "fixed.txt"
    changed.write_text("fixed", encoding="utf-8")
    changed_digest = hashlib.sha256(changed.read_bytes()).hexdigest()
    manifest = {"request_id": "request-real"}
    card = {
        "task_id": "TASK-REAL",
        "runner": "codex",
        "topic": "metrics",
        "claim_epoch": 1,
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {"request_id": "request-real"},
                "changed_paths": ["fixed.txt"],
                "changed_path_hashes": {"fixed.txt": changed_digest},
                "attempt_artifact_manifest": manifest,
                "workspace": {"base_oid": "base"},
            },
        },
    }
    conn.execute(
        "INSERT INTO tasks (task_id, runner, topic, status, worker_status, card_json, "
        "created_at, updated_at, claimed_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "TASK-REAL", "codex", "metrics", "review", "review",
            json.dumps(card), "now", "now", "codex",
        ),
    )
    conn.commit()
    conn.close()

    readiness = server.task_store.StorageReadiness(True, "ready", "repo-one", str(db_path))
    monkeypatch.setattr(server.core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(server.task_store, "_require_ready", lambda repo: (readiness, db_path))
    monkeypatch.setattr(server.task_store, "storage_readiness", lambda repo: readiness)
    unsigned = {
        "schema_id": server.task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA,
        "task_id": "TASK-REAL",
        "request_id": "request-real",
        "claim_epoch": 1,
        "base_oid": "base",
        "promoted_paths": ["fixed.txt"],
        "changed_path_hashes": {"fixed.txt": changed_digest},
        "attempt_artifact_manifest_id": server.task_engine._canonical_json_hash(manifest),
        "repository_revision": "sha256:" + server.task_engine._canonical_json_hash({
            "base_oid": "base", "changed_path_hashes": {"fixed.txt": changed_digest}
        }),
    }
    receipt = {
        **unsigned,
        "receipt_id": "sha256:" + server.task_engine._canonical_json_hash(unsigned),
    }
    accepted = server.task_engine.accept_review(
        tmp_path,
        "TASK-REAL",
        runner="codex",
        topic="metrics",
        request_id="request-real",
        evidence={},
        accepted_outcome_receipt=receipt,
    )
    assert accepted["ok"] is True

    result = server.needfix_add(
        title="escaped defect",
        description="found after canonical acceptance",
        caused_by={
            "schema_id": server.needfix_store.CAUSED_BY_SCHEMA_ID,
            "repository_id": "repo-one",
            "task_id": "TASK-REAL",
            "request_id": "request-real",
            "accepted_outcome_receipt": receipt,
        },
    )
    assert result["ok"] is True
    stored = server.needfix_store.get_needfix(tmp_path, result["id"])
    assert stored["caused_by"]["accepted_outcome_receipt"] == receipt
