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
    tmp_path,
    monkeypatch,
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

    assert claude_auth.record_runtime_auth_failure(
        sys.executable,
        http_status=401,
        error_code="authentication_failed",
        session_id="session-1",
    )
    status = claude_auth.auth_status(sys.executable, force=True)

    assert status["launchable"] is False
    assert status["status"] == "authentication_expired"
    assert status["blocker_reason"] == "claude_subscription_session_refresh_required"
    assert status["http_status"] == 401
    assert status["runtime_observed"] is True
    assert len(calls) == 1


def test_live_provider_401_survives_runtime_reload_without_persisting_secret(
    tmp_path,
    monkeypatch,
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

    assert claude_auth.record_runtime_auth_failure(
        sys.executable,
        http_status=401,
        error_code="authentication_failed",
        session_id="session-2",
    )
    claude_auth._runtime_failures.clear()
    status = claude_auth.auth_status(sys.executable)

    assert status["launchable"] is False
    assert status["persisted_runtime_observation"] is True
    assert status["http_status"] == 401
    state = state_path.read_text(encoding="utf-8")
    payload = json.loads(state)
    assert "must-not-persist" not in state
    assert "session-2" not in state
    assert str(sys.executable) not in state
    assert payload["http_status"] == 401
    assert len(payload["session_id_sha256"]) == 64
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert state_path.parent.stat().st_mode & 0o777 == 0o700


def test_runtime_auth_failure_classifier_rejects_non_exact_receipts(
    tmp_path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "runtime" / "claude-auth.json"
    monkeypatch.setenv("AIWORKHUB_CLAUDE_AUTH_STATE_FILE", str(state_path))
    claude_auth.invalidate()

    rejected = [
        {"http_status": True, "error_code": "authentication_failed", "session_id": "s"},
        {
            "http_status": "401",
            "error_code": "authentication_failed",
            "session_id": "s",
        },
        {"http_status": 403, "error_code": "authentication_failed", "session_id": "s"},
        {"http_status": 401, "error_code": "quota_exceeded", "session_id": "s"},
        {"http_status": 401, "error_code": "policy_violation", "session_id": "s"},
        {"http_status": 401, "error_code": "network_error", "session_id": "s"},
        {"http_status": 401, "error_code": "authentication_failed", "session_id": ""},
        {"http_status": 401, "error_code": "authentication failed", "session_id": "s"},
    ]

    for receipt in rejected:
        assert not claude_auth.record_runtime_auth_failure(sys.executable, **receipt)

    assert not state_path.exists()


def test_runtime_auth_failure_classifier_returns_only_hashed_session() -> None:
    classified = claude_auth.classify_runtime_auth_failure(
        http_status=401,
        error_code="authentication_failed",
        session_id="provider-session-secret",
    )

    assert classified is not None
    assert classified["http_status"] == 401
    assert len(classified["session_id_sha256"]) == 64
    assert "provider-session-secret" not in json.dumps(classified)
    assert (
        claude_auth.classify_runtime_auth_failure(
            http_status=True,
            error_code="authentication_failed",
            session_id="provider-session-secret",
        )
        is None
    )


def test_fresh_success_clears_persisted_runtime_failure(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "runtime" / "claude-auth.json"
    monkeypatch.setenv("AIWORKHUB_CLAUDE_AUTH_STATE_FILE", str(state_path))
    claude_auth.invalidate()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stdout=b'{"loggedIn":true,"authMethod":"claude.ai"}',
            stderr=b"",
        ),
    )

    assert claude_auth.record_runtime_auth_failure(
        sys.executable,
        http_status=401,
        error_code="authentication_failed",
        session_id="session-3",
    )
    claude_auth._runtime_failures.clear()

    fresh = claude_auth.auth_status(sys.executable, force=True)
    cached = claude_auth.auth_status(sys.executable)

    assert fresh["launchable"] is True
    assert cached["launchable"] is True
    assert not state_path.exists()


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
            "blocker_reason": "claude_subscription_session_refresh_required",
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
    assert claude["blocker_reason"] == "claude_subscription_session_refresh_required"
    assert claude["runtime_observed"] is True
