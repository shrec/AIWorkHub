from __future__ import annotations

import hashlib
from types import SimpleNamespace

from aiworkhub import process_launcher, worker_workspace
from aiworkhub import successful_rework_recovery as recovery

REQUEST_ID = "c" * 32
TASK_ID = "NF01865"
CLAIM_EPOCH = 4


def _workspace(tmp_path, baseline_bytes):
    root = tmp_path / "worktree"
    root.mkdir()
    for relative, data in baseline_bytes.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    baseline = {relative: worker_workspace._hash_path(root / relative) for relative in baseline_bytes}
    return SimpleNamespace(repo=tmp_path, path=root, parent_baseline=baseline)


def _metadata(predecessor_paths):
    hashes = {path: hashlib.sha256(path.encode()).hexdigest() for path in predecessor_paths}
    return {"task_id": TASK_ID, "claim_epoch": CLAIM_EPOCH, "rework_predecessor": {"changed_path_hashes": hashes}}


def _stub_seal(monkeypatch):
    def seal(workspace, metadata, request_id, paths, captured_entries=None):
        return {
            "sealed": True, "schema_id": "aiworkhub.rework_delta_descriptor.v1", "request_id": request_id,
            "task_id": metadata["task_id"], "claim_epoch": metadata["claim_epoch"],
        }

    monkeypatch.setattr(process_launcher, "_terminal_rework_delta_evidence", seal)


def test_inherited_path_restored_to_canonical_is_dropped_from_candidate(tmp_path, monkeypatch):
    _stub_seal(monkeypatch)
    workspace = _workspace(tmp_path, {"src/reverted.py": b"canonical\n", "src/kept.py": b"canonical kept\n"})
    (workspace.path / "src/kept.py").write_bytes(b"predecessor bytes\n")
    paths, hashes, _ = recovery.successful_candidate_evidence(
        workspace, _metadata(["src/reverted.py", "src/kept.py"]), REQUEST_ID, [],
    )
    assert paths == ["src/kept.py"]
    assert set(hashes) == {"src/kept.py"}


def test_changed_path_is_kept_even_when_it_matches_canonical(tmp_path, monkeypatch):
    _stub_seal(monkeypatch)
    workspace = _workspace(tmp_path, {"src/reverted.py": b"canonical\n"})
    paths, _, _ = recovery.successful_candidate_evidence(
        workspace, _metadata(["src/reverted.py"]), REQUEST_ID, ["src/reverted.py"],
    )
    assert paths == ["src/reverted.py"]


def test_inherited_path_without_canonical_baseline_entry_is_kept(tmp_path, monkeypatch):
    _stub_seal(monkeypatch)
    workspace = _workspace(tmp_path, {})
    (workspace.path / "src").mkdir()
    (workspace.path / "src/new.py").write_bytes(b"new\n")
    paths, _, _ = recovery.successful_candidate_evidence(
        workspace, _metadata(["src/new.py"]), REQUEST_ID, [],
    )
    assert paths == ["src/new.py"]
