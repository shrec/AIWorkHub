from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aiworkhub import process_launcher, task_store, vscode_lm_bridge, vscode_lm_worker, worker_ai_tools_mcp


def _request(
    tmp_path: Path,
    edit: dict[str, object],
    *,
    allowed: list[str] | None = None,
    create_paths: list[str] | None = None,
) -> tuple[Path, Path]:
    workspace = tmp_path / "worktree"
    home = tmp_path / "home"
    workspace.mkdir(parents=True)
    home.mkdir(parents=True)
    request_id = "a" * 32
    response_path = home / "response.json"
    spec_path = home / "spec.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema_id": "aiworkhub.vscode_lm.worker_spec.v1",
                "request_id": request_id,
                "workspace_path": str(workspace),
                "response_path": str(response_path),
                "allowed_writes": allowed
                or ["src/*.py", "docs/*.md", "out/*.txt"],
                "create_paths": create_paths or [],
                "timeout_seconds": 30,
            }
        ),
        encoding="utf-8",
    )
    response_path.write_text(
        json.dumps(
            {
                "schema_id": vscode_lm_bridge.RESPONSE_SCHEMA_ID,
                "request_id": request_id,
                "error": "",
                "text": json.dumps(edit),
            }
        ),
        encoding="utf-8",
    )
    return spec_path, workspace


def _v2(
    *,
    edits: list[dict[str, object]] | None = None,
    creates: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_id": vscode_lm_bridge.EDIT_RESPONSE_SCHEMA_ID_V2,
        "summary": "patch",
        "edits": edits or [],
        "creates": creates or [],
    }


def _v3(
    *,
    edits: list[dict[str, object]] | None = None,
    creates: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_id": vscode_lm_bridge.EDIT_RESPONSE_SCHEMA_ID,
        "summary": "semantic patch",
        "edits": edits or [],
        "creates": creates or [],
    }


def _authenticated_handoff(
    spec_path: Path, relative: str, start: int, end: int, new: str,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    """Real coordinator/session/HMAC apply in synthetic, test-owned storage."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    workspace = Path(spec["workspace_path"])
    home = spec_path.parent
    request_id = spec["request_id"]
    repo = workspace.parent / "authority"
    repo.mkdir()
    task_store.initialize_repository(repo)
    process_dir = repo / ".aiworkhub" / "runtime" / "processes"
    process_dir.mkdir(parents=True)
    ledger, key = home / "audit.jsonl", home / "audit.key"
    ledger.write_text("", encoding="utf-8")
    key.write_bytes(b"synthetic-test-audit-key-32-bytes!")
    metadata_path = process_dir / f"{request_id}.request.json"
    metadata_path.write_text(json.dumps({
        "request_id": request_id, "task_id": "NF1298_PATCH_TEST",
        "runner": "editor_test", "topic": "patch_protocol", "adapter_id": "glm_vscode_lm",
        "workspace": {"path": str(workspace), "home": str(home)},
        "worker_mcp": {
            "authority_repo": str(repo), "source_graph_targets": [relative],
            "allowed_writes": spec["allowed_writes"], "session_topic": "patch_protocol",
            "audit_ledger_path": str(ledger), "audit_hmac_key_path": str(key),
        },
    }), encoding="utf-8")
    manager = process_launcher.ProcessManager(
        repo=repo, process_log_path=repo / "events.jsonl",
        process_dir=process_dir, isolation_enabled=False,
    )
    event = {"request_id": request_id, "adapter_id": "glm_vscode_lm",
             "state": "running", "metadata_path": str(metadata_path)}
    monkeypatch.setattr(manager, "_request_events", lambda rid: [event] if rid == request_id else [])
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    prepared = manager.invoke_vscode_lm_worker_tool(
        request_id, "aiworkhub_worker_semantic_edit_prepare",
        {"file_path": relative, "start_line": start, "end_line": end,
         "provider_call_id": "nf1298.prepare"},
    )
    assert prepared["ok"] is True, prepared
    applied = manager.invoke_vscode_lm_worker_tool(
        request_id, "aiworkhub_worker_semantic_edit_apply",
        {"target_id": prepared["target_id"], "new": new,
         "idempotency_key": "nf1298.apply", "provider_call_id": "nf1298.apply"},
    )
    assert applied["ok"] is True, applied
    assert applied["preimage_verified"] is True
    assert applied["before_sha256"] == prepared["current_sha256"]
    assert applied["after_sha256"] == hashlib.sha256((workspace / relative).read_bytes()).hexdigest()
    verification = worker_ai_tools_mcp.verify_audit_ledger(
        ledger, key, task_id="NF1298_PATCH_TEST", runner="editor_test",
        topic="patch_protocol", request_id=request_id,
    )
    assert verification["ok"] is True, verification
    receipts = verification["semantic_edit_apply_receipts"]
    assert len(receipts) == 1
    assert receipts[0]["path_sha256"] == hashlib.sha256(relative.encode("utf-8")).hexdigest()
    for field in ("file_bytes", "range_count", "old_region_bytes", "replacement_bytes"):
        assert receipts[0][field] == applied[field]
    # Authenticated existing edits are already applied; only creates remain pending.
    response_path = Path(spec["response_path"])
    response = json.loads(response_path.read_text(encoding="utf-8"))
    final = vscode_lm_worker._normalize_staged_final_envelope(json.loads(response["text"]))
    final["edits"] = []
    response["text"] = json.dumps(final)
    response_path.write_text(json.dumps(response), encoding="utf-8")
    return applied


def test_v3_applies_only_bounded_line_range_and_reports_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = "header\ndef old():\n    return 1\nfooter\n"
    spec, workspace = _request(
        tmp_path,
        _v3(edits=[{
            "path": "src/app.py",
            "current_sha256": hashlib.sha256(current.encode()).hexdigest(),
            "ranges": [{"start_line": 2, "end_line": 3, "new": "def new():\n    return 2"}],
        }]),
    )
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_bytes(current.encode("utf-8"))

    with pytest.raises(RuntimeError, match="existing_edit_requires_authenticated_apply"):
        vscode_lm_worker.run(spec)
    assert target.read_text(encoding="utf-8") == current
    metric = _authenticated_handoff(spec, "src/app.py", 2, 3, "def new():\n    return 2\n", monkeypatch)
    result = vscode_lm_worker.run(spec)

    assert target.read_text(encoding="utf-8") == "header\ndef new():\n    return 2\nfooter\n"
    assert result["edit_protocol"] == vscode_lm_bridge.EDIT_RESPONSE_SCHEMA_ID
    assert result["semantic_edit_metrics"] == []
    assert metric["model_reemitted_old_bytes"] == 0
    assert metric["whole_file_output_required"] is False
    assert metric["token_savings_claimed"] is False


def test_v3_can_fill_existing_empty_file_with_virtual_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    spec, workspace = _request(
        tmp_path,
        _v3(edits=[{
            "path": "out/result.txt",
            "current_sha256": empty_sha256,
            "ranges": [{"start_line": 1, "end_line": 1, "new": "created\n"}],
        }]),
    )
    target = workspace / "out" / "result.txt"
    target.parent.mkdir()
    target.write_bytes(b"")

    with pytest.raises(RuntimeError, match="existing_edit_requires_authenticated_apply"):
        vscode_lm_worker.run(spec)
    assert target.read_bytes() == b""
    metric = _authenticated_handoff(spec, "out/result.txt", 1, 1, "created\n", monkeypatch)
    result = vscode_lm_worker.run(spec)

    assert target.read_text(encoding="utf-8") == "created\n"
    assert result["semantic_edit_metrics"] == []
    assert metric["old_region_bytes"] == 0
    assert metric["replacement_bytes"] == len(b"created\n")
    assert metric["whole_file_output_required"] is False


def test_v3_can_fill_required_empty_placeholder_with_virtual_line(
    tmp_path: Path,
) -> None:
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    spec, workspace = _request(
        tmp_path,
        _v3(edits=[{
            "path": "out/result.txt",
            "current_sha256": empty_sha256,
            "ranges": [{"start_line": 1, "end_line": 1, "new": "created\n"}],
        }]),
        create_paths=["out/result.txt"],
    )
    target = workspace / "out" / "result.txt"
    target.parent.mkdir()
    target.write_bytes(b"")

    result = vscode_lm_worker.run(spec)

    assert result["changed_paths"] == ["out/result.txt"]
    assert target.read_text(encoding="utf-8") == "created\n"
    metric = result["semantic_edit_metrics"][0]
    assert metric["old_region_bytes"] == 0
    assert metric["create"] is True
    assert metric["replacement_bytes"] == len(b"created\n")
    assert metric["whole_file_output_required"] is True
    assert metric["token_savings_claimed"] is False


def test_v3_create_replaces_only_declared_empty_workspace_placeholder(
    tmp_path: Path,
) -> None:
    spec, workspace = _request(
        tmp_path,
        _v3(creates=[{"path": "out/result.txt", "content": "created\n"}]),
        create_paths=["out/result.txt"],
    )
    target = workspace / "out" / "result.txt"
    target.parent.mkdir()
    target.write_bytes(b"")

    result = vscode_lm_worker.run(spec)

    assert result["changed_paths"] == ["out/result.txt"]
    assert target.read_text(encoding="utf-8") == "created\n"
    metric = result["semantic_edit_metrics"][0]
    assert metric["create"] is True
    assert metric["replacement_bytes"] == len(b"created\n")
    assert metric["whole_file_output_required"] is True


def test_v3_rejects_nonempty_declared_create_placeholder(tmp_path: Path) -> None:
    current = "unexpected\n"
    spec, workspace = _request(
        tmp_path,
        _v3(edits=[{
            "path": "out/result.txt",
            "current_sha256": hashlib.sha256(current.encode()).hexdigest(),
            "ranges": [{"start_line": 1, "end_line": 1, "new": "created\n"}],
        }]),
        create_paths=["out/result.txt"],
    )
    target = workspace / "out" / "result.txt"
    target.parent.mkdir()
    target.write_text(current, encoding="utf-8")

    with pytest.raises(RuntimeError, match="create_exists"):
        vscode_lm_worker.run(spec)
    assert target.read_text(encoding="utf-8") == current


def test_v3_required_pyi_ellipsis_rejected_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = "def keep():\n    return 1\n"
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    spec, workspace = _request(
        tmp_path,
        _v3(edits=[
            {
                "path": "src/app.py",
                "current_sha256": hashlib.sha256(current.encode()).hexdigest(),
                "ranges": [{
                    "start_line": 1,
                    "end_line": 2,
                    "new": "def keep():\n    return 2\n",
                }],
            },
            {
                "path": "stubs/new.pyi",
                "current_sha256": empty_sha256,
                "ranges": [{"start_line": 1, "end_line": 1, "new": "..."}],
            },
        ]),
        allowed=["src/*.py", "stubs/*.pyi"],
        create_paths=["stubs/new.pyi"],
    )
    existing = workspace / "src" / "app.py"
    existing.parent.mkdir()
    existing.write_bytes(current.encode("utf-8"))
    placeholder = workspace / "stubs" / "new.pyi"
    placeholder.parent.mkdir()
    placeholder.write_bytes(b"")
    changed_paths: list[str] = []
    real_write = vscode_lm_worker._write_atomic

    def recording_write(workspace: Path, relative: str, content: str) -> None:
        changed_paths.append(relative)
        real_write(workspace, relative, content)

    monkeypatch.setattr(vscode_lm_worker, "_write_atomic", recording_write)

    with pytest.raises(RuntimeError, match=r"ellipsis_only:stubs/new\.pyi:v3_create"):
        vscode_lm_worker.run(spec)
    assert changed_paths == []
    assert existing.read_text(encoding="utf-8") == current
    assert placeholder.read_bytes() == b""


def test_v3_rejects_overlap_and_stale_hash_without_mutation(tmp_path: Path) -> None:
    current = "one\ntwo\nthree\n"
    overlap, workspace = _request(
        tmp_path / "overlap",
        _v3(edits=[{
            "path": "src/app.py",
            "current_sha256": hashlib.sha256(current.encode()).hexdigest(),
            "ranges": [
                {"start_line": 1, "end_line": 2, "new": "x"},
                {"start_line": 2, "end_line": 3, "new": "y"},
            ],
        }]),
    )
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_bytes(current.encode("utf-8"))
    with pytest.raises(RuntimeError, match="ranges_overlap"):
        vscode_lm_worker.run(overlap)
    assert target.read_text(encoding="utf-8") == current

    stale, stale_workspace = _request(
        tmp_path / "stale",
        _v3(edits=[{
            "path": "src/app.py",
            "current_sha256": "0" * 64,
            "ranges": [{"start_line": 1, "end_line": 1, "new": "x"}],
        }]),
    )
    stale_target = stale_workspace / "src" / "app.py"
    stale_target.parent.mkdir()
    stale_target.write_bytes(current.encode("utf-8"))
    with pytest.raises(RuntimeError, match="stale_hash"):
        vscode_lm_worker.run(stale)
    assert stale_target.read_text(encoding="utf-8") == current


def _edit(path: str, content: str, *, expected_count: int = 1) -> dict[str, object]:
    return {
        "path": path,
        "current_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "replacements": [
            {"old": "needle", "new": "replacement", "expected_count": expected_count}
        ],
    }


def test_v2_rejects_stale_hash_before_writing(tmp_path: Path) -> None:
    spec, workspace = _request(
        tmp_path,
        _v2(
            edits=[
                {
                    "path": "src/app.py",
                    "current_sha256": "0" * 64,
                    "replacements": [
                        {"old": "old", "new": "new", "expected_count": 1}
                    ],
                }
            ]
        ),
    )
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_bytes(b"old\n")

    with pytest.raises(RuntimeError, match="stale_hash"):
        vscode_lm_worker.run(spec)
    assert target.read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize("expected_count", [1, True])
def test_v2_rejects_ambiguous_or_noninteger_replacement_count(
    tmp_path: Path,
    expected_count: object,
) -> None:
    current = "needle\nneedle\n"
    replacement = {
        "old": "needle",
        "new": "once",
        "expected_count": expected_count,
    }
    spec, workspace = _request(
        tmp_path,
        _v2(
            edits=[
                {
                    "path": "src/app.py",
                    "current_sha256": hashlib.sha256(current.encode()).hexdigest(),
                    "replacements": [replacement],
                }
            ]
        ),
    )
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_bytes(current.encode("utf-8"))

    with pytest.raises(RuntimeError, match="replacement_(count|invalid)") as raised:
        vscode_lm_worker.run(spec)
    error = str(raised.value)
    assert "response_sha256=" in error
    assert "response_bytes=" in error
    if expected_count is not True:
        assert "index=0:actual=2:expected=1" in error
        assert "old_sha256=" in error
        assert "old_bytes=6" in error
    assert target.read_text(encoding="utf-8") == current


def test_v2_rejects_duplicate_and_out_of_scope_paths(tmp_path: Path) -> None:
    duplicate, _ = _request(
        tmp_path / "duplicate",
        _v2(
            creates=[
                {"path": "docs/a.md", "content": "a\n"},
                {"path": "docs/a.md", "content": "b\n"},
            ]
        ),
    )
    with pytest.raises(RuntimeError, match="duplicate_path"):
        vscode_lm_worker.run(duplicate)

    scoped, _ = _request(
        tmp_path / "scoped",
        _v2(creates=[{"path": "secrets/a.md", "content": "bad\n"}]),
    )
    with pytest.raises(RuntimeError, match="out_of_scope"):
        vscode_lm_worker.run(scoped)


def test_v2_create_fails_when_target_exists(tmp_path: Path) -> None:
    spec, workspace = _request(
        tmp_path,
        _v2(creates=[{"path": "docs/new.md", "content": "new\n"}]),
    )
    target = workspace / "docs" / "new.md"
    target.parent.mkdir()
    target.write_bytes(b"existing\n")

    with pytest.raises(RuntimeError, match="create_exists"):
        vscode_lm_worker.run(spec)
    assert target.read_text(encoding="utf-8") == "existing\n"


def test_v2_create_replaces_only_declared_empty_workspace_placeholder(
    tmp_path: Path,
) -> None:
    spec, workspace = _request(
        tmp_path,
        _v2(creates=[{"path": "docs/new.md", "content": "new\n"}]),
        create_paths=["docs/new.md"],
    )
    target = workspace / "docs" / "new.md"
    target.parent.mkdir()
    target.write_bytes(b"")

    result = vscode_lm_worker.run(spec)

    assert result["changed_paths"] == ["docs/new.md"]
    assert target.read_text(encoding="utf-8") == "new\n"


def test_v2_create_rejects_nonempty_declared_placeholder(tmp_path: Path) -> None:
    spec, workspace = _request(
        tmp_path,
        _v2(creates=[{"path": "docs/new.md", "content": "new\n"}]),
        create_paths=["docs/new.md"],
    )
    target = workspace / "docs" / "new.md"
    target.parent.mkdir()
    target.write_bytes(b"unexpected\n")

    with pytest.raises(RuntimeError, match="create_exists"):
        vscode_lm_worker.run(spec)
    assert target.read_text(encoding="utf-8") == "unexpected\n"


def test_v2_validates_every_output_before_first_write(tmp_path: Path) -> None:
    current = "needle\n"
    spec, workspace = _request(
        tmp_path,
        _v2(
            edits=[_edit("src/app.py", current)],
            creates=[
                {"path": "docs/good.md", "content": "good\n"},
                {"path": "docs/existing.md", "content": "bad\n"},
            ],
        ),
    )
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_bytes(current.encode("utf-8"))
    existing = workspace / "docs" / "existing.md"
    existing.parent.mkdir()
    existing.write_bytes(b"keep\n")

    with pytest.raises(RuntimeError, match="create_exists"):
        vscode_lm_worker.run(spec)
    assert target.read_text(encoding="utf-8") == current
    assert not (workspace / "docs" / "good.md").exists()
    assert existing.read_text(encoding="utf-8") == "keep\n"


def test_v2_replaces_large_file_and_creates_root_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = ("prefix\n" * 2000) + "needle\n" + ("suffix\n" * 2000)
    spec, workspace = _request(
        tmp_path,
        _v2(
            edits=[_edit("src/app.py", content)],
            creates=[{"path": "root.txt", "content": "created\n"}],
        ),
        allowed=["src/*.py", "root.txt"],
    )
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_bytes(content.encode("utf-8"))

    with pytest.raises(RuntimeError, match="existing_edit_requires_authenticated_apply"):
        vscode_lm_worker.run(spec)
    assert target.read_text(encoding="utf-8") == content
    assert not (workspace / "root.txt").exists()
    receipt = _authenticated_handoff(spec, "src/app.py", 2001, 2001, "replacement\n", monkeypatch)
    result = vscode_lm_worker.run(spec)

    assert receipt["preimage_verified"] is True
    assert receipt["path"] == "src/app.py"
    assert result["changed_paths"] == ["root.txt"]
    updated = target.read_text(encoding="utf-8")
    assert updated == content.replace("needle\n", "replacement\n", 1)
    assert (workspace / "root.txt").read_text(encoding="utf-8") == "created\n"


def test_v1_full_file_response_remains_accepted(tmp_path: Path) -> None:
    spec, workspace = _request(
        tmp_path,
        {
            "schema_id": vscode_lm_bridge.EDIT_RESPONSE_SCHEMA_ID_V1,
            "summary": "legacy",
            "files": [{"path": "out/result.txt", "content": "legacy\n"}],
        },
        allowed=["out/*.txt"],
    )

    result = vscode_lm_worker.run(spec)

    assert result["changed_paths"] == ["out/result.txt"]
    assert (workspace / "out" / "result.txt").read_text(encoding="utf-8") == (
        "legacy\n"
    )


def test_staged_action_and_string_lines_plan_when_hash_matches(tmp_path: Path) -> None:
    current = "alpha\nbeta\ngamma\n"
    digest = hashlib.sha256(current.encode()).hexdigest()
    spec, workspace = _request(
        tmp_path,
        {
            "schema_id": "aiworkhub.vscode_lm.tool_request.v1",
            "name": "aiworkhub_manager_semantic_edit_stage",
            "input": {
                "action": "edit",
                "file_path": "src/app.py",
                "start_line": "2",
                "end_line": "2",
                "new": "BETA\n",
                "current_sha256": digest,
            },
        },
    )
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_bytes(current.encode("utf-8"))

    response = json.loads(spec.with_name("response.json").read_text(encoding="utf-8"))
    normalized = vscode_lm_worker._normalize_staged_final_envelope(json.loads(response["text"]))
    assert normalized["schema_id"] == vscode_lm_bridge.EDIT_RESPONSE_SCHEMA_ID
    planned, _metrics = vscode_lm_worker._v3_planned_outputs(
        workspace, normalized, ["src/*.py"], set(),
    )
    assert planned == [("src/app.py", "alpha\nBETA\ngamma\n")]
    with pytest.raises(RuntimeError, match="existing_edit_requires_authenticated_apply") as raised:
        vscode_lm_worker.run(spec)
    assert "final_edit_invalid" not in str(raised.value)
    assert target.read_text(encoding="utf-8") == current


def test_flat_stage_inside_v3_edits_is_not_final_edit_invalid(tmp_path: Path) -> None:
    current = "alpha\nbeta\ngamma\n"
    digest = hashlib.sha256(current.encode()).hexdigest()
    spec, workspace = _request(
        tmp_path,
        {
            "schema_id": vscode_lm_bridge.EDIT_RESPONSE_SCHEMA_ID,
            "summary": "one line",
            "edits": [{
                "action": "replace_range",
                "file_path": "src/app.py",
                "start_line": "2",
                "end_line": "2",
                "new": "BETA\n",
                "current_sha256": digest,
            }],
            "creates": [],
        },
    )
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_bytes(current.encode("utf-8"))

    response = json.loads(spec.with_name("response.json").read_text(encoding="utf-8"))
    normalized = vscode_lm_worker._normalize_staged_final_envelope(json.loads(response["text"]))
    planned, _metrics = vscode_lm_worker._v3_planned_outputs(
        workspace, normalized, ["src/*.py"], set(),
    )
    assert planned == [("src/app.py", "alpha\nBETA\ngamma\n")]
    with pytest.raises(RuntimeError, match="existing_edit_requires_authenticated_apply") as raised:
        vscode_lm_worker.run(spec)
    assert "final_edit_invalid" not in str(raised.value)
    assert target.read_text(encoding="utf-8") == current


def test_string_line_stage_still_rejects_overlap_stale_hash_and_bounds(
    tmp_path: Path,
) -> None:
    current = "one\ntwo\nthree\n"
    digest = hashlib.sha256(current.encode()).hexdigest()
    overlap, workspace = _request(
        tmp_path / "overlap",
        _v3(edits=[{
            "action": "replace_range",
            "file_path": "src/app.py",
            "current_sha256": digest,
            "ranges": [
                {"start_line": "1", "end_line": "2", "new": "x"},
                {"start_line": "2", "end_line": "2", "new": "y"},
            ],
        }]),
    )
    target = workspace / "src" / "app.py"
    target.parent.mkdir()
    target.write_bytes(current.encode("utf-8"))
    with pytest.raises(RuntimeError, match="ranges_overlap"):
        vscode_lm_worker.run(overlap)
    assert target.read_text(encoding="utf-8") == current

    stale, stale_workspace = _request(
        tmp_path / "stale",
        {
            "schema_id": vscode_lm_bridge.EDIT_RESPONSE_SCHEMA_ID,
            "summary": "stale stage",
            "edits": [{
                "operation": "replace_range",
                "file_path": "src/app.py",
                "start_line": "1",
                "end_line": "1",
                "new": "x\n",
                "current_sha256": "0" * 64,
            }],
            "creates": [],
        },
    )
    stale_target = stale_workspace / "src" / "app.py"
    stale_target.parent.mkdir()
    stale_target.write_bytes(current.encode("utf-8"))
    with pytest.raises(RuntimeError, match="stale_hash"):
        vscode_lm_worker.run(stale)
    assert stale_target.read_text(encoding="utf-8") == current

    bounds, bounds_workspace = _request(
        tmp_path / "bounds",
        {
            "action": "v3_range",
            "file_path": "src/app.py",
            "start_line": "9",
            "end_line": "9",
            "new": "nope\n",
            "current_sha256": digest,
        },
    )
    bounds_target = bounds_workspace / "src" / "app.py"
    bounds_target.parent.mkdir()
    bounds_target.write_bytes(current.encode("utf-8"))
    with pytest.raises(RuntimeError, match="out_of_bounds"):
        vscode_lm_worker.run(bounds)
    assert bounds_target.read_text(encoding="utf-8") == current
