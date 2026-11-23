"""NF-2026-02373: a delta-retaining terminal records its measured workspace.

Drives ``process_launcher_evidence.harvest_delta_terminal_evidence`` -- the
block ``ProcessManager._monitor`` runs for timed_out/worker_failed/
validation_failed terminals -- over a tmp ``WorkerWorkspace`` fixture, and the
launch replies over a fake manager.  No provider CLI or manager loop starts.
"""
from __future__ import annotations

import inspect
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import process_launcher, process_launcher_evidence, server, worker_supervisor, worker_workspace  # noqa: E402

REQUIRED = ["src/a.py", "src/b.py", "tests/test_a.py", "docs/c.md", "src/gone.py"]
PRESENT = {
    "src/a.py": b"print('a')\n",
    "src/b.py": b"print('b')\n",
    "tests/test_a.py": b"def test_a():\n    pass\n",
    "docs/c.md": b"# c\n",
}


def _workspace(tmp_path: Path) -> process_launcher.WorkerWorkspace:
    repo = tmp_path / "repo"
    worktree = tmp_path / "request" / "worktree"
    home = tmp_path / "request" / "home"
    for directory in (repo, worktree, home):
        directory.mkdir(parents=True, exist_ok=True)
    for relative, content in PRESENT.items():
        path = worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return process_launcher.WorkerWorkspace(
        request_id="req",
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=tuple(REQUIRED),
        parent_baseline={},
        workspace_baseline={},
    )


def _metadata() -> dict[str, Any]:
    return {"required_outputs": list(REQUIRED), "validation": ["pytest -q"]}


def _evidence(metadata: dict[str, Any]) -> dict[str, Any]:
    return {"request_id": "req", **process_launcher._declared_failure_denominators(metadata)}


def _harvest(
    evidence: dict[str, Any],
    workspace: Any,
    metadata: dict[str, Any],
    *,
    enforce_scope: Any,
    retain: Any = None,
    terminal_state: str = "timed_out",
) -> None:
    process_launcher_evidence.harvest_delta_terminal_evidence(
        evidence,
        terminal_state,
        workspace,
        metadata,
        "req",
        enforce_scope=enforce_scope,
        claim_state=lambda: "claimed",
        retain=retain or (lambda *_a, **_k: {}),
        validate=process_launcher.validate_required_outputs,
    )


def test_changed_paths_recorded_when_retention_returns_empty(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    metadata = _metadata()
    evidence = _evidence(metadata)
    changed = sorted(PRESENT)
    _harvest(evidence, workspace, metadata, enforce_scope=lambda: list(changed))
    assert evidence["changed_paths"] == changed
    assert "changed_paths_error" not in evidence


def test_retention_only_adds_seal_evidence(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    metadata = _metadata()
    evidence = _evidence(metadata)
    changed = sorted(PRESENT)
    _harvest(
        evidence,
        workspace,
        metadata,
        enforce_scope=lambda: list(changed),
        retain=lambda *_a, **_k: {"retained_candidate": {"sealed": True}, "changed_paths": []},
    )
    assert evidence["retained_candidate"] == {"sealed": True}
    assert evidence["changed_paths"] == changed


def test_retention_exception_keeps_changed_paths(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    metadata = _metadata()
    evidence = _evidence(metadata)

    def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("seal failed")

    _harvest(evidence, workspace, metadata, enforce_scope=lambda: ["src/a.py"], retain=_boom)
    assert evidence["changed_paths"] == ["src/a.py"]
    assert evidence["retention_error"] == "RuntimeError:seal failed"


def test_scope_exception_is_named_never_silent_empty(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    metadata = _metadata()
    evidence = _evidence(metadata)

    def _raise() -> list[str]:
        raise process_launcher.WorkspaceError("scope_violation:" + "x" * 500)

    _harvest(evidence, workspace, metadata, enforce_scope=_raise)
    assert "changed_paths" not in evidence
    error = evidence["changed_paths_error"]
    assert error.startswith("WorkspaceError:scope_violation:")
    assert len(error) == len("WorkspaceError:") + 200


def test_required_outputs_are_measured_in_live_workspace(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    metadata = _metadata()
    evidence = _evidence(metadata)
    _harvest(evidence, workspace, metadata, enforce_scope=lambda: sorted(PRESENT))
    rows = {row["pattern"]: row for row in evidence["required_outputs"]}
    assert list(rows) == REQUIRED
    for relative, content in PRESENT.items():
        row = rows[relative]
        assert row["missing"] is False, row
        assert row["reason"] == "measured_at_terminal"
        assert row["path"] == relative
        assert row["bytes"] == len(content)
        assert row["sha256"] == worker_workspace._hash_path(workspace.path / relative)
    gone = rows["src/gone.py"]
    assert gone["missing"] is True
    assert gone["reason"] == "measured_at_terminal"
    assert gone["bytes"] is None
    assert sum(1 for row in rows.values() if row["missing"]) == 1


def test_violation_hash_matches_record_format_and_stays_inside_root(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    root = workspace.path
    row = process_launcher_evidence._hash_present_path(root, "src/a.py")
    assert row is not None
    assert row["bytes"] == len(PRESENT["src/a.py"])
    assert row["sha256"] == worker_workspace._hash_path(root / "src/a.py")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside bytes")
    for escape in ("", str(outside), "../../outside.txt", "src/../../../outside.txt"):
        assert process_launcher_evidence._hash_present_path(root, escape) is None, escape


def test_gone_workspace_keeps_declared_denominators_byte_identical(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    shutil.rmtree(workspace.path)
    metadata = _metadata()
    evidence = _evidence(metadata)
    declared = process_launcher._declared_failure_denominators(metadata)["required_outputs"]
    _harvest(evidence, workspace, metadata, enforce_scope=lambda: [])
    assert evidence["required_outputs"] == declared
    assert all(row["reason"] == "worker_terminal_before_output_validation" for row in declared)


def test_non_retaining_terminal_is_untouched(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    metadata = _metadata()
    evidence = _evidence(metadata)
    before = dict(evidence)
    _harvest(
        evidence, workspace, metadata, enforce_scope=lambda: ["src/a.py"], terminal_state="cancelled"
    )
    assert evidence == before


def test_launcher_routes_delta_terminal_through_harvest() -> None:
    source = inspect.getsource(process_launcher)
    assert "_harvest_delta_terminal_evidence(" in source
    assert "enforce_scope=_enforce_finalization_scope" in source
    assert 'failure_evidence["changed_paths"] = timeout_changed' not in source


def _stall() -> dict[str, Any]:
    return {
        "timeout_policy": "stall_gated",
        "stall_window_seconds": worker_supervisor.DEFAULT_STALL_WINDOW_SECONDS,
    }


def test_legacy_timeout_fields_name_stall_gated_policy() -> None:
    assert process_launcher._legacy_timeout_fields(7200) == {
        "timeout_seconds": 7200,
        "timeout_enforced": False,
        **_stall(),
    }


def test_server_launch_replies_name_stall_gated_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeManager:
        def launch(self, **_kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "state": "running"}

        def launch_quality_reviewer(self, **_kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "state": "starting"}

    monkeypatch.setattr(server.process_launcher, "default_manager", lambda: FakeManager())
    worker = server.aiworkhub_agent_launch_task("T1", "r", "coding", "claude_cli")
    reviewer = server.aiworkhub_quality_reviewer_launch(
        "req", "T1", "R1", "r", "claude_cli", "correctness",
    )
    for reply in (worker, reviewer):
        assert reply["timeout_enforced"] is False
        for key, value in _stall().items():
            assert reply[key] == value
