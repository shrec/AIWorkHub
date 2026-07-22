"""B343: local deepseek_copilot_cli adapter + secure BYOK credential tests.

Replayable, isolation-safe (per-test tmp dirs, env overrides) coverage for:
adapter identity/argv, model routing guard, credential file validation, secret
non-leakage, sanitized environment, missing-credential no-claim behavior,
isolated launch planning, process/usage parsing, adapter readiness, cancellation,
and regression of the Claude/Codex paths.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    deepseek_credentials as dc,
    process_launcher,
    runtime_adapters,
    worker_workspace,
)

# An obviously-fake, non-real key. It must never appear in argv, logs, audit
# events, dashboard payloads, or Git -- only in the launched child env.
FAKE_KEY = "sk-deepseek-B343-FAKE-not-a-real-key"
ADAPTER = "deepseek_copilot_cli"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _copilot(tmp_path: Path) -> Path:
    exe = tmp_path / "copilot"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe.resolve()


def _write_credential(path: Path, *, api_key: str = FAKE_KEY, base_url: str | None = None) -> Path:
    return dc.bootstrap_credential(
        path=path,
        api_key=api_key,
        base_url=base_url or dc.DEEPSEEK_BASE_URL,
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, shell=False,
    )


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
        "task_id": "TASK_DS_B343",
        "runner": "deepseek_worker_b343",
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


def _plan(argv: list[str]):
    def build(**kwargs):
        return SimpleNamespace(argv=list(argv), cwd=str(kwargs["repo"]), launchable=True, reason="")

    return build


def _wait_terminal(manager, request_id, timeout=15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = manager.collect(request_id)
        if result.get("terminal"):
            return result
        time.sleep(0.02)
    raise AssertionError("worker did not become terminal")


# ---------------------------------------------------------------------------
# Adapter identity + argv shape
# ---------------------------------------------------------------------------

def test_deepseek_copilot_is_a_supported_local_adapter():
    assert ADAPTER in runtime_adapters.SUPPORTED_ADAPTERS
    assert ADAPTER in runtime_adapters.LOCAL_ADAPTERS
    assert ADAPTER not in runtime_adapters.MANUAL_ONLY_ADAPTERS
    assert runtime_adapters.ADAPTER_EXECUTABLES[ADAPTER] == "copilot"
    # deepseek_manual remains an explicit manual-only fallback.
    assert "deepseek_manual" in runtime_adapters.MANUAL_ONLY_ADAPTERS


def test_deepseek_argv_is_noninteractive_byok_shape(monkeypatch, tmp_path):
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
        "--model", "deepseek-v4-pro",  # production coding default
    ]
    assert plan.launchable is True
    assert plan.manual_only is False
    # Never pass --allow-all-paths/--yolo: permissions stay subordinate to the
    # outer filesystem sandbox.
    assert "--allow-all-paths" not in plan.argv
    assert "--yolo" not in plan.argv
    assert "--allow-all" not in plan.argv


def test_deepseek_prompt_is_a_single_argv_token_no_injection(monkeypatch, tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    exe = _copilot(tmp_path)
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: str(exe))
    prompt = "შეასწორე; rm -rf / && echo $COPILOT_PROVIDER_API_KEY `whoami`"

    plan = runtime_adapters.build_runtime_command(ADAPTER, prompt, repo, model="deepseek-v4-flash")

    # The whole prompt (shell metacharacters included) is exactly one token.
    assert plan.argv.count(prompt) == 1
    assert plan.argv[plan.argv.index("-p") + 1] == prompt
    assert "deepseek-v4-flash" in plan.argv


def test_deepseek_flash_and_pro_selectable(monkeypatch, tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    exe = _copilot(tmp_path)
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: str(exe))
    for model in ("deepseek-v4-pro", "deepseek-v4-flash"):
        plan = runtime_adapters.build_runtime_command(ADAPTER, "x", repo, model=model)
        assert plan.launchable is True
        assert plan.argv[plan.argv.index("--model") + 1] == model


@pytest.mark.parametrize("bad_model", ["gpt-5.4", "claude-sonnet-4", "deepseek-chat", "auto"])
def test_deepseek_rejects_non_deepseek_models(monkeypatch, tmp_path, bad_model):
    repo = tmp_path / "r"
    repo.mkdir()
    exe = _copilot(tmp_path)
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: str(exe))
    plan = runtime_adapters.build_runtime_command(ADAPTER, "x", repo, model=bad_model)
    assert plan.launchable is False
    assert plan.validation_ok is False
    assert "unsupported_deepseek_model" in plan.validation_reason
    assert bad_model not in " ".join(plan.argv)


def test_resolve_deepseek_model_defaults_to_pro():
    assert runtime_adapters.resolve_deepseek_model(None) == ("deepseek-v4-pro", None)
    assert runtime_adapters.resolve_deepseek_model("deepseek-v4-flash") == ("deepseek-v4-flash", None)
    model, err = runtime_adapters.resolve_deepseek_model("gpt-5.4")
    assert model is None and "unsupported_deepseek_model" in err


def test_deepseek_manual_still_manual_only(monkeypatch, tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()

    def unexpected(_):
        raise AssertionError("manual-only adapters have no executable")

    monkeypatch.setattr(runtime_adapters.shutil, "which", unexpected)
    plan = runtime_adapters.build_runtime_command("deepseek_manual", "review", repo)
    assert plan.manual_only is True
    assert plan.launchable is False
    assert plan.argv == []


def test_claude_and_codex_argv_unchanged(monkeypatch, tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    exe = _copilot(tmp_path)
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: str(exe))
    claude = runtime_adapters.build_runtime_command("claude_cli", "p", repo)
    assert claude.argv[:2] == [str(exe), "-p"]
    assert "--no-session-persistence" in claude.argv
    codex = runtime_adapters.build_runtime_command("codex_cli", "p", repo)
    assert codex.argv[1] == "exec"
    assert codex.argv[-1] == "p"


# ---------------------------------------------------------------------------
# Credential file validation + secret handling
# ---------------------------------------------------------------------------

def test_bootstrap_writes_0600_file_and_loads(tmp_path):
    path = tmp_path / "cred.json"
    written = _write_credential(path)
    assert written == path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    cred = dc.load_credential(path=path)
    assert cred.base_url == dc.DEEPSEEK_BASE_URL
    assert cred.provider_type == "openai"
    assert cred.default_model == "deepseek-v4-pro"
    assert cred.supported_models == runtime_adapters.DEEPSEEK_SUPPORTED_MODELS


def test_bootstrap_uses_getpass_never_argv(tmp_path):
    path = tmp_path / "cred.json"
    prompts: list[str] = []

    def fake_getpass(prompt: str) -> str:
        prompts.append(prompt)
        return FAKE_KEY

    dc.bootstrap_credential(path=path, getpass_fn=fake_getpass)
    assert prompts and "key" in prompts[0].lower()
    assert dc.load_credential(path=path).api_key == FAKE_KEY


def test_credential_repr_and_provider_env(tmp_path):
    path = tmp_path / "cred.json"
    _write_credential(path)
    cred = dc.load_credential(path=path)
    # repr must never leak the key (api_key is repr=False).
    assert FAKE_KEY not in repr(cred)
    env = cred.provider_env("deepseek-v4-pro")
    assert env == {
        "COPILOT_PROVIDER_TYPE": "openai",
        "COPILOT_PROVIDER_BASE_URL": "https://api.deepseek.com/v1",
        "COPILOT_MODEL": "deepseek-v4-pro",
        "COPILOT_PROVIDER_API_KEY": FAKE_KEY,
    }


def test_missing_credential_file_reports_absent(tmp_path):
    with pytest.raises(dc.CredentialError) as exc:
        dc.load_credential(path=tmp_path / "nope.json")
    assert exc.value.reason == "credential_file_absent"


def test_group_or_world_readable_credential_is_rejected(tmp_path):
    path = tmp_path / "cred.json"
    _write_credential(path)
    os.chmod(path, 0o644)
    with pytest.raises(dc.CredentialError) as exc:
        dc.load_credential(path=path)
    assert exc.value.reason == "credential_group_or_world_accessible"


def test_symlinked_credential_is_rejected(tmp_path):
    real = tmp_path / "real.json"
    _write_credential(real)
    link = tmp_path / "cred.json"
    link.symlink_to(real)
    with pytest.raises(dc.CredentialError) as exc:
        dc.load_credential(path=link)
    assert exc.value.reason == "credential_symlink_rejected"


def test_empty_api_key_is_rejected(tmp_path):
    path = tmp_path / "cred.json"
    path.write_text(json.dumps({"base_url": dc.DEEPSEEK_BASE_URL, "api_key": "  "}), encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(dc.CredentialError) as exc:
        dc.load_credential(path=path)
    assert exc.value.reason == "credential_empty_api_key"


@pytest.mark.parametrize(
    "base_url",
    ["https://api.openai.com/v1", "http://api.deepseek.com/v1", "https://evil.example/v1"],
)
def test_non_deepseek_endpoint_is_rejected(tmp_path, base_url):
    path = tmp_path / "cred.json"
    path.write_text(json.dumps({"base_url": base_url, "api_key": FAKE_KEY}), encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(dc.CredentialError) as exc:
        dc.load_credential(path=path)
    assert "endpoint" in exc.value.reason


def test_credential_inside_repository_is_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = repo / "cred.json"
    _write_credential(path)
    with pytest.raises(dc.CredentialError) as exc:
        dc.load_credential(path=path, repo=repo)
    assert exc.value.reason == "credential_inside_repository"


def test_bootstrap_refuses_non_deepseek_endpoint(tmp_path):
    with pytest.raises(dc.CredentialError):
        dc.bootstrap_credential(path=tmp_path / "cred.json", api_key=FAKE_KEY, base_url="https://api.openai.com/v1")


# ---------------------------------------------------------------------------
# Sanitized environment
# ---------------------------------------------------------------------------

def test_sanitized_env_only_carries_provider_env_when_given():
    base = worker_workspace.sanitized_env(ADAPTER)
    assert "COPILOT_PROVIDER_API_KEY" not in base
    assert "COPILOT_PROVIDER_BASE_URL" not in base
    provider = {
        "COPILOT_PROVIDER_TYPE": "openai",
        "COPILOT_PROVIDER_BASE_URL": dc.DEEPSEEK_BASE_URL,
        "COPILOT_MODEL": "deepseek-v4-pro",
        "COPILOT_PROVIDER_API_KEY": FAKE_KEY,
    }
    merged = worker_workspace.sanitized_env(ADAPTER, provider_env=provider)
    assert merged["COPILOT_PROVIDER_API_KEY"] == FAKE_KEY
    assert merged["COPILOT_PROVIDER_BASE_URL"] == dc.DEEPSEEK_BASE_URL
    # The explicit minimal allowlist is still built from scratch (no broad copy).
    assert merged["PATH"] == "/usr/local/bin:/usr/bin:/bin"


def test_sanitized_env_does_not_read_key_from_os_environ(monkeypatch):
    monkeypatch.setenv("COPILOT_PROVIDER_API_KEY", "leak-me-if-buggy")
    env = worker_workspace.sanitized_env(ADAPTER)
    # No passthrough entry copies the key from the coordinator environment.
    assert env.get("COPILOT_PROVIDER_API_KEY") != "leak-me-if-buggy"
    assert "COPILOT_PROVIDER_API_KEY" not in env


# ---------------------------------------------------------------------------
# Readiness (secret-free)
# ---------------------------------------------------------------------------

def test_credential_status_is_secret_free(tmp_path, monkeypatch):
    path = tmp_path / "cred.json"
    _write_credential(path)
    monkeypatch.setattr(runtime_adapters, "resolve_executable",
                        lambda a, *_a, **_k: runtime_adapters.ExecutableResolution(a, "/x/copilot", True, ""))
    status = dc.credential_status(path=path)
    assert status["installed"] is True
    assert status["credential_present"] is True
    assert status["launchable"] is True
    assert status["blocker_reason"] == ""
    assert status["endpoint"] == dc.DEEPSEEK_BASE_URL
    assert set(status["supported_models"]) == set(runtime_adapters.DEEPSEEK_SUPPORTED_MODELS)
    assert FAKE_KEY not in json.dumps(status)
    assert "api_key" not in json.dumps(status)


def test_credential_status_missing_credential_blocks_and_names_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_adapters, "resolve_executable",
                        lambda a, *_a, **_k: runtime_adapters.ExecutableResolution(a, "/x/copilot", True, ""))
    status = dc.credential_status(path=tmp_path / "absent.json")
    assert status["credential_present"] is False
    assert status["launchable"] is False
    assert status["blocker_reason"] == "credential_file_absent"


def test_adapter_readiness_lists_all_adapters_no_secret(tmp_path, monkeypatch):
    path = tmp_path / "cred.json"
    _write_credential(path)
    monkeypatch.setenv(dc.CREDENTIAL_PATH_ENV, str(path))
    report = dc.adapter_readiness()
    ids = {a["adapter_id"] for a in report["adapters"]}
    assert ids == set(runtime_adapters.SUPPORTED_ADAPTERS)
    assert report["readonly"] is True
    assert report["authority_flags"]["secret_export"] is False
    assert FAKE_KEY not in json.dumps(report)
    manual = next(a for a in report["adapters"] if a["adapter_id"] == "deepseek_manual")
    assert manual["launchable"] is False


# ---------------------------------------------------------------------------
# Launcher: runner->adapter routing + missing-credential no-claim
# ---------------------------------------------------------------------------

def _manager(tmp_path: Path, card: dict, argv: list[str], *, isolation: bool) -> process_launcher.ProcessManager:
    repo = tmp_path / "mrepo"
    repo.mkdir(exist_ok=True)
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(card),
        collision_guard=_collision,
        adapter_builder=_plan(argv),
        isolation_enabled=isolation,
    )


def test_deepseek_runner_rejects_github_hosted_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    card = _card()
    manager = _manager(tmp_path, card, [sys.executable, "-c", "pass"], isolation=False)
    for bad_adapter in ("claude_cli", "codex_cli"):
        result = manager.launch(
            task_id=card["task_id"], runner=card["runner"], topic=card["topic"],
            adapter_id=bad_adapter, timeout_seconds=30,
        )
        assert result["ok"] is False
        assert "runner_adapter_mismatch" in result["blocked_reason"]


def test_missing_credential_fails_before_claim_and_leaves_task_unclaimed(monkeypatch, tmp_path):
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    monkeypatch.setenv(dc.CREDENTIAL_PATH_ENV, str(tmp_path / "absent-credential.json"))
    claims: list[tuple] = []
    monkeypatch.setattr(process_launcher.core, "claim_start_exact",
                        lambda *a: claims.append(a) or {"ok": True})
    card = _card()
    # Isolated manager: _resolve_provider_env runs before select_sandbox_backend
    # AND before claim_start_exact, so a missing credential blocks with no claim.
    manager = _manager(tmp_path, card, [sys.executable, "-c", "pass"], isolation=True)
    result = manager.launch(
        task_id=card["task_id"], runner=card["runner"], topic=card["topic"],
        adapter_id=ADAPTER, timeout_seconds=60,
    )
    assert result["ok"] is False
    assert "deepseek_credential_missing:credential_file_absent" in result["blocked_reason"]
    assert claims == [], "claim-start must not run on a missing credential"
    # The blocked reason never contains the (absent) key path's contents.
    assert FAKE_KEY not in json.dumps(result)


def test_bad_deepseek_model_blocks_before_claim(monkeypatch, tmp_path):
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    cred = tmp_path / "cred.json"
    _write_credential(cred)
    monkeypatch.setenv(dc.CREDENTIAL_PATH_ENV, str(cred))
    claims: list[tuple] = []
    monkeypatch.setattr(process_launcher.core, "claim_start_exact",
                        lambda *a: claims.append(a) or {"ok": True})
    card = _card()
    manager = _manager(tmp_path, card, [sys.executable, "-c", "pass"], isolation=True)
    result = manager.launch(
        task_id=card["task_id"], runner=card["runner"], topic=card["topic"],
        adapter_id=ADAPTER, model="gpt-5.4", timeout_seconds=60,
    )
    assert result["ok"] is False
    assert "deepseek_model_rejected" in result["blocked_reason"]
    assert claims == []


# ---------------------------------------------------------------------------
# Direct-path launch: credential reaches ONLY the child env; then cancel
# ---------------------------------------------------------------------------

def test_direct_launch_injects_key_into_child_env_and_cancels(monkeypatch, tmp_path):
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    cred = tmp_path / "cred.json"
    _write_credential(cred)
    monkeypatch.setenv(dc.CREDENTIAL_PATH_ENV, str(cred))

    dump = tmp_path / "child_env_key.txt"
    script = (
        "import os,time;"
        f"open({str(dump)!r},'w').write(os.environ.get('COPILOT_PROVIDER_API_KEY','MISSING')+'|'+"
        "os.environ.get('COPILOT_PROVIDER_BASE_URL','')+'|'+os.environ.get('COPILOT_MODEL',''));"
        "time.sleep(30)"
    )
    card = _card()
    manager = _manager(tmp_path, card, [sys.executable, "-c", script], isolation=False)
    launched = manager.launch(
        task_id=card["task_id"], runner=card["runner"], topic=card["topic"],
        adapter_id=ADAPTER, model="deepseek-v4-flash", timeout_seconds=120,
    )
    assert launched["ok"] is True
    # Wait for the child to dump its env.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not dump.exists():
        time.sleep(0.02)
    assert dump.exists(), "child never wrote env marker"
    key, base_url, model = dump.read_text(encoding="utf-8").split("|")
    assert key == FAKE_KEY
    assert base_url == dc.DEEPSEEK_BASE_URL
    assert model == "deepseek-v4-flash"

    cancelled = manager.cancel(launched["request_id"], reason="test")
    assert cancelled["ok"] is True
    result = _wait_terminal(manager, launched["request_id"])
    assert result["state"] == "cancelled"

    # The event log never records the key.
    log_text = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert FAKE_KEY not in log_text


# ---------------------------------------------------------------------------
# Isolated (sandboxed) launch: exact claim + key reaches sandboxed child
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 1,
    reason="Landlock is not supported by this kernel",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="GitHub hosted runners cannot complete nested Landlock execution",
)
def test_isolated_launch_claims_and_passes_key_through_sandbox(monkeypatch, tmp_path, repo):
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    monkeypatch.setenv(worker_workspace.SANDBOX_BACKEND_ENV, "landlock")
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    cred = tmp_path / "cred.json"  # outside the fixture repo
    _write_credential(cred)
    monkeypatch.setenv(dc.CREDENTIAL_PATH_ENV, str(cred))

    card = _card()
    calls: list[tuple] = []

    def claim(repo_root, task_id, runner, topic, *, request_id=None):
        assert repo_root == repo
        assert request_id
        calls.append(("claim", task_id, runner, topic))
        card.update({"status": "processing", "worker_status": "in_progress", "claimed_by": runner})
        return {"ok": True}

    def review(task_id, *, runner, topic):
        calls.append(("review", task_id, runner, topic))
        card.update({"status": "review", "worker_status": "review", "review_requested_by": runner})
        return {"ok": True}

    monkeypatch.setattr(process_launcher.task_engine, "claim_start_exact", claim)
    monkeypatch.setattr(process_launcher.core, "mark_review", review)

    # The worker writes the key it received into its single allowed output.
    script = (
        "import os;"
        "open('out/result.txt','w').write(os.environ.get('COPILOT_PROVIDER_API_KEY','MISSING'))"
    )
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(card),
        collision_guard=_collision,
        adapter_builder=_plan([sys.executable, "-c", script]),
    )
    launched = manager.launch(
        task_id=card["task_id"], runner=card["runner"], topic=card["topic"],
        adapter_id=ADAPTER, model="deepseek-v4-pro", timeout_seconds=60,
    )
    assert launched["ok"] is True
    assert launched["workspace_isolated"] is True

    result = _wait_terminal(manager, launched["request_id"])
    assert result["state"] == "review_ready"
    assert [c[0] for c in calls] == ["claim", "review"]
    # The sandboxed child received the key and wrote it into the promoted file.
    assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == FAKE_KEY


# ---------------------------------------------------------------------------
# Process/usage parsing for Copilot JSONL / DeepSeek OpenAI-compatible usage
# ---------------------------------------------------------------------------

def test_usage_parser_reads_deepseek_openai_compatible_jsonl(tmp_path):
    output = tmp_path / "copilot.jsonl"
    lines = [
        json.dumps({"type": "assistant", "message": {"role": "assistant"}}),
        json.dumps({
            "type": "result",
            "stats": {
                "usage": {
                    "prompt_tokens": 210,
                    "completion_tokens": 64,
                    "total_tokens": 274,
                    "prompt_cache_hit_tokens": 128,
                }
            },
        }),
    ]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    usage = process_launcher._usage_from_output(output)
    assert usage["input_tokens"] == 210
    assert usage["output_tokens"] == 64
    assert usage["cached_input_tokens"] == 128  # observability only
    # OpenAI-compatible: prompt_tokens already includes cached; no double count.
    assert process_launcher._ledger_input_tokens(usage, ADAPTER) == 210


def test_usage_parser_invents_nothing_when_no_usage(tmp_path):
    output = tmp_path / "copilot.jsonl"
    output.write_text(json.dumps({"type": "assistant", "content": "done"}) + "\n", encoding="utf-8")
    usage = process_launcher._usage_from_output(output)
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# Dashboard readiness surfacing (read-only, secret-free)
# ---------------------------------------------------------------------------

def test_dashboard_snapshot_includes_adapter_readiness_without_secret(tmp_path):
    from aiworkhub import dashboard

    path = tmp_path / "cred.json"
    _write_credential(path)

    class _Provider:
        def list_tasks(self, status):
            return []

        def get_completion_inbox(self):
            return {"review_queue": [], "stale_processing": [], "read_errors": []}

        def get_cost_ledger(self):
            return {"aggregates": {"by_runner": {}}, "source_status": {}}

        def get_collision_report(self):
            return {"collision_free": True, "active_cards": 0, "collision_count": 0, "file_collisions": []}

        def get_agent_processes(self):
            return {"ok": True, "processes": [], "total_requests": 0}

        def get_adapter_readiness(self):
            return dc.adapter_readiness(repo=tmp_path)

    snapshot = dashboard.build_snapshot(_Provider())
    readiness = snapshot["adapter_readiness"]
    ids = {a["adapter_id"] for a in readiness["adapters"]}
    assert ADAPTER in ids
    assert FAKE_KEY not in json.dumps(snapshot)


def test_completion_inbox_mcp_tool_surfaces_adapter_readiness(monkeypatch, tmp_path):
    from aiworkhub import server

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    path = tmp_path / "cred.json"  # OUTSIDE the repo (required)
    _write_credential(path)
    monkeypatch.setenv(dc.CREDENTIAL_PATH_ENV, str(path))
    monkeypatch.setattr(server.completion_inbox, "build_completion_inbox",
                        lambda **_k: {"review_queue": [], "stale_processing": []})
    monkeypatch.setattr(server.process_launcher, "default_manager",
                        lambda: SimpleNamespace(list_processes=lambda limit: {"ok": True, "processes": []}))
    monkeypatch.setattr(server.core, "repo_root", lambda: repo_dir)

    result = server.aiworkhub_completion_inbox()
    readiness = result["adapter_readiness"]
    ids = {a["adapter_id"] for a in readiness["adapters"]}
    assert ADAPTER in ids
    ds = next(a for a in readiness["adapters"] if a["adapter_id"] == ADAPTER)
    assert ds["credential_present"] is True
    assert ds["endpoint"] == dc.DEEPSEEK_BASE_URL
    assert FAKE_KEY not in json.dumps(result)


def test_dashboard_snapshot_tolerates_provider_without_readiness_method():
    from aiworkhub import dashboard

    class _MinimalProvider:
        def list_tasks(self, status):
            return []

        def get_completion_inbox(self):
            return {"review_queue": [], "stale_processing": [], "read_errors": []}

        def get_cost_ledger(self):
            return {"aggregates": {"by_runner": {}}, "source_status": {}}

        def get_collision_report(self):
            return {"collision_free": True, "collision_count": 0, "file_collisions": []}

        def get_agent_processes(self):
            return {"ok": True, "processes": [], "total_requests": 0}

    snapshot = dashboard.build_snapshot(_MinimalProvider())
    assert snapshot["adapter_readiness"] == {"ok": True, "readonly": True, "adapters": []}


def test_record_usage_labels_deepseek_copilot_provider(monkeypatch, tmp_path):
    output = tmp_path / "copilot.jsonl"
    output.write_text(json.dumps({
        "type": "result",
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }) + "\n", encoding="utf-8")
    card = _card()
    captured: list[list[str]] = []
    monkeypatch.setattr(process_launcher.core, "run_taskctl",
                        lambda args, **_k: captured.append(args) or SimpleNamespace(returncode=0, stderr=""))
    manager = _manager(tmp_path, card, [sys.executable, "-c", "pass"], isolation=False)
    usage, recorded, error = manager._record_usage(
        "req-ds", card["task_id"], card["runner"], ADAPTER, "deepseek-v4-pro", output,
    )
    assert recorded is True and error == ""
    args = captured[0]
    assert args[args.index("--provider") + 1] == "deepseek_copilot"
    assert args[args.index("--input-tokens") + 1] == "100"
    assert args[args.index("--model") + 1] == "deepseek-v4-pro"
