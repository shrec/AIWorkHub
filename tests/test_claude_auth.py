from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

from aiworkhub import claude_auth, deepseek_credentials, platform_io, runtime_adapters


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


def _no_backoff(monkeypatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(claude_auth.time, "sleep", sleeps.append)
    return sleeps


def _ready_result() -> SimpleNamespace:
    return SimpleNamespace(
        returncode=0,
        stdout=b'{"loggedIn":true,"authMethod":"claude.ai"}',
        stderr=b"",
    )


def _resolved_executable() -> str:
    return str(claude_auth.Path(sys.executable).resolve(strict=True))


def _default_bounds() -> list[float]:
    floor = claude_auth.STATUS_TIMEOUT_FLOOR_SECONDS
    return [floor, min(claude_auth.STATUS_TIMEOUT_CEILING_SECONDS, 2 * floor)]


def test_timeout_is_transient_without_stderr_or_credentials(monkeypatch) -> None:
    claude_auth.invalidate()
    _no_backoff(monkeypatch)

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=5, output=b"token=secret")

    monkeypatch.setattr(subprocess, "run", timeout)
    status = claude_auth.auth_status(sys.executable, force=True)

    assert status["launchable"] is False
    assert status["transient"] is True
    assert status["status"] == "auth_status_transient"
    assert status["blocker_reason"] == "claude_auth_status_transient:TimeoutExpired"
    assert "authentication_required" not in json.dumps(status)
    assert status["retry_after_seconds"] > 0
    assert "secret" not in json.dumps(status)


def test_probe_bound_tolerates_measured_loaded_latency() -> None:
    claude_auth.invalidate()
    first, retry = claude_auth.status_probe_timeouts(_resolved_executable())
    measured = claude_auth.MEASURED_LOADED_STATUS_LATENCY_SECONDS

    assert measured == 8.7
    assert first >= claude_auth.STATUS_LATENCY_HEADROOM * measured
    assert first == claude_auth.STATUS_TIMEOUT_FLOOR_SECONDS
    assert first < retry <= claude_auth.STATUS_TIMEOUT_CEILING_SECONDS


def test_logged_in_probe_at_measured_latency_launches_first_try(monkeypatch) -> None:
    claude_auth.invalidate()
    timeouts: list[float] = []

    def loaded_host(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        if kwargs["timeout"] < claude_auth.MEASURED_LOADED_STATUS_LATENCY_SECONDS:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])
        return _ready_result()

    monkeypatch.setattr(subprocess, "run", loaded_host)
    status = claude_auth.auth_status(sys.executable, force=True)

    assert status["launchable"] is True
    assert status["status"] == "ready"
    assert len(timeouts) == 1


def test_probe_bound_is_derived_from_measured_latency(monkeypatch) -> None:
    claude_auth.invalidate()
    clock = [1000.0]
    latency = [30.0]

    def slow_ready(_argv, **_kwargs):
        clock[0] += latency[0]
        return _ready_result()

    monkeypatch.setattr(subprocess, "run", slow_ready)
    monkeypatch.setattr(claude_auth.time, "monotonic", lambda: clock[0])
    path = _resolved_executable()

    claude_auth.auth_status(sys.executable, force=True)
    first, retry = claude_auth.status_probe_timeouts(path)
    assert first == claude_auth.STATUS_LATENCY_HEADROOM * 30.0
    assert retry == min(claude_auth.STATUS_TIMEOUT_CEILING_SECONDS, 2 * first)

    latency[0] = 10 * claude_auth.STATUS_TIMEOUT_CEILING_SECONDS
    claude_auth.auth_status(sys.executable, force=True)
    assert claude_auth.status_probe_timeouts(path) == (
        claude_auth.STATUS_TIMEOUT_CEILING_SECONDS,
        claude_auth.STATUS_TIMEOUT_CEILING_SECONDS,
    )


def test_latency_samples_are_bounded() -> None:
    claude_auth.invalidate()
    for index in range(claude_auth.MAX_PROBE_LATENCY_PATHS + 5):
        claude_auth._record_probe_latency(f"/opt/claude-{index}", 1.0)
    for _ in range(claude_auth.STATUS_LATENCY_SAMPLES + 3):
        claude_auth._record_probe_latency("/opt/claude-latest", 1.0)

    assert len(claude_auth._probe_latencies) == claude_auth.MAX_PROBE_LATENCY_PATHS
    assert (
        len(claude_auth._probe_latencies["/opt/claude-latest"])
        == claude_auth.STATUS_LATENCY_SAMPLES
    )
    claude_auth.invalidate()
    assert claude_auth._probe_latencies == {}


def test_transient_probe_timeout_is_retried_once_after_backoff(
    tmp_path, monkeypatch
) -> None:
    state_path = tmp_path / "runtime" / "claude-auth.json"
    monkeypatch.setenv("AIWORKHUB_CLAUDE_AUTH_STATE_FILE", str(state_path))
    claude_auth.invalidate()
    sleeps = _no_backoff(monkeypatch)
    timeouts: list[float] = []

    def slow_then_ready(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        if len(timeouts) == 1:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])
        return _ready_result()

    monkeypatch.setattr(subprocess, "run", slow_then_ready)
    status = claude_auth.auth_status(sys.executable, force=True)

    assert status["launchable"] is True
    assert status["status"] == "ready"
    assert timeouts == _default_bounds()
    assert sleeps == [claude_auth.STATUS_RETRY_BACKOFF_SECONDS]
    assert not state_path.exists()


def test_timeout_without_cache_is_transient_measured_and_never_cached(
    tmp_path, monkeypatch
) -> None:
    state_path = tmp_path / "runtime" / "claude-auth.json"
    monkeypatch.setenv("AIWORKHUB_CLAUDE_AUTH_STATE_FILE", str(state_path))
    claude_auth.invalidate()
    _no_backoff(monkeypatch)
    timeouts: list[float] = []

    def always_slow(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", always_slow)
    status = claude_auth.auth_status(sys.executable)

    assert status["launchable"] is False
    assert status["status"] == "auth_status_transient"
    assert status["blocker_reason"] == "claude_auth_status_transient:TimeoutExpired"
    assert status["executable_path"] == _resolved_executable()
    attempts = status["probe_attempts"]
    assert [item["timeout_seconds"] for item in attempts] == _default_bounds()
    assert all(item["outcome"] == "TimeoutExpired" for item in attempts)
    assert all(
        isinstance(item["elapsed_seconds"], float) and item["elapsed_seconds"] >= 0
        for item in attempts
    )
    assert len(timeouts) == 2
    assert not state_path.exists()

    # A negative result is never cached: the next call re-probes.
    again = claude_auth.auth_status(sys.executable)
    assert again["cache_hit"] is False
    assert len(timeouts) == 4
    assert _resolved_executable() not in claude_auth._cache
    # A timed-out probe is not a latency measurement: bounds do not escalate.
    assert timeouts[2:] == _default_bounds()
    assert not state_path.exists()


def _cached_positive_then_slow(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "AIWORKHUB_CLAUDE_AUTH_STATE_FILE", str(tmp_path / "claude-auth.json")
    )
    claude_auth.invalidate()
    _no_backoff(monkeypatch)
    clock = [1000.0]
    slow = [False]
    timeouts: list[float] = []

    def probe(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        if slow[0]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])
        return _ready_result()

    monkeypatch.setattr(subprocess, "run", probe)
    monkeypatch.setattr(claude_auth.time, "monotonic", lambda: clock[0])
    assert claude_auth.auth_status(sys.executable, force=True)["launchable"] is True
    slow[0] = True
    return timeouts, clock


def test_timeout_with_cached_positive_does_not_refuse_launch(
    tmp_path, monkeypatch
) -> None:
    timeouts, clock = _cached_positive_then_slow(monkeypatch, tmp_path)

    clock[0] += claude_auth.CACHE_REFRESH_SECONDS + 1
    status = claude_auth.auth_status(sys.executable)

    assert status["launchable"] is True
    assert status["status"] == "ready"
    assert status["cache_hit"] is True
    assert status["probe_transient"] is True
    assert status["cached_age_seconds"] == claude_auth.CACHE_REFRESH_SECONDS + 1
    outcomes = [item["outcome"] for item in status["probe_attempts"]]
    assert outcomes == ["TimeoutExpired", "TimeoutExpired"]
    assert len(timeouts) == 3

    forced = claude_auth.auth_status(sys.executable, force=True)
    assert forced["launchable"] is True
    assert forced["probe_transient"] is True
    assert len(timeouts) == 5


def test_cached_positive_expires_after_ttl_and_timeout_is_transient(
    tmp_path, monkeypatch
) -> None:
    timeouts, clock = _cached_positive_then_slow(monkeypatch, tmp_path)

    clock[0] = 1000.0 + claude_auth.CACHE_TTL_SECONDS - 1
    within = claude_auth.auth_status(sys.executable)
    clock[0] = 1000.0 + claude_auth.CACHE_TTL_SECONDS
    expired = claude_auth.auth_status(sys.executable)

    assert within["launchable"] is True
    assert within["probe_transient"] is True
    assert expired["launchable"] is False
    assert expired["status"] == "auth_status_transient"
    assert expired["transient"] is True
    assert "probe_transient" not in expired
    assert _resolved_executable() not in claude_auth._cache
    assert len(timeouts) == 5


def test_positive_result_is_cached_for_bounded_refresh_window(monkeypatch) -> None:
    claude_auth.invalidate()
    calls: list[list[str]] = []
    clock = [1000.0]

    def ready(argv, **_kwargs):
        calls.append(list(argv))
        return _ready_result()

    monkeypatch.setattr(subprocess, "run", ready)
    monkeypatch.setattr(claude_auth.time, "monotonic", lambda: clock[0])
    first = claude_auth.auth_status(sys.executable, force=True)
    clock[0] += claude_auth.CACHE_REFRESH_SECONDS - 1
    within = claude_auth.auth_status(sys.executable)
    clock[0] = 1000.0 + claude_auth.CACHE_REFRESH_SECONDS
    refreshed = claude_auth.auth_status(sys.executable)

    assert claude_auth.CACHE_REFRESH_SECONDS < claude_auth.CACHE_TTL_SECONDS
    assert first["launchable"] is True
    assert within["cache_hit"] is True
    assert refreshed["cache_hit"] is False
    assert refreshed["launchable"] is True
    assert len(calls) == 2


def test_negative_result_is_never_cached_and_evicts_positive(monkeypatch) -> None:
    claude_auth.invalidate()
    logged_in = [True]
    calls: list[list[str]] = []

    def probe(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"loggedIn": logged_in[0]}).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", probe)
    assert claude_auth.auth_status(sys.executable, force=True)["launchable"] is True
    logged_in[0] = False
    logged_out = claude_auth.auth_status(sys.executable, force=True)
    again = claude_auth.auth_status(sys.executable)

    assert logged_out["status"] == "authentication_required"
    assert again["launchable"] is False
    assert again["cache_hit"] is False
    assert len(calls) == 3
    assert _resolved_executable() not in claude_auth._cache


def test_unrunnable_probe_is_unavailable_and_never_cached(monkeypatch) -> None:
    claude_auth.invalidate()
    calls: list[float] = []

    def missing(_argv, **kwargs):
        calls.append(kwargs["timeout"])
        raise FileNotFoundError("claude")

    monkeypatch.setattr(subprocess, "run", missing)
    first = claude_auth.auth_status(sys.executable)
    second = claude_auth.auth_status(sys.executable)

    assert first["status"] == "auth_status_unavailable"
    assert first["blocker_reason"] == "claude_auth_status_failed:FileNotFoundError"
    assert "transient" not in first
    assert second["cache_hit"] is False
    assert len(calls) == 2


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
    if platform_io.posix_path_modes_supported():
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


def test_retry_refresh_bypasses_but_does_not_clear_live_failure(
    tmp_path, monkeypatch
) -> None:
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
        session_id="session-retry",
    )

    refreshed = claude_auth.refresh_subscription_session_for_retry(sys.executable)
    still_blocked = claude_auth.auth_status(sys.executable)

    assert refreshed["launchable"] is True
    assert still_blocked["launchable"] is False
    assert still_blocked["blocker_reason"] == (
        "claude_subscription_session_refresh_required"
    )
    assert state_path.exists()


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
