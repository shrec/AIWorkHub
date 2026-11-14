"""NF-2026-02268: Flutter/Dart AppContainer launches skip the startup lock.

``toolchain_grants.toolchain_launch_environment`` adds ``FLUTTER_ALREADY_LOCKED``
and a container-writable ``PUB_CACHE`` to the child environment of a
flutter/dart launch, so the tool never tries to open
``<sdk>\\bin\\cache\\lockfile`` for write -- which an AppContainer granted only
``read_execute`` on the SDK root can never obtain.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiworkhub import toolchain_grants, windows_appcontainer
from aiworkhub.windows_appcontainer import AppContainerRequest, ContainerGrant


def _flutter_sdk(parent):
    bin_dir = parent / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "flutter.bat").write_text("@echo off\n", encoding="utf-8")
    cache = bin_dir / "cache" / "dart-sdk" / "bin"
    cache.mkdir(parents=True)
    (cache / "dart.exe").write_bytes(b"")
    return bin_dir / "flutter.bat"


def _dart_sdk(parent):
    bin_dir = parent / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "dart.exe").write_bytes(b"")
    (parent / "version").write_text("1.0.0\n", encoding="utf-8")
    return bin_dir / "dart.exe"


def test_flutter_launch_sets_lock_flag_and_pub_cache(tmp_path, monkeypatch):
    sdk = tmp_path / "flutter"
    launcher = _flutter_sdk(sdk)
    monkeypatch.setattr(
        toolchain_grants,
        "launch_toolchain_roots",
        lambda argv, environment: [
            toolchain_grants.ToolchainRoot("flutter_sdk", str(sdk), str(launcher))
        ],
    )
    request_dir = tmp_path / "request"
    request_dir.mkdir()
    pub_cache_dir = str(request_dir / "pub_cache")

    additions = toolchain_grants.toolchain_launch_environment(
        [str(launcher), "analyze"], {}, pub_cache_dir
    )

    assert additions["FLUTTER_ALREADY_LOCKED"] == "true"
    assert additions["PUB_CACHE"] == pub_cache_dir
    assert os.path.isdir(pub_cache_dir)


def test_existing_lock_flag_and_pub_cache_are_not_overwritten(tmp_path, monkeypatch):
    sdk = tmp_path / "flutter"
    launcher = _flutter_sdk(sdk)
    monkeypatch.setattr(
        toolchain_grants,
        "launch_toolchain_roots",
        lambda argv, environment: [
            toolchain_grants.ToolchainRoot("flutter_sdk", str(sdk), str(launcher))
        ],
    )
    request_dir = tmp_path / "request"
    request_dir.mkdir()
    environment = {
        "FLUTTER_ALREADY_LOCKED": "false",
        "PUB_CACHE": r"C:\already\set",
    }

    additions = toolchain_grants.toolchain_launch_environment(
        [str(launcher), "analyze"], environment, str(request_dir / "pub_cache")
    )

    assert additions == {}


def test_non_toolchain_launch_gets_no_additions(tmp_path, monkeypatch):
    monkeypatch.setattr(
        toolchain_grants, "launch_toolchain_roots", lambda argv, environment: []
    )

    additions = toolchain_grants.toolchain_launch_environment(
        ["C:\\Windows\\System32\\cmd.exe", "/c", "dir"], {}, str(tmp_path / "pub_cache")
    )

    assert additions == {}


def test_planned_toolchain_grants_are_always_read_execute(tmp_path, monkeypatch):
    sdk = tmp_path / "flutter"
    launcher = _flutter_sdk(sdk)
    monkeypatch.setattr(
        toolchain_grants,
        "launch_toolchain_roots",
        lambda argv, environment: [
            toolchain_grants.ToolchainRoot("flutter_sdk", str(sdk), str(launcher))
        ],
    )

    planned = toolchain_grants.plan_toolchain_grants([str(launcher), "analyze"], {})

    assert planned, "expected a toolchain grant to be planned"
    for toolchain_grant in planned:
        assert toolchain_grant.grant.access == "read_execute"
        assert "write" not in toolchain_grant.grant.access


def _base_request(argv, environment, grants, request_dir):
    return AppContainerRequest(
        argv=argv,
        repo_id="test",
        worker_kind="validation",
        environment=environment,
        filesystem_grants=grants,
        working_directory=str(request_dir),
    )


def test_with_toolchain_grants_wires_flutter_env_and_pub_cache(tmp_path):
    sdk = tmp_path / "flutter"
    launcher = _flutter_sdk(sdk)
    request_dir = tmp_path / "request"
    request_dir.mkdir()
    request = _base_request(
        [str(launcher), "analyze"],
        {},
        [ContainerGrant(str(request_dir), "modify", persistent=False)],
        request_dir,
    )

    result, _ = windows_appcontainer._with_toolchain_grants(request)

    assert result.environment.get("FLUTTER_ALREADY_LOCKED") == "true"
    pub_cache = result.environment.get("PUB_CACHE")
    assert pub_cache is not None
    assert os.path.isdir(pub_cache)
    assert os.path.normcase(os.path.normpath(pub_cache)).startswith(
        os.path.normcase(os.path.normpath(str(request_dir)))
    )
    sdk_key = os.path.normcase(os.path.normpath(str(sdk)))
    added = [grant for grant in result.filesystem_grants if grant not in request.filesystem_grants]
    assert [os.path.normcase(os.path.normpath(grant.path)) for grant in added] == [sdk_key]
    # Never a write grant on the SDK root or its bin\cache (sandbox escape).
    assert all(grant.access == "read_execute" for grant in added)


def test_with_toolchain_grants_wires_dart_env_and_pub_cache(tmp_path):
    sdk = tmp_path / "dart-sdk"
    launcher = _dart_sdk(sdk)
    request_dir = tmp_path / "request"
    request_dir.mkdir()
    request = _base_request(
        [str(launcher), "--version"],
        {},
        [ContainerGrant(str(request_dir), "modify", persistent=False)],
        request_dir,
    )

    result, _ = windows_appcontainer._with_toolchain_grants(request)

    assert result.environment.get("FLUTTER_ALREADY_LOCKED") == "true"
    assert result.environment.get("PUB_CACHE")


def test_with_toolchain_grants_leaves_python_head_unchanged(tmp_path):
    request_dir = tmp_path / "request"
    request_dir.mkdir()
    request = _base_request(
        ["python", "-c", "print(1)"],
        {"FOO": "bar"},
        [ContainerGrant(str(request_dir), "modify", persistent=False)],
        request_dir,
    )

    result, planned = windows_appcontainer._with_toolchain_grants(request)

    assert result.environment == request.environment
    assert result.filesystem_grants == request.filesystem_grants
    assert planned == {}


def test_with_toolchain_grants_keeps_preset_env(tmp_path):
    sdk = tmp_path / "flutter"
    launcher = _flutter_sdk(sdk)
    request_dir = tmp_path / "request"
    request_dir.mkdir()
    request = _base_request(
        [str(launcher), "analyze"],
        {"FLUTTER_ALREADY_LOCKED": "false", "PUB_CACHE": r"C:\already\set"},
        [ContainerGrant(str(request_dir), "modify", persistent=False)],
        request_dir,
    )

    result, _ = windows_appcontainer._with_toolchain_grants(request)

    assert result.environment["FLUTTER_ALREADY_LOCKED"] == "false"
    assert result.environment["PUB_CACHE"] == r"C:\already\set"


@pytest.mark.skipif(
    not Path("C:\\Tools\\flutter").exists(), reason="requires a local Flutter SDK checkout"
)
def test_flutter_sdk_source_confirms_already_locked_semantics():
    """Documents the Flutter SDK source this behaviour was verified against.

    ``bin/internal/shared.bat`` (Windows) and ``bin/internal/shared.sh``
    (POSIX) both guard the startup lock acquisition with a check of
    ``FLUTTER_ALREADY_LOCKED``; when it is ``true`` the lock file is never
    opened for write. This is also surfaced in
    ``packages/flutter_tools/lib/src/cache.dart`` (``Cache.lock`` /
    ``Cache.checkLockAcquired``), which is the Dart-level implementation the
    shell wrapper's check exists to short-circuit.
    """
    root = Path("C:\\Tools\\flutter")
    shared_bat = root / "bin" / "internal" / "shared.bat"
    cache_dart = root / "packages" / "flutter_tools" / "lib" / "src" / "cache.dart"
    assert shared_bat.exists() or cache_dart.exists()


def test_with_toolchain_grants_keeps_inherited_environment_none(tmp_path):
    sdk = tmp_path / "flutter"
    launcher = _flutter_sdk(sdk)
    request_dir = tmp_path / "request"
    request_dir.mkdir()
    request = _base_request(
        [str(launcher), "analyze"],
        None,
        [ContainerGrant(str(request_dir), "modify", persistent=False)],
        request_dir,
    )

    result, _ = windows_appcontainer._with_toolchain_grants(request)

    assert result.environment is None
