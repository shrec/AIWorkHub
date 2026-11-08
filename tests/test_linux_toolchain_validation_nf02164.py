"""NF-2026-02164: C++ validation on a Linux host (clang++-20, mingw g++ cross).

Downstream report (0.12.94): a card validating with ``clang++-20`` and
``x86_64-w64-mingw32-g++-posix`` was refused and nothing told the manager how to
register a compiler.  Every fixture is a tmp_path fake toolchain, so the tests
need no real compiler and no Linux sandbox.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_templates, toolchain_registry, validation_spawn_diagnosis  # noqa: E402
from aiworkhub import validation_toolchains as vt  # noqa: E402
from aiworkhub import worker_ai_tools_mcp as w  # noqa: E402
from aiworkhub import worker_workspace  # noqa: E402

REGISTRY_PATH = ".aiworkhub/config/toolchains.json"
VERSIONED = "clang++-20"
CROSS = "x86_64-w64-mingw32-g++-posix"


@pytest.fixture(autouse=True)
def _isolated_memo() -> None:
    w._validation_memo_reset()
    yield
    w._validation_memo_reset()


def _empty_registry() -> toolchain_registry.ToolchainRegistry:
    return toolchain_registry.ToolchainRegistry(sha256="test", toolchains=())


def _fake_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake")
    if os.name != "nt":
        path.chmod(0o755)
    return path


# -- 1. g++ / clang++ are trusted; versioned and cross names stay registry-only


def test_cxx_drivers_are_trusted_system_executables() -> None:
    assert {"g++", "clang++"} <= vt.SYSTEM_VALIDATION_EXECUTABLES
    assert {"g++", "clang++"} <= vt.VALIDATION_EXECUTABLES_BY_LANGUAGE["cpp"]
    assert {"g++", "clang++"} <= vt.VALIDATION_EXECUTABLES_BY_LANGUAGE["objective_c"]
    assert {"g++", "clang++"} <= worker_workspace._TRUSTED_VALIDATION_SYSTEM_EXECUTABLES


@pytest.mark.parametrize("name", [VERSIONED, CROSS])
def test_versioned_and_cross_names_remain_registry_only(name: str) -> None:
    assert name not in vt.SYSTEM_VALIDATION_EXECUTABLES
    assert name not in worker_workspace._TRUSTED_VALIDATION_SYSTEM_EXECUTABLES


# -- 2. the remedy reaches every non-AppContainer lane


def _refuse_with(monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    def _raise(workspace: object, command: str) -> dict[str, object]:
        raise worker_workspace.WorkspaceError(message)

    monkeypatch.setattr(worker_workspace, "resolve_worker_validation_argv", _raise)


def _run_refused(tmp_path: Path, command: str) -> dict[str, object]:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    ctx = w.WorkerToolContext(
        task_id="TASK_NF02164",
        runner="claude_worker",
        topic="task_mcp",
        request_id="req-nf02164",
        repo=worktree,
        authority_repo=tmp_path,
        source_graph_targets=(),
        session_topic="task_mcp",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        contract_packet_path=tmp_path / "packet.json",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    return w._run_one_validation(
        ctx,
        workspace=types.SimpleNamespace(path=str(worktree)),
        command=command,
        index=0,
        candidate_digest="digest",
        run_dir=run_dir,
        force=True,
        timeout_seconds=5,
    )


def test_not_approved_refusal_carries_toolchain_not_registered_remedy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "llvm"
    suffix = ".exe" if os.name == "nt" else ""
    exe = _fake_executable(root / "bin" / f"{VERSIONED}{suffix}")
    monkeypatch.setenv("PATH", str(exe.parent))
    monkeypatch.setenv("PATHEXT", ".EXE" if os.name == "nt" else "")
    monkeypatch.setattr(validation_spawn_diagnosis, "load_registry", _empty_registry)
    _refuse_with(monkeypatch, f"validation_executable_not_approved:{VERSIONED}")

    receipt = _run_refused(tmp_path, f"{VERSIONED} --version")

    assert receipt["resolved"] is False
    assert receipt["failure_class"] == "tool_unavailable"
    assert receipt["reason"] == f"validation_executable_not_approved:{VERSIONED}"
    diagnosis = receipt["spawn_diagnosis"]
    assert diagnosis["reason"] == "toolchain_not_registered"
    assert REGISTRY_PATH in diagnosis["remedy"]
    entry = json.loads(diagnosis["remedy"].split(": ", 1)[1])
    assert list(entry.values()) == [{"executables": [VERSIONED], "roots": [str(root)]}]


def test_unavailable_refusal_names_command_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(validation_spawn_diagnosis, "load_registry", _empty_registry)
    _refuse_with(monkeypatch, f"validation_executable_unavailable:{CROSS}")

    receipt = _run_refused(tmp_path, f"{CROSS} --version")

    assert receipt["spawn_diagnosis"]["reason"] == "command_not_found"
    assert REGISTRY_PATH in receipt["spawn_diagnosis"]["remedy"]


def test_other_refusals_carry_no_diagnosis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _refuse_with(monkeypatch, "validation_parse_failed:nope")
    receipt = _run_refused(tmp_path, "whatever")
    assert "spawn_diagnosis" not in receipt
    assert receipt["resolved"] is False


# -- 3. the create-time advisory


@pytest.fixture
def no_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(toolchain_registry, "registered_toolchain", lambda name: None)
    monkeypatch.setattr(task_templates.shutil, "which", lambda name: None)


def _found_warning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, Path]:
    root = tmp_path / "llvm"
    exe = _fake_executable(root / "bin" / VERSIONED)
    monkeypatch.setattr(task_templates.shutil, "which", lambda name: str(exe))
    warnings = task_templates.validation_command_head_warnings(f"{VERSIONED} --version")
    assert len(warnings) == 1, warnings
    return warnings[0], root


def test_unapproved_bare_head_warning_prefix(
    no_registered: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    warning, _root = _found_warning(monkeypatch, tmp_path)
    assert warning.startswith(f"validation_head_not_approved:{VERSIONED}:")
    assert REGISTRY_PATH in warning


def test_unapproved_bare_head_warning_entry_json(
    no_registered: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    warning, _root = _found_warning(monkeypatch, tmp_path)
    entry = json.loads(warning.split(f"{REGISTRY_PATH}: ", 1)[1])
    (body,) = entry.values()
    assert body["executables"] == [VERSIONED]


def test_unapproved_bare_head_warning_names_the_install_root(
    no_registered: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    warning, root = _found_warning(monkeypatch, tmp_path)
    entry = json.loads(warning.split(f"{REGISTRY_PATH}: ", 1)[1])
    ((_, body),) = entry.items()
    (named_root,) = body["roots"]
    assert Path(named_root).resolve() == root.resolve()


def test_unapproved_head_not_on_path_still_names_the_entry(no_registered: None) -> None:
    warnings = task_templates.validation_command_head_warnings(f"{CROSS} -c x.cpp")
    assert len(warnings) == 1
    assert f"validation_head_not_approved:{CROSS}" in warnings[0]
    assert f'"executables": ["{CROSS}"]' in warnings[0]
    assert REGISTRY_PATH in warnings[0]


def test_head_after_env_and_cd_prefix_is_the_one_checked(no_registered: None) -> None:
    warnings = task_templates.validation_command_head_warnings(
        f"cd build && CC=x {VERSIONED} -v"
    )
    assert [w_.split(":")[0] for w_ in warnings] == ["validation_head_not_approved"]
    assert f"validation_head_not_approved:{VERSIONED}" in warnings[0]


@pytest.mark.parametrize(
    "command",
    [
        "g++ --version",
        "clang++ --version",
        "cmake --build build",
        "git status",
        "python3 -m pytest -q tests/test_x.py",
        "python -m ruff check src",
        "pytest -q",
        "ruff check src",
        "mypy src",
        "cd sub && python3 -m pytest -q",
        "PYTHONPATH=. python3 -m pytest -q",
        "./build/run_tests",
        "tools/run.sh",
        "/usr/bin/clang++-20 --version",
        "",
    ],
)
def test_approved_or_path_heads_produce_no_warning(
    no_registered: None, command: str
) -> None:
    assert task_templates.validation_command_head_warnings(command) == []


def test_registered_head_produces_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    toolchain = toolchain_registry.Toolchain(
        id="llvm",
        executables=(VERSIONED,),
        roots=("/opt/llvm",),
        path_prepend=(),
        env=(),
    )
    monkeypatch.setattr(
        toolchain_registry,
        "registered_toolchain",
        lambda name: toolchain if name == VERSIONED else None,
    )
    assert task_templates.validation_command_head_warnings(f"{VERSIONED} -v") == []


def test_existing_venv_warnings_are_unchanged(no_registered: None) -> None:
    assert task_templates.validation_command_head_warnings(".venv/bin/python -m pytest") == [
        "venv_relative_head_absent_from_worker_worktree:.venv/bin/python"
    ]
    assert task_templates.validation_command_head_warnings("/x/.venv/bin/python -m pytest") == [
        "absolute_venv_head_not_portable:/x/.venv/bin/python"
    ]


def test_unreadable_registry_never_refuses_or_guesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(name: str) -> None:
        raise RuntimeError("toolchain_registry_invalid:not_json")

    monkeypatch.setattr(toolchain_registry, "registered_toolchain", _boom)
    assert task_templates.validation_command_head_warnings(f"{VERSIONED} -v") == []


# -- 4. absolute heads are returned unchanged by the argv normalizer: no
# executable root is produced, so the refusal for ``/usr/bin/clang++-20`` is
# NOT in worker_workspace._normalize_trusted_validation_executable_argv_with_authority.


def test_absolute_head_passes_the_normalizer_unchanged(tmp_path: Path) -> None:
    head = str(tmp_path / VERSIONED)
    argv, roots, authority = (
        worker_workspace._normalize_trusted_validation_executable_argv_with_authority(
            [head, "--version"], tmp_path
        )
    )
    assert argv == [head, "--version"]
    assert roots == ()
    assert authority is None
