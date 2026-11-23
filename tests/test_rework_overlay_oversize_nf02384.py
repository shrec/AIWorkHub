"""NF-2026-02384: a rework overlay over the inline size budget.

A reject+relaunch whose predecessor changed one ~11 MB file used to fail with
``rework overlay content exceeds limit`` because every inherited file was
inlined as ``content_base64``.  The packet now carries any file that would push
the inline total past ``MAX_REWORK_OVERLAY_CONTENT_BYTES`` hash-only (path +
sha256), deciding in path order, and the bootstrap consumer refuses by name
when it would need the bytes of a hash-only file that is absent from the tree.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import rework_overlay_packet, worker_workspace
from aiworkhub.worker_ai_tools_mcp import (
    WorkerToolError,
    materialize_rework_overlay_sealed_files,
)

_MIB = 1024 * 1024
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _workspace_packet(repo: Path, entries: list) -> dict:
    return json.loads(worker_workspace.materialize_rework_overlay(
        "successor-request", "rework-task", "predecessor-request", "rework-task",
        repo, entries,
    ))


def _recomputed_digest(packet: dict) -> str:
    payload = {key: value for key, value in packet.items() if key != "canonical_digest"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _oversized_set() -> tuple[bytes, bytes, bytes, list]:
    big = b"b" * (9 * _MIB)
    small_a = b"a = 1\n"
    small_z = b"z = 2\n"
    entries = [
        ("src/z.py", _sha(small_z), small_z),
        ("data/big.bin", _sha(big), big),
        ("src/a.py", _sha(small_a), small_a),
    ]
    return big, small_a, small_z, entries


def test_oversized_inherited_set_packs_big_file_hash_only(tmp_path: Path) -> None:
    big, small_a, small_z, entries = _oversized_set()
    assert len(big) > worker_workspace.MAX_REWORK_OVERLAY_CONTENT_BYTES

    packet = _workspace_packet(tmp_path, entries)

    assert [entry["path"] for entry in packet["files"]] == [
        "data/big.bin", "src/a.py", "src/z.py",
    ]
    files = {entry["path"]: entry for entry in packet["files"]}
    assert files["data/big.bin"] == {"path": "data/big.bin", "sha256": _sha(big)}
    assert base64.b64decode(files["src/a.py"]["content_base64"]) == small_a
    assert base64.b64decode(files["src/z.py"]["content_base64"]) == small_z
    # canonical_digest covers the entries exactly as emitted (hash-only included).
    assert packet["canonical_digest"] == _recomputed_digest(packet)


def test_inline_budget_is_decided_in_path_order_and_is_deterministic(
    tmp_path: Path,
) -> None:
    a, b, c = b"aaaaaa", b"bbbbbb", b"cccc"
    entries = [("a.txt", _sha(a), a), ("b.txt", _sha(b), b), ("c.txt", _sha(c), c)]

    def build(order: list) -> bytes:
        return rework_overlay_packet.materialize_rework_overlay(
            "successor-request", "rework-task", "predecessor-request", "rework-task",
            tmp_path, order,
            request_id_pattern=_REQUEST_ID_RE, max_files=16, max_content_bytes=10,
        )

    forward = build(entries)
    assert build(list(reversed(entries))) == forward
    files = {entry["path"]: entry for entry in json.loads(forward)["files"]}
    assert "content_base64" in files["a.txt"]  # 6 <= 10
    assert files["b.txt"] == {"path": "b.txt", "sha256": _sha(b)}  # 12 > 10
    assert "content_base64" in files["c.txt"]  # 6 + 4 == 10


def test_inline_bytes_are_still_hash_bound(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="rework overlay content hash mismatch: x.txt"):
        _workspace_packet(tmp_path, [("x.txt", _sha(b"other"), b"bytes")])


def test_worker_overlay_with_oversized_inherited_file_writes_a_packet(
    tmp_path: Path,
) -> None:
    big, small_a, _small_z, _entries = _oversized_set()
    worktree = tmp_path / "worktree"
    (worktree / "data").mkdir(parents=True)
    (worktree / "src").mkdir()
    (worktree / "data" / "big.bin").write_bytes(big)
    (worktree / "src" / "a.py").write_bytes(small_a)
    workspace = SimpleNamespace(
        request_id="successor-request",
        repo=tmp_path,
        path=worktree,
        home=tmp_path / "home",
        inherited_rework_paths=("data/big.bin", "src/a.py"),
        workspace_baseline={},
    )
    card = {"rework_predecessor": {
        "request_id": "predecessor-request",
        "task_id": "rework-task",
        "changed_path_hashes": {"data/big.bin": _sha(big), "src/a.py": _sha(small_a)},
    }}
    written: list[tuple[Path, dict]] = []

    path, packet = rework_overlay_packet.materialize_worker_rework_overlay(
        workspace, "rework-task", card, worker_workspace.materialize_rework_overlay,
        lambda target, payload: written.append((target, payload)),
        worker_workspace.WorkspaceError,
        lambda _card, task_id, **_kwargs: task_id,
    )

    assert packet is not None and written == [(path, packet)]
    files = {entry["path"]: entry for entry in packet["files"]}
    assert files["data/big.bin"] == {"path": "data/big.bin", "sha256": _sha(big)}
    assert base64.b64decode(files["src/a.py"]["content_base64"]) == small_a


def test_consumer_refuses_an_absent_hash_only_file_by_name(tmp_path: Path) -> None:
    _big, _small_a, _small_z, entries = _oversized_set()
    packet = _workspace_packet(tmp_path, entries)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with pytest.raises(
        WorkerToolError, match="rework_overlay_hash_only_missing:data/big.bin"
    ):
        materialize_rework_overlay_sealed_files(worktree, packet)
    # Nothing is fabricated for the missing file.
    assert not (worktree / "data").exists()


def test_consumer_hash_checks_a_present_hash_only_file(tmp_path: Path) -> None:
    big, small_a, small_z, entries = _oversized_set()
    packet = _workspace_packet(tmp_path, entries)
    worktree = tmp_path / "worktree"
    (worktree / "data").mkdir(parents=True)
    (worktree / "data" / "big.bin").write_bytes(big)

    assert materialize_rework_overlay_sealed_files(worktree, packet) == [
        "src/a.py", "src/z.py",
    ]
    assert (worktree / "data" / "big.bin").read_bytes() == big
    assert (worktree / "src" / "a.py").read_bytes() == small_a
    assert (worktree / "src" / "z.py").read_bytes() == small_z

    # A present file whose digest differs is refused outside allowed_writes ...
    (worktree / "data" / "big.bin").write_bytes(b"tampered")
    with pytest.raises(WorkerToolError, match="rework_overlay_hash_mismatch:data/big.bin"):
        materialize_rework_overlay_sealed_files(worktree, packet)
    # ... and admitted as the worker's own edit inside allowed_writes.
    assert materialize_rework_overlay_sealed_files(
        worktree, packet, allowed_writes=("data/big.bin",)
    ) == []
