from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from aiworkhub import (
    process_launcher,
    repository_state,
    task_store,
    vscode_lm_bridge,
    vscode_lm_worker,
    worker_ai_tools_mcp,
)


def test_atomic_json_skips_redundant_chmod_in_restricted_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "private" / "request.json"

    def denied(*_args, **_kwargs):
        raise PermissionError("sandbox denies chmod")

    monkeypatch.setattr(vscode_lm_bridge.os, "chmod", denied)
    monkeypatch.setattr(vscode_lm_bridge, "chmod_fd", denied)
    vscode_lm_bridge._atomic_json(target, {"text": "ქართული → UTF-8"})

    assert json.loads(target.read_text(encoding="utf-8"))["text"] == "ქართული → UTF-8"
    if vscode_lm_bridge.posix_path_modes_supported():
        assert target.parent.stat().st_mode & 0o777 == 0o700
        assert target.stat().st_mode & 0o777 == 0o600


def test_vscode_lm_tool_input_invalid_has_bounded_diagnostics() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "vscode-extension" / "extension.js"
    ).read_text(encoding="utf-8")
    invalid_branch_start = source.index(
        'if (!envelope.input || typeof envelope.input !== "object" || Array.isArray(envelope.input)) {'
    )
    invalid_branch = source[invalid_branch_start:invalid_branch_start + 1800]
    assert "buildToolInputInvalidDiagnostic(" in invalid_branch
    assert "responseDiagnostics = diagnostic" in invalid_branch
    outer_catch_start = source.index("diagnostics = {", invalid_branch_start)
    outer_catch = source[outer_catch_start:outer_catch_start + 1200]
    assert "tool_input_invalid: (err && err.responseDiagnostics) || null" in outer_catch

    helper_start = source.index("const TOOL_INPUT_DIAGNOSTIC_MAX_CHARS")
    helper_end = source.index("// Record HOW this extension host dies", helper_start)
    helper_source = source[helper_start:helper_end]
    sanitize_start = source.index("function sanitizeStderrChunk(")
    sanitize_open = source.index("{", sanitize_start)
    depth = 0
    sanitize_end = sanitize_open
    while sanitize_end < len(source):
        if source[sanitize_end] == "{":
            depth += 1
        elif source[sanitize_end] == "}":
            depth -= 1
            if depth == 0:
                sanitize_end += 1
                break
        sanitize_end += 1
    sanitize_source = source[sanitize_start:sanitize_end]
    node = shutil.which("node")
    if node is None:
        pytest.skip("node executable is required for the bounded diagnostic harness")
    harness = f"""
const MCP_MAX_STDERR_LOG_BYTES = 4096;
{sanitize_source}
{helper_source}
const trace = Array.from({{length: 20}}, (_, index) => ({{index}}));
const result = buildToolInputInvalidDiagnostic(
  "aiworkhub_worker_source_graph_query",
  new Error("schema mismatch"),
  {{authorization: "Bearer secret-value", nested: {{api_key: "sk-secretvalue"}}, payload: "x".repeat(5000)}},
  trace,
);
process.stdout.write(JSON.stringify(result));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as stream:
        stream.write(harness)
        harness_path = Path(stream.name)
    try:
        completed = subprocess.run(
            [node, str(harness_path)], check=True, capture_output=True, text=True, timeout=10,
        )
    finally:
        harness_path.unlink(missing_ok=True)
    diagnostic = json.loads(completed.stdout)
    assert diagnostic["tool_name"] == "aiworkhub_worker_source_graph_query"
    assert diagnostic["parser_error"] == "schema mismatch"
    assert 0 < len(diagnostic["protocol_preview"]) <= 780
    assert "secret-value" not in diagnostic["protocol_preview"]
    assert "sk-secretvalue" not in diagnostic["protocol_preview"]
    assert "[redacted]" in diagnostic["protocol_preview"]
    assert len(diagnostic["turn_trace"]) == 8


def test_atomic_json_fails_closed_when_parent_identity_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private"
    target = parent / "note.json"
    parent.mkdir(parents=True)
    original_inode = parent.stat().st_ino
    real_mkstemp = vscode_lm_bridge.tempfile.mkstemp

    def rotating_mkstemp(*args, **kwargs):
        moved = parent.with_name(parent.name + ".moved")
        parent.rename(moved)
        parent.mkdir()
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(vscode_lm_bridge.tempfile, "mkstemp", rotating_mkstemp)

    with pytest.raises((OSError, RuntimeError)):
        vscode_lm_bridge._atomic_json(target, {"rotation": "detected"})  # noqa: SLF001

    assert not target.exists()
    assert list(parent.iterdir()) == []
    assert parent.stat().st_ino != original_inode


def test_atomic_json_rejects_symlink_replacement_via_descriptor_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private"
    target = parent / "note.json"
    parent.mkdir(parents=True)
    decoy = tmp_path / "decoy.json"
    decoy.write_text("attacker", encoding="utf-8")
    decoy_mode = decoy.stat().st_mode & 0o777
    real_replace = os.replace

    def racing_replace(src, dst):
        real_replace(src, dst)
        replaced = Path(dst)
        replaced.unlink()
        replaced.symlink_to(decoy)

    monkeypatch.setattr(vscode_lm_bridge.os, "replace", racing_replace)

    with pytest.raises((OSError, RuntimeError)):
        vscode_lm_bridge._atomic_json(target, {"safe": "no"})  # noqa: SLF001

    assert decoy.read_text(encoding="utf-8") == "attacker"
    assert decoy.stat().st_mode & 0o777 == decoy_mode


def test_atomic_json_remains_portable_when_getuid_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "private" / "note.json"
    monkeypatch.delattr(vscode_lm_bridge.os, "getuid", raising=False)
    payload = {"portable": "über", "ok": True}

    vscode_lm_bridge._atomic_json(target, payload)  # noqa: SLF001

    raw = target.read_bytes()
    assert "über".encode("utf-8") in raw
    assert json.loads(raw.decode("utf-8")) == payload
    assert target.is_file()
    if vscode_lm_bridge.posix_path_modes_supported():
        assert target.stat().st_mode & 0o777 == 0o600


def test_atomic_json_retries_repo_spool_cleanup_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "requests" / "repo_test" / "request.json"
    real_replace = os.replace
    replace_calls = 0

    def cleanup_before_first_publish(src, dst):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            Path(src).unlink()
            Path(dst).parent.rmdir()
            raise FileNotFoundError(dst)
        real_replace(src, dst)

    monkeypatch.setattr(vscode_lm_bridge.os, "replace", cleanup_before_first_publish)

    vscode_lm_bridge._atomic_json(target, {"retry": "bounded"})  # noqa: SLF001

    assert replace_calls == 2
    assert json.loads(target.read_text(encoding="utf-8")) == {"retry": "bounded"}


def test_atomic_json_accepts_immediate_owner_only_request_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "requests" / "repo_test" / "request.json"
    claim = Path(f"{target}.claim-window_test")
    real_replace = os.replace

    def claim_immediately(src, dst):
        real_replace(src, dst)
        real_replace(dst, claim)

    monkeypatch.setattr(vscode_lm_bridge.os, "replace", claim_immediately)

    vscode_lm_bridge._atomic_json(  # noqa: SLF001
        target,
        {"claimed": True},
        allow_owner_claim_move=True,
    )

    assert not target.exists()
    assert json.loads(claim.read_text(encoding="utf-8")) == {"claimed": True}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    return repo


def _host(
    bridge_root: Path,
    repo_id: str,
    *,
    models: list[str],
    access_state: str = "unknown",
) -> Path:
    path = bridge_root / "hosts" / repo_id / "window_test.json"
    vscode_lm_bridge._atomic_json(  # noqa: SLF001 - contract-level test
        path,
        {
            "schema_id": vscode_lm_bridge.HOST_SCHEMA_ID,
            "repo_id": repo_id,
            "window_id": "window_test",
            "models": models,
            "model_metadata": [
                {"canonical": model, "id": model, "family": model, "access_state": access_state}
                for model in models
            ],
            "permission_granted": access_state.startswith("granted"),
        },
    )
    return path


def test_readiness_is_repo_scoped_and_credential_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = _repo(tmp_path)
    repo_id = repository_state.inspect_repository(repo).manifest.repo_id

    absent = vscode_lm_bridge.bridge_readiness(repo)
    assert absent["launchable"] is False
    assert absent["blocker_reason"] == "vscode_lm_host_unavailable"
    assert absent["credential_required"] is False

    host = _host(root, repo_id, models=["glm-5.2"])
    ready = vscode_lm_bridge.bridge_readiness(repo)
    assert ready["launchable"] is True
    assert ready["access_observed"] is False
    assert ready["consent_required"] is True
    assert ready["window_id"] == "window_test"
    deepseek_absent = vscode_lm_bridge.bridge_readiness(
        repo, model="deepseek-v4-pro", adapter_id="deepseek_vscode_lm"
    )
    assert deepseek_absent["adapter_id"] == "deepseek_vscode_lm"
    assert deepseek_absent["launchable"] is False
    _host(root, repo_id, models=["glm-5.2", "deepseek-v4-pro"])
    deepseek_ready = vscode_lm_bridge.bridge_readiness(
        repo, model="deepseek-v4-pro", adapter_id="deepseek_vscode_lm"
    )
    assert deepseek_ready["launchable"] is True
    assert deepseek_ready["access_state"] == "unknown"
    assert deepseek_ready["credential_required"] is False
    shared = vscode_lm_bridge.bridge_readiness(
        repo, model=None, adapter_id="vscode_lm"
    )
    assert shared["launchable"] is True
    assert shared["access_observed"] is False
    assert shared["live_host_count"] == 1
    assert shared["observed_models"] == ["deepseek-v4-pro", "glm-5.2"]
    if os.name != "nt":
        assert host.stat().st_mode & 0o077 == 0


def test_readiness_reports_durable_model_consent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = _repo(tmp_path)
    repo_id = repository_state.inspect_repository(repo).manifest.repo_id
    _host(root, repo_id, models=["glm-5.2"], access_state="granted_remembered")

    ready = vscode_lm_bridge.bridge_readiness(repo)

    assert ready["launchable"] is True
    assert ready["access_observed"] is True
    assert ready["consent_required"] is False
    assert ready["access_state"] == "granted_remembered"


def _bridge_request_for_cancel_test(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    request_id: str,
) -> vscode_lm_bridge.BridgeRequest:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = tmp_path / "repo"
    if not repo.exists():
        repo.mkdir()
        task_store.initialize_repository(repo)
    workspace = tmp_path / request_id / "worktree"
    home = tmp_path / request_id / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
    return vscode_lm_bridge.create_request(
        repo=repo,
        request_id=request_id,
        workspace_path=workspace,
        workspace_home=home,
        prompt="Bounded cancellation test.",
        model="glm-5.2",
        allowed_writes=[],
        timeout_seconds=30,
    )


def _response_decision(
    request: vscode_lm_bridge.BridgeRequest,
) -> dict[str, object]:
    return {
        "repo_id": request.repo_id,
        "decision": {
            "action": "response",
            "cancel_token": request.cancel_token,
        },
    }


def test_cancel_before_claim_removes_request_without_stale_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _bridge_request_for_cancel_test(
        tmp_path, monkeypatch, request_id="1" * 32,
    )

    assert vscode_lm_bridge.cancel_request(request) == "removed"

    assert not request.request_path.exists()
    assert not request.cancel_path.exists()


def test_cancel_after_claim_publishes_private_identity_bound_decision_in_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = _bridge_request_for_cancel_test(
        tmp_path, monkeypatch, request_id="2" * 32,
    )
    isolated = _bridge_request_for_cancel_test(
        tmp_path, monkeypatch, request_id="3" * 32,
    )
    claim_path = Path(f"{cancelled.request_path}.claim-window_test")
    cancelled.request_path.rename(claim_path)

    assert vscode_lm_bridge.cancel_request(cancelled) == "cancelled"
    assert vscode_lm_bridge.cancel_request(cancelled) == "cancelled"

    decision = json.loads(cancelled.cancel_path.read_text(encoding="utf-8"))
    assert decision["schema_id"] == vscode_lm_bridge.RESPONSE_SCHEMA_ID
    assert decision["request_id"] == cancelled.request_id
    assert decision["repo_id"] == cancelled.repo_id
    assert decision["error"] == "vscode_lm_request_cancelled"
    assert decision["diagnostics"] == {
        "action": "cancel",
        "cancel_token": cancelled.cancel_token,
        "phase": "cancelled",
    }
    assert isolated.request_path.is_file()
    assert not isolated.cancel_path.exists()
    if vscode_lm_bridge.posix_path_modes_supported():
        assert cancelled.cancel_path.stat().st_mode & 0o777 == 0o600


def test_cancel_after_claim_does_not_reclassify_private_completed_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _bridge_request_for_cancel_test(
        tmp_path, monkeypatch, request_id="4" * 32,
    )
    claim_path = Path(f"{request.request_path}.claim-window_test")
    request.request_path.rename(claim_path)
    vscode_lm_bridge._atomic_json(  # noqa: SLF001 - exact bridge response contract
        request.response_path,
        {
            "schema_id": vscode_lm_bridge.RESPONSE_SCHEMA_ID,
            "request_id": request.request_id,
            "repo_id": request.repo_id,
            "decision": {
                "action": "response",
                "cancel_token": request.cancel_token,
            },
        },
    )

    assert vscode_lm_bridge.cancel_request(request) == "completed"
    assert request.cancel_path.exists()
    assert json.loads(request.cancel_path.read_text(encoding="utf-8"))["decision"] == {
        "action": "response",
        "cancel_token": request.cancel_token,
    }


def test_cancel_claim_rename_race_publishes_terminal_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _bridge_request_for_cancel_test(
        tmp_path, monkeypatch, request_id="8" * 32,
    )
    claim_path = Path(f"{request.request_path}.claim-window_race")
    real_rename = vscode_lm_bridge.os.rename

    def claim_before_cancel(source, destination):
        if Path(source) == request.request_path:
            real_rename(source, claim_path)
            raise FileNotFoundError(source)
        return real_rename(source, destination)

    monkeypatch.setattr(vscode_lm_bridge.os, "rename", claim_before_cancel)

    assert vscode_lm_bridge.cancel_request(request) == "cancelled"
    assert claim_path.is_file()
    payload = json.loads(request.response_path.read_text(encoding="utf-8"))
    assert payload["decision"] == {
        "action": "cancel",
        "cancel_token": request.cancel_token,
    }


def test_cancel_never_unlinks_request_replaced_by_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _bridge_request_for_cancel_test(
        tmp_path, monkeypatch, request_id="9" * 32,
    )
    decoy = tmp_path / "decoy.json"
    decoy.write_text("do-not-touch", encoding="utf-8")
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to(decoy)
    except OSError:
        pytest.skip("file symlinks are unavailable on this Windows host")
    probe.unlink()
    real_rename = vscode_lm_bridge.os.rename

    def replace_before_move(source, destination):
        if Path(source) == request.request_path:
            request.request_path.unlink()
            request.request_path.symlink_to(decoy)
        return real_rename(source, destination)

    monkeypatch.setattr(vscode_lm_bridge.os, "rename", replace_before_move)

    with pytest.raises(
        vscode_lm_bridge.BridgeError,
        match="quarantined_identity_changed",
    ):
        vscode_lm_bridge.cancel_request(request)

    assert decoy.read_text(encoding="utf-8") == "do-not-touch"
    quarantined = list(request.request_path.parent.glob(".*.cancel-*"))
    assert len(quarantined) == 1
    assert quarantined[0].is_symlink()


def test_cancel_retries_then_reports_persistent_partial_decision_without_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _bridge_request_for_cancel_test(
        tmp_path, monkeypatch, request_id="c" * 32,
    )
    request.request_path.rename(Path(f"{request.request_path}.claim-window_test"))
    request.response_path.write_bytes(b"{")
    if vscode_lm_bridge.posix_path_modes_supported():
        request.response_path.chmod(0o600)
    monkeypatch.setattr(vscode_lm_bridge.time, "sleep", lambda _seconds: None)

    with pytest.raises(
        vscode_lm_bridge.BridgeError,
        match="bridge_terminal_decision_persistent_invalid",
    ):
        vscode_lm_bridge.cancel_request(request)

    assert request.response_path.read_bytes() == b"{"


def test_cancel_rejects_forged_response_token_and_preserves_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _bridge_request_for_cancel_test(
        tmp_path, monkeypatch, request_id="d" * 32,
    )
    request.request_path.rename(Path(f"{request.request_path}.claim-window_test"))
    forged = {
        "schema_id": vscode_lm_bridge.RESPONSE_SCHEMA_ID,
        "request_id": request.request_id,
        "repo_id": request.repo_id,
        "error": "",
        "decision": {"action": "response", "cancel_token": "0" * 64},
    }
    vscode_lm_bridge._atomic_json(request.response_path, forged)  # noqa: SLF001

    with pytest.raises(
        vscode_lm_bridge.BridgeError,
        match="bridge_terminal_decision_contract_mismatch",
    ):
        vscode_lm_bridge.cancel_request(request)

    assert json.loads(request.response_path.read_text(encoding="utf-8")) == forged


def test_persisted_bridge_metadata_round_trips_and_rejects_path_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _bridge_request_for_cancel_test(
        tmp_path, monkeypatch, request_id="e" * 32,
    )
    metadata = vscode_lm_bridge.bridge_request_metadata(request)

    restored = vscode_lm_bridge.bridge_request_from_metadata(
        metadata, expected_request_id=request.request_id,
    )
    assert restored == request

    tampered = {**metadata, "cancel_path": str(tmp_path / "foreign.json")}
    with pytest.raises(
        vscode_lm_bridge.BridgeError,
        match="decision_path_mismatch",
    ):
        vscode_lm_bridge.bridge_request_from_metadata(
            tampered, expected_request_id=request.request_id,
        )


def test_isolated_process_exit_publishes_claimed_bridge_cancellation_before_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _bridge_request_for_cancel_test(
        tmp_path, monkeypatch, request_id="5" * 32,
    )
    claim_path = Path(f"{request.request_path}.claim-window_test")
    request.request_path.rename(claim_path)
    finalized: list[tuple[str, int | None]] = []

    class ExitedProcess:
        pid = 12345

        @staticmethod
        def wait(*, timeout: float) -> int:
            assert timeout > 0
            return 1

        @staticmethod
        def poll() -> int:
            return 1

    manager = process_launcher.ProcessManager(
        repo=tmp_path / "repo",
        process_log_path=tmp_path / "processes.jsonl",
        process_dir=tmp_path / "processes",
    )
    monkeypatch.setattr(
        manager,
        "_finalize_after_process_exit",
        lambda request_id, returncode: finalized.append((request_id, returncode)),
    )
    live = process_launcher._LiveProcess(  # noqa: SLF001 - monitor contract test
        request_id=request.request_id,
        task_id="CANCEL_MONITOR_TEST",
        runner="codex",
        topic="cancel-monitor",
        adapter_id="vscode_lm",
        model="glm-5.2",
        process=ExitedProcess(),  # type: ignore[arg-type]
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
        started_at="2026-08-10T00:00:00+00:00",
        timeout_seconds=30,
        isolated=True,
        bridge_request=request,
    )
    manager._live[request.request_id] = live  # noqa: SLF001

    manager._monitor(live)  # noqa: SLF001

    assert finalized == [(request.request_id, 1)]
    assert json.loads(request.cancel_path.read_text(encoding="utf-8"))["diagnostics"]["action"] == "cancel"


def test_prompt_process_cancel_publishes_bridge_terminal_before_process_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _bridge_request_for_cancel_test(
        tmp_path, monkeypatch, request_id="6" * 32,
    )
    request.request_path.rename(Path(f"{request.request_path}.claim-window_test"))
    order: list[str] = []
    real_cancel = vscode_lm_bridge.cancel_request

    def publish_cancel(bound_request: vscode_lm_bridge.BridgeRequest) -> str:
        order.append("bridge")
        return real_cancel(bound_request)

    monkeypatch.setattr(vscode_lm_bridge, "cancel_request", publish_cancel)
    monkeypatch.setattr(
        process_launcher,
        "_terminate_process_group",
        lambda _pid, *, grace_seconds: order.append(f"terminate:{grace_seconds}"),
    )

    class RunningProcess:
        pid = 23456

        @staticmethod
        def poll() -> None:
            return None

    manager = process_launcher.ProcessManager(
        repo=tmp_path / "repo",
        process_log_path=tmp_path / "cancel-processes.jsonl",
        process_dir=tmp_path / "cancel-processes",
    )
    live = process_launcher._LiveProcess(  # noqa: SLF001 - cancellation contract
        request_id=request.request_id,
        task_id="PROMPT_CANCEL_TEST",
        runner="codex",
        topic="prompt-cancel",
        adapter_id="vscode_lm",
        model="glm-5.2",
        process=RunningProcess(),  # type: ignore[arg-type]
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
        started_at="2026-08-10T00:00:00+00:00",
        timeout_seconds=30,
        bridge_request=request,
    )
    manager._live[request.request_id] = live  # noqa: SLF001
    manager._append_event({  # noqa: SLF001
        "request_id": request.request_id,
        "task_id": live.task_id,
        "runner": live.runner,
        "topic": live.topic,
        "adapter_id": live.adapter_id,
        "state": "running",
        "pid": live.process.pid,
    })

    result = manager.cancel(request.request_id, "test cancellation")

    assert result["ok"] is True
    assert result["state"] == "cancelled"
    assert order == ["bridge", "terminate:5.0"]
    assert json.loads(request.response_path.read_text(encoding="utf-8"))["error"] == (
        "vscode_lm_request_cancelled"
    )


def test_worker_bridge_tool_authorization_denies_cancel_requested_state(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "auth-processes.jsonl",
        process_dir=tmp_path / "auth-processes",
    )
    request_id = "7" * 32
    manager._append_event({  # noqa: SLF001 - exact authorization-state setup
        "request_id": request_id,
        "task_id": "CANCEL_AUTH_TEST",
        "runner": "codex",
        "topic": "cancel-auth",
        "adapter_id": "vscode_lm",
        "state": "cancel_requested",
    })

    result = manager.invoke_vscode_lm_worker_tool(
        request_id,
        "aiworkhub_manager_session_write_intent",
        {"action": "append", "content": "must not run"},
    )

    assert result == {"ok": False, "reason": "worker_bridge_request_not_active"}


def test_readiness_distinguishes_stale_host_from_missing_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = _repo(tmp_path)
    repo_id = repository_state.inspect_repository(repo).manifest.repo_id
    host = _host(root, repo_id, models=["glm-5.2"])

    missing = vscode_lm_bridge.bridge_readiness(
        repo, model="deepseek-v4-pro", adapter_id="deepseek_vscode_lm"
    )
    assert missing["blocker_reason"] == "vscode_lm_model_not_visible"
    assert missing["live_host_count"] == 1

    stale_at = vscode_lm_bridge.time.time() - vscode_lm_bridge.HOST_TTL_SECONDS - 5
    os.utime(host, (stale_at, stale_at))
    stale = vscode_lm_bridge.bridge_readiness(
        repo, model="deepseek-v4-pro", adapter_id="deepseek_vscode_lm"
    )
    assert stale["launchable"] is False
    assert stale["blocker_reason"] == "vscode_lm_host_stale"
    assert stale["live_host_count"] == 0
    assert stale["stale_host_count"] == 1
    assert stale["observed_models"] == []


def test_worker_applies_only_fully_validated_allowed_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = _repo(tmp_path)
    repo_id = repository_state.inspect_repository(repo).manifest.repo_id
    _host(root, repo_id, models=["glm-5.2"])
    request_id = "a" * 32
    request_root = tmp_path / request_id
    workspace = request_root / "worktree"
    home = request_root / "home"
    workspace.mkdir(parents=True)
    home.mkdir()

    request = vscode_lm_bridge.create_request(
        repo=repo,
        request_id=request_id,
        workspace_path=workspace,
        workspace_home=home,
        prompt="Use Source Graph and implement the bounded output.",
        model="glm-5.2",
        allowed_writes=["out/*.txt"],
        timeout_seconds=30,
        source_graph_request={"mode": "focus", "query": "bounded output", "budget": 32},
    )
    published = json.loads(request.request_path.read_text(encoding="utf-8"))
    assert published["request_kind"] == "worker"
    assert published["initial_source_graph_request"] == {
        "mode": "focus",
        "query": "bounded output",
        "budget": 32,
        "workflow_stage": "orientation",
    }
    assert published["initial_source_graph_result"] is None
    response = {
        "schema_id": vscode_lm_bridge.RESPONSE_SCHEMA_ID,
        "request_id": request_id,
        "repo_id": repo_id,
        "model": {"id": "glm-5.2"},
        "text": json.dumps(
            {
                "schema_id": vscode_lm_bridge.EDIT_RESPONSE_SCHEMA_ID,
                "summary": "implemented",
                "creates": [{"path": "out/result.txt", "content": "ok\n"}],
                "edits": [],
            }
        ),
        "error": "",
        **_response_decision(request),
    }
    vscode_lm_bridge._atomic_json(request.response_path, response)  # noqa: SLF001
    # The production Landlock/seccomp worker denies fchmod.  The writer must
    # rely on mkstemp's owner-only mode and remain functional without it.
    def _deny_fchmod(_fd: int, _mode: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(vscode_lm_worker.os, "fchmod", _deny_fchmod, raising=False)
    result = vscode_lm_worker.run(request.worker_spec_path)
    assert result["is_error"] is False
    assert result["changed_paths"] == ["out/result.txt"]
    assert (workspace / "out" / "result.txt").read_text(encoding="utf-8") == "ok\n"
    if os.name != "nt":
        assert (workspace / "out" / "result.txt").stat().st_mode & 0o077 == 0


def test_quality_review_request_kind_is_explicit_and_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(tmp_path / "bridge"))
    repo = _repo(tmp_path)
    request_id = "f" * 32
    request_root = tmp_path / request_id
    workspace = request_root / "worktree"
    home = request_root / "home"
    workspace.mkdir(parents=True)
    home.mkdir()

    request = vscode_lm_bridge.create_request(
        repo=repo,
        request_id=request_id,
        workspace_path=workspace,
        workspace_home=home,
        prompt="Review the bound packet.",
        model="claude-sonnet-5",
        allowed_writes=[],
        timeout_seconds=30,
        request_kind="quality_review",
    )
    published = json.loads(request.request_path.read_text(encoding="utf-8"))
    assert published["request_kind"] == "quality_review"

    with pytest.raises(vscode_lm_bridge.BridgeError, match="request_kind_invalid"):
        vscode_lm_bridge.create_request(
            repo=repo,
            request_id="e" * 32,
            workspace_path=tmp_path / ("e" * 32) / "worktree",
            workspace_home=tmp_path / ("e" * 32) / "home",
            prompt="bad kind",
            model="claude-sonnet-5",
            allowed_writes=[],
            timeout_seconds=30,
            request_kind="other",
        )


def test_request_carries_verified_initial_source_graph_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = _repo(tmp_path)
    request_id = "9" * 32
    workspace = tmp_path / request_id / "worktree"
    home = tmp_path / request_id / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
    result = {
        "ok": True,
        "tool": "source_graph",
        "mode": "focus",
        "workflow_stage": "orientation",
        "content": "{\"matches\":[]}",
        "hit_count": 0,
    }

    request = vscode_lm_bridge.create_request(
        repo=repo,
        request_id=request_id,
        workspace_path=workspace,
        workspace_home=home,
        prompt="Use the verified graph receipt.",
        model="glm-5.2",
        allowed_writes=[],
        timeout_seconds=30,
        source_graph_request={"mode": "focus", "query": "bootstrap"},
        source_graph_result=result,
    )

    published = json.loads(request.request_path.read_text(encoding="utf-8"))
    assert published["initial_source_graph_result"] == result


def test_request_rejects_unverified_initial_source_graph_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = _repo(tmp_path)
    request_id = "8" * 32
    workspace = tmp_path / request_id / "worktree"
    home = tmp_path / request_id / "home"
    workspace.mkdir(parents=True)
    home.mkdir()

    with pytest.raises(
        vscode_lm_bridge.BridgeError,
        match="bridge_source_graph_result_invalid",
    ):
        vscode_lm_bridge.create_request(
            repo=repo,
            request_id=request_id,
            workspace_path=workspace,
            workspace_home=home,
            prompt="Do not trust failed graph evidence.",
            model="glm-5.2",
            allowed_writes=[],
            timeout_seconds=30,
            source_graph_request={"mode": "focus", "query": "bootstrap"},
            source_graph_result={"ok": False, "reason": "database is locked"},
        )


def test_request_publishes_hash_pinned_edit_and_create_path_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = _repo(tmp_path)
    request_id = "f" * 32
    workspace = tmp_path / request_id / "worktree"
    home = tmp_path / request_id / "home"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir(parents=True)
    home.mkdir()
    current = workspace / "src" / "app.py"
    current.write_text("print('current')\n", encoding="utf-8")
    placeholder = workspace / "tests" / "test_new.py"
    placeholder.write_bytes(b"")

    request = vscode_lm_bridge.create_request(
        repo=repo,
        request_id=request_id,
        workspace_path=workspace,
        workspace_home=home,
        prompt="bounded",
        model="glm-5.2",
        allowed_writes=["src/app.py", "tests/test_new.py", "docs/*.md"],
        workspace_parent_baseline={
            "src/app.py": "file:664:prior",
            "tests/test_new.py": None,
        },
        timeout_seconds=30,
    )
    published = json.loads(request.request_path.read_text(encoding="utf-8"))
    spec = json.loads(request.worker_spec_path.read_text(encoding="utf-8"))
    contracts = published["path_contracts"]
    assert contracts["src/app.py"] == {
        "action": "edit",
        "current_sha256": hashlib.sha256(current.read_bytes()).hexdigest(),
        "line_count": 1,
        "parent_existed": True,
    }
    assert contracts["tests/test_new.py"]["action"] == "create"
    assert contracts["tests/test_new.py"]["current_sha256"] == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert contracts["tests/test_new.py"]["line_count"] == 0
    assert "docs/*.md" not in contracts
    assert spec["create_paths"] == ["tests/test_new.py"]
    assert spec["path_contracts"] == contracts


def test_missing_parent_baseline_with_nonempty_rework_file_is_edit_not_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = _repo(tmp_path)
    request_id = "e" * 32
    workspace = tmp_path / request_id / "worktree"
    home = tmp_path / request_id / "home"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir(parents=True)
    home.mkdir(mode=0o700)
    inherited = workspace / "src" / "reworked.py"
    inherited.write_text("print('inherited from predecessor rework')\n", encoding="utf-8")

    request = vscode_lm_bridge.create_request(
        repo=repo,
        request_id=request_id,
        workspace_path=workspace,
        workspace_home=home,
        prompt="bounded",
        model="glm-5.2",
        allowed_writes=["src/reworked.py", "tests/test_truly_new.py"],
        workspace_parent_baseline={
            "src/reworked.py": None,
            "tests/test_truly_new.py": None,
        },
        timeout_seconds=30,
    )

    published = json.loads(request.request_path.read_text(encoding="utf-8"))
    spec = json.loads(request.worker_spec_path.read_text(encoding="utf-8"))
    contracts = published["path_contracts"]
    assert contracts["src/reworked.py"] == {
        "action": "edit",
        "current_sha256": hashlib.sha256(inherited.read_bytes()).hexdigest(),
        "line_count": 1,
        "parent_existed": False,
    }
    assert contracts["tests/test_truly_new.py"]["action"] == "create"
    assert contracts["tests/test_truly_new.py"]["parent_existed"] is False
    assert "src/reworked.py" not in spec["create_paths"]
    assert spec["create_paths"] == ["tests/test_truly_new.py"]


def test_path_contract_line_count_matches_semantic_edit_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = _repo(tmp_path)
    request_id = "0" * 32
    workspace = tmp_path / request_id / "worktree"
    home = tmp_path / request_id / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
    (workspace / "trailing.py").write_text("one\ntwo\n", encoding="utf-8")
    (workspace / "no_trailing.py").write_text("one\ntwo", encoding="utf-8")
    (workspace / "empty.py").write_bytes(b"")

    request = vscode_lm_bridge.create_request(
        repo=repo,
        request_id=request_id,
        workspace_path=workspace,
        workspace_home=home,
        prompt="bounded",
        model="glm-5.2",
        allowed_writes=["trailing.py", "no_trailing.py", "empty.py"],
        timeout_seconds=30,
    )
    contracts = json.loads(request.request_path.read_text(encoding="utf-8"))["path_contracts"]
    assert contracts["trailing.py"]["line_count"] == 2
    assert contracts["no_trailing.py"]["line_count"] == 2
    assert contracts["empty.py"]["line_count"] == 0


def test_worker_rejects_whole_mixed_scope_response_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = _repo(tmp_path)
    request_id = "b" * 32
    workspace = tmp_path / request_id / "worktree"
    home = tmp_path / request_id / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
    request = vscode_lm_bridge.create_request(
        repo=repo,
        request_id=request_id,
        workspace_path=workspace,
        workspace_home=home,
        prompt="bounded",
        model="glm-5.2",
        allowed_writes=["out/*.txt"],
        timeout_seconds=30,
    )
    vscode_lm_bridge._atomic_json(  # noqa: SLF001
        request.response_path,
        {
            "schema_id": vscode_lm_bridge.RESPONSE_SCHEMA_ID,
            "request_id": request_id,
            **_response_decision(request),
            "error": "",
            "text": json.dumps(
                {
                    "schema_id": vscode_lm_bridge.EDIT_RESPONSE_SCHEMA_ID_V1,
                    "summary": "bad mixed response",
                    "files": [
                        {"path": "out/good.txt", "content": "must not land"},
                        {"path": "../escape.txt", "content": "bad"},
                    ],
                }
            ),
        },
    )
    with pytest.raises(RuntimeError, match="path_escape"):
        vscode_lm_worker.run(request.worker_spec_path)
    assert not (workspace / "out" / "good.txt").exists()


def test_worker_surfaces_bounded_bridge_diagnostics_on_provider_failure(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    request_id = "e" * 32
    workspace = tmp_path / request_id / "worktree"
    home = tmp_path / request_id / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
    request = vscode_lm_bridge.create_request(
        repo=repo,
        request_id=request_id,
        workspace_path=workspace,
        workspace_home=home,
        prompt="bounded",
        model="glm-5.2",
        allowed_writes=["out/*.txt"],
        timeout_seconds=30,
    )
    vscode_lm_bridge._atomic_json(  # noqa: SLF001
        request.response_path,
        {
            "schema_id": vscode_lm_bridge.RESPONSE_SCHEMA_ID,
            "request_id": request_id,
            **_response_decision(request),
            "error": "vscode_lm_finalization_limit",
            "text": "",
            "diagnostics": {
                "protocol_preview": "unsupported provider prose",
                "turn_trace": [{"turn": 13, "phase": "final", "outcome": "invalid_json"}],
            },
        },
    )

    with pytest.raises(RuntimeError) as captured:
        vscode_lm_worker.run(request.worker_spec_path)

    message = str(captured.value)
    assert "vscode_lm_request_failed:vscode_lm_finalization_limit" in message
    assert "unsupported provider prose" in message
    assert '"phase":"final"' in message


def test_create_request_rejects_cross_request_workspace(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    request_id = "c" * 32
    workspace = tmp_path / "different" / "worktree"
    home = tmp_path / "different" / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
    with pytest.raises(vscode_lm_bridge.BridgeError, match="request_mismatch"):
        vscode_lm_bridge.create_request(
            repo=repo,
            request_id=request_id,
            workspace_path=workspace,
            workspace_home=home,
            prompt="bounded",
            model="glm-5.2",
            allowed_writes=[],
            timeout_seconds=30,
        )


def test_packaged_glm_worker_module_is_importable_from_isolated_cwd(tmp_path: Path) -> None:
    package_root = worker_ai_tools_mcp.resolve_host_package_import_root()
    provider_env = process_launcher._glm_vscode_worker_env(None, package_root)  # noqa: SLF001
    env = process_launcher.sanitized_env(
        "glm_vscode_lm",
        home=tmp_path,
        isolated_task_queue_db=True,
        provider_env=provider_env,
    )
    completed = subprocess.run(
        [sys.executable, "-m", "aiworkhub.vscode_lm_worker", "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--spec" in completed.stdout


def test_glm_bridge_tool_runs_with_exact_worker_audit_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    process_dir = repo / ".aiworkhub" / "runtime" / "processes"
    process_dir.mkdir(parents=True)
    request_id = "d" * 32
    workspace = tmp_path / request_id / "worktree"
    home = tmp_path / request_id / "home"
    workspace.mkdir(parents=True)
    source_file = workspace / "src" / "app.py"
    source_file.parent.mkdir()
    source_file.write_bytes(b"before\ndef target():\n    return 1\nafter\n")
    home.mkdir()
    ledger = home / "audit.jsonl"
    key = home / "audit.key"
    ledger.write_text("", encoding="utf-8")
    key.write_bytes(b"k" * 32)
    metadata_path = process_dir / f"{request_id}.request.json"
    metadata_path.write_text(json.dumps({
        "request_id": request_id,
        "task_id": "GLM_BRIDGE_TEST",
        "runner": "glm52_bridge_test",
        "topic": "bridge_test",
        "adapter_id": "glm_vscode_lm",
        "workspace": {"path": str(workspace)},
        "worker_mcp": {
            "authority_repo": str(repo),
            "source_graph_targets": ["src/aiworkhub"],
            "allowed_writes": ["src/*.py"],
            "session_topic": "bounded bridge test",
            "audit_ledger_path": str(ledger),
            "audit_hmac_key_path": str(key),
        },
    }), encoding="utf-8")
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
    )
    event = {
        "request_id": request_id,
        "adapter_id": "glm_vscode_lm",
        "state": "running",
        "metadata_path": str(metadata_path),
    }
    monkeypatch.setattr(manager, "_request_events", lambda _rid: [event])
    observed: dict[str, object] = {}

    def _source(ctx: worker_ai_tools_mcp.WorkerToolContext, **kwargs: object) -> dict[str, object]:
        observed.update({"ctx": ctx, "kwargs": kwargs})
        return {"ok": True, "tool": "source_graph", "hit_count": 1}

    monkeypatch.setattr(worker_ai_tools_mcp, "source_graph_query", _source)
    result = manager.invoke_vscode_lm_worker_tool(
        request_id,
        "aiworkhub_manager_source_graph_query",
        {"mode": "focus", "query": "bridge", "budget": 16},
    )
    ctx = observed["ctx"]
    assert result["ok"] is True
    assert isinstance(ctx, worker_ai_tools_mcp.WorkerToolContext)
    assert ctx.request_id == request_id
    assert ctx.task_id == "GLM_BRIDGE_TEST"
    assert ctx.authority_repo == repo.resolve()
    assert ctx.audit_ledger_path == ledger
    assert observed["kwargs"] == {"mode": "focus", "query": "bridge", "budget": 16}

    prepared = manager.invoke_vscode_lm_worker_tool(
        request_id,
        "aiworkhub_manager_semantic_edit_prepare",
        {"file_path": "src/app.py", "start_line": 2, "end_line": 3},
    )
    assert prepared["ok"] is True
    assert prepared["fragment"] == "def target():\n    return 1\n"
    assert prepared["file_bytes"] > prepared["fragment_bytes"]
    assert prepared["token_savings_claimed"] is False

    text_provider_alias = manager.invoke_vscode_lm_worker_tool(
        request_id,
        "aiworkhub_manager_semantic_edit_prepare",
        {"path": "src/app.py", "start_line": 2, "end_line": 3},
    )
    assert text_provider_alias["ok"] is True
    assert text_provider_alias["path"] == "src/app.py"
    assert text_provider_alias["fragment"] == prepared["fragment"]

    def _session_intent(
        bound_ctx: worker_ai_tools_mcp.WorkerToolContext, **kwargs: object,
    ) -> dict[str, object]:
        observed.update({"intent_ctx": bound_ctx, "intent_kwargs": kwargs})
        return {"ok": True, "status": "pending_manager_review", "intent_id": "f" * 64}

    monkeypatch.setattr(worker_ai_tools_mcp, "session_write_intent", _session_intent)
    intent = manager.invoke_vscode_lm_worker_tool(
        request_id,
        "aiworkhub_manager_session_write_intent",
        {
            "action": "checkpoint",
            "content": "bounded",
            "idempotency_key": "session:bridge:0001",
            "provenance": "bridge test",
        },
    )
    assert intent["status"] == "pending_manager_review"
    assert observed["intent_ctx"] is ctx or observed["intent_ctx"].request_id == request_id
    assert observed["intent_kwargs"] == {
        "action": "checkpoint",
        "content": "bounded",
        "idempotency_key": "session:bridge:0001",
        "provenance": "bridge test",
    }


def _progress_payload(*, sequence: int = 1) -> dict[str, object]:
    return {
        "schema_id": vscode_lm_bridge.PROGRESS_RECEIPT_SCHEMA_ID,
        "request_id": "a" * 32,
        "repo_id": "repo_test",
        "sequence": sequence,
        "phase": "tool_turn",
        "updated_at": "2026-08-06T08:00:00+00:00",
        "tool_name": "aiworkhub_worker_quality_review_submit",
        "tool_state": "failed",
        "elapsed_ms": 120001,
        "error_code": "mcp_request_timeout",
        "timeout_phase": "request_wait",
        "timeout_ms": 120000,
    }


def test_progress_receipt_missing_is_backward_compatible_no_progress(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    assert vscode_lm_bridge.read_progress_receipt(
        missing, "a" * 32, "repo_test",
    ) == {}
    assert vscode_lm_worker._read_progress_with_retry(
        missing,
        "a" * 32,
        "repo_test",
        defer_transient=False,
    ) == ({}, None)


def test_progress_receipt_is_owner_private_identity_bound_and_monotonic(tmp_path: Path) -> None:
    progress = tmp_path / "progress.json"
    vscode_lm_bridge._atomic_json(progress, _progress_payload(sequence=2))
    receipt = vscode_lm_bridge.read_progress_receipt(
        progress,
        "a" * 32,
        "repo_test",
        owner_uid=os.getuid() if hasattr(os, "getuid") else None,
        previous_sequence=1,
    )
    assert receipt["sequence"] == 2
    assert receipt["tool_name"] == "aiworkhub_worker_quality_review_submit"
    assert receipt["timeout_phase"] == "request_wait"
    with pytest.raises(vscode_lm_bridge.BridgeError, match="bridge_progress_non_monotonic"):
        vscode_lm_bridge.read_progress_receipt(
            progress, "a" * 32, "repo_test", previous_sequence=2,
        )


def test_progress_receipt_persistent_malformed_json_fails_closed(tmp_path: Path) -> None:
    progress = tmp_path / "progress.json"
    progress.write_text("{", encoding="utf-8")
    if os.name != "nt":
        os.chmod(progress, 0o600)

    with pytest.raises(vscode_lm_bridge.BridgeError) as captured:
        vscode_lm_bridge.read_progress_receipt(progress, "a" * 32, "repo_test")

    diagnostic = str(captured.value)
    assert diagnostic.startswith("bridge_progress_invalid_json:line=1:column=2:")
    assert "bytes=1" in diagnostic
    assert f"sha256={hashlib.sha256(b'{').hexdigest()}" in diagnostic


def test_progress_receipt_present_unsafe_sidecars_fail_closed(tmp_path: Path) -> None:
    progress = tmp_path / "progress.json"
    vscode_lm_bridge._atomic_json(progress, _progress_payload())
    if os.name != "nt":
        os.chmod(progress, 0o644)
        with pytest.raises(vscode_lm_bridge.BridgeError, match="bridge_progress_insecure_mode"):
            vscode_lm_bridge.read_progress_receipt(progress, "a" * 32, "repo_test")
    progress.unlink()
    progress.symlink_to(tmp_path / "absent-target.json")
    with pytest.raises(vscode_lm_bridge.BridgeError, match="bridge_progress_symlink"):
        vscode_lm_bridge.read_progress_receipt(progress, "a" * 32, "repo_test")


def test_progress_receipt_open_handle_rejects_aba_swapped_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress = tmp_path / "progress.json"
    saved = tmp_path / "saved.json"
    forged = tmp_path / "forged.json"
    vscode_lm_bridge._atomic_json(progress, _progress_payload(sequence=1))
    forged_payload = _progress_payload(sequence=99)
    forged_payload["phase"] = "final_edit"
    vscode_lm_bridge._atomic_json(forged, forged_payload)

    real_read_bytes = Path.read_bytes
    real_open = vscode_lm_bridge.os.open

    def path_read_aba(path: Path) -> bytes:
        if path != progress:
            return real_read_bytes(path)
        progress.rename(saved)
        try:
            return real_read_bytes(forged)
        finally:
            saved.rename(progress)

    def redirected_open(path: str | os.PathLike[str], flags: int) -> int:
        if Path(path) == progress:
            return real_open(forged, flags)
        return real_open(path, flags)

    # The old path-based implementation accepted the forged bytes after the
    # path was restored.  The handle-bound implementation detects that the
    # opened file identity does not match the pre-open lstat identity.
    monkeypatch.setattr(Path, "read_bytes", path_read_aba)
    monkeypatch.setattr(vscode_lm_bridge.os, "open", redirected_open)
    with pytest.raises(
        vscode_lm_bridge.ProgressReadTransientError,
        match="bridge_progress_snapshot_changed",
    ):
        vscode_lm_bridge.read_progress_receipt_snapshot(
            progress, "a" * 32, "repo_test",
        )

    assert json.loads(progress.read_text(encoding="utf-8"))["sequence"] == 1


def test_progress_security_failure_receipt_is_structured_and_path_free(
    tmp_path: Path,
) -> None:
    progress = tmp_path / "sensitive-parent" / "progress.json"
    progress.parent.mkdir()
    vscode_lm_bridge._atomic_json(progress, _progress_payload(sequence=7))

    with pytest.raises(vscode_lm_bridge.ProgressReceiptSecurityError) as captured:
        vscode_lm_bridge.read_progress_receipt(
            progress,
            "a" * 32,
            "different_repo",
        )

    assert captured.value.receipt == {
        "schema_id": "aiworkhub.vscode_lm.progress_security_failure.v1",
        "validation_phase": "repository_identity",
        "invariant": "bridge_progress_repo_mismatch",
        "observed_sequence": 7,
    }
    assert str(progress) not in json.dumps(captured.value.receipt)


def test_worker_streams_monotonic_progress_before_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = _repo(tmp_path)
    repo_id = repository_state.inspect_repository(repo).manifest.repo_id
    _host(root, repo_id, models=["glm-5.2"])
    request_id = "a" * 32
    workspace = tmp_path / request_id / "worktree"
    home = tmp_path / request_id / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
    request = vscode_lm_bridge.create_request(
        repo=repo,
        request_id=request_id,
        workspace_path=workspace,
        workspace_home=home,
        prompt="Return one bounded edit.",
        model="glm-5.2",
        allowed_writes=["out.txt"],
        timeout_seconds=30,
    )
    progress_path = Path(
        json.loads(request.worker_spec_path.read_text(encoding="utf-8"))["progress_path"]
    )
    response = {
        "schema_id": vscode_lm_bridge.RESPONSE_SCHEMA_ID,
        "request_id": request_id,
        "repo_id": repo_id,
        "model": {"id": "glm-5.2"},
        "text": json.dumps({
            "schema_id": vscode_lm_bridge.EDIT_RESPONSE_SCHEMA_ID,
            "summary": "done",
            "creates": [{"path": "out.txt", "content": "ok\n"}],
            "edits": [],
        }),
        "error": "",
        **_response_decision(request),
    }
    sleeps = 0
    progress_read_failures = 0
    real_os_read = vscode_lm_bridge.os.read

    def transient_progress_read(fd: int, size: int) -> bytes:
        nonlocal progress_read_failures
        if progress_read_failures < 2:
            progress_read_failures += 1
            raise PermissionError(13, "simulated Windows sharing violation")
        return real_os_read(fd, size)

    def advance(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            vscode_lm_bridge._atomic_json(
                progress_path,
                {
                    "schema_id": vscode_lm_bridge.PROGRESS_RECEIPT_SCHEMA_ID,
                    "request_id": request_id,
                    "repo_id": repo_id,
                    "sequence": 1,
                    "phase": "tool_turn",
                    "updated_at": "2026-08-06T08:00:00+00:00",
                },
            )
        elif sleeps == 2:
            vscode_lm_bridge._atomic_json(
                progress_path,
                {
                    "schema_id": vscode_lm_bridge.PROGRESS_RECEIPT_SCHEMA_ID,
                    "request_id": request_id,
                    "repo_id": repo_id,
                    "sequence": 2,
                    "phase": "tool_turn",
                    "updated_at": "2026-08-06T08:00:01+00:00",
                },
            )
        elif sleeps == 5:
            vscode_lm_bridge._atomic_json(request.response_path, response)

    monkeypatch.setattr(vscode_lm_bridge.os, "read", transient_progress_read)
    monkeypatch.setattr(vscode_lm_worker.time, "sleep", advance)

    result = vscode_lm_worker.run(request.worker_spec_path)

    output = capsys.readouterr().out
    progress = json.loads(output.strip())
    assert progress == {
        "phase": "tool_turn",
        "sequence": 2,
        "type": "aiworkhub_progress",
        "updated_at": "2026-08-06T08:00:01+00:00",
    }
    assert sleeps == 5
    assert progress_read_failures == 2
    assert result["is_error"] is False


def test_worker_terminal_progress_read_persistent_error_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = _repo(tmp_path)
    repo_id = repository_state.inspect_repository(repo).manifest.repo_id
    _host(root, repo_id, models=["glm-5.2"])
    request_id = "b" * 32
    workspace = tmp_path / request_id / "worktree"
    home = tmp_path / request_id / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
    request = vscode_lm_bridge.create_request(
        repo=repo,
        request_id=request_id,
        workspace_path=workspace,
        workspace_home=home,
        prompt="Return one read-only result.",
        model="glm-5.2",
        allowed_writes=[],
        timeout_seconds=30,
    )
    worker_spec = json.loads(request.worker_spec_path.read_text(encoding="utf-8"))
    progress_path = Path(worker_spec["progress_path"])
    vscode_lm_bridge._atomic_json(
        progress_path,
        {
            **_progress_payload(sequence=1),
            "request_id": request_id,
            "repo_id": repo_id,
        },
    )
    vscode_lm_bridge._atomic_json(
        request.response_path,
        {
            "schema_id": vscode_lm_bridge.RESPONSE_SCHEMA_ID,
            "request_id": request_id,
            "repo_id": repo_id,
            "model": {"id": "glm-5.2"},
            "text": json.dumps({
                "schema_id": vscode_lm_bridge.EDIT_RESPONSE_SCHEMA_ID,
                "summary": "read only",
                "creates": [],
                "edits": [],
            }),
            "error": "",
            **_response_decision(request),
        },
    )
    progress_read_attempts = 0

    def persistently_denied(_fd: int) -> bytes:
        nonlocal progress_read_attempts
        progress_read_attempts += 1
        raise vscode_lm_bridge.ProgressReadTransientError(
            "bridge_progress_read_transient:operation=read:"
            "type=PermissionError:errno=13"
        )

    monkeypatch.setattr(vscode_lm_bridge, "_read_progress_fd", persistently_denied)
    with pytest.raises(RuntimeError) as captured:
        vscode_lm_worker.run(request.worker_spec_path)

    assert str(captured.value) == (
        "vscode_lm_progress_terminal_read_failed:"
        "bridge_progress_read_transient:operation=read:"
        "type=PermissionError:errno=13"
    )
    assert progress_read_attempts == vscode_lm_worker.PROGRESS_READ_MAX_ATTEMPTS
