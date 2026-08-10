from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from aiworkhub import quality_evidence


def test_completion_quality_gate_passes_valid_changed_sources(tmp_path) -> None:
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")
    packet = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["good.py"]
    )
    assert packet["passed"] is True
    assert packet["blocking_checks"] == []
    assert {row["status"] for row in packet["checks"]} == {"passed"}
    assert all(row["status"] in {"skipped", "not_available"} for row in packet["optional_gates"])
    assert packet["repository_quality_policy"]["status"] == "unverified"
    assert packet["verification_scope"] == "builtin_and_task_contract_only"


def test_completion_quality_gate_blocks_invalid_python(tmp_path) -> None:
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    packet = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["broken.py"]
    )
    assert packet["passed"] is False
    assert packet["blocking_checks"] == ["builtin:syntax:broken.py"]


def test_completion_quality_gate_blocks_unavailable_declared_check(tmp_path) -> None:
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "checks": [{
            "id": "codeql-security",
            "kind": "security",
            "command": ["definitely-not-installed-aiworkhub-checker"],
        }]
    }), encoding="utf-8")
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")
    packet = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["good.py"]
    )
    assert packet["passed"] is False
    assert packet["blocking_checks"] == ["codeql-security"]
    declared = next(row for row in packet["checks"] if row["check_id"] == "codeql-security")
    assert declared["status"] == "not_available"
    assert declared["kind"] == "security"
    assert declared["error"] == "command_not_found"


def test_windows_declared_npm_uses_native_node_argv_without_batch_wrapper(
    tmp_path, monkeypatch
) -> None:
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    declared_command = ["npm", "--prefix", "vscode-extension", "test"]
    config.write_text(
        json.dumps({
            "checks": [{
                "id": "extension-regression",
                "kind": "test",
                "command": declared_command,
            }]
        }),
        encoding="utf-8",
    )
    install_root = tmp_path / "trusted-nodejs"
    npm_cmd = install_root / "npm.cmd"
    node = install_root / "node.exe"
    npm_cli = install_root / "node_modules" / "npm" / "bin" / "npm-cli.js"
    npm_cli.parent.mkdir(parents=True)
    npm_cmd.write_text("@echo off\n", encoding="utf-8")
    node.write_bytes(b"native-node-placeholder")
    npm_cli.write_text("require('../lib/cli.js')\n", encoding="utf-8")
    observed: dict[str, object] = {}

    monkeypatch.setattr(quality_evidence, "os", SimpleNamespace(name="nt"))

    def fake_which(executable: str) -> str | None:
        observed["which"] = executable
        return str(npm_cmd)

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="passed", stderr="")

    monkeypatch.setattr(quality_evidence, "_which", fake_which)
    monkeypatch.setattr(quality_evidence.subprocess, "run", fake_run)

    checks = quality_evidence.run_declared_checks(tmp_path)

    assert checks[0].status == "passed"
    assert checks[0].command == tuple(declared_command)
    assert observed["which"] == "npm"
    assert observed["argv"] == [
        str(node.resolve()),
        str(npm_cli.resolve()),
        "--prefix",
        "vscode-extension",
        "test",
    ]
    assert observed["kwargs"]["shell"] is False


def test_windows_unknown_batch_wrapper_fails_before_process_creation(
    tmp_path, monkeypatch
) -> None:
    wrapper = tmp_path / "custom.cmd"
    wrapper.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(quality_evidence, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(quality_evidence, "_which", lambda _name: str(wrapper))

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("batch wrapper reached subprocess.run")

    monkeypatch.setattr(quality_evidence.subprocess, "run", unexpected_run)

    status, _stdout, error, _duration = quality_evidence._run_command_array(
        ("custom", "&echo", "AIWORKHUB_CMD_WRAPPER_PROBE"),
        cwd=tmp_path,
        timeout_seconds=10,
    )

    assert status == "not_available"
    assert error == "windows_batch_wrapper_forbidden"


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 path normalization")
@pytest.mark.parametrize("suffix", [".", " "])
def test_windows_noncanonical_batch_alias_fails_before_process_creation(
    tmp_path, monkeypatch, suffix
) -> None:
    npm_cmd = tmp_path / "npm.cmd"
    npm_cmd.write_text("@echo off\n", encoding="utf-8")
    alias = f"{npm_cmd}{suffix}"
    monkeypatch.setattr(quality_evidence, "_which", lambda _name: alias)

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("non-canonical batch alias reached subprocess.run")

    monkeypatch.setattr(quality_evidence.subprocess, "run", unexpected_run)

    status, _stdout, error, _duration = quality_evidence._run_command_array(
        (alias, "&echo", "AIWORKHUB_CMD_WRAPPER_PROBE"),
        cwd=tmp_path,
        timeout_seconds=10,
    )

    assert status == "not_available"
    assert error == "windows_noncanonical_executable_alias"


@pytest.mark.parametrize("suffix", [".", " "])
def test_windows_noncanonical_npm_ancestor_alias_fails_before_resolution(
    tmp_path, monkeypatch, suffix
) -> None:
    alias = tmp_path / f"trusted-nodejs{suffix}" / "npm.cmd"
    monkeypatch.setattr(quality_evidence, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(quality_evidence, "_which", lambda _name: str(alias))

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("non-canonical npm ancestor reached subprocess.run")

    monkeypatch.setattr(quality_evidence.subprocess, "run", unexpected_run)

    status, _stdout, error, _duration = quality_evidence._run_command_array(
        ("npm", "--version"), cwd=tmp_path, timeout_seconds=10
    )

    assert status == "not_available"
    assert error == "windows_noncanonical_executable_alias"


@pytest.mark.parametrize("suffix", [".", " "])
def test_windows_noncanonical_native_ancestor_alias_fails_before_resolution(
    tmp_path, monkeypatch, suffix
) -> None:
    alias = tmp_path / f"native-tools{suffix}" / "quality-tool.exe"
    monkeypatch.setattr(quality_evidence, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(quality_evidence, "_which", lambda _name: str(alias))

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("non-canonical native ancestor reached subprocess.run")

    monkeypatch.setattr(quality_evidence.subprocess, "run", unexpected_run)

    status, _stdout, error, _duration = quality_evidence._run_command_array(
        ("quality-tool",), cwd=tmp_path, timeout_seconds=10
    )

    assert status == "not_available"
    assert error == "windows_noncanonical_executable_alias"


@pytest.mark.parametrize(
    "raw_path",
    [
        r"\\server.\share\tools\quality-tool.exe",
        r"\\server \share\tools\quality-tool.exe",
        r"\\server\share.\tools\quality-tool.exe",
        r"\\server\share \tools\quality-tool.exe",
    ],
)
def test_windows_unc_server_share_alias_fails_before_resolution(
    tmp_path, monkeypatch, raw_path
) -> None:
    monkeypatch.setattr(quality_evidence, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(quality_evidence, "_which", lambda _name: raw_path)

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("non-canonical UNC alias reached subprocess.run")

    monkeypatch.setattr(quality_evidence.subprocess, "run", unexpected_run)

    status, _stdout, error, _duration = quality_evidence._run_command_array(
        (raw_path,), cwd=tmp_path, timeout_seconds=10
    )

    assert status == "not_available"
    assert error == "windows_noncanonical_executable_alias"


@pytest.mark.parametrize(
    "raw_path",
    [
        "C:\\quality-tool.exe",
        "C:quality-tool.exe",
        "\\quality-tool.exe",
        r"\\server\share\quality-tool.exe",
    ],
)
def test_windows_drive_root_and_canonical_unc_controls_are_not_aliases(raw_path) -> None:
    assert quality_evidence._windows_noncanonical_path_alias(raw_path) is False


@pytest.mark.parametrize(
    ("raw_path", "target_path", "expected_error", "symlink_checked"),
    [
        (
            r"C:\tools\npm.cmd",
            r"C:\Windows\System32\where.exe",
            "windows_path_reparse_forbidden",
            True,
        ),
        (
            r"C:\tools\custom.cmd",
            r"C:\Windows\System32\where.exe",
            "windows_path_reparse_forbidden",
            True,
        ),
        (
            r"C:\tools\custom.bat",
            r"C:\tools\native.com",
            "windows_path_reparse_forbidden",
            True,
        ),
        (
            r"C:\tools\quality-tool.py",
            r"C:\Windows\System32\where.exe",
            "windows_non_native_executable_forbidden",
            False,
        ),
        (
            r"C:\tools\quality-tool.exe",
            r"C:\Windows\System32\where.exe",
            "windows_path_reparse_forbidden",
            True,
        ),
        (
            r"C:\tools\quality-tool.com",
            r"C:\tools\native.com",
            "windows_path_reparse_forbidden",
            True,
        ),
    ],
)
def test_windows_raw_type_and_symlink_policy_precedes_resolution(
    tmp_path,
    monkeypatch,
    raw_path,
    target_path,
    expected_error,
    symlink_checked,
) -> None:
    calls = {"component_chain": 0, "resolve": 0}

    class FakeCandidate:
        def resolve(self, *, strict=False):
            calls["resolve"] += 1
            raise AssertionError(
                f"raw symlink candidate resolved to forbidden target: {target_path}"
            )

    monkeypatch.setattr(quality_evidence, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(quality_evidence, "_which", lambda _name: raw_path)
    monkeypatch.setattr(quality_evidence, "Path", lambda _value: FakeCandidate())

    def fake_component_chain(_path):
        calls["component_chain"] += 1
        return "windows_path_reparse_forbidden"

    monkeypatch.setattr(
        quality_evidence, "_windows_path_component_chain_error", fake_component_chain
    )

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("forbidden raw candidate reached subprocess.run")

    monkeypatch.setattr(quality_evidence.subprocess, "run", unexpected_run)

    status, _stdout, error, _duration = quality_evidence._run_command_array(
        (raw_path,), cwd=tmp_path, timeout_seconds=10
    )

    assert status == "not_available"
    assert error == expected_error
    assert calls == {"component_chain": int(symlink_checked), "resolve": 0}


@pytest.mark.parametrize("filename", ["quality-tool.exe", "quality-tool.com", "npm.cmd"])
def test_windows_real_directory_symlink_ancestor_fails_before_process_creation(
    tmp_path, monkeypatch, filename
) -> None:
    target = tmp_path / "real-tools"
    target.mkdir()
    (target / filename).write_bytes(b"placeholder")
    ancestor_link = tmp_path / "ancestor-link"
    try:
        ancestor_link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")
    monkeypatch.setattr(quality_evidence, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        quality_evidence, "_which", lambda _name: str(ancestor_link / filename)
    )

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("directory-symlink ancestor reached subprocess.run")

    monkeypatch.setattr(quality_evidence.subprocess, "run", unexpected_run)

    status, _stdout, error, _duration = quality_evidence._run_command_array(
        ("npm" if filename == "npm.cmd" else filename,),
        cwd=tmp_path,
        timeout_seconds=10,
    )

    assert status == "not_available"
    assert error == "windows_path_reparse_forbidden"


def test_windows_direct_native_ancestor_junction_fails_before_process_creation(
    tmp_path, monkeypatch
) -> None:
    tools = tmp_path / "native-tools"
    tools.mkdir()
    executable = tools / "quality-tool.exe"
    executable.write_bytes(b"placeholder")
    path_type = type(tools)
    original_is_junction = getattr(path_type, "is_junction", None)

    def fake_is_junction(self):
        if self == tools:
            return True
        return original_is_junction(self) if original_is_junction else False

    monkeypatch.setattr(path_type, "is_junction", fake_is_junction, raising=False)
    monkeypatch.setattr(quality_evidence, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(quality_evidence, "_which", lambda _name: str(executable))

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("junction ancestor reached subprocess.run")

    monkeypatch.setattr(quality_evidence.subprocess, "run", unexpected_run)

    status, _stdout, error, _duration = quality_evidence._run_command_array(
        ("quality-tool",), cwd=tmp_path, timeout_seconds=10
    )

    assert status == "not_available"
    assert error == "windows_path_reparse_forbidden"


def _write_standard_npm_layout(install_root) -> None:
    npm_cli = install_root / "node_modules" / "npm" / "bin" / "npm-cli.js"
    npm_cli.parent.mkdir(parents=True)
    (install_root / "npm.cmd").write_text("@echo off\n", encoding="utf-8")
    (install_root / "node.exe").write_bytes(b"placeholder")
    npm_cli.write_text("require('../lib/cli.js')\n", encoding="utf-8")


def test_windows_npm_layout_directory_symlink_fails_before_process_creation(
    tmp_path, monkeypatch
) -> None:
    install_root = tmp_path / "nodejs"
    install_root.mkdir()
    (install_root / "npm.cmd").write_text("@echo off\n", encoding="utf-8")
    (install_root / "node.exe").write_bytes(b"placeholder")
    external = tmp_path / "external-node-modules"
    npm_cli = external / "npm" / "bin" / "npm-cli.js"
    npm_cli.parent.mkdir(parents=True)
    npm_cli.write_text("require('../lib/cli.js')\n", encoding="utf-8")
    try:
        (install_root / "node_modules").symlink_to(
            external, target_is_directory=True
        )
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")
    monkeypatch.setattr(quality_evidence, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        quality_evidence, "_which", lambda _name: str(install_root / "npm.cmd")
    )

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("npm layout symlink reached subprocess.run")

    monkeypatch.setattr(quality_evidence.subprocess, "run", unexpected_run)

    status, _stdout, error, _duration = quality_evidence._run_command_array(
        ("npm", "--version"), cwd=tmp_path, timeout_seconds=10
    )

    assert status == "not_available"
    assert error == "windows_path_reparse_forbidden"


def test_windows_npm_layout_intermediate_junction_fails_before_process_creation(
    tmp_path, monkeypatch
) -> None:
    install_root = tmp_path / "nodejs"
    _write_standard_npm_layout(install_root)
    junction = install_root / "node_modules" / "npm"
    path_type = type(junction)
    original_is_junction = getattr(path_type, "is_junction", None)

    def fake_is_junction(self):
        if self == junction:
            return True
        return original_is_junction(self) if original_is_junction else False

    monkeypatch.setattr(path_type, "is_junction", fake_is_junction, raising=False)
    monkeypatch.setattr(quality_evidence, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        quality_evidence, "_which", lambda _name: str(install_root / "npm.cmd")
    )

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("npm layout junction reached subprocess.run")

    monkeypatch.setattr(quality_evidence.subprocess, "run", unexpected_run)

    status, _stdout, error, _duration = quality_evidence._run_command_array(
        ("npm", "--version"), cwd=tmp_path, timeout_seconds=10
    )

    assert status == "not_available"
    assert error == "windows_path_reparse_forbidden"


def test_windows_non_native_pathext_candidate_fails_before_process_creation(
    tmp_path, monkeypatch
) -> None:
    script = tmp_path / "quality-tool.py"
    script.write_text("raise SystemExit(0)\n", encoding="utf-8")
    monkeypatch.setattr(quality_evidence, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(quality_evidence, "_which", lambda _name: str(script))

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("non-native PATHEXT candidate reached subprocess.run")

    monkeypatch.setattr(quality_evidence.subprocess, "run", unexpected_run)

    status, _stdout, error, _duration = quality_evidence._run_command_array(
        ("quality-tool", "&echo", "AIWORKHUB_CMD_WRAPPER_PROBE"),
        cwd=tmp_path,
        timeout_seconds=10,
    )

    assert status == "not_available"
    assert error == "windows_non_native_executable_forbidden"


def test_windows_process_start_oserror_is_safe_unavailable(
    tmp_path, monkeypatch
) -> None:
    executable = tmp_path / "quality-tool.exe"
    executable.write_bytes(b"not-a-real-portable-executable")
    monkeypatch.setattr(quality_evidence, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(quality_evidence, "_which", lambda _name: str(executable))

    def fail_start(*_args, **_kwargs):
        raise OSError(193, "not a valid Win32 application")

    monkeypatch.setattr(quality_evidence.subprocess, "run", fail_start)

    status, _stdout, error, _duration = quality_evidence._run_command_array(
        ("quality-tool",), cwd=tmp_path, timeout_seconds=10
    )

    assert status == "not_available"
    assert error == "process_start_failed"


def test_windows_nonstandard_npm_wrapper_fails_before_process_creation(
    tmp_path, monkeypatch
) -> None:
    npm_cmd = tmp_path / "npm.cmd"
    npm_cmd.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(quality_evidence, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(quality_evidence, "_which", lambda _name: str(npm_cmd))

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("non-standard npm wrapper reached subprocess.run")

    monkeypatch.setattr(quality_evidence.subprocess, "run", unexpected_run)

    status, _stdout, error, _duration = quality_evidence._run_command_array(
        ("npm", "--version"), cwd=tmp_path, timeout_seconds=10
    )

    assert status == "not_available"
    assert error == "npm_native_entrypoint_unavailable"


def test_windows_live_npm_metacharacters_do_not_execute_second_command(
    tmp_path,
) -> None:
    if os.name != "nt":
        pytest.skip("requires Windows batch-wrapper process semantics")
    if quality_evidence._which("npm") is None:
        pytest.skip("npm is not installed")
    marker = "AIWORKHUB_CMD_WRAPPER_PROBE"
    probe = tmp_path / "cmd-wrapper-probe.txt"

    status, stdout, stderr, _duration = quality_evidence._run_command_array(
        ("npm", "&echo", f"{marker}>{probe.name}"),
        cwd=tmp_path,
        timeout_seconds=30,
    )

    assert status == "failed"
    assert not probe.exists()
    assert marker not in {line.strip() for line in stdout.splitlines()}
    assert marker not in {line.strip() for line in stderr.splitlines()}


def test_windows_live_trailing_dot_npm_alias_cannot_execute_second_command(
    tmp_path,
) -> None:
    if os.name != "nt":
        pytest.skip("requires Win32 trailing-dot normalization")
    resolved_npm = quality_evidence._which("npm")
    if resolved_npm is None:
        pytest.skip("npm is not installed")
    marker = "AIWORKHUB_CMD_ALIAS_PROBE"
    probe = tmp_path / "cmd-alias-probe.txt"
    alias = f"{resolved_npm}."

    status, _stdout, error, _duration = quality_evidence._run_command_array(
        (alias, "&echo", f"{marker}>{probe.name}"),
        cwd=tmp_path,
        timeout_seconds=30,
    )

    assert status == "not_available"
    assert error == "windows_noncanonical_executable_alias"
    assert not probe.exists()


def test_posix_declared_command_keeps_executable_token(tmp_path, monkeypatch) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setattr(quality_evidence, "os", SimpleNamespace(name="posix"))

    def unexpected_which(executable: str) -> str | None:
        raise AssertionError(f"POSIX executable unexpectedly resolved: {executable}")

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="passed", stderr="")

    monkeypatch.setattr(quality_evidence, "_which", unexpected_which)
    monkeypatch.setattr(quality_evidence.subprocess, "run", fake_run)

    status, _stdout, _stderr, _duration = quality_evidence._run_command_array(
        ("npm", "test"), cwd=tmp_path, timeout_seconds=10
    )

    assert status == "passed"
    assert observed["argv"] == ["npm", "test"]
    assert observed["kwargs"]["shell"] is False


@pytest.mark.parametrize("module", ["ruff", "mypy"])
def test_python_quality_module_uses_path_entrypoint_when_runtime_lacks_module(
    tmp_path, monkeypatch, module
) -> None:
    entrypoint = tmp_path / module
    entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
    observed: dict[str, object] = {}
    receipt: dict[str, object] = {}

    monkeypatch.setattr(quality_evidence, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(
        quality_evidence.importlib.util,
        "find_spec",
        lambda name: None if name == module else object(),
    )
    monkeypatch.setattr(
        quality_evidence,
        "_which",
        lambda name: str(entrypoint) if name == module else None,
    )

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="passed", stderr="")

    monkeypatch.setattr(quality_evidence.subprocess, "run", fake_run)

    status, _stdout, _stderr, _duration = quality_evidence._run_command_array(
        ("{python}", "-m", module, "check", "."),
        cwd=tmp_path,
        timeout_seconds=10,
        execution_receipt=receipt,
    )

    assert status == "passed"
    assert observed["argv"] == [str(entrypoint), "check", "."]
    assert observed["kwargs"]["shell"] is False
    assert receipt == {
        "declared_command": ["{python}", "-m", module, "check", "."],
        "executed_command": [str(entrypoint), "check", "."],
        "command_resolution": "python_module_path_entrypoint",
    }


def test_python_module_path_fallback_is_allowlisted(tmp_path, monkeypatch) -> None:
    observed: dict[str, object] = {}

    monkeypatch.setattr(quality_evidence, "os", SimpleNamespace(name="posix"))

    def unexpected_which(executable: str) -> str | None:
        raise AssertionError(f"untrusted module unexpectedly resolved: {executable}")

    def fake_run(argv, **_kwargs):
        observed["argv"] = argv
        return SimpleNamespace(returncode=1, stdout="", stderr="missing")

    monkeypatch.setattr(quality_evidence, "_which", unexpected_which)
    monkeypatch.setattr(quality_evidence.subprocess, "run", fake_run)

    status, _stdout, _stderr, _duration = quality_evidence._run_command_array(
        ("{python}", "-m", "untrusted_quality_module"),
        cwd=tmp_path,
        timeout_seconds=10,
    )

    assert status == "failed"
    assert observed["argv"] == [
        quality_evidence.sys.executable,
        "-m",
        "untrusted_quality_module",
    ]


def test_completion_quality_gate_accepts_codeql_like_static_analysis_kind(tmp_path) -> None:
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "checks": [{
            "id": "bounded-sast",
            "kind": "static_analysis",
            "command": ["python3", "-c", "raise SystemExit(0)"],
        }]
    }), encoding="utf-8")
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["good.py"]
    )

    assert packet["passed"] is True
    declared = next(row for row in packet["checks"] if row["check_id"] == "bounded-sast")
    assert declared["kind"] == "static_analysis"
    assert declared["status"] == "passed"


def test_completion_quality_gate_normalizes_declared_coverage_report(tmp_path) -> None:
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "checks": [{
            "id": "python-coverage",
            "kind": "coverage",
            "command": ["{python}", "-c", "raise SystemExit(0)"],
            "report": {
                "format": "coverage_json",
                "path": "coverage.json",
                "min_percent": 80,
            },
        }]
    }), encoding="utf-8")
    (tmp_path / "coverage.json").write_text(
        json.dumps({"total": {"lines": {"pct": 85.5}}}),
        encoding="utf-8",
    )
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["good.py"]
    )

    assert packet["passed"] is True
    report = next(
        row for row in packet["checks"]
        if row["check_id"] == "python-coverage:report"
    )
    assert report["kind"] == "coverage"
    assert report["status"] == "passed"
    assert report["provenance"] == "adapter:coverage_summary:coverage.json"


def test_completion_quality_gate_blocks_below_threshold_declared_report(tmp_path) -> None:
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "checks": [{
            "id": "python-coverage",
            "kind": "coverage",
            "command": ["{python}", "-c", "raise SystemExit(0)"],
            "report": {
                "format": "coverage_json",
                "path": "coverage.json",
                "min_percent": 80,
            },
        }]
    }), encoding="utf-8")
    (tmp_path / "coverage.json").write_text(
        json.dumps({"total": {"lines": {"pct": 42.0}}}),
        encoding="utf-8",
    )
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["good.py"]
    )

    assert packet["passed"] is False
    assert packet["blocking_checks"] == ["python-coverage:report"]


def test_declared_report_path_rejects_globs_and_parent_escape(tmp_path) -> None:
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    for unsafe in ("../coverage.json", "reports/*.json"):
        config.write_text(json.dumps({
            "checks": [{
                "id": "unsafe-report",
                "kind": "coverage",
                "command": ["{python}", "-c", "raise SystemExit(0)"],
                "report": {"format": "coverage_json", "path": unsafe},
            }]
        }), encoding="utf-8")
        try:
            quality_evidence.load_repo_config(tmp_path)
        except quality_evidence.MalformedConfigError as exc:
            assert "unsafe report path" in str(exc)
        else:
            raise AssertionError(f"unsafe report path was accepted: {unsafe}")


def test_declared_report_symlink_fails_closed(tmp_path) -> None:
    if os.name == "nt":
        return
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "checks": [{
            "id": "linked-coverage",
            "kind": "coverage",
            "command": ["{python}", "-c", "raise SystemExit(0)"],
            "report": {"format": "coverage_json", "path": "coverage.json"},
        }]
    }), encoding="utf-8")
    target = tmp_path / "actual-coverage.json"
    target.write_text(
        json.dumps({"total": {"lines": {"pct": 100}}}), encoding="utf-8"
    )
    (tmp_path / "coverage.json").symlink_to(target)
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["good.py"]
    )

    assert packet["passed"] is False
    assert packet["blocking_checks"] == ["linked-coverage:report"]
    report = next(row for row in packet["checks"] if row["check_id"].endswith(":report"))
    assert report["error"] == "report_symlink_forbidden"


def test_declared_check_skips_unrelated_exact_delta(tmp_path) -> None:
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "checks": [{
            "id": "extension-only",
            "kind": "test",
            "command": ["python3", "-c", "raise SystemExit(19)"],
            "paths": ["vscode-extension/**"],
        }]
    }), encoding="utf-8")
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = quality_evidence.run_completion_quality_gate(
        tmp_path, changed_paths=["good.py"]
    )

    assert packet["passed"] is True
    row = next(check for check in packet["checks"] if check["check_id"] == "extension-only")
    assert row["status"] == "skipped"
    assert row["summary"] == "changed_paths_not_applicable"


def test_declared_check_runs_for_matching_delta_and_respects_minimum_risk(tmp_path) -> None:
    config = tmp_path / ".aiworkhub" / "quality.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "checks": [{
            "id": "high-risk-python",
            "kind": "test",
            "command": ["python3", "-c", "raise SystemExit(19)"],
            "paths": ["src/*.py"],
            "minimum_risk": "high",
        }]
    }), encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "engine.py").write_text("value = 1\n", encoding="utf-8")

    low = quality_evidence.run_declared_checks(
        tmp_path,
        changed_paths=["src/engine.py"],
        effective_risk_tier="low",
    )
    high = quality_evidence.run_declared_checks(
        tmp_path,
        changed_paths=["src/engine.py"],
        effective_risk_tier="high",
    )

    assert low[0].status == "skipped"
    assert low[0].summary == "risk_below_minimum:low<high"
    assert high[0].status == "failed"


def test_destructive_diff_blocks_multi_signal_module_replacement(tmp_path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    for root in (baseline, candidate):
        (root / "src").mkdir(parents=True)
        (root / "tests").mkdir(parents=True)
    functions = "\n".join(
        f"def public_{index}():\n    return {index}\n" for index in range(120)
    )
    (baseline / "src" / "engine.py").write_text(functions, encoding="utf-8")
    (candidate / "src" / "engine.py").write_text(
        "def public_0():\n    return 0\n", encoding="utf-8"
    )
    (candidate / "tests" / "test_engine.py").write_text(
        "def test_small():\n    assert True\n", encoding="utf-8"
    )

    checks = quality_evidence.run_destructive_diff_checks(
        baseline,
        candidate,
        changed_paths=["src/engine.py", "tests/test_engine.py"],
    )

    blocker = next(row for row in checks if row.check_id.endswith("src/engine.py"))
    assert blocker.status == "failed"
    assert "tests_changed=true" in blocker.summary
    assert "public_api_loss" in blocker.summary


def test_destructive_diff_allows_small_or_nonshrinking_edit(tmp_path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    before = "\n".join(f"value_{index} = {index}" for index in range(250)) + "\n"
    (baseline / "engine.py").write_text(before, encoding="utf-8")
    (candidate / "engine.py").write_text(before + "new_value = 1\n", encoding="utf-8")

    checks = quality_evidence.run_destructive_diff_checks(
        baseline, candidate, changed_paths=["engine.py"]
    )

    assert [row.status for row in checks] == ["passed"]
