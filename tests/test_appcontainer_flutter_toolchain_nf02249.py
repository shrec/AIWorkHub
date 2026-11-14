"""NF-2026-02249: a Flutter SDK outside the repo is granted to the AppContainer.

External report (AIWorkHub 0.12.97): ``flutter analyze`` inside the Windows
AppContainer validation sandbox exited 1 with ``PermissionError [WinError 5]``
from ``_winapi.CreateProcess`` -- the container SID could not open
``C:\\tools\\flutter\\bin\\flutter.bat`` nor the SDK's cached ``dart.exe``.
These tests build a fake SDK layout under ``tmp_path`` and drive the planner
and :func:`launch_appcontainer` through the existing mocked Win32 boundary; the
live part runs only where a real Flutter SDK is on PATH.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

import aiworkhub.toolchain_grants as tg
import aiworkhub.validation_spawn_diagnosis as spawn_diagnosis
import aiworkhub.windows_appcontainer as wac
from aiworkhub.windows_appcontainer import (
    AppContainerError,
    AppContainerRequest,
    ContainerGrant,
    launch_appcontainer,
)
from test_windows_appcontainer import FakeWin32Api

_PYTHON = "C:\\Python312\\python.exe"
# worker_workspace._APPCONTAINER_TEMP_TRAMPOLINE: the validation lane's shape.
_TRAMPOLINE = "import subprocess,sys;c=subprocess.call(sys.argv[1:]);sys.exit(c-(c>>31<<32))"
_PATHEXT = ".COM;.EXE;.BAT;.CMD"
# The AppContainer sandbox and its PATHEXT launcher resolution exist only on Windows.
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows AppContainer only")


def _flutter_sdk(parent):
    """A Flutter SDK as the installer lays it out: ``bin\\flutter(.bat)``,
    ``bin\\dart.bat`` and the cached ``bin\\cache\\dart-sdk``."""
    root = parent / "flutter"
    bin_dir = root / "bin"
    dart_bin = bin_dir / "cache" / "dart-sdk" / "bin"
    dart_bin.mkdir(parents=True)
    (bin_dir / "flutter").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (bin_dir / "flutter.bat").write_text("@ECHO off\r\n", encoding="utf-8")
    (bin_dir / "dart.bat").write_text("@ECHO off\r\n", encoding="utf-8")
    (dart_bin / "dart.exe").write_bytes(b"MZ")
    (dart_bin.parent / "version").write_text("3.5.0\n", encoding="utf-8")
    return root


def _real(path):
    return os.path.realpath(str(path))


def _environment(*directories):
    return {"PATH": os.pathsep.join(str(d) for d in directories), "PATHEXT": _PATHEXT}


def _grant_events(fake):
    return [event for event in fake.events if event.startswith(("grant:", "revoke:"))]


# -- root resolution ---------------------------------------------------------


def test_every_flutter_launcher_resolves_to_the_sdk_root(tmp_path):
    root = _flutter_sdk(tmp_path)
    dart_exe = root / "bin" / "cache" / "dart-sdk" / "bin" / "dart.exe"
    for launcher in (
        root / "bin" / "flutter.bat",
        root / "bin" / "flutter",
        root / "bin" / "dart.bat",
        dart_exe,
    ):
        found = tg.toolchain_root_for(launcher)
        assert found is not None, launcher
        assert (found.kind, found.root) == ("flutter_sdk", str(root))
    # The root covers the cached Dart SDK: one inheritable grant serves both.
    assert str(dart_exe).startswith(str(root) + os.sep)


def test_a_standalone_dart_sdk_and_unrelated_heads(tmp_path):
    sdk = tmp_path / "dart-sdk"
    (sdk / "bin").mkdir(parents=True)
    (sdk / "bin" / "dart.exe").write_bytes(b"MZ")
    (sdk / "version").write_text("3.5.0\n", encoding="utf-8")
    found = tg.toolchain_root_for(sdk / "bin" / "dart.exe")
    assert found is not None and (found.kind, found.root) == ("dart_sdk", str(sdk))

    stray = tmp_path / "stray" / "bin"
    stray.mkdir(parents=True)
    (stray / "flutter.exe").write_bytes(b"MZ")
    (stray / "ruff.exe").write_bytes(b"MZ")
    assert tg.toolchain_root_for(stray / "flutter.exe") is None
    assert tg.toolchain_root_for(stray / "ruff.exe") is None
    assert tg.toolchain_root_for(tmp_path / "flutter.bat") is None


def test_command_heads_see_through_cmd_and_the_validation_trampoline():
    assert tg.command_heads(["flutter", "analyze"]) == ["flutter"]
    cmd = "C:\\Windows\\System32\\cmd.exe"
    assert tg.command_heads([cmd, "/d", "/c", "flutter analyze"]) == [cmd, "flutter"]
    assert tg.command_heads([cmd, "/C", '"C:\\tools\\flutter\\bin\\flutter.bat" analyze']) == [
        cmd,
        "C:\\tools\\flutter\\bin\\flutter.bat",
    ]
    assert tg.command_heads([_PYTHON, "-c", _TRAMPOLINE, "flutter", "analyze"]) == [
        _PYTHON,
        "flutter",
    ]
    assert tg.command_heads([_PYTHON, "-m", "pytest"]) == [_PYTHON]
    assert tg.command_heads([]) == []


# -- grant planning ----------------------------------------------------------


@pytest.mark.parametrize("shape", ["bare", "trampoline", "cmd", "absolute", "dart"])
def test_the_sdk_root_is_planned_read_execute_persistent(tmp_path, shape):
    root = _flutter_sdk(tmp_path / "tools")
    bin_dir = root / "bin"
    argv = {
        "bare": ["flutter", "analyze"],
        "trampoline": [_PYTHON, "-c", _TRAMPOLINE, "flutter", "analyze"],
        "cmd": ["C:\\Windows\\System32\\cmd.exe", "/c", "flutter analyze"],
        "absolute": [str(bin_dir / "flutter"), "analyze"],
        "dart": [str(bin_dir / "cache" / "dart-sdk" / "bin" / "dart.exe"), "analyze"],
    }[shape]
    planned = tg.plan_toolchain_grants(argv, _environment(tmp_path / "elsewhere", bin_dir))
    assert [item.grant for item in planned] == [
        ContainerGrant(_real(root), "read_execute", persistent=True)
    ]
    assert planned[0].toolchain.kind == "flutter_sdk"


def test_nothing_is_resolved_for_a_non_toolchain_head(tmp_path, monkeypatch):
    def _never(*_args):
        raise AssertionError("only a flutter/dart head is ever resolved")

    monkeypatch.setattr(spawn_diagnosis, "_resolve", _never)
    env = _environment(tmp_path)
    assert tg.plan_toolchain_grants(["C:\\tools\\claude.exe", "--flag"], env) == []
    assert tg.plan_toolchain_grants([_PYTHON, "-m", "pytest"], env) == []


def test_covered_missing_and_system_tree_roots_plan_nothing(tmp_path, monkeypatch):
    root = _flutter_sdk(tmp_path / "worktree")
    env = _environment(root / "bin")
    assert tg.plan_toolchain_grants(
        ["flutter", "analyze"], env, covered=[_real(tmp_path / "worktree")]
    ) == []
    assert tg.plan_toolchain_grants(["flutter"], _environment(tmp_path / "nowhere")) == []

    program_files = tmp_path / "Program Files"
    installed = _flutter_sdk(program_files)
    monkeypatch.setattr(
        wac, "_sensitive_roots", lambda: ([], [os.path.normcase(_real(program_files))])
    )
    assert tg.plan_toolchain_grants(["flutter"], _environment(installed / "bin")) == []


# -- launch ------------------------------------------------------------------


def _flutter_request(root, **overrides):
    params = dict(
        argv=[_PYTHON, "-c", _TRAMPOLINE, "flutter", "analyze"],
        repo_id="repo_57de971f",
        worker_kind="claude_cli",
        stdout_handle=101,
        stderr_handle=102,
        environment=_environment(root / "bin"),
    )
    params.update(overrides)
    return AppContainerRequest(**params)


def test_a_flutter_validation_launch_grants_the_sdk_root(tmp_path):
    """Fails on base: no grant reached the SDK, so CreateProcess got WinError 5."""
    root = _flutter_sdk(tmp_path / "tools")
    (tmp_path / "home").mkdir()
    other = _real(tmp_path / "home")
    fake = FakeWin32Api()
    launch_appcontainer(
        _flutter_request(root, filesystem_grants=[ContainerGrant(str(other), "modify")]),
        api=fake,
    )
    assert _grant_events(fake) == [
        f"grant:modify:{other}",
        f"grant:read_execute:{_real(root)}",
    ]
    assert fake.resumed


def test_a_non_toolchain_launch_is_unchanged(tmp_path):
    fake = FakeWin32Api()
    launch_appcontainer(
        _flutter_request(_flutter_sdk(tmp_path), argv=["C:\\tools\\claude.exe", "--flag"]),
        api=fake,
    )
    assert _grant_events(fake) == []


def test_a_grant_that_cannot_be_applied_names_the_root_and_cause(tmp_path):
    root = _flutter_sdk(tmp_path / "tools")
    fake = FakeWin32Api()
    fake.fail_grant_attempt = 1
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(_flutter_request(root), api=fake)
    error = excinfo.value
    message = str(error)
    assert message.startswith(f"{error.reason.value}: toolchain_grant_failed: flutter_sdk")
    assert repr(_real(root)) in message
    assert "forced-grant" in message
    assert (error.operation, error.win_error) == ("grant_path_access", 5)
    assert fake.creation is None and fake.identity_freed


def test_a_root_grant_validation_refuses_naming_the_root(tmp_path, monkeypatch):
    root = _flutter_sdk(tmp_path / "tools")
    protected = root / "profile"
    protected.mkdir()
    monkeypatch.setattr(wac, "_sensitive_roots", lambda: ([os.path.normcase(_real(protected))], []))
    fake = FakeWin32Api()
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(_flutter_request(root), api=fake)
    message = str(excinfo.value)
    assert "toolchain_grant_failed: flutter_sdk toolchain root" in message
    assert repr(_real(root)) in message and "protected tree" in message
    assert fake.identity is None and _grant_events(fake) == []


def test_command_heads_cmd_outer_quotes_and_trampoline_over_cmd():
    tool = "C:\\tools\\flutter\\bin\\flutter.bat"
    heads = tg.command_heads(["cmd.exe", "/s", "/c", f'""{tool}" analyze"'])
    assert tool in heads
    heads = tg.command_heads(
        [_PYTHON, "-c", _TRAMPOLINE, "cmd.exe", "/c", "flutter analyze"]
    )
    assert heads == [_PYTHON, "cmd.exe", "flutter"]


# -- live --------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("flutter") is None,
    reason="needs a real Flutter SDK on PATH (Windows)",
)
def test_live_flutter_on_path_plans_its_sdk_root():
    launcher = os.path.realpath(shutil.which("flutter") or "")
    planned = tg.plan_toolchain_grants(["flutter", "analyze"], dict(os.environ))
    roots = {os.path.normcase(item.grant.path) for item in planned}
    expected = os.path.normcase(os.path.dirname(os.path.dirname(launcher)))
    system = wac._sensitive_roots()[1]
    if any(wac._within(expected, tree) for tree in system):
        assert roots == set()
    else:
        assert roots == {expected}
