from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from aiworkhub import source_graph
from aiworkhub.repository_state import bootstrap_repository
from aiworkhub.worker_ai_tools_mcp import WorkerToolContext, source_graph_query


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _packet(authority: Path, files: list[dict[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {
        "successor_request_id": "successor-request",
        "successor_task_id": "same-task",
        "predecessor_request_id": "predecessor-request",
        "predecessor_task_id": "same-task",
        "authority_repo": str(authority.resolve()),
        "files": files,
    }
    return {
        **payload,
        "canonical_digest": hashlib.sha256(json.dumps(
            payload, sort_keys=True, ensure_ascii=True,
        ).encode("utf-8")).hexdigest(),
    }


def _ctx(
    authority: Path,
    workspace: Path,
    packet: dict[str, object] | None,
) -> WorkerToolContext:
    return WorkerToolContext(
        task_id="same-task",
        runner="test",
        topic="source_graph_reliability",
        request_id="successor-request",
        repo=workspace,
        authority_repo=authority,
        source_graph_targets=("src/changed.py", "src/deleted.py", "src/stable.py"),
        allowed_writes=("src/changed.py", "src/deleted.py"),
        session_topic="source_graph_reliability",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        rework_overlay_packet=packet,
    )


@pytest.fixture
def overlay_repo(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    authority = tmp_path / "authority"
    workspace = tmp_path / "workspace"
    authority.mkdir()
    workspace.mkdir()
    bootstrap_repository(authority, repo_name="authority")
    bootstrap_repository(workspace, repo_name="workspace")
    _write(authority / "src/changed.py", "def canonical_symbol():\n    return 'old'\n")
    _write(authority / "src/deleted.py", "def deleted_symbol():\n    return True\n")
    _write(authority / "src/stable.py", "def stable_symbol():\n    return True\n")
    source_graph.build_index(authority, incremental=False)

    changed = b"def worktree_symbol():\n    return 'new'\n"
    _write(workspace / "src/changed.py", changed.decode("utf-8"))
    _write(workspace / "src/stable.py", "def stable_symbol():\n    return True\n")
    packet = _packet(authority, [
        {
            "path": "src/changed.py",
            "sha256": hashlib.sha256(changed).hexdigest(),
            "content_base64": base64.b64encode(changed).decode("ascii"),
        },
        {"path": "src/deleted.py", "deleted": True},
    ])
    return authority, workspace, packet


def test_overlay_query_shadows_changed_and_tombstones_deleted(
    overlay_repo: tuple[Path, Path, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, workspace, packet = overlay_repo
    monkeypatch.setattr(
        source_graph,
        "build_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("query built index")),
    )
    ctx = _ctx(authority, workspace, packet)

    body = source_graph_query(
        ctx, mode="body", query="worktree_symbol", target="src/changed.py",
        workflow_stage="rework", compact_replay=False,
    )
    assert body["ok"] is True
    assert body["authority_source"] == "rework_overlay"
    assert body["authority_state"] == "request_scoped_worktree"
    assert "worktree_symbol" in body["content"]
    assert "canonical_symbol" not in body["content"]
    parsed = json.loads(body["content"])
    assert parsed["matches"][0]["provenance"] == "request_scoped_worktree"
    assert parsed["matches"][0]["source_hash"] == hashlib.sha256(
        (workspace / "src/changed.py").read_bytes()
    ).hexdigest()

    deleted = source_graph_query(
        ctx, mode="focus", query="deleted_symbol",
        workflow_stage="rework", compact_replay=False,
    )
    deleted_payload = json.loads(deleted["content"])
    assert deleted["hit_count"] == 0
    assert "src/deleted.py" not in deleted_payload.get("candidate_files", [])


def test_overlay_query_merges_unscoped_focus_deterministically(
    overlay_repo: tuple[Path, Path, dict[str, object]],
) -> None:
    authority, workspace, packet = overlay_repo
    ctx = _ctx(authority, workspace, packet)
    first = source_graph_query(
        ctx, mode="focus", query="worktree_symbol",
        workflow_stage="rework", compact_replay=False,
    )
    second = source_graph_query(
        ctx, mode="focus", query="worktree_symbol",
        workflow_stage="rework", compact_replay=False,
    )
    assert first["hit_count"] >= 1
    assert first["content"] == second["content"]
    payload = json.loads(first["content"])
    assert payload["matches"][0]["file_path"] == "src/changed.py"
    assert payload["overlay"]["deleted_paths"] == ["src/deleted.py"]


def test_empty_or_unchanged_overlay_preserves_canonical_provenance(tmp_path: Path) -> None:
    authority = tmp_path / "authority"
    workspace = tmp_path / "workspace"
    authority.mkdir()
    workspace.mkdir()
    bootstrap_repository(authority, repo_name="authority")
    bootstrap_repository(workspace, repo_name="workspace")
    stable = "def stable_symbol():\n    return True\n"
    _write(authority / "src/stable.py", stable)
    _write(workspace / "src/stable.py", stable)
    source_graph.build_index(authority, incremental=False)

    empty_result = source_graph_query(
        _ctx(authority, workspace, _packet(authority, [])),
        mode="body", query="stable_symbol", target="src/stable.py",
        workflow_stage="rework", compact_replay=False,
    )
    stable_bytes = stable.encode("utf-8")
    unchanged_result = source_graph_query(
        _ctx(authority, workspace, _packet(authority, [{
            "path": "src/stable.py",
            "sha256": hashlib.sha256(stable_bytes).hexdigest(),
            "content_base64": base64.b64encode(stable_bytes).decode("ascii"),
        }])),
        mode="body", query="stable_symbol", target="src/stable.py",
        workflow_stage="rework", compact_replay=False,
    )
    assert empty_result["authority_source"] == "canonical"
    assert unchanged_result["authority_source"] == "canonical"
    assert empty_result["content"] == unchanged_result["content"]


def test_overlay_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    authority = tmp_path / "authority"
    workspace = tmp_path / "workspace"
    authority.mkdir()
    workspace.mkdir()
    bootstrap_repository(authority, repo_name="authority")
    bootstrap_repository(workspace, repo_name="workspace")
    _write(authority / "src/stable.py", "def stable_symbol():\n    return True\n")
    source_graph.build_index(authority, incremental=False)
    packet = _packet(authority, [])
    packet["authority_repo"] = str(tmp_path / "foreign")
    # Context construction normally verifies this packet. This regression checks
    # that the verifier, not the query merger, owns identity admission.
    from aiworkhub.worker_ai_tools_mcp import WorkerToolError, _verify_rework_overlay_packet

    with pytest.raises(WorkerToolError, match="authority_mismatch"):
        _verify_rework_overlay_packet(
            packet, "same-task", "successor-request", "test", authority,
        )
