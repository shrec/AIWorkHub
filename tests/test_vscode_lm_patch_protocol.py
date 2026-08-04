from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aiworkhub import vscode_lm_bridge, vscode_lm_worker


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
        "schema_id": vscode_lm_bridge.EDIT_RESPONSE_SCHEMA_ID,
        "summary": "patch",
        "edits": edits or [],
        "creates": creates or [],
    }


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
    target.write_text("old\n", encoding="utf-8")

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
    target.write_text(current, encoding="utf-8")

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
    target.write_text("existing\n", encoding="utf-8")

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
    target.write_text("unexpected\n", encoding="utf-8")

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
    target.write_text(current, encoding="utf-8")
    existing = workspace / "docs" / "existing.md"
    existing.parent.mkdir()
    existing.write_text("keep\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="create_exists"):
        vscode_lm_worker.run(spec)
    assert target.read_text(encoding="utf-8") == current
    assert not (workspace / "docs" / "good.md").exists()
    assert existing.read_text(encoding="utf-8") == "keep\n"


def test_v2_replaces_large_file_and_creates_root_file(tmp_path: Path) -> None:
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
    target.write_text(content, encoding="utf-8")

    result = vscode_lm_worker.run(spec)

    assert result["changed_paths"] == ["root.txt", "src/app.py"]
    updated = target.read_text(encoding="utf-8")
    assert "needle" not in updated
    assert "replacement\n" in updated
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
