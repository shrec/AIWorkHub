"""NF1330 optional new-file authority vs mandatory create outputs.

The publisher keeps ``create_paths`` as the authorized new-file scope while a
separate, trusted ``required_create_paths`` list carries the mandatory-create
obligation.  These tests build a real bridge spool plus private worker spec
(the producer) and then run the actual Python v1/v2/v3 final parsers against
the emitted scope.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import task_store
from aiworkhub import vscode_lm_bridge
from aiworkhub import vscode_lm_worker

EMPTY_FILE_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)


def _valid_newtest() -> str:
    """Complete, non-placeholder required output content."""

    return '\"\"\"NF1330 required new test module.\"\"\"\n\n\ndef test_required_new_file_body() -> None:\n    assert True\n'


def _publish_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    request_id: str,
    required_outputs: list[str] | None,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Build a real spool request plus private worker spec via the publisher."""

    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    parent = tmp_path / request_id
    workspace = parent / "worktree"
    home = parent / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
    (workspace / "mgr.py").write_text("print('mgr')\n", encoding="utf-8")
    request = vscode_lm_bridge.create_request(
        repo=repo,
        request_id=request_id,
        workspace_path=workspace,
        workspace_home=home,
        prompt="NF1330 optional create contract",
        model="glm-5.2",
        allowed_writes=["mgr.py", "helper.py", "newtest.py"],
        workspace_parent_baseline={
            "mgr.py": "file:1:prior",
            "helper.py": None,
            "newtest.py": None,
        },
        required_outputs=required_outputs,
        timeout_seconds=30,
    )
    published = json.loads(request.request_path.read_text(encoding="utf-8"))
    spec = json.loads(request.worker_spec_path.read_text(encoding="utf-8"))
    return workspace, published, spec


def _mgr_v3_edit(workspace: Path) -> dict[str, Any]:
    data = (workspace / "mgr.py").read_bytes()
    return {
        "path": "mgr.py",
        "current_sha256": hashlib.sha256(data).hexdigest(),
        "ranges": [
            {
                "start_line": 1,
                "end_line": 1,
                "new": "print('mgr')\n# NF1330 v3 final\n",
                "preserve_trailing_newline": True,
            }
        ],
    }


def _v1_envelope(*, files: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_id": vscode_lm_worker.EDIT_RESPONSE_SCHEMA_ID_V1,
        "summary": "nf1330 v1 final",
        "files": files,
    }


def _v2_envelope(
    workspace: Path, *, creates: list[dict[str, Any]]
) -> dict[str, Any]:
    data = (workspace / "mgr.py").read_bytes().decode("utf-8")
    return {
        "schema_id": vscode_lm_worker.EDIT_RESPONSE_SCHEMA_ID_V2,
        "summary": "nf1330 v2 final",
        "edits": [
            {
                "path": "mgr.py",
                "current_sha256": hashlib.sha256(data.encode("utf-8")).hexdigest(),
                "replacements": [
                    {
                        "old": data,
                        "new": data + "# NF1330 v2 final\n",
                        "expected_count": 1,
                    }
                ],
            }
        ],
        "creates": creates,
    }


def _v3_envelope(
    workspace: Path, *, creates: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_id": vscode_lm_worker.EDIT_RESPONSE_SCHEMA_ID,
        "summary": "nf1330 v3 final",
        "edits": [_mgr_v3_edit(workspace)],
        "creates": creates,
    }


def _newtest_create() -> dict[str, Any]:
    return {"path": "newtest.py", "content": _valid_newtest()}


def _newtest_file() -> dict[str, Any]:
    return {"path": "newtest.py", "content": _valid_newtest()}


def _mgr_v1_file() -> dict[str, Any]:
    return {"path": "mgr.py", "content": "print('mgr')\n# NF1330 v1 final\n"}


def test_publisher_separates_optional_scope_from_mandatory_creates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, published, spec = _publish_contract(
        tmp_path,
        monkeypatch,
        request_id="1" * 32,
        required_outputs=["mgr.py", "newtest.py"],
    )
    contracts = published["path_contracts"]
    assert workspace.is_dir()
    assert contracts["mgr.py"]["action"] == "edit"
    assert contracts["helper.py"]["action"] == "create"
    assert contracts["newtest.py"]["action"] == "create"
    assert contracts["newtest.py"]["current_sha256"] == ""
    assert spec["create_paths"] == ["helper.py", "newtest.py"]
    assert published["required_create_paths"] == ["newtest.py"]
    assert spec["required_create_paths"] == ["newtest.py"]
    assert published["required_outputs"] == ["mgr.py", "newtest.py"]


def test_legacy_spec_without_required_metadata_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _published, spec = _publish_contract(
        tmp_path,
        monkeypatch,
        request_id="2" * 32,
        required_outputs=None,
    )
    assert "required_create_paths" not in spec
    assert spec["create_paths"] == ["helper.py", "newtest.py"]
    envelope = _v3_envelope(workspace, creates=[_newtest_create()])
    with pytest.raises(RuntimeError, match="missing_required_create:helper.py"):
        vscode_lm_worker._v3_planned_outputs(
            workspace,
            envelope,
            spec["allowed_writes"],
            set(spec["create_paths"]),
        )


def test_explicit_empty_required_outputs_make_optional_omission_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _published, spec = _publish_contract(
        tmp_path,
        monkeypatch,
        request_id="3" * 32,
        required_outputs=[],
    )
    assert spec["required_create_paths"] == []
    envelope = _v3_envelope(workspace, creates=[_newtest_create()])
    planned, _metrics = vscode_lm_worker._v3_planned_outputs(
        workspace,
        envelope,
        spec["allowed_writes"],
        set(spec["create_paths"]),
        set(),
    )
    assert {relative for relative, _content in planned} == {"mgr.py", "newtest.py"}


@pytest.mark.parametrize("protocol", ["v1", "v2", "v3"])
def test_optional_untouched_new_file_may_omit_final_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    workspace, _published, spec = _publish_contract(
        tmp_path,
        monkeypatch,
        request_id="4" * 32,
        required_outputs=["mgr.py", "newtest.py"],
    )
    allowed = spec["allowed_writes"]
    create_scope = set(spec["create_paths"])
    required = set(spec["required_create_paths"])
    if protocol == "v1":
        planned = vscode_lm_worker._v1_planned_outputs(
            workspace,
            _v1_envelope(files=[_mgr_v1_file(), _newtest_file()]),
            allowed,
            create_scope,
            required,
        )
    elif protocol == "v2":
        planned = vscode_lm_worker._v2_planned_outputs(
            workspace,
            _v2_envelope(workspace, creates=[_newtest_create()]),
            allowed,
            create_scope,
            required,
        )
    else:
        planned, _metrics = vscode_lm_worker._v3_planned_outputs(
            workspace,
            _v3_envelope(workspace, creates=[_newtest_create()]),
            allowed,
            create_scope,
            required,
        )
    assert {relative for relative, _content in planned} == {"mgr.py", "newtest.py"}


@pytest.mark.parametrize(
    ("protocol", "blank"),
    [("v1", ""), ("v2", "\t \n"), ("v3", "   ")],
)
def test_chosen_optional_create_blank_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    blank: str,
) -> None:
    workspace, _published, spec = _publish_contract(
        tmp_path,
        monkeypatch,
        request_id="5" * 32,
        required_outputs=["mgr.py", "newtest.py"],
    )
    allowed = spec["allowed_writes"]
    create_scope = set(spec["create_paths"])
    required = set(spec["required_create_paths"])
    if protocol == "v1":
        envelope = _v1_envelope(
            files=[
                _mgr_v1_file(),
                _newtest_file(),
                {"path": "helper.py", "content": blank},
            ]
        )
        planner = lambda: vscode_lm_worker._v1_planned_outputs(
            workspace, envelope, allowed, create_scope, required
        )
    elif protocol == "v2":
        envelope = _v2_envelope(
            workspace,
            creates=[_newtest_create(), {"path": "helper.py", "content": blank}],
        )
        planner = lambda: vscode_lm_worker._v2_planned_outputs(
            workspace, envelope, allowed, create_scope, required
        )
    else:
        envelope = _v3_envelope(
            workspace,
            creates=[_newtest_create(), {"path": "helper.py", "content": blank}],
        )
        planner = lambda: vscode_lm_worker._v3_planned_outputs(
            workspace, envelope, allowed, create_scope, required
        )
    with pytest.raises(RuntimeError, match="empty_required_create"):
        planner()


def test_chosen_optional_create_ellipsis_placeholder_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _published, spec = _publish_contract(
        tmp_path,
        monkeypatch,
        request_id="6" * 32,
        required_outputs=["mgr.py", "newtest.py"],
    )
    envelope = _v3_envelope(
        workspace,
        creates=[_newtest_create(), {"path": "helper.py", "content": "..."}],
    )
    with pytest.raises(RuntimeError, match="ellipsis_only"):
        vscode_lm_worker._v3_planned_outputs(
            workspace,
            envelope,
            spec["allowed_writes"],
            set(spec["create_paths"]),
            set(spec["required_create_paths"]),
        )


@pytest.mark.parametrize("protocol", ["v1", "v2", "v3"])
def test_required_create_omission_still_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    workspace, _published, spec = _publish_contract(
        tmp_path,
        monkeypatch,
        request_id="7" * 32,
        required_outputs=["mgr.py", "newtest.py"],
    )
    allowed = spec["allowed_writes"]
    create_scope = set(spec["create_paths"])
    required = set(spec["required_create_paths"])
    if protocol == "v1":
        envelope = _v1_envelope(files=[_mgr_v1_file()])
        planner = lambda: vscode_lm_worker._v1_planned_outputs(
            workspace, envelope, allowed, create_scope, required
        )
    elif protocol == "v2":
        envelope = _v2_envelope(workspace, creates=[])
        planner = lambda: vscode_lm_worker._v2_planned_outputs(
            workspace, envelope, allowed, create_scope, required
        )
    else:
        envelope = _v3_envelope(workspace, creates=[])
        planner = lambda: vscode_lm_worker._v3_planned_outputs(
            workspace, envelope, allowed, create_scope, required
        )
    with pytest.raises(RuntimeError, match="missing_required_create:newtest.py"):
        planner()


@pytest.mark.parametrize("protocol", ["v1", "v2", "v3"])
def test_required_create_empty_still_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    workspace, _published, spec = _publish_contract(
        tmp_path,
        monkeypatch,
        request_id="8" * 32,
        required_outputs=["mgr.py", "newtest.py"],
    )
    allowed = spec["allowed_writes"]
    create_scope = set(spec["create_paths"])
    required = set(spec["required_create_paths"])
    if protocol == "v1":
        envelope = _v1_envelope(
            files=[_mgr_v1_file(), {"path": "newtest.py", "content": ""}]
        )
        planner = lambda: vscode_lm_worker._v1_planned_outputs(
            workspace, envelope, allowed, create_scope, required
        )
    elif protocol == "v2":
        envelope = _v2_envelope(
            workspace, creates=[{"path": "newtest.py", "content": "\t"}]
        )
        planner = lambda: vscode_lm_worker._v2_planned_outputs(
            workspace, envelope, allowed, create_scope, required
        )
    else:
        envelope = _v3_envelope(
            workspace, creates=[{"path": "newtest.py", "content": "  "}]
        )
        planner = lambda: vscode_lm_worker._v3_planned_outputs(
            workspace, envelope, allowed, create_scope, required
        )
    with pytest.raises(RuntimeError, match="empty_required_create"):
        planner()


def test_required_create_metadata_disallowed_path_denied() -> None:
    with pytest.raises(RuntimeError):
        vscode_lm_worker._required_create_paths(
            {"newtest.py"},
            ["newtest.py"],
            {"../escape.py"},
        )


def test_v3_preimage_hash_tamper_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _published, spec = _publish_contract(
        tmp_path,
        monkeypatch,
        request_id="9" * 32,
        required_outputs=["mgr.py", "newtest.py"],
    )
    tampered = _v3_envelope(workspace, creates=[_newtest_create()])
    tampered["edits"][0]["current_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="stale_hash"):
        vscode_lm_worker._v3_planned_outputs(
            workspace,
            tampered,
            spec["allowed_writes"],
            set(spec["create_paths"]),
            set(spec["required_create_paths"]),
        )


def test_v3_replay_duplicate_create_identity_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _published, spec = _publish_contract(
        tmp_path,
        monkeypatch,
        request_id="a" * 32,
        required_outputs=["mgr.py", "newtest.py"],
    )
    envelope = _v3_envelope(
        workspace, creates=[_newtest_create(), _newtest_create()]
    )
    with pytest.raises(RuntimeError, match="duplicate_path"):
        vscode_lm_worker._v3_planned_outputs(
            workspace,
            envelope,
            spec["allowed_writes"],
            set(spec["create_paths"]),
            set(spec["required_create_paths"]),
        )


def test_v3_create_wrong_identity_over_existing_file_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _published, spec = _publish_contract(
        tmp_path,
        monkeypatch,
        request_id="b" * 32,
        required_outputs=["mgr.py", "newtest.py"],
    )
    envelope = _v3_envelope(
        workspace,
        creates=[_newtest_create(), {"path": "mgr.py", "content": _valid_newtest()}],
    )
    envelope["edits"] = []
    with pytest.raises(RuntimeError, match="create_exists:mgr.py"):
        vscode_lm_worker._v3_planned_outputs(
            workspace,
            envelope,
            spec["allowed_writes"],
            set(spec["create_paths"]),
            set(spec["required_create_paths"]),
        )
