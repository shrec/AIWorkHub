from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

from aiworkhub import claude_auth, deepseek_credentials, runtime_adapters


def test_subscription_status_is_bounded_cached_and_secret_free(monkeypatch) -> None:
    claude_auth.invalidate()
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "loggedIn": True,
                "authMethod": "claude.ai",
                "subscriptionType": "max",
                "accessToken": "must-not-escape",
            }).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    first = claude_auth.auth_status(sys.executable, force=True)
    second = claude_auth.auth_status(sys.executable)

    assert first["launchable"] is True
    assert first["auth_method"] == "claude.ai"
    assert first["subscription_type"] == "max"
    assert "accessToken" not in first
    assert "must-not-escape" not in json.dumps(first)
    assert second["cache_hit"] is True
    assert len(calls) == 1
    assert calls[0][1:] == ["auth", "status", "--json"]


def test_logged_out_status_fails_closed(monkeypatch) -> None:
    claude_auth.invalidate()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stdout=b'{"loggedIn":false,"authMethod":"none"}',
            stderr=b"",
        ),
    )

    status = claude_auth.auth_status(sys.executable, force=True)

    assert status["launchable"] is False
    assert status["status"] == "authentication_required"
    assert status["blocker_reason"] == "claude_authentication_required"


def test_timeout_is_classified_without_stderr_or_credentials(monkeypatch) -> None:
    claude_auth.invalidate()

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=5, output=b"token=secret")

    monkeypatch.setattr(subprocess, "run", timeout)
    status = claude_auth.auth_status(sys.executable, force=True)

    assert status["launchable"] is False
    assert status["blocker_reason"] == "claude_auth_status_failed:TimeoutExpired"
    assert "secret" not in json.dumps(status)


def test_editor_launcher_never_receives_claude_auth_arguments(
    tmp_path, monkeypatch
) -> None:
    claude_auth.invalidate()
    editor = tmp_path / "code"
    editor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    editor.chmod(0o755)

    def forbidden_run(*_args, **_kwargs):
        raise AssertionError("editor launcher must not be executed as Claude")

    monkeypatch.setattr(subprocess, "run", forbidden_run)
    status = claude_auth.auth_status(str(editor), force=True)

    assert status["launchable"] is False
    assert status["blocker_reason"] == "claude_executable_is_editor_launcher"


def test_live_provider_401_temporarily_overrides_stale_logged_in_status(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv(
        "AIWORKHUB_CLAUDE_AUTH_STATE_FILE", str(tmp_path / "claude-auth.json")
    )
    claude_auth.invalidate()
    calls: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(argv, **_kwargs):
        if list(argv)[1:] != ["auth", "status", "--json"]:
            return real_run(argv, **_kwargs)
        calls.append(list(argv))
        return SimpleNamespace(
            returncode=0,
            stdout=b'{"loggedIn":true,"authMethod":"claude.ai","subscriptionType":"max"}',
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert claude_auth.auth_status(sys.executable, force=True)["launchable"] is True

    claude_auth.record_runtime_auth_failure(sys.executable, http_status=401)
    status = claude_auth.auth_status(sys.executable, force=True)

    assert status["launchable"] is False
    assert status["status"] == "authentication_expired"
    assert status["blocker_reason"] == "claude_runtime_authentication_failed"
    assert status["http_status"] == 401
    assert status["runtime_observed"] is True
    assert len(calls) == 1


def test_live_provider_401_survives_runtime_reload_without_persisting_secret(
    tmp_path, monkeypatch,
) -> None:
    state_path = tmp_path / "runtime" / "claude-auth.json"
    monkeypatch.setenv("AIWORKHUB_CLAUDE_AUTH_STATE_FILE", str(state_path))
    claude_auth.invalidate()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stdout=b'{"loggedIn":true,"accessToken":"must-not-persist"}',
            stderr=b"",
        ),
    )

    claude_auth.record_runtime_auth_failure(sys.executable, http_status=401)
    claude_auth._runtime_failures.clear()
    status = claude_auth.auth_status(sys.executable, force=True)

    assert status["launchable"] is False
    assert status["persisted_runtime_observation"] is True
    assert status["http_status"] == 401
    state = state_path.read_text(encoding="utf-8")
    assert "must-not-persist" not in state
    assert str(sys.executable) not in state


def test_compatibility_adapter_readiness_uses_claude_live_auth_truth(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime_adapters,
        "resolve_executable",
        lambda adapter_id: runtime_adapters.ExecutableResolution(
            adapter_id, "/usr/bin/claude", True, ""
        ),
    )
    monkeypatch.setattr(
        claude_auth,
        "auth_status",
        lambda _executable=None: {
            "authenticated": False,
            "launchable": False,
            "blocker_reason": "claude_runtime_authentication_failed",
            "runtime_observed": True,
        },
    )

    report = deepseek_credentials.adapter_readiness()
    claude = next(
        item for item in report["adapters"] if item["adapter_id"] == "claude_cli"
    )

    assert claude["installed"] is True
    assert claude["credential_present"] is False
    assert claude["launchable"] is False
    assert claude["blocker_reason"] == "claude_runtime_authentication_failed"
    assert claude["runtime_observed"] is True
