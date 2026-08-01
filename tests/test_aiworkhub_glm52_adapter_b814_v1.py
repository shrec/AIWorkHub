"""B814: GLM 5.2 Copilot BYOK adapter candidate tests."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import glm_credentials as gc  # noqa: E402
from aiworkhub import deepseek_credentials, process_launcher, runtime_adapters  # noqa: E402


FAKE_KEY = "sk-glm-B814-FAKE-not-a-real-key"
ADAPTER = "glm_copilot_cli"


def _copilot(tmp_path: Path) -> Path:
    exe = tmp_path / "copilot"
    fd = os.open(exe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o755)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    return exe.resolve()


def _write_credential(path: Path, *, api_key: str = FAKE_KEY, base_url: str | None = None) -> Path:
    return gc.bootstrap_credential(
        path=path,
        api_key=api_key,
        base_url=base_url or gc.GLM_BASE_URL,
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )


def _chmod_blocked_by_sandbox() -> bool:
    import tempfile

    with tempfile.TemporaryDirectory() as name:
        try:
            os.chmod(name, 0o700)
        except PermissionError:
            return True
    return False


@pytest.fixture(autouse=True)
def _bridge_chmod_sandbox_restriction(monkeypatch: pytest.MonkeyPatch) -> None:
    if _chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Task MCP Tests").returncode == 0
    (root / "out").mkdir()
    (root / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    assert _git(root, "add", "out/result.txt").returncode == 0
    assert _git(root, "commit", "-qm", "fixture").returncode == 0
    return root


def _card() -> dict:
    return {
        "task_id": "TASK_GLM_B814",
        "runner": "glm_worker_b814",
        "topic": "task_mcp",
        "status": "pending",
        "worker_status": "unclaimed",
        "claimed_by": "",
        "review_requested_by": "",
        "allowed_writes": ["out/result.txt"],
        "read_first": [],
        "validation": [],
        "priority": "high",
    }


def _show(card: dict):
    def show(task_id: str) -> dict:
        assert task_id == card["task_id"]
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    return show


def _collision(**_kwargs) -> dict:
    return {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""}


def _manager(tmp_path: Path, repo: Path, card: dict) -> process_launcher.ProcessManager:
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(card),
        collision_guard=_collision,
        adapter_builder=lambda **_kwargs: SimpleNamespace(argv=[], launchable=True, reason=""),
        isolation_enabled=True,
    )


def test_glm_copilot_is_supported_local_adapter() -> None:
    assert ADAPTER in runtime_adapters.SUPPORTED_ADAPTERS
    assert ADAPTER in runtime_adapters.LOCAL_ADAPTERS
    assert ADAPTER not in runtime_adapters.MANUAL_ONLY_ADAPTERS
    assert runtime_adapters.ADAPTER_EXECUTABLES[ADAPTER] == "copilot"
    assert runtime_adapters.GLM_SUPPORTED_MODELS == ("glm-5.2",)


def test_glm_argv_is_noninteractive_byok_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    exe = _copilot(tmp_path)
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: str(exe))

    plan = runtime_adapters.build_runtime_command(ADAPTER, "Implement change", repo)

    assert plan.argv == [
        str(exe),
        "-p", "Implement change",
        "--output-format", "json",
        "--allow-all-tools",
        "--excluded-tools=grep,glob",
        "--no-ask-user",
        "--no-remote",
        "--no-remote-export",
        "--disable-builtin-mcps",
        "--no-color",
        "--no-auto-update",
        "--secret-env-vars", "COPILOT_PROVIDER_API_KEY",
        "--deny-tool=shell(grep:*)",
        "--deny-tool=shell(rg:*)",
        "--deny-tool=shell(find:*)",
        "--deny-tool=shell(tree:*)",
        "-C", str(repo.resolve()),
        "--model", "glm-5.2",
    ]
    assert FAKE_KEY not in json.dumps(plan.as_dict())
    assert "--allow-all-paths" not in plan.argv
    assert "--yolo" not in plan.argv


def test_glm_rejects_every_non_glm52_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    exe = _copilot(tmp_path)
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: str(exe))
    for bad_model in ("glm-4", "glm-5.1", "gpt-5.4", "auto"):
        plan = runtime_adapters.build_runtime_command(ADAPTER, "x", repo, model=bad_model)
        assert plan.launchable is False
        assert "unsupported_glm_model" in plan.validation_reason
        assert plan.argv == []


def test_glm_credential_default_path_is_under_aiworkhub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(gc.CREDENTIAL_PATH_ENV, raising=False)
    assert gc.credential_path() == home / ".config" / "aiworkhub" / "glm_copilot_credential.json"


def test_glm_bootstrap_loads_0600_credential_and_provider_env(tmp_path: Path) -> None:
    path = tmp_path / "cred.json"
    written = _write_credential(path)
    assert written == path
    assert os.name == "nt" or stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert os.name == "nt" or stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    cred = gc.load_credential(path=path)
    assert cred.base_url == gc.GLM_BASE_URL
    assert cred.provider_type == "openai"
    assert cred.default_model == "glm-5.2"
    assert cred.supported_models == runtime_adapters.GLM_SUPPORTED_MODELS
    assert FAKE_KEY not in repr(cred)
    assert cred.provider_env("glm-5.2") == {
        "COPILOT_PROVIDER_TYPE": "openai",
        "COPILOT_PROVIDER_BASE_URL": gc.GLM_BASE_URL,
        "COPILOT_MODEL": "glm-5.2",
        "COPILOT_PROVIDER_API_KEY": FAKE_KEY,
    }


@pytest.mark.parametrize(
    "base_url",
    ["https://api.openai.com/v1", "http://open.bigmodel.cn/api/paas/v4", "https://evil.example/v1"],
)
def test_glm_rejects_non_https_or_non_allowlisted_endpoint(tmp_path: Path, base_url: str) -> None:
    path = tmp_path / "cred.json"
    path.write_text(json.dumps({"provider": "glm", "base_url": base_url, "api_key": FAKE_KEY}), encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(gc.CredentialError) as exc:
        gc.load_credential(path=path)
    assert "endpoint" in exc.value.reason


def test_glm_credential_inside_repository_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = repo / "cred.json"
    _write_credential(path)
    with pytest.raises(gc.CredentialError) as exc:
        gc.load_credential(path=path, repo=repo)
    assert exc.value.reason == "credential_inside_repository"


def test_glm_credential_rejects_unbounded_fields(tmp_path: Path) -> None:
    path = tmp_path / "cred.json"
    path.write_text(
        json.dumps({"provider": "glm", "base_url": gc.GLM_BASE_URL, "api_key": FAKE_KEY, "extra": "x"}),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    with pytest.raises(gc.CredentialError) as exc:
        gc.load_credential(path=path)
    assert exc.value.reason == "credential_unsupported_fields:extra"


def test_glm_status_is_secret_free_and_names_setup_status_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "cred.json"
    _write_credential(path)
    monkeypatch.setattr(
        runtime_adapters,
        "resolve_executable",
        lambda a, *_a, **_k: runtime_adapters.ExecutableResolution(a, "/x/copilot", True, ""),
    )
    status = gc.credential_status(path=path)
    assert status["launchable"] is True
    assert status["setup_command"] == "python3 -m aiworkhub.glm_credentials setup"
    assert status["status_command"] == "python3 -m aiworkhub.glm_credentials status"
    assert status["supported_models"] == ["glm-5.2"]
    assert FAKE_KEY not in json.dumps(status)
    assert "api_key" not in json.dumps(status)


def test_combined_adapter_readiness_does_not_false_positive_glm_without_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(gc.CREDENTIAL_PATH_ENV, str(tmp_path / "absent-glm.json"))
    monkeypatch.setattr(
        runtime_adapters,
        "resolve_executable",
        lambda a, *_a, **_k: runtime_adapters.ExecutableResolution(a, "/x/copilot", True, ""),
    )

    readiness = deepseek_credentials.adapter_readiness(repo=tmp_path)
    glm = next(row for row in readiness["adapters"] if row["adapter_id"] == ADAPTER)

    assert glm["installed"] is True
    assert glm["credential_present"] is False
    assert glm["launchable"] is False
    assert glm["blocker_reason"] == "credential_file_absent"
    assert glm["kind"] == "local_copilot_byok_glm"


def test_glm_runner_rejects_github_hosted_adapters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path) -> None:
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    monkeypatch.setattr(process_launcher.project_context, "collect_project_context", lambda *_a, **_k: None)
    card = _card()
    manager = _manager(tmp_path, repo, card)
    for bad_adapter in ("claude_cli", "codex_cli", "deepseek_copilot_cli"):
        result = manager.launch(
            task_id=card["task_id"],
            runner=card["runner"],
            topic=card["topic"],
            adapter_id=bad_adapter,
            timeout_seconds=60,
        )
        assert result["ok"] is False
        assert "runner_adapter_mismatch" in result["blocked_reason"]


def test_missing_glm_credential_fails_before_claim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path) -> None:
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    monkeypatch.setenv(gc.CREDENTIAL_PATH_ENV, str(tmp_path / "absent.json"))
    monkeypatch.setattr(process_launcher.project_context, "collect_project_context", lambda *_a, **_k: None)
    claims: list[tuple] = []
    monkeypatch.setattr(process_launcher.core, "claim_start_exact", lambda *a, **_k: claims.append(a) or {"ok": True})
    card = _card()
    manager = _manager(tmp_path, repo, card)

    result = manager.launch(
        task_id=card["task_id"],
        runner=card["runner"],
        topic=card["topic"],
        adapter_id=ADAPTER,
        timeout_seconds=60,
    )

    assert result["ok"] is False
    assert "glm_credential_missing:credential_file_absent" in result["blocked_reason"]
    assert claims == []
    assert FAKE_KEY not in json.dumps(result)


def test_process_launcher_provider_env_is_secret_only_from_loaded_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    cred = tmp_path / "cred.json"
    _write_credential(cred)
    monkeypatch.setenv(gc.CREDENTIAL_PATH_ENV, str(cred))
    monkeypatch.setenv("COPILOT_PROVIDER_API_KEY", "leak-me-if-buggy")
    manager = _manager(tmp_path, repo, _card())

    provider_env, model = manager._resolve_provider_env(ADAPTER, None)
    assert model == "glm-5.2"
    assert provider_env is not None
    child_env = process_launcher.sanitized_env(ADAPTER, provider_env=provider_env)
    assert child_env["COPILOT_PROVIDER_API_KEY"] == FAKE_KEY
    assert child_env["COPILOT_PROVIDER_BASE_URL"] == gc.GLM_BASE_URL
    assert child_env["COPILOT_MODEL"] == "glm-5.2"


def test_usage_parser_reads_glm_openai_compatible_jsonl(tmp_path: Path) -> None:
    output = tmp_path / "copilot.jsonl"
    output.write_text(
        json.dumps({
            "type": "result",
            "stats": {
                "usage": {
                    "prompt_tokens": 321,
                    "completion_tokens": 45,
                    "prompt_cache_hit_tokens": 200,
                }
            },
        }) + "\n",
        encoding="utf-8",
    )
    usage = process_launcher._usage_from_output(output)
    assert usage["input_tokens"] == 321
    assert usage["output_tokens"] == 45
    assert usage["cached_input_tokens"] == 200
    assert process_launcher._ledger_input_tokens(usage, ADAPTER) == 321
