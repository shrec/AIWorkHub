"""NF-2026-01094: the supervisor spawn breaks away from the MCP server's job."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from aiworkhub import process_launcher_launch_isolated as launch_mod

BREAKAWAY = 0x01000000
NEW_GROUP = 0x00000200


class _Stdin:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _Proc:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.pid = 4242
        self.stdin = _Stdin()


class _FakePopen:
    def __init__(self, outcomes: list[BaseException | None]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.procs: list[_Proc] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> _Proc:
        self.calls.append((argv, kwargs))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        proc = _Proc(f"call{len(self.calls)}")
        self.procs.append(proc)
        return proc


def _denied(winerror: int | None) -> PermissionError:
    exc = PermissionError(13, "denied")
    if winerror is not None:
        exc.winerror = winerror  # type: ignore[attr-defined]
    return exc


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launch_mod, "_is_windows_host", lambda: True)


@pytest.fixture
def posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launch_mod, "_is_windows_host", lambda: False)


def test_windows_ors_breakaway_into_existing_creationflags(windows: None) -> None:
    popen = _FakePopen([None])
    proc = launch_mod.spawn_with_stdin_payload(
        popen, ["sup"], stdin_payload=None, creationflags=NEW_GROUP, cwd="x"
    )
    assert len(popen.calls) == 1
    kwargs = popen.calls[0][1]
    assert kwargs["creationflags"] & NEW_GROUP == NEW_GROUP
    assert kwargs["creationflags"] & BREAKAWAY == BREAKAWAY
    assert kwargs["cwd"] == "x"
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert proc is popen.procs[0]


def test_windows_without_existing_creationflags_sets_only_breakaway(
    windows: None,
) -> None:
    popen = _FakePopen([None])
    launch_mod.spawn_with_stdin_payload(popen, ["sup"], stdin_payload=None)
    assert popen.calls[0][1]["creationflags"] == BREAKAWAY


def test_winerror_5_retries_once_without_the_flag(windows: None) -> None:
    popen = _FakePopen([_denied(5), None])
    proc = launch_mod.spawn_with_stdin_payload(
        popen, ["sup"], stdin_payload=b"prompt", creationflags=NEW_GROUP
    )
    assert len(popen.calls) == 2
    assert popen.calls[0][1]["creationflags"] == NEW_GROUP | BREAKAWAY
    assert popen.calls[1][1]["creationflags"] == NEW_GROUP
    assert proc is popen.procs[0]
    assert proc.tag == "call2"
    assert proc.stdin.written == [b"prompt"]
    assert proc.stdin.closed


def test_winerror_5_retry_without_creationflags_omits_the_kwarg(
    windows: None,
) -> None:
    popen = _FakePopen([_denied(5), None])
    launch_mod.spawn_with_stdin_payload(popen, ["sup"], stdin_payload=None)
    assert "creationflags" not in popen.calls[1][1]


@pytest.mark.parametrize(
    "error",
    [_denied(None), _denied(6), OSError(22, "invalid"), ValueError("boom")],
)
def test_other_errors_propagate_after_one_call(
    windows: None, error: BaseException
) -> None:
    popen = _FakePopen([error])
    with pytest.raises(type(error)) as info:
        launch_mod.spawn_with_stdin_payload(popen, ["sup"], stdin_payload=b"p")
    assert info.value is error
    assert len(popen.calls) == 1


def test_retry_failure_propagates(windows: None) -> None:
    second = _denied(5)
    popen = _FakePopen([_denied(5), second])
    with pytest.raises(PermissionError) as info:
        launch_mod.spawn_with_stdin_payload(popen, ["sup"], stdin_payload=None)
    assert info.value is second
    assert len(popen.calls) == 2


def test_non_windows_kwargs_unchanged(posix: None) -> None:
    popen = _FakePopen([None])
    proc = launch_mod.spawn_with_stdin_payload(
        popen, ["sup", "a"], stdin_payload=b"hello", start_new_session=True
    )
    assert len(popen.calls) == 1
    argv, kwargs = popen.calls[0]
    assert argv == ["sup", "a"]
    assert kwargs == {"stdin": subprocess.PIPE, "start_new_session": True}
    assert proc.stdin.written == [b"hello"]


def test_non_windows_permission_error_is_not_retried(posix: None) -> None:
    popen = _FakePopen([_denied(5)])
    with pytest.raises(PermissionError):
        launch_mod.spawn_with_stdin_payload(popen, ["sup"], stdin_payload=None)
    assert len(popen.calls) == 1
