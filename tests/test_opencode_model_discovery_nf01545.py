"""NF-2026-01545: OpenCode model discovery retries, keeps last-known-good, names failures."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import model_settings, repo_policy, runtime_adapters

_EXE = "/tmp/opencode-nf01545"
_ROWS = ["opencode/a-free", "opencode-go/b"]
_EMPTY_POLICY = {"providers": {}, "adapters": {}, "models": {}}


@pytest.fixture(autouse=True)
def _clean_probe_state():
    with repo_policy._OPENCODE_PROBE_LOCK:
        repo_policy._OPENCODE_LAST_GOOD_MODELS.clear()
        repo_policy._OPENCODE_LAST_PROBE.clear()
    yield
    with repo_policy._OPENCODE_PROBE_LOCK:
        repo_policy._OPENCODE_LAST_GOOD_MODELS.clear()
        repo_policy._OPENCODE_LAST_PROBE.clear()


def _script_run(monkeypatch: pytest.MonkeyPatch, steps: list) -> list[dict]:
    calls: list[dict] = []
    queue = list(steps)

    def fake_run(argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        step = queue.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    monkeypatch.setattr(repo_policy.subprocess, "run", fake_run)
    return calls


def _ok(rows: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        returncode=0, stdout="\n".join(rows).encode("utf-8") + b"\n", stderr=b""
    )


def _timeout() -> subprocess.TimeoutExpired:
    return subprocess.TimeoutExpired([_EXE, "models"], 5)


def test_double_timeout_serves_last_known_good_with_named_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _script_run(monkeypatch, [_ok(_ROWS), _timeout(), _timeout()])

    assert repo_policy._list_opencode_models(_EXE) == _ROWS
    assert repo_policy._opencode_probe_record(_EXE) == {"state": "ok", "count": 2}

    assert repo_policy._list_opencode_models(_EXE) == _ROWS
    assert repo_policy._opencode_probe_record(_EXE) == {
        "state": "failed",
        "reason": "timeout",
        "served_last_known_good": True,
    }
    assert [call["timeout"] for call in calls] == [5, 5, 15]


def test_timeout_then_success_on_retry_uses_longer_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _script_run(monkeypatch, [_timeout(), _ok(_ROWS)])

    assert repo_policy._list_opencode_models(_EXE) == _ROWS
    assert [call["timeout"] for call in calls] == [5, 15]
    assert repo_policy._opencode_probe_record(_EXE) == {"state": "ok", "count": 2}


def test_nonzero_exit_without_prior_success_names_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _script_run(
        monkeypatch, [SimpleNamespace(returncode=3, stdout=b"", stderr=b"")]
    )

    assert repo_policy._list_opencode_models(_EXE) == []
    assert len(calls) == 1
    assert repo_policy._opencode_probe_record(_EXE) == {
        "state": "failed",
        "reason": "exit_3",
        "served_last_known_good": False,
    }


def test_zero_exit_with_no_rows_is_empty_and_oserror_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _script_run(
        monkeypatch,
        [
            SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
            SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
            OSError("gone"),
        ],
    )

    assert repo_policy._list_opencode_models(_EXE) == []
    assert repo_policy._opencode_probe_record(_EXE)["reason"] == "empty"
    assert repo_policy._list_opencode_models(_EXE) == []
    assert repo_policy._opencode_probe_record(_EXE)["reason"] == "oserror"


def test_oversized_payload_is_a_named_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _script_run(
        monkeypatch,
        [
            _ok(_ROWS),
            SimpleNamespace(returncode=0, stdout=b"x" * (64 * 1024 + 1), stderr=b""),
        ],
    )

    assert repo_policy._list_opencode_models(_EXE) == _ROWS
    assert repo_policy._list_opencode_models(_EXE) == _ROWS
    assert repo_policy._opencode_probe_record(_EXE) == {
        "state": "failed",
        "reason": "oversized",
        "served_last_known_good": True,
    }


def test_provider_status_exposes_opencode_models_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: False)
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: runtime_adapters.ExecutableResolution(
            adapter_id, _EXE, True, ""
        ),
    )
    _script_run(monkeypatch, [_ok(_ROWS), _timeout(), _timeout()])
    policy = {
        "providers": {
            "allowed_adapters": list(
                repo_policy.DEFAULT_POLICY["providers"]["allowed_adapters"]
            )
        }
    }

    def status() -> dict:
        return repo_policy._provider_status(
            Path("."), "opencode_cli", policy, "bubblewrap", "",
            model_policy=_EMPTY_POLICY,
        )

    first = status()
    assert first["opencode_models_probe"] == {"state": "ok", "count": 2}
    assert first["provider_observed_models"] == _ROWS

    second = status()
    assert second["opencode_models_probe"] == {
        "state": "failed",
        "reason": "timeout",
        "served_last_known_good": True,
    }
    assert second["provider_observed_models"] == _ROWS


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("opencode-go/gemini-4-argon-contributor", True),
        ("opencode/foo-free", True),
        ("opencode/foo", False),
        ("opencode-go/a/b", False),
        ("other/foo", False),
    ],
)
def test_opencode_identity_defaults(model: str, expected: bool) -> None:
    assert (
        model_settings.evaluate_state(
            _EMPTY_POLICY, provider="opencode", adapter="opencode_cli", model=model
        )
        is expected
    )


def test_exact_override_still_wins_over_opencode_go_default() -> None:
    model = "opencode-go/gemini-4-argon-contributor"
    policy = {
        "providers": {},
        "adapters": {},
        "models": {"opencode": {"opencode_cli": {model: False}}},
    }
    assert (
        model_settings.evaluate_state(
            policy, provider="opencode", adapter="opencode_cli", model=model
        )
        is False
    )


@pytest.mark.parametrize("pad", [" ", " ", "\u0085", "　"])
def test_padded_identity_never_bypasses_an_exact_deny(pad: str) -> None:
    model = "opencode-go/gemini-4-argon-contributor"
    policy = {
        "providers": {},
        "adapters": {},
        "models": {"opencode": {"opencode_cli": {model: False}}},
    }
    for padded in (model + pad, pad + model):
        assert (
            model_settings.evaluate_state(
                policy, provider="opencode", adapter="opencode_cli", model=padded
            )
            is False
        )


def test_probe_record_is_per_thread_and_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _script_run(monkeypatch, [_ok(_ROWS), _timeout(), _timeout()])
    assert repo_policy._list_opencode_models(_EXE) == _ROWS
    other = threading.Thread(target=repo_policy._list_opencode_models, args=(_EXE,))
    other.start()
    other.join()
    assert repo_policy._opencode_probe_record(_EXE)["state"] == "failed"
    assert repo_policy._OPENCODE_PROBE_LOCAL.record == {"state": "ok", "count": 2}

    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: False)
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: runtime_adapters.ExecutableResolution(
            adapter_id, _EXE, True, ""
        ),
    )
    monkeypatch.setattr(repo_policy, "_list_opencode_models", lambda executable: list(_ROWS))
    policy = {
        "providers": {
            "allowed_adapters": list(
                repo_policy.DEFAULT_POLICY["providers"]["allowed_adapters"]
            )
        }
    }
    result = repo_policy._provider_status(
        Path("."), "opencode_cli", policy, "bubblewrap", "",
        model_policy=_EMPTY_POLICY,
    )
    assert "opencode_models_probe" not in result
    assert result["provider_observed_models"] == _ROWS


def test_empty_then_success_on_retry_uses_longer_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _script_run(
        monkeypatch,
        [SimpleNamespace(returncode=0, stdout=b"", stderr=b""), _ok(_ROWS)],
    )

    assert repo_policy._list_opencode_models(_EXE) == _ROWS
    assert [call["timeout"] for call in calls] == [5, 15]
    assert repo_policy._opencode_probe_record(_EXE) == {"state": "ok", "count": 2}


def test_double_empty_serves_last_known_good_with_named_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _script_run(
        monkeypatch,
        [
            _ok(_ROWS),
            SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
            SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
        ],
    )

    assert repo_policy._list_opencode_models(_EXE) == _ROWS
    assert repo_policy._list_opencode_models(_EXE) == _ROWS
    assert repo_policy._opencode_probe_record(_EXE) == {
        "state": "failed",
        "reason": "empty",
        "served_last_known_good": True,
    }
    assert [call["timeout"] for call in calls] == [5, 5, 15]


def test_exit_1_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _script_run(
        monkeypatch, [SimpleNamespace(returncode=1, stdout=b"", stderr=b"")]
    )

    assert repo_policy._list_opencode_models(_EXE) == []
    assert len(calls) == 1
    assert repo_policy._opencode_probe_record(_EXE) == {
        "state": "failed",
        "reason": "exit_1",
        "served_last_known_good": False,
    }
