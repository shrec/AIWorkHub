"""NF-2026-00081 Windows VS Code LM launch portability regressions."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from aiworkhub import process_launcher, vscode_lm_bridge, worker_workspace


_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "aiworkhub"


@pytest.mark.parametrize(
    ("relative", "functions"),
    [
        ("process_launcher.py", {"_touch_0600"}),
        ("vscode_lm_bridge.py", {"_atomic_json"}),
        ("worker_supervisor.py", {"_write_json_0600", "_open_0600"}),
        ("worker_workspace.py", {"write_json_0600"}),
    ],
)
def test_launch_critical_writers_do_not_call_raw_chmod(
    relative: str,
    functions: set[str],
) -> None:
    source = (_SOURCE_ROOT / relative).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in functions
    }
    assert found.keys() == functions
    assert all("os.chmod(" not in body for body in found.values())


def test_worker_launch_cwd_uses_workspace_on_windows(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    monkeypatch.setattr(process_launcher.os, "name", "nt")

    assert process_launcher._worker_launch_cwd(workspace) == str(workspace.resolve())


def test_worker_launch_cwd_preserves_posix_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(process_launcher.os, "name", "posix")
    assert process_launcher._worker_launch_cwd(tmp_path) == "/"


def test_bridge_atomic_json_uses_windows_acl_authority(monkeypatch, tmp_path: Path) -> None:
    destination = tmp_path / "bridge" / "request.json"
    monkeypatch.setattr(vscode_lm_bridge, "posix_path_modes_supported", lambda: False)

    def denied(_path, _mode):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(vscode_lm_bridge, "chmod_path", denied)
    vscode_lm_bridge._atomic_json(destination, {"text": "ქართული → UTF-8"})

    assert json.loads(destination.read_text(encoding="utf-8"))["text"].endswith("UTF-8")


def test_workspace_json_writer_uses_platform_chmod_helper(monkeypatch, tmp_path: Path) -> None:
    destination = tmp_path / "runtime" / "request.json"
    calls: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        worker_workspace,
        "chmod_path",
        lambda path, mode: calls.append((Path(path), mode)),
    )

    worker_workspace.write_json_0600(destination, {"ok": True})

    assert json.loads(destination.read_text(encoding="utf-8")) == {"ok": True}
    assert calls == [(destination.parent, 0o700), (destination, 0o600)]


def test_supervisor_spawn_failure_records_exact_phase() -> None:
    source = (_SOURCE_ROOT / "worker_supervisor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    constants = {
        node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
    }
    assert {"child_spawn", "job_assignment", "spawn_phase"} <= constants
    assert "windows_job.assign(child)" in source
