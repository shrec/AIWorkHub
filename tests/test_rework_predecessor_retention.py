"""NF-2026-00138 / NF-2026-00246 / NF-2026-01044: rework predecessor retention
+ gate + materialization.

- A timed-out worker's delta is retained as a rework predecessor (the same
  pinning a validation failure receives), so the successor starts from the work
  instead of nothing.
- A rework attempt no longer discards fully-green work over context-tool
  receipts the rework (validation-only replay) path structurally never makes.
- A recorded delta artifact is preferred over a predecessor's live worktree at
  materialization time, since a retention sweep can leave that worktree
  directory in place but emptied (NF-2026-01044).

Exercised through the module-level seams; no ProcessManager is constructed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import process_launcher as pl
from aiworkhub import worker_workspace


class _FakeWorkspace:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.repo = path.parent
        self.allowed_writes = ("delta.py",)
        self.base_oid = "base-oid"
        self.parent_baseline: dict[str, str] = {}

    def as_metadata(self) -> dict[str, Any]:
        return {
            "repo": str(self.repo),
            "path": str(self.path),
            "home": str(self.path.parent / "home"),
            "request_id": "req-rework",
            "allowed_writes": list(self.allowed_writes),
            "parent_baseline": dict(self.parent_baseline),
            "base_oid": self.base_oid,
        }


def _seed(tmp_path: Path) -> _FakeWorkspace:
    (tmp_path / "delta.py").write_text("PARTIAL = 1\n", encoding="utf-8")
    return _FakeWorkspace(tmp_path)


def test_timed_out_delta_is_retained_as_rework_predecessor(tmp_path: Path) -> None:
    workspace = _seed(tmp_path)
    metadata = {"task_id": "T", "runner": "r", "topic": "t"}
    evidence = pl.retained_rework_candidate_evidence(
        "timed_out", workspace, metadata, "req-rework", ["delta.py"], "claimed",
    )
    # The exact bytes a successor can resume from are pinned.
    assert "changed_path_hashes" in evidence
    assert evidence["changed_path_hashes"]["delta.py"]
    assert "workspace" in evidence
    assert evidence["request_identity"]["request_id"] == "req-rework"
    workspace_metadata = evidence["workspace"]
    request_identity = evidence["request_identity"]
    assert workspace_metadata["allowed_writes"] == list(workspace.allowed_writes)
    assert workspace_metadata["allowed_writes"] == request_identity["allowed_writes"]
    assert workspace_metadata["base_oid"] == workspace.base_oid
    assert workspace_metadata["base_oid"] == request_identity["base_oid"]
    assert workspace_metadata["parent_baseline"] == workspace.parent_baseline
    assert workspace_metadata["parent_baseline"] == request_identity["parent_baseline"]
    candidate_authority = evidence["python_candidate_authority"]
    assert workspace_metadata["python_candidate_authority"] == candidate_authority
    assert candidate_authority["sources"] == [
        {
            "path": "delta.py",
            "state": "added",
            "bytes_sha256": evidence["changed_path_hashes"]["delta.py"],
        },
    ]
    assert "timed_out" in pl.DELTA_RETAINING_TERMINAL_STATES
    # NF-2026-01199: the retained evidence now carries the seal derived from
    # the SAME capture as these hashes, so the coordinator can never read a
    # changed_path_hashes/artifact pair that a second read of the worktree
    # split.  This fake metadata carries no claim_epoch, so the seal is
    # refused by name instead of published -- never an inconsistent pair.
    delta = evidence["rework_delta"]
    assert delta["sealed"] is False
    assert delta["reason"].partition(":")[0] in {
        "rework_delta_identity_invalid",
        "rework_delta_capture_failed",
    }


def test_states_without_a_usable_delta_retain_nothing(tmp_path: Path) -> None:
    workspace = _seed(tmp_path)
    metadata = {"task_id": "T", "runner": "r", "topic": "t"}
    # A clean exit or an empty change set retains nothing.
    assert pl.retained_rework_candidate_evidence(
        "exited", workspace, metadata, "req", ["delta.py"], "claimed",
    ) == {}
    assert pl.retained_rework_candidate_evidence(
        "timed_out", workspace, metadata, "req", [], "claimed",
    ) == {}


def _rework_metadata(rework: bool) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "task_id": "T",
        "runner": "r",
        "topic": "t",
        "worker_mcp": {
            "audit_ledger_path": "/runtime/ledger.jsonl",
            "audit_hmac_key_path": "/runtime/key.bin",
        },
        "project_context": {
            "task_context_policy": {"task_type": "code"},
            "sections": [
                {"name": "session_current_state", "requested": True},
                {"name": "ai_memory", "requested": True},
                {"name": "kb", "requested": True},
            ],
        },
    }
    if rework:
        metadata["rework_predecessor"] = {"request_id": "pred-1"}
    return metadata


def test_is_rework_attempt_detection() -> None:
    assert pl._is_rework_attempt({"rework_predecessor": {"request_id": "x"}}) is True
    assert pl._is_rework_attempt({"rework_predecessor": {}}) is False
    assert pl._is_rework_attempt({}) is False


def test_rework_does_not_discard_green_work_over_missing_context_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Source Graph is freshly called this attempt, but the rework path did not
    # re-issue the session/memory/kb context calls the predecessor made.
    verification = {
        "ok": True,
        "reason": "",
        "live_source_graph_calls": 1,
        "successful_call_count_by_tool": {},
        "policy_violations": 0,
        "receipt_conformance": {"status": "pass", "blocking": False, "blockers": []},
    }
    monkeypatch.setattr(
        pl.worker_ai_tools_mcp, "verify_audit_ledger", lambda *a, **k: verification,
    )

    normal = pl._worker_mcp_live_call_gate(_rework_metadata(rework=False), "req-a")
    rework = pl._worker_mcp_live_call_gate(_rework_metadata(rework=True), "req-a")

    # Without the rework marker the missing context calls fail the gate.
    assert normal["satisfied"] is False
    assert set(normal["missing_tools"]) == {"session_current_state", "ai_memory", "kb"}
    # A rework honors the predecessor's receipts instead of discarding the work.
    assert rework["satisfied"] is True
    assert rework["missing_tools"] == []


def test_recovered_rework_gets_fresh_request_but_active_lost_ack_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    predecessor_request_id = "eb03529091744fc49b8ed788916611e3"
    base = {
        "task_id": "NF622-V6",
        "runner": "codex_5.6",
        "topic": "nf622-v6",
        "allowed_writes": ["delta.py"],
        "required_outputs": ["delta.py"],
        "rework_predecessor": {
            "request_id": predecessor_request_id,
            "task_id": "NF622-V6",
            "changed_path_hashes": {"delta.py": "0" * 64},
        },
        "request_id": predecessor_request_id,
    }
    manager = object.__new__(pl.ProcessManager)
    manager.repo = tmp_path
    manager._toolchain_authority = type(
        "Authority",
        (),
        {
            "evaluate": lambda _self, _card: type(
                "Snapshot", (), {"available": True, "missing": []}
            )(),
            "repair": lambda _self, _snapshot: None,
        },
    )()
    manager._collision_guard = lambda **_kwargs: {"returncode": 0}
    monkeypatch.setattr(pl, "_validate_scope", lambda *_args: None)
    monkeypatch.setattr(pl, "_validate_required_outputs_contract", lambda *_args: None)
    monkeypatch.setattr(pl.core, "task_card_path_conflicts", lambda _card: [])
    monkeypatch.setattr(pl.repo_policy, "validate_launch", lambda *_args: {"ok": True})
    monkeypatch.setattr(
        pl._toolchain_authority, "authority_receipt", lambda *_args: {}
    )
    monkeypatch.setattr(pl, "identical_relaunch_refusal", lambda *_args, **_kwargs: "")

    recovered = {
        **base,
        "status": "pending",
        "worker_status": "unclaimed",
        "claimed_by": "",
        "claim_epoch": 7,
    }
    manager._show_task = lambda _task_id: {
        "returncode": 0,
        "stdout": json.dumps(recovered),
    }
    fresh = manager._preflight_card("NF622-V6", "codex_5.6", "nf622-v6", "codex_cli")
    assert fresh["request_id"] != predecessor_request_id
    assert len(fresh["request_id"]) == 32
    assert fresh["rework_predecessor"] == recovered["rework_predecessor"]

    active = {
        **recovered,
        "status": "processing",
        "worker_status": "claimed",
        "claimed_by": "codex_5.6",
        "launch_request_id": fresh["request_id"],
        "request_id": fresh["request_id"],
    }
    manager._show_task = lambda _task_id: {
        "returncode": 0,
        "stdout": json.dumps(active),
    }
    replay = manager._preflight_card(
        "NF622-V6",
        "codex_5.6",
        "nf622-v6",
        "codex_cli",
        reserved_request_id=fresh["request_id"],
    )
    assert replay["request_id"] == fresh["request_id"]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "parent"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Task MCP Tests").returncode == 0
    (root / "out").mkdir()
    (root / "out" / "result.txt").write_bytes(b"result-v1\n")
    assert _git(root, "add", "out/result.txt").returncode == 0
    assert _git(root, "commit", "-qm", "fixture").returncode == 0
    return root


def _rework_predecessor_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
    request_id: str,
    allowed_writes: list[str],
) -> Any:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    return worker_workspace.create_workspace(
        repo, request_id, {"allowed_writes": allowed_writes}, "validation",
    )


def test_rework_predecessor_seeds_from_artifact_when_worktree_left_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path,
) -> None:
    """NF-2026-01044: a Windows retention sweep can empty a predecessor's
    worktree directory without removing it (another process still holds it
    as cwd). A recorded delta artifact must still seed the successor instead
    of blocking on a hash mismatch against the now-empty directory."""
    predecessor = _rework_predecessor_workspace(
        monkeypatch, tmp_path, repo, "predecessor-empty", ["out/result.txt"]
    )
    successor = None
    try:
        candidate = predecessor.path / "out" / "result.txt"
        candidate.write_bytes(b"reviewed candidate\n")
        content = candidate.read_bytes()
        content_hash = hashlib.sha256(content).hexdigest()
        descriptor = worker_workspace.seal_rework_delta_artifact(
            repo,
            "task-empty",
            "predecessor-empty",
            1,
            [("out/result.txt", content)],
            tmp_path / "artifacts",
        )
        predecessor_metadata = predecessor.as_metadata()

        # Simulate the Windows retention race: files are gone but the
        # directory itself is still present (another process holds it open).
        candidate.unlink()
        assert predecessor.path.is_dir()

        successor = worker_workspace.create_workspace(
            repo,
            "successor-empty",
            {
                "allowed_writes": ["out/result.txt"],
                "rework_predecessor": {
                    "schema_id": "aiworkhub.rework_predecessor.v1",
                    "request_id": "predecessor-empty",
                    "task_id": "task-empty",
                    "claim_epoch": 1,
                    "workspace": predecessor_metadata,
                    "changed_path_hashes": {"out/result.txt": content_hash},
                    "delta_artifact": descriptor,
                },
            },
            "validation",
        )
        assert (successor.path / "out" / "result.txt").read_bytes() == content
        assert successor.inherited_rework_paths == ("out/result.txt",)
    finally:
        if successor is not None:
            worker_workspace.cleanup_workspace(repo, successor.path, successor.home)
        worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)


def test_rework_predecessor_seeds_from_artifact_when_worktree_missing_one_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path,
) -> None:
    """A partially emptied worktree -- missing just one expected file -- must
    also fall back to the sealed artifact rather than partially
    materializing from whatever the worktree still happens to hold."""
    predecessor = _rework_predecessor_workspace(
        monkeypatch,
        tmp_path,
        repo,
        "predecessor-partial",
        ["out/result.txt", "out/extra.txt"],
    )
    successor = None
    try:
        result_path = predecessor.path / "out" / "result.txt"
        extra_path = predecessor.path / "out" / "extra.txt"
        result_path.write_bytes(b"reviewed result\n")
        extra_path.write_bytes(b"reviewed extra\n")
        result_content = result_path.read_bytes()
        extra_content = extra_path.read_bytes()
        result_hash = hashlib.sha256(result_content).hexdigest()
        extra_hash = hashlib.sha256(extra_content).hexdigest()
        descriptor = worker_workspace.seal_rework_delta_artifact(
            repo,
            "task-partial",
            "predecessor-partial",
            1,
            [("out/result.txt", result_content), ("out/extra.txt", extra_content)],
            tmp_path / "artifacts",
        )
        predecessor_metadata = predecessor.as_metadata()

        extra_path.unlink()
        assert predecessor.path.is_dir()

        successor = worker_workspace.create_workspace(
            repo,
            "successor-partial",
            {
                "allowed_writes": ["out/result.txt", "out/extra.txt"],
                "rework_predecessor": {
                    "schema_id": "aiworkhub.rework_predecessor.v1",
                    "request_id": "predecessor-partial",
                    "task_id": "task-partial",
                    "claim_epoch": 1,
                    "workspace": predecessor_metadata,
                    "changed_path_hashes": {
                        "out/result.txt": result_hash,
                        "out/extra.txt": extra_hash,
                    },
                    "delta_artifact": descriptor,
                },
            },
            "validation",
        )
        assert (successor.path / "out" / "result.txt").read_bytes() == result_content
        assert (successor.path / "out" / "extra.txt").read_bytes() == extra_content
    finally:
        if successor is not None:
            worker_workspace.cleanup_workspace(repo, successor.path, successor.home)
        worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)


def test_rework_predecessor_without_artifact_still_uses_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path,
) -> None:
    """No delta artifact was ever recorded: materialization is unchanged and
    still reads the live worktree directly, exactly as before."""
    predecessor = _rework_predecessor_workspace(
        monkeypatch, tmp_path, repo, "predecessor-no-artifact", ["out/result.txt"]
    )
    successor = None
    try:
        candidate = predecessor.path / "out" / "result.txt"
        candidate.write_bytes(b"reviewed candidate\n")
        content_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()

        successor = worker_workspace.create_workspace(
            repo,
            "successor-no-artifact",
            {
                "allowed_writes": ["out/result.txt"],
                "rework_predecessor": {
                    "schema_id": "aiworkhub.rework_predecessor.v1",
                    "request_id": "predecessor-no-artifact",
                    "workspace": predecessor.as_metadata(),
                    "changed_path_hashes": {"out/result.txt": content_hash},
                },
            },
            "validation",
        )
        assert (successor.path / "out" / "result.txt").read_text(
            encoding="utf-8"
        ) == "reviewed candidate\n"
        assert successor.inherited_rework_paths == ("out/result.txt",)
    finally:
        if successor is not None:
            worker_workspace.cleanup_workspace(repo, successor.path, successor.home)
        worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)


def test_rework_predecessor_rejects_tampered_artifact_despite_intact_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path,
) -> None:
    """A recorded artifact is preferred even when the worktree is fully
    intact, so a tampered artifact must still block launch -- materialization
    must never silently fall back to the worktree just because it happens to
    be available. Verification is not weakened by adding the fallback."""
    predecessor = _rework_predecessor_workspace(
        monkeypatch, tmp_path, repo, "predecessor-tampered", ["out/result.txt"]
    )
    try:
        candidate = predecessor.path / "out" / "result.txt"
        candidate.write_bytes(b"reviewed candidate\n")
        content = candidate.read_bytes()
        content_hash = hashlib.sha256(content).hexdigest()
        descriptor = worker_workspace.seal_rework_delta_artifact(
            repo,
            "task-tampered",
            "predecessor-tampered",
            1,
            [("out/result.txt", content)],
            tmp_path / "artifacts",
        )
        Path(descriptor["path"]).write_bytes(b"{}")
        assert predecessor.path.is_dir()
        assert candidate.read_bytes() == content

        with pytest.raises(
            worker_workspace.WorkspaceError, match="rework_delta_artifact_tampered"
        ):
            worker_workspace.create_workspace(
                repo,
                "successor-tampered",
                {
                    "allowed_writes": ["out/result.txt"],
                    "rework_predecessor": {
                        "schema_id": "aiworkhub.rework_predecessor.v1",
                        "request_id": "predecessor-tampered",
                        "task_id": "task-tampered",
                        "claim_epoch": 1,
                        "workspace": predecessor.as_metadata(),
                        "changed_path_hashes": {"out/result.txt": content_hash},
                        "delta_artifact": descriptor,
                    },
                },
                "validation",
            )
    finally:
        worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)
