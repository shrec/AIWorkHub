from __future__ import annotations

import io
import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from aiworkhub import codex_auth


def test_probes_forward_shared_background_launch_policy(monkeypatch):
    marker = {"start_new_session": True}
    calls = []
    monkeypatch.setattr(codex_auth, "background_process_launch_kwargs", lambda: marker)

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(codex_auth.subprocess, "run", run)
    assert codex_auth._login_ready("codex") is True
    assert calls[-1][1]["start_new_session"] is True

    def popen(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("stop after capture")

    monkeypatch.setattr(codex_auth.subprocess, "Popen", popen)
    with pytest.raises(codex_auth._ProbeError, match="codex_app_server_start_failed"):
        codex_auth._probe_app_server("codex")
    assert calls[-1][1]["start_new_session"] is True


def _line(request_id: int, result: dict) -> bytes:
    return json.dumps({"id": request_id, "result": result}).encode() + b"\n"


class _CaptureIO(io.BytesIO):
    def close(self) -> None:
        self.flush()


class _FakeProcess:
    def __init__(self, stdout: bytes) -> None:
        self.stdin = _CaptureIO()
        self.stdout = _CaptureIO(stdout)
        self.killed = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = -9 if self.returncode is None else self.returncode
        return self.returncode


def test_app_server_probe_is_read_only_bounded_and_follows_pagination(monkeypatch):
    output = b"".join(
        (
            _line(1, {"serverInfo": {"name": "codex"}}),
            _line(2, {"account": {"type": "chatgpt", "email": "private@example.test"},
                      "requiresOpenaiAuth": True}),
            _line(3, {"data": [{"id": "gpt-5.5"}], "nextCursor": "page-2"}),
            _line(4, {"data": [{"id": "gpt-5.3-codex"}], "nextCursor": None}),
        )
    )
    fake = _FakeProcess(output)
    launched = []

    def popen(argv, **kwargs):
        launched.append((argv, kwargs))
        return fake

    monkeypatch.setattr(codex_auth.subprocess, "Popen", popen)
    authenticated, models = codex_auth._probe_app_server("/safe/codex")

    assert authenticated is True
    assert models == ["gpt-5.5", "gpt-5.3-codex"]
    assert launched[0][0] == ["/safe/codex", "app-server", "--stdio"]
    assert launched[0][1]["shell"] is False
    requests = fake.stdin.getvalue().decode().splitlines()
    methods = [json.loads(line)["method"] for line in requests]
    assert methods == ["initialize", "initialized", "account/read", "model/list", "model/list"]
    assert not any(method.startswith(("thread/", "turn/")) for method in methods)
    account = json.loads(requests[2])
    assert account["params"] == {"refreshToken": False}
    final_page = json.loads(requests[-1])
    assert final_page["params"]["cursor"] == "page-2"
    assert fake.killed is True


@pytest.mark.parametrize(
    "payload, reason",
    [
        (b"not-json\n", "codex_app_server_malformed"),
        (b'{"id":1,"result":{}}', "codex_app_server_output_limit"),
    ],
)
def test_app_server_probe_rejects_malformed_or_partial_output(
    monkeypatch, payload, reason
):
    monkeypatch.setattr(
        codex_auth.subprocess, "Popen", lambda *_args, **_kwargs: _FakeProcess(payload)
    )
    with pytest.raises(codex_auth._ProbeError, match=reason):
        codex_auth._probe_app_server("/safe/codex")


def test_response_timeout_fails_closed():
    with pytest.raises(codex_auth._ProbeError, match="codex_app_server_timeout"):
        codex_auth._response(
            queue.Queue(), 1, time.monotonic() + 0.001, {"messages": 0, "bytes": 0}
        )


def test_response_rejects_unexpected_integer_id():
    responses = queue.Queue()
    responses.put(_line(2, {}))
    with pytest.raises(
        codex_auth._ProbeError,
        match="codex_app_server_unexpected_response_id",
    ):
        codex_auth._response(
            responses,
            1,
            time.monotonic() + 1.0,
            {"messages": 0, "bytes": 0},
        )


def test_app_server_rejects_oversized_model_id(monkeypatch):
    output = b"".join(
        (
            _line(1, {}),
            _line(2, {"account": {"type": "apiKey"}, "requiresOpenaiAuth": True}),
            _line(3, {"data": [{"id": "x" * 129}], "nextCursor": None}),
        )
    )
    monkeypatch.setattr(
        codex_auth.subprocess, "Popen", lambda *_args, **_kwargs: _FakeProcess(output)
    )
    with pytest.raises(codex_auth._ProbeError, match="codex_model_id_oversized"):
        codex_auth._probe_app_server("/safe/codex")


def test_app_server_rejects_unsafe_model_id(monkeypatch):
    output = b"".join(
        (
            _line(1, {}),
            _line(2, {"account": {"type": "apiKey"}, "requiresOpenaiAuth": True}),
            _line(3, {"data": [{"id": "gpt-5.5\nspoof"}], "nextCursor": None}),
        )
    )
    monkeypatch.setattr(
        codex_auth.subprocess, "Popen", lambda *_args, **_kwargs: _FakeProcess(output)
    )
    with pytest.raises(codex_auth._ProbeError, match="codex_model_id_invalid"):
        codex_auth._probe_app_server("/safe/codex")


def test_app_server_unauthenticated_account_returns_no_models(monkeypatch):
    output = b"".join(
        (
            _line(1, {}),
            _line(2, {"account": None, "requiresOpenaiAuth": True}),
        )
    )
    monkeypatch.setattr(
        codex_auth.subprocess, "Popen", lambda *_args, **_kwargs: _FakeProcess(output)
    )
    assert codex_auth._probe_app_server("/safe/codex") == (False, [])


def test_app_server_requires_complete_final_page(monkeypatch):
    output = b"".join(
        (
            _line(1, {}),
            _line(2, {"account": {"type": "apiKey"}, "requiresOpenaiAuth": True}),
            _line(3, {"data": [{"id": "gpt-5.5"}]}),
        )
    )
    monkeypatch.setattr(
        codex_auth.subprocess, "Popen", lambda *_args, **_kwargs: _FakeProcess(output)
    )
    with pytest.raises(codex_auth._ProbeError, match="codex_model_catalog_incomplete"):
        codex_auth._probe_app_server("/safe/codex")


def test_capability_status_cache_is_single_flight_and_secret_free(monkeypatch):
    codex_auth.invalidate()
    monkeypatch.setattr(codex_auth, "_resolved_executable", lambda _value: "/safe/codex")
    monkeypatch.setattr(codex_auth, "_executable_identity", lambda _path: "a" * 64)
    monkeypatch.setattr(codex_auth, "_login_ready", lambda _path: True)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def probe(_path):
        calls.append(1)
        entered.set()
        assert release.wait(timeout=2)
        return True, ["gpt-5.5"]

    monkeypatch.setattr(codex_auth, "_probe_app_server", probe)
    results = []
    first = threading.Thread(target=lambda: results.append(codex_auth.capability_status()))
    second = threading.Thread(target=lambda: results.append(codex_auth.capability_status()))
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert len(calls) == 1
    assert sorted(result["cache_hit"] for result in results) == [False, True]
    assert all(result["observed_models"] == ["gpt-5.5"] for result in results)
    assert all(
        result["cache_ttl_seconds"] == codex_auth.POSITIVE_CACHE_TTL_SECONDS
        for result in results
    )
    serialized = json.dumps(results, sort_keys=True)
    assert "/safe/codex" not in serialized
    assert "email" not in serialized.casefold()


def test_negative_capability_result_uses_short_cache_ttl(monkeypatch):
    codex_auth.invalidate()
    monkeypatch.setattr(codex_auth, "_resolved_executable", lambda _value: "/safe/codex")
    monkeypatch.setattr(codex_auth, "_executable_identity", lambda _path: "b" * 64)
    monkeypatch.setattr(codex_auth, "_login_ready", lambda _path: False)

    first = codex_auth.capability_status()
    second = codex_auth.capability_status()

    assert first["launchable"] is False
    assert first["cache_ttl_seconds"] == codex_auth.NEGATIVE_CACHE_TTL_SECONDS
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True


def test_unauthenticated_early_return_never_marks_catalog_complete(monkeypatch):
    codex_auth.invalidate()
    monkeypatch.setattr(codex_auth, "_resolved_executable", lambda _value: "/safe/codex")
    monkeypatch.setattr(codex_auth, "_executable_identity", lambda _path: "c" * 64)
    monkeypatch.setattr(codex_auth, "_login_ready", lambda _path: True)
    monkeypatch.setattr(codex_auth, "_probe_app_server", lambda _path: (False, []))

    result = codex_auth.capability_status()

    assert result["authenticated"] is False
    assert result["launchable"] is False
    assert result["model_catalog_complete"] is False


def test_login_probe_discards_all_provider_output_and_uses_fixed_argv(monkeypatch):
    captured = {}

    def run(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(codex_auth.subprocess, "run", run)
    assert codex_auth._login_ready("/safe/codex") is True
    assert captured["argv"] == ["/safe/codex", "login", "status"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["stdout"] is codex_auth.subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] is codex_auth.subprocess.DEVNULL
