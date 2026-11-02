"""NF-2026-01936: a validation spawn failure carries a typed reason and remedy.

Field reports (EntryLink, megoproject, ultrafastsecp256k1; 2026-11-01/02): a
worker's declared dotnet/flutter/node/cmake validation came back as a bare
``tool_unavailable`` with only the raw OSError text, so nobody could tell
whether the head was missing, a batch wrapper, or an unregistered install root.
Every fixture here is a tmp_path fake toolchain with PATH/PATHEXT passed
explicitly, so the tests need no real toolchain and run inside the AppContainer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import worker_ai_tools_mcp as w  # noqa: E402
from aiworkhub import worker_workspace  # noqa: E402
from aiworkhub.toolchain_registry import Toolchain, ToolchainRegistry  # noqa: E402
from aiworkhub.validation_spawn_diagnosis import (  # noqa: E402
    SPAWN_DIAGNOSIS_REASONS,
    diagnose_validation_spawn_failure,
)

PATHEXT = os.pathsep.join([".COM", ".EXE", ".BAT", ".CMD"])
LAUNCH_ERROR = "[WinError 2] The system cannot find the file specified"


@pytest.fixture(autouse=True)
def _isolated_memo() -> None:
    w._validation_memo_reset()
    yield
    w._validation_memo_reset()


def _fake_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake")
    if os.name != "nt":  # POSIX which() needs X_OK; Windows ignores the mode bits
        path.chmod(0o755)
    return path


def _registry(*toolchains: Toolchain) -> ToolchainRegistry:
    return ToolchainRegistry(sha256="test", toolchains=toolchains)


def _toolchain(root: Path) -> Toolchain:
    return Toolchain(
        id="tool", executables=("tool",), roots=(str(root),), path_prepend=(), env=()
    )


def _ctx(tmp_path: Path) -> w.WorkerToolContext:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    return w.WorkerToolContext(
        task_id="TASK_NF01936",
        runner="claude_worker",
        topic="task_mcp",
        request_id="req-nf01936",
        repo=worktree,
        authority_repo=tmp_path,
        source_graph_targets=(),
        session_topic="task_mcp",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        contract_packet_path=tmp_path / "packet.json",
    )


def test_command_not_found(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    out = diagnose_validation_spawn_failure(
        "dotnet", LAUNCH_ERROR, path=str(empty), pathext=PATHEXT, registry=_registry()
    )
    assert out["reason"] == "command_not_found"
    assert out["resolved_path"] is None
    assert "dotnet" in out["remedy"] and "toolchains.json" in out["remedy"]
    assert set(out) == {"reason", "resolved_path", "remedy"}
    assert "is not on the host PATH" in out["remedy"]


def test_command_not_found_absolute_head_names_the_path_not_path_env(
    tmp_path: Path,
) -> None:
    missing = str(tmp_path / "sdk" / "dotnet.exe")
    out = diagnose_validation_spawn_failure(
        missing, LAUNCH_ERROR, path="", pathext=PATHEXT, registry=_registry()
    )
    assert out["reason"] == "command_not_found"
    assert out["resolved_path"] is None
    assert missing in out["remedy"] and "does not exist" in out["remedy"]
    assert "host PATH" not in out["remedy"]
    assert "toolchains.json" in out["remedy"]


def test_windows_batch_wrapper_names_native_entry_point(tmp_path: Path) -> None:
    nodejs = tmp_path / "nodejs"
    wrapper = _fake_executable(nodejs / "npm.cmd")
    out = diagnose_validation_spawn_failure(
        "npm", LAUNCH_ERROR, path=str(nodejs), pathext=PATHEXT, registry=_registry()
    )
    assert out["reason"] == "windows_batch_wrapper"
    assert Path(out["resolved_path"]).resolve() == wrapper.resolve()
    assert "npm-cli.js" in out["remedy"]
    assert "node" in out["remedy"]
    assert "never run with shell interpretation" in out["remedy"]


def test_toolchain_not_registered_gives_exact_registry_entry(tmp_path: Path) -> None:
    root = tmp_path / "Tool"
    exe = _fake_executable(root / "bin" / "tool.exe")
    out = diagnose_validation_spawn_failure(
        "tool", LAUNCH_ERROR, path=str(exe.parent), pathext=PATHEXT, registry=_registry()
    )
    assert out["reason"] == "toolchain_not_registered"
    assert Path(out["resolved_path"]).resolve() == exe.resolve()
    entry = json.loads(out["remedy"].split(": ", 1)[1])
    assert entry == {"tool": {"executables": ["tool"], "roots": [str(root)]}}
    assert ".aiworkhub/config/toolchains.json" in out["remedy"]


def test_unrelated_registered_root_is_still_not_registered(tmp_path: Path) -> None:
    exe = _fake_executable(tmp_path / "Tool" / "tool.exe")
    other = tmp_path / "Other"
    other.mkdir()
    out = diagnose_validation_spawn_failure(
        "tool",
        LAUNCH_ERROR,
        path=str(exe.parent),
        pathext=PATHEXT,
        registry=_registry(_toolchain(other)),
    )
    assert out["reason"] == "toolchain_not_registered"


def test_toolchain_registered_launch_failed_quotes_message(tmp_path: Path) -> None:
    root = tmp_path / "Tool"
    exe = _fake_executable(root / "bin" / "tool.exe")
    out = diagnose_validation_spawn_failure(
        "tool",
        "[WinError 5] Access is denied",
        path=str(exe.parent),
        pathext=PATHEXT,
        registry=_registry(_toolchain(root)),
    )
    assert out["reason"] == "toolchain_registered_launch_failed"
    assert "[WinError 5] Access is denied" in out["remedy"]
    assert "'tool'" in out["remedy"]


def test_unknown_keeps_raw_message(tmp_path: Path) -> None:
    out = diagnose_validation_spawn_failure(
        "", LAUNCH_ERROR, path=str(tmp_path), pathext=PATHEXT, registry=_registry()
    )
    assert out["reason"] == "unknown"
    assert LAUNCH_ERROR in out["remedy"]
    script = tmp_path / "notes.txt"
    script.write_text("x", encoding="utf-8")
    out = diagnose_validation_spawn_failure(
        str(script), LAUNCH_ERROR, path="", pathext=PATHEXT, registry=_registry()
    )
    assert out["reason"] == "unknown"
    assert LAUNCH_ERROR in out["remedy"]
    assert set(SPAWN_DIAGNOSIS_REASONS) >= {out["reason"]}


def test_direct_lane_receipt_carries_spawn_diagnosis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("PATHEXT", PATHEXT)
    monkeypatch.setattr(
        worker_workspace,
        "resolve_worker_validation_argv",
        lambda workspace, command: {"argv": ["nf01936-missing-tool", "test"]},
    )
    monkeypatch.setattr(w, "_validation_child_env", lambda *a, **k: {})

    def _raise(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(subprocess, "run", _raise)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx = _ctx(tmp_path)
    receipt = w._run_one_validation(
        ctx,
        workspace=types.SimpleNamespace(path=str(ctx.repo)),
        command="nf01936-missing-tool test",
        index=0,
        candidate_digest="digest",
        run_dir=run_dir,
        force=True,
        timeout_seconds=5,
    )
    assert receipt["failure_class"] == "tool_unavailable"
    assert receipt["returncode"] is None
    assert receipt["spawn_diagnosis"]["reason"] == "command_not_found"
    assert receipt["spawn_diagnosis"]["resolved_path"] is None


def test_contained_lane_receipt_carries_spawn_diagnosis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    nodejs = tmp_path / "nodejs"
    _fake_executable(nodejs / "npm.cmd")
    monkeypatch.setenv("PATH", str(nodejs))
    monkeypatch.setenv("PATHEXT", PATHEXT)
    row = {
        "returncode": None,
        "timed_out": False,
        "declared_argv": ["npm", "test"],
        "launch_error_message": LAUNCH_ERROR,
    }
    receipt = w._contained_validation_receipt(
        _ctx(tmp_path),
        index=0,
        command="npm test",
        command_sha256="0" * 64,
        memo_key=("req-nf01936", "0" * 64, "digest"),
        row=row,
        failure="validation_spawn_failed",
        run_dir=tmp_path,
    )
    assert receipt["failure_class"] == "tool_unavailable"
    assert receipt["reason"] == LAUNCH_ERROR
    assert receipt["spawn_diagnosis"]["reason"] == "windows_batch_wrapper"
    assert "npm-cli.js" in receipt["spawn_diagnosis"]["remedy"]


def test_contained_lane_unresolved_row_has_no_spawn_diagnosis(tmp_path: Path) -> None:
    receipt = w._contained_validation_receipt(
        _ctx(tmp_path),
        index=0,
        command="npm test",
        command_sha256="0" * 64,
        memo_key=("req-nf01936", "0" * 64, "digest"),
        row=None,
        failure="validation_unresolved:refused",
        run_dir=tmp_path,
    )
    assert receipt["failure_class"] == "tool_unavailable"
    assert "spawn_diagnosis" not in receipt
