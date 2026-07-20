"""Focused tests for allow_empty_required_outputs (B536).

Contract:
- Default behavior unchanged: zero-byte required output still fails.
- ``allow_empty_required_outputs`` is a non-empty list of exact repo-relative
  paths (no globs, directories, traversal, NUL, absolute).
- Validate before launch; snapshot into isolated request metadata.
- Only exact allowlisted zero-byte regular files pass.
- Missing, symlink, non-file, undeclared-zero and unchanged-baseline remain
  rejected.
- Permitted empty outputs appear in required-output audit records with
  bytes=0 and hash.
- Metadata round-trip uses only snapshotted values.
- Mutable-card-after-launch is ignored (the metadata wins).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from geoai_task_mcp import process_launcher, worker_workspace
from geoai_task_mcp.process_launcher import (
    LaunchRejected,
    _validate_allow_empty_required_outputs,
    _validate_required_outputs_contract,
)
from geoai_task_mcp.worker_workspace import (
    WorkerWorkspace,
    WorkspaceError,
    validate_required_outputs,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(
    tmp_path: Path,
    *,
    allowed_writes: tuple[str, ...] = ("out/*.jsonl",),
    parent_baseline: dict | None = None,
    workspace_baseline: dict | None = None,
) -> WorkerWorkspace:
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    return WorkerWorkspace(
        request_id="b536",
        repo=tmp_path,
        path=tmp_path,
        home=tmp_path / "home",
        allowed_writes=allowed_writes,
        parent_baseline=parent_baseline or {},
        workspace_baseline=workspace_baseline or {},
    )


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# ---------------------------------------------------------------------------
# 1. Legacy zero rejection (no allow_empty at all)
# ---------------------------------------------------------------------------


def test_legacy_zero_rejection_without_allow_empty(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    _write(ws.path / "out/a.jsonl", b"")
    with pytest.raises(WorkspaceError, match="required_output_zero_bytes:out/a.jsonl"):
        validate_required_outputs(ws, ["out/a.jsonl"])


def test_legacy_zero_rejection_with_empty_allow_empty(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    _write(ws.path / "out/a.jsonl", b"")
    with pytest.raises(WorkspaceError, match="required_output_zero_bytes:out/a.jsonl"):
        validate_required_outputs(ws, ["out/a.jsonl"], allow_empty=())


def test_legacy_zero_rejection_with_none_allow_empty(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    _write(ws.path / "out/a.jsonl", b"")
    with pytest.raises(WorkspaceError, match="required_output_zero_bytes:out/a.jsonl"):
        validate_required_outputs(ws, ["out/a.jsonl"], allow_empty=None)


# ---------------------------------------------------------------------------
# 2. Exact-path zero acceptance
# ---------------------------------------------------------------------------


def test_exact_path_zero_acceptance(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    _write(ws.path / "out/empty.jsonl", b"")
    records = validate_required_outputs(
        ws, ["out/empty.jsonl"], allow_empty=("out/empty.jsonl",)
    )
    assert len(records) == 1
    assert records[0]["path"] == "out/empty.jsonl"
    assert records[0]["bytes"] == 0
    assert "sha256" in records[0]
    assert records[0]["sha256"] is not None
    assert records[0]["sha256"].startswith("file:")


def test_allowlisted_zero_path_still_rejected_if_unchanged(tmp_path: Path) -> None:
    _write(tmp_path / "out/empty.jsonl", b"")
    baseline_hash = worker_workspace._hash_path(tmp_path / "out/empty.jsonl")
    ws = _make_workspace(
        tmp_path,
        workspace_baseline={"out/empty.jsonl": baseline_hash},
    )
    _write(ws.path / "out/empty.jsonl", b"")  # unchanged
    with pytest.raises(WorkspaceError, match="required_output_unchanged"):
        validate_required_outputs(
            ws, ["out/empty.jsonl"], allow_empty=("out/empty.jsonl",)
        )


def test_allowlisted_zero_via_glob_pattern(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    _write(ws.path / "out/valid.jsonl", b'{"k":"v"}\n')
    _write(ws.path / "out/empty.jsonl", b"")
    records = validate_required_outputs(
        ws, ["out/*.jsonl"], allow_empty=("out/empty.jsonl",)
    )
    assert len(records) == 2
    paths = {r["path"]: r["bytes"] for r in records}
    assert paths["out/empty.jsonl"] == 0
    assert paths["out/valid.jsonl"] > 0


# ---------------------------------------------------------------------------
# 3. Mixed required outputs
# ---------------------------------------------------------------------------


def test_mixed_zero_and_nonzero_outputs(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    _write(ws.path / "out/a.jsonl", b"x")
    _write(ws.path / "out/b.jsonl", b"")
    records = validate_required_outputs(
        ws, ["out/a.jsonl", "out/b.jsonl"], allow_empty=("out/b.jsonl",)
    )
    assert len(records) == 2
    assert {r["path"] for r in records} == {"out/a.jsonl", "out/b.jsonl"}
    for r in records:
        if r["path"] == "out/b.jsonl":
            assert r["bytes"] == 0
        else:
            assert r["bytes"] > 0


def test_undeclared_zero_in_mixed_output_rejected(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    _write(ws.path / "out/a.jsonl", b"x")
    _write(ws.path / "out/b.jsonl", b"")  # zero but not allowlisted
    with pytest.raises(WorkspaceError, match="required_output_zero_bytes:out/b.jsonl"):
        validate_required_outputs(ws, ["out/a.jsonl", "out/b.jsonl"])


# ---------------------------------------------------------------------------
# 4. Malformed / glob / traversal / absolute declarations
# ---------------------------------------------------------------------------


def test_allow_empty_glob_rejected() -> None:
    with pytest.raises(LaunchRejected, match="allow_empty_required_outputs_invalid"):
        _validate_allow_empty_required_outputs(
            {"allow_empty_required_outputs": ["out/*.jsonl"]},
            required_outputs=["out/*.jsonl"],
            allowed_writes=["out/*.jsonl"],
        )


def test_allow_empty_traversal_rejected() -> None:
    with pytest.raises(LaunchRejected, match="allow_empty_required_outputs_invalid"):
        _validate_allow_empty_required_outputs(
            {"allow_empty_required_outputs": ["../escape.jsonl"]},
            required_outputs=["../escape.jsonl"],
            allowed_writes=["../escape.jsonl"],
        )


def test_allow_empty_absolute_rejected() -> None:
    with pytest.raises(LaunchRejected, match="allow_empty_required_outputs_invalid"):
        _validate_allow_empty_required_outputs(
            {"allow_empty_required_outputs": ["/etc/passwd"]},
            required_outputs=["/etc/passwd"],
            allowed_writes=["/etc/passwd"],
        )


def test_allow_empty_nul_byte_rejected() -> None:
    with pytest.raises(LaunchRejected, match="allow_empty_required_outputs_invalid"):
        _validate_allow_empty_required_outputs(
            {"allow_empty_required_outputs": ["out/\x00bad.jsonl"]},
            required_outputs=["out/\x00bad.jsonl"],
            allowed_writes=["out/\x00bad.jsonl"],
        )


def test_allow_empty_empty_string_rejected() -> None:
    with pytest.raises(LaunchRejected, match="allow_empty_required_outputs_invalid"):
        _validate_allow_empty_required_outputs(
            {"allow_empty_required_outputs": ["  "]},
            required_outputs=["  "],
            allowed_writes=["  "],
        )


def test_allow_empty_not_a_list_rejected() -> None:
    with pytest.raises(LaunchRejected, match="allow_empty_required_outputs_invalid"):
        _validate_allow_empty_required_outputs(
            {"allow_empty_required_outputs": "out/x.jsonl"},
            required_outputs=["out/x.jsonl"],
            allowed_writes=["out/x.jsonl"],
        )


def test_allow_empty_empty_list_rejected() -> None:
    with pytest.raises(LaunchRejected, match="allow_empty_required_outputs_invalid"):
        _validate_allow_empty_required_outputs(
            {"allow_empty_required_outputs": []},
            required_outputs=["out/x.jsonl"],
            allowed_writes=["out/x.jsonl"],
        )


# ---------------------------------------------------------------------------
# 5. Non-subset declarations
# ---------------------------------------------------------------------------


def test_allow_empty_not_in_required_outputs_rejected() -> None:
    with pytest.raises(LaunchRejected, match="allow_empty_not_in_required_outputs"):
        _validate_allow_empty_required_outputs(
            {"allow_empty_required_outputs": ["out/other.jsonl"]},
            required_outputs=["out/data.jsonl"],
            allowed_writes=["out/*.jsonl"],
        )


def test_allow_empty_not_in_allowed_writes_rejected() -> None:
    with pytest.raises(LaunchRejected, match="allow_empty_not_in_allowed_writes"):
        _validate_allow_empty_required_outputs(
            {"allow_empty_required_outputs": ["out/data.jsonl"]},
            required_outputs=["out/data.jsonl"],
            allowed_writes=["other/*.jsonl"],
        )


def test_allow_empty_in_required_outputs_via_glob() -> None:
    """Should pass: out/data.jsonl fnmatches out/*.jsonl."""
    _validate_allow_empty_required_outputs(
        {"allow_empty_required_outputs": ["out/data.jsonl"]},
        required_outputs=["out/*.jsonl"],
        allowed_writes=["out/*.jsonl"],
    )


# ---------------------------------------------------------------------------
# 6. Symlink / missing / directory rejection
# ---------------------------------------------------------------------------


def test_allowlisted_path_still_rejects_symlink(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    real = ws.path / "out/real.jsonl"
    _write(real, b"data")
    link = ws.path / "out/empty.jsonl"
    link.symlink_to(real)
    # _require_beneath catches the symlink component first, before the
    # explicit is_symlink() check; either rejection is correct fail-closed.
    with pytest.raises(WorkspaceError, match="symlink"):
        validate_required_outputs(
            ws, ["out/empty.jsonl"], allow_empty=("out/empty.jsonl",)
        )


def test_allowlisted_path_still_rejects_missing(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    with pytest.raises(WorkspaceError, match="required_output_missing"):
        validate_required_outputs(
            ws, ["out/empty.jsonl"], allow_empty=("out/empty.jsonl",)
        )


def test_allowlisted_path_still_rejects_directory(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    (ws.path / "out/empty.jsonl").mkdir()
    with pytest.raises(WorkspaceError, match="required_output_missing"):
        validate_required_outputs(
            ws, ["out/empty.jsonl"], allow_empty=("out/empty.jsonl",)
        )


# ---------------------------------------------------------------------------
# 7. Metadata round-trip
# ---------------------------------------------------------------------------


def test_metadata_snapshots_allow_empty(tmp_path: Path) -> None:
    """Simulate the launch-metadata path: card -> metadata -> validate."""
    card = {
        "task_id": "TASK_B536",
        "runner": "test_runner",
        "topic": "test",
        "allowed_writes": ["out/*.jsonl"],
        "required_outputs": ["out/*.jsonl"],
        "allow_empty_required_outputs": ["out/empty.jsonl"],
        "validation": [],
    }
    _validate_required_outputs_contract(card)

    metadata = {
        "required_outputs": list(card.get("required_outputs") or []),
        "allow_empty_required_outputs": list(
            card.get("allow_empty_required_outputs") or []
        ),
    }
    assert metadata["allow_empty_required_outputs"] == ["out/empty.jsonl"]

    ws = _make_workspace(tmp_path)
    _write(ws.path / "out/empty.jsonl", b"")
    _write(ws.path / "out/data.jsonl", b'{"x":1}\n')
    records = validate_required_outputs(
        ws,
        metadata["required_outputs"],
        allow_empty=tuple(metadata["allow_empty_required_outputs"]),
    )
    assert len(records) == 2
    zero_rec = next(r for r in records if r["path"] == "out/empty.jsonl")
    assert zero_rec["bytes"] == 0
    assert zero_rec["sha256"] is not None


# ---------------------------------------------------------------------------
# 8. Mutable-card-after-launch isolation
# ---------------------------------------------------------------------------


def test_mutable_card_ignored_metadata_wins(tmp_path: Path) -> None:
    """Prove that the mutable card is not re-read during finalize."""
    orig_card = {
        "task_id": "TASK_B536",
        "runner": "test_runner",
        "topic": "test",
        "allowed_writes": ["out/*.jsonl"],
        "required_outputs": ["out/a.jsonl", "out/b.jsonl"],
        "allow_empty_required_outputs": ["out/a.jsonl"],
        "validation": [],
    }
    _validate_required_outputs_contract(orig_card)

    metadata = {
        "required_outputs": list(orig_card.get("required_outputs") or []),
        "allow_empty_required_outputs": list(
            orig_card.get("allow_empty_required_outputs") or []
        ),
    }
    mutated_card = dict(orig_card)
    mutated_card["allow_empty_required_outputs"] = []
    with pytest.raises(
        LaunchRejected, match="allow_empty_required_outputs_invalid"
    ):
        _validate_required_outputs_contract(mutated_card)

    ws = _make_workspace(tmp_path)
    _write(ws.path / "out/a.jsonl", b"")
    _write(ws.path / "out/b.jsonl", b"x")
    records = validate_required_outputs(
        ws,
        metadata["required_outputs"],
        allow_empty=tuple(metadata["allow_empty_required_outputs"]),
    )
    assert len(records) == 2
    assert any(r["path"] == "out/a.jsonl" and r["bytes"] == 0 for r in records)


# ---------------------------------------------------------------------------
# 9. Contract validation: None / absent allow_empty are silently OK
# ---------------------------------------------------------------------------


def test_allow_empty_none_is_valid() -> None:
    _validate_required_outputs_contract({
        "task_id": "T",
        "runner": "r",
        "topic": "t",
        "allowed_writes": ["out/*.jsonl"],
        "required_outputs": ["out/a.jsonl"],
    })


def test_allow_empty_absent_is_valid() -> None:
    card = {
        "task_id": "T",
        "runner": "r",
        "topic": "t",
        "allowed_writes": ["out/*.jsonl"],
        "required_outputs": ["out/a.jsonl"],
    }
    _validate_required_outputs_contract(card)
    assert "allow_empty_required_outputs" not in card


def test_allow_empty_without_required_outputs_rejected() -> None:
    with pytest.raises(
        LaunchRejected,
        match="allow_empty_required_outputs_requires_required_outputs",
    ):
        _validate_required_outputs_contract({
            "task_id": "T",
            "runner": "r",
            "topic": "t",
            "allowed_writes": ["out/*.jsonl"],
            "allow_empty_required_outputs": ["out/empty.jsonl"],
        })


# ---------------------------------------------------------------------------
# 10. Non-zero allowlisted file passes normally
# ---------------------------------------------------------------------------


def test_allowlisted_path_with_nonzero_content_passes(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    _write(ws.path / "out/empty.jsonl", b"not empty")
    records = validate_required_outputs(
        ws, ["out/empty.jsonl"], allow_empty=("out/empty.jsonl",)
    )
    assert len(records) == 1
    assert records[0]["bytes"] > 0
