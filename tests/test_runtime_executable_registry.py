"""Production-plan tests for repo-owned executable registration.

These tests exercise the REAL worker and manager command builders
(``build_adapter_command`` and ``build_manager_command``) end to end:
registration selection happens inside ``build_runtime_command`` before
``resolve_executable``, and every selected registration is SHA/provenance
bound.  No test here claims native Muse or MCP qualification: the manifest
``fixture_kind`` is ``host_tested_only`` per the task contract.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from aiworkhub import runtime_adapters, runtime_executable_registry
from aiworkhub.runtime_adapters import OPENCODE_CLI_ADAPTER

ARTIFACT_VERSION = "2.0.16+aiworkhub.winfix.1"
SOURCE_TAG = "v2.0.16"
SOURCE_COMMIT = "3a103fe0aff726a4edc7492f03f7b88195d9e4c9"
MODEL = "anthropic/claude-sonnet-4-5"
PROMPT = "summarize the repository"


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _registered_executable(repo: Path) -> Path:
    """An executable regular file inside the repo (host-tested fixture only)."""

    target = repo / "artifacts" / "opencode-2.0.16-winfix.1" / "bin" / "opencode"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        # copy2 preserves the interpreter's executable mode bits without
        # chmod, which this sandbox forbids; the bytes are fully copied so
        # the registered SHA256 can be computed over the actual file.
        shutil.copy2(sys.executable, target)
    return target


def _manifest_path(repo: Path) -> Path:
    return (
        repo
        / runtime_executable_registry.REGISTRY_DIR_NAME
        / "runtime"
        / runtime_executable_registry.REGISTRY_FILENAME
    )


def _write_registration(repo: Path, **overrides: object) -> Path:
    executable = _registered_executable(repo)
    entry: dict[str, object] = {
        "platform": "linux",
        "artifact_version": ARTIFACT_VERSION,
        "source_tag": SOURCE_TAG,
        "source_commit": SOURCE_COMMIT,
        "executable_relative": "artifacts/opencode-2.0.16-winfix.1/bin/opencode",
        "sha256": _sha256(executable),
        "fixture_kind": "host_tested_only",
    }
    entry.update(overrides)
    manifest_path = _manifest_path(repo)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema": runtime_executable_registry.MANIFEST_SCHEMA_ID,
                "registrations": {"opencode_cli": entry},
            }
        ),
        encoding="utf-8",
    )
    return executable


def _override_executable(repo: Path) -> Path:
    target = repo / "override" / "opencode-override"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copy2(sys.executable, target)
    return target


@pytest.fixture
def linux_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Platform selection stays behind the shared PlatformIO helpers."""

    monkeypatch.setattr(
        runtime_executable_registry.platform_io, "is_windows", lambda: False
    )
    monkeypatch.setattr(
        runtime_executable_registry.platform_io, "is_linux", lambda: True
    )
    monkeypatch.setattr(
        runtime_executable_registry.platform_io, "is_macos", lambda: False
    )


def _host_not_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)


def test_manager_command_uses_registered_executable(
    tmp_path: Path, linux_platform: None
) -> None:
    registered = _write_registration(tmp_path)

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER, PROMPT, tmp_path, model=MODEL
    )

    assert plan.launchable
    assert plan.executable == str(registered)
    assert plan.argv[0] == str(registered)


def test_worker_adapter_command_uses_registered_executable(
    tmp_path: Path, linux_platform: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = _write_registration(tmp_path)
    _host_not_windows(monkeypatch)

    plan = runtime_adapters.build_adapter_command(
        OPENCODE_CLI_ADAPTER,
        PROMPT,
        tmp_path,
        model=MODEL,
        outer_sandbox_backend="landlock",
    )

    assert plan.launchable
    assert plan.executable == str(registered)
    assert plan.argv[0] == str(registered)


def test_isolated_launch_selects_the_canonical_repo_registration(
    tmp_path: Path, linux_platform: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # NF-2026-01250: the isolated launch builds its plan with repo=<worktree>,
    # which has no .aiworkhub/runtime; the launcher must still pick the
    # registration the canonical repo owns, never PATH.
    from aiworkhub import process_launcher

    registered = _write_registration(tmp_path)
    worktree = tmp_path / ".aiworkhub" / "runtime" / "worktrees" / "r1" / "worktree"
    worktree.mkdir(parents=True)
    _host_not_windows(monkeypatch)

    plan = process_launcher.ProcessManager(repo=tmp_path)._build_adapter(
        adapter_id=OPENCODE_CLI_ADAPTER,
        prompt=PROMPT,
        repo=worktree,
        model=MODEL,
        outer_sandbox_backend="landlock",
    )

    assert plan.launchable
    assert plan.argv[0] == str(registered)

    _manifest_path(tmp_path).write_text("{not json", encoding="utf-8")
    with pytest.raises(process_launcher.LaunchRejected):
        process_launcher.ProcessManager(repo=tmp_path)._build_adapter(
            adapter_id=OPENCODE_CLI_ADAPTER,
            prompt=PROMPT,
            repo=worktree,
            model=MODEL,
            outer_sandbox_backend="landlock",
        )

def test_explicit_override_priority_beats_registration(
    tmp_path: Path, linux_platform: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = _write_registration(tmp_path)
    override = _override_executable(tmp_path)
    _host_not_windows(monkeypatch)

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER,
        PROMPT,
        tmp_path,
        model=MODEL,
        executable_overrides={"opencode_cli": str(override)},
    )

    assert plan.launchable
    assert plan.argv[0] == str(override)
    assert str(registered) not in plan.argv


def test_explicit_sibling_override_stays_untouched_while_registration_applies(
    tmp_path: Path, linux_platform: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = _write_registration(tmp_path)
    sibling_override = _override_executable(tmp_path)
    _host_not_windows(monkeypatch)

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER,
        PROMPT,
        tmp_path,
        model=MODEL,
        executable_overrides={"codex_cli": str(sibling_override)},
    )

    # The request adapter uses its qualified registration, never the sibling
    # override, and the unrequested sibling override is simply left alone.
    assert plan.launchable
    assert plan.argv[0] == str(registered)
    assert str(sibling_override) not in plan.argv


def test_unregistered_default_route_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_binary = _registered_executable(tmp_path)
    _host_not_windows(monkeypatch)
    monkeypatch.setattr(
        runtime_adapters.shutil, "which", lambda _binary: str(default_binary)
    )

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER, PROMPT, tmp_path, model=MODEL
    )

    assert plan.launchable
    assert plan.executable == str(default_binary)
    assert plan.argv[0] == str(default_binary)


def test_registration_for_sibling_adapter_is_not_applied(
    tmp_path: Path, linux_platform: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registration(tmp_path)
    manifest_path = _manifest_path(tmp_path)
    sibling_manifest = {
        "schema": runtime_executable_registry.MANIFEST_SCHEMA_ID,
        "registrations": {"claude_cli": {"platform": "linux"}},
    }
    manifest_path.write_text(json.dumps(sibling_manifest), encoding="utf-8")
    default_binary = _registered_executable(tmp_path)
    monkeypatch.setattr(
        runtime_adapters.shutil, "which", lambda _binary: str(default_binary)
    )
    _host_not_windows(monkeypatch)

    selected, error = runtime_executable_registry.select_executable_overrides(
        OPENCODE_CLI_ADAPTER, tmp_path, None
    )
    assert selected is None
    assert error is None

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER, PROMPT, tmp_path, model=MODEL
    )
    assert plan.launchable
    assert plan.argv[0] == str(default_binary)


def test_selected_registration_sha_tampering_fails_closed_worker_plan(
    tmp_path: Path, linux_platform: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registration(
        tmp_path, sha256=hashlib.sha256(b"tampered bytes").hexdigest()
    )
    _host_not_windows(monkeypatch)

    plan = runtime_adapters.build_adapter_command(
        OPENCODE_CLI_ADAPTER, PROMPT, tmp_path, model=MODEL
    )

    assert not plan.launchable
    assert plan.argv == []
    assert plan.validation_reason == "registration_sha256_mismatch"


def test_selected_registration_malformed_fails_closed_manager_plan(
    tmp_path: Path, linux_platform: None
) -> None:
    _write_registration(tmp_path)
    manifest_path = _manifest_path(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["registrations"]["opencode_cli"]["sha256"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER, PROMPT, tmp_path, model=MODEL
    )

    assert not plan.launchable
    assert plan.argv == []
    assert plan.validation_reason == "registration_sha256_missing"


def test_selected_registration_platform_mismatch_fails_closed(
    tmp_path: Path, linux_platform: None
) -> None:
    _write_registration(tmp_path, platform="win32")

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER, PROMPT, tmp_path, model=MODEL
    )

    assert not plan.launchable
    assert plan.argv == []
    assert plan.validation_reason == "registration_platform_mismatch"


def test_selected_registration_unsafe_executable_relative_fails_closed(
    tmp_path: Path, linux_platform: None
) -> None:
    _write_registration(tmp_path, executable_relative="../escape/opencode")

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER, PROMPT, tmp_path, model=MODEL
    )

    assert not plan.launchable
    assert plan.argv == []
    assert plan.validation_reason == "registration_executable_unsafe"


def test_selected_registration_missing_executable_fails_closed(
    tmp_path: Path, linux_platform: None
) -> None:
    _write_registration(tmp_path, executable_relative="artifacts/missing/opencode")

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER, PROMPT, tmp_path, model=MODEL
    )

    assert not plan.launchable
    assert plan.argv == []
    assert plan.validation_reason == "registration_executable_missing"


def test_selected_registration_unexpected_fields_fail_closed(
    tmp_path: Path, linux_platform: None
) -> None:
    _write_registration(tmp_path, unqualified={"unexpected": True})

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER, PROMPT, tmp_path, model=MODEL
    )

    assert not plan.launchable
    assert plan.validation_reason == "registry_strictness:unexpected_fields"


def test_selected_registration_missing_provenance_fails_closed(
    tmp_path: Path, linux_platform: None
) -> None:
    _write_registration(tmp_path)
    manifest_path = _manifest_path(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["registrations"]["opencode_cli"]["source_commit"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER, PROMPT, tmp_path, model=MODEL
    )

    assert not plan.launchable
    assert plan.validation_reason == "registration_source_commit_missing"


def test_selected_registration_fixture_kind_invalid_fails_closed(
    tmp_path: Path, linux_platform: None
) -> None:
    _write_registration(tmp_path, fixture_kind="fabricated")

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER, PROMPT, tmp_path, model=MODEL
    )

    assert not plan.launchable
    assert plan.validation_reason == "registration_fixture_kind_invalid"


def test_manifest_json_malformed_fails_closed(
    tmp_path: Path, linux_platform: None
) -> None:
    _write_registration(tmp_path)
    _manifest_path(tmp_path).write_text("{not json", encoding="utf-8")

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER, PROMPT, tmp_path, model=MODEL
    )

    assert not plan.launchable
    assert plan.validation_reason == "manifest_json_malformed"


def test_manifest_present_but_directory_fails_closed(
    tmp_path: Path, linux_platform: None
) -> None:
    _manifest_path(tmp_path).mkdir(parents=True, exist_ok=False)

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER, PROMPT, tmp_path, model=MODEL
    )

    assert not plan.launchable
    assert plan.validation_reason == "manifest_present_but_not_a_file"


def test_actual_caller_repo_str_and_path_both_launch_registered(
    tmp_path: Path, linux_platform: None
) -> None:
    _write_registration(tmp_path)
    for repo in (str(tmp_path), tmp_path):
        plan = runtime_adapters.build_manager_command(
            OPENCODE_CLI_ADAPTER, PROMPT, repo, model=MODEL
        )
        assert plan.launchable
        assert Path(plan.argv[0]) == _registered_executable(tmp_path)

    _manifest_path(tmp_path).write_text("{not json", encoding="utf-8")
    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER, PROMPT, str(tmp_path), model=MODEL
    )
    assert not plan.launchable
    assert plan.validation_reason

def test_manifest_strict_top_level_shape_fails_closed(
    tmp_path: Path, linux_platform: None
) -> None:
    _write_registration(tmp_path)
    manifest_path = _manifest_path(tmp_path)
    manifest = {"schema": runtime_executable_registry.MANIFEST_SCHEMA_ID}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER, PROMPT, tmp_path, model=MODEL
    )

    assert not plan.launchable
    assert plan.validation_reason == "manifest_schema_strict_violation"


def test_explicit_override_priority_survives_tampered_registration(
    tmp_path: Path, linux_platform: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registration(
        tmp_path, sha256=hashlib.sha256(b"tampered bytes").hexdigest()
    )
    override = _override_executable(tmp_path)
    _host_not_windows(monkeypatch)

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER,
        PROMPT,
        tmp_path,
        model=MODEL,
        executable_overrides={"opencode_cli": str(override)},
    )

    # The explicit override is consumed before any registration validation,
    # so a tampered repo registration never shadows or poisons it.
    assert plan.launchable
    assert plan.argv[0] == str(override)


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlink creation requires privilege on Windows"
)
def test_registered_executable_symlink_escape_fails_closed(
    tmp_path: Path, linux_platform: None
) -> None:
    outside = tmp_path.parent / f"outside_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    (outside / "opencode").write_bytes(b"escape")
    link_dir = tmp_path / "artifacts" / "opencode-2.0.16-winfix.1" / "bin"
    link_dir.mkdir(parents=True, exist_ok=True)
    (link_dir / "opencode").symlink_to(outside / "opencode")
    _write_registration(
        tmp_path,
        executable_relative="artifacts/opencode-2.0.16-winfix.1/bin/opencode",
    )

    plan = runtime_adapters.build_manager_command(
        OPENCODE_CLI_ADAPTER, PROMPT, tmp_path, model=MODEL
    )

    assert not plan.launchable
    assert plan.validation_reason == "registration_executable_escapes_repo"


def test_select_helper_no_manifest_returns_unregistered_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime_executable_registry.platform_io, "is_linux", lambda: True
    )

    selected, error = runtime_executable_registry.select_executable_overrides(
        OPENCODE_CLI_ADAPTER, tmp_path, None
    )

    assert selected is None
    assert error is None


def test_select_helper_explicit_override_wins_before_manifest_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = _override_executable(tmp_path)

    selected, error = runtime_executable_registry.select_executable_overrides(
        OPENCODE_CLI_ADAPTER,
        tmp_path,
        {"opencode_cli": str(override)},
    )

    assert error is None
    assert selected == {"opencode_cli": str(override)}
