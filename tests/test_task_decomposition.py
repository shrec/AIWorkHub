from __future__ import annotations

import hashlib
import json

import pytest

from aiworkhub import task_decomposition


def _receipt(root, *, mode="impact", content=None):
    payload = content or {
        "affected_files": ["src/a.py", "src/b.py"],
        "symbols": [{"qualname": "pkg.a.run"}],
    }
    serialized = json.dumps(payload, sort_keys=True)
    return {
        "ok": True,
        "tool": "source_graph",
        "mode": mode,
        "authority_repo": str(root.resolve()),
        "authority_source": "canonical",
        "content": serialized,
        "content_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "index_revision": "revision-1",
        "hit_count": 2,
    }


def _children():
    return [
        {
            "task_id": "child-a",
            "title": "Change A",
            "objective": "Change the exact A boundary.",
            "allowed_writes": ["src/a.py"],
            "required_outputs": ["src/a.py"],
            "depends_on": [],
            "evidence_refs": ["src/a.py"],
        },
        {
            "task_id": "child-b",
            "title": "Change B",
            "objective": "Change the exact B boundary after A.",
            "allowed_writes": ["src/b.py"],
            "required_outputs": ["src/b.py"],
            "depends_on": ["child-a"],
            "evidence_refs": ["src/b.py"],
        },
    ]


def test_build_proposal_is_grounded_deterministic_and_non_mutating(tmp_path):
    first = task_decomposition.build_proposal(
        tmp_path,
        parent_task_id="parent",
        objective="Split one verified change.",
        source_graph_receipt=_receipt(tmp_path),
        children=_children(),
    )
    second = task_decomposition.build_proposal(
        tmp_path,
        parent_task_id="parent",
        objective="Split one verified change.",
        source_graph_receipt=_receipt(tmp_path),
        children=_children(),
    )

    assert first["proposal_digest"] == second["proposal_digest"]
    assert first["manager_approval_required"] is True
    assert first["approval_boundary"] == {
        "boundary_id": "aiworkhub_rm17_decomposition_approval_boundary",
        "action_class": "low_confidence_large_decomposition",
    }
    assert first["task_creation_performed"] is False
    assert first["task_launch_performed"] is False
    assert first["parallel_child_count"] == 1
    assert first["layers"] == [
        {"index": 0, "task_ids": ["child-a"]},
        {"index": 1, "task_ids": ["child-b"]},
    ]


def test_approval_action_class_registry_is_exact_and_stable():
    assert task_decomposition.APPROVAL_ACTION_CLASSES == (
        "architecture_broad_refactor",
        "dependency_toolchain_change",
        "destructive_storage",
        "security_sensitive_change",
        "release_promotion",
        "low_confidence_large_decomposition",
    )


@pytest.mark.parametrize("action_class", [None, "", "release", "unknown"])
def test_approval_boundary_fails_closed_for_omitted_or_unknown_class(action_class):
    reason = (
        "approval_action_class_required"
        if action_class is None
        else "unknown_approval_action_class"
    )
    with pytest.raises(task_decomposition.TaskDecompositionError, match=f"^{reason}$"):
        task_decomposition.approval_boundary(action_class)


def test_approval_boundary_identity_participates_in_digest(tmp_path, monkeypatch):
    original = task_decomposition.APPROVAL_BOUNDARY_ID
    first = task_decomposition.build_proposal(
        tmp_path,
        parent_task_id="parent",
        objective="Split one verified change.",
        source_graph_receipt=_receipt(tmp_path),
        children=_children(),
    )
    monkeypatch.setattr(
        task_decomposition, "APPROVAL_BOUNDARY_ID", f"{original}.changed"
    )
    second = task_decomposition.build_proposal(
        tmp_path,
        parent_task_id="parent",
        objective="Split one verified change.",
        source_graph_receipt=_receipt(tmp_path),
        children=_children(),
    )

    assert first["approval_boundary"] != second["approval_boundary"]
    assert first["proposal_digest"] != second["proposal_digest"]


def test_build_proposal_rejects_tampered_source_graph_receipt(tmp_path):
    receipt = _receipt(tmp_path)
    receipt["content"] += " "

    with pytest.raises(
        task_decomposition.TaskDecompositionError,
        match="source_graph_content_hash_mismatch",
    ):
        task_decomposition.build_proposal(
            tmp_path,
            parent_task_id="parent",
            objective="Split one verified change.",
            source_graph_receipt=receipt,
            children=_children(),
        )


def test_build_proposal_rejects_unverified_child_evidence(tmp_path):
    children = _children()
    children[0]["evidence_refs"] = ["src/not-in-receipt.py"]

    with pytest.raises(
        task_decomposition.TaskDecompositionError,
        match="child_source_graph_evidence_unverified",
    ):
        task_decomposition.build_proposal(
            tmp_path,
            parent_task_id="parent",
            objective="Split one verified change.",
            source_graph_receipt=_receipt(tmp_path),
            children=children,
        )


def test_build_proposal_rejects_write_collision(tmp_path):
    children = _children()
    children[1]["allowed_writes"] = ["src/a.py"]
    children[1]["required_outputs"] = ["src/a.py"]

    with pytest.raises(
        task_decomposition.TaskDecompositionError,
        match="child_write_scope_collision",
    ):
        task_decomposition.build_proposal(
            tmp_path,
            parent_task_id="parent",
            objective="Split one verified change.",
            source_graph_receipt=_receipt(tmp_path),
            children=children,
        )


def test_build_proposal_rejects_cycle_and_external_dependency(tmp_path):
    cyclic = _children()
    cyclic[0]["depends_on"] = ["child-b"]
    with pytest.raises(
        task_decomposition.TaskDecompositionError,
        match="child_dag_invalid:dependency_cycle_detected",
    ):
        task_decomposition.build_proposal(
            tmp_path,
            parent_task_id="parent",
            objective="Split one verified change.",
            source_graph_receipt=_receipt(tmp_path),
            children=cyclic,
        )

    external = _children()
    external[1]["depends_on"] = ["outside"]
    with pytest.raises(
        task_decomposition.TaskDecompositionError,
        match="child_dependency_outside_proposal",
    ):
        task_decomposition.build_proposal(
            tmp_path,
            parent_task_id="parent",
            objective="Split one verified change.",
            source_graph_receipt=_receipt(tmp_path),
            children=external,
        )
