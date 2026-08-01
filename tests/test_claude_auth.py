from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

from aiworkhub import claude_auth


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
