"""NF-2026-02165: a regular-file base-drift conflict no longer blocks rework relaunch."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import promotion_merge  # noqa: E402
from aiworkhub import rework_overlay_packet  # noqa: E402
from aiworkhub import worker_workspace  # noqa: E402

RESULT = "out/result.txt"
OTHER = "out/other.txt"
NOTICE_NAME = "rework_base_drift_conflict.json"
_LINES = tuple(f"line-{index}\n" for index in range(1, 9))


def _bytes(**replaced: str) -> bytes:
    lines = list(_LINES)
    for key, value in replaced.items():
        lines[int(key.removeprefix("line")) - 1] = value
    return "".join(lines).encode("utf-8")


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


def _commit(repo: Path, files: dict[str, bytes], message: str, mode: str = "100644") -> None:
    for relative, content in files.items():
        if mode == "100644":
            (repo / relative).write_bytes(content)
            assert _git(repo, "add", relative).returncode == 0
        else:  # plumbing commits a symlink without needing symlink privilege
            (source := repo.parent / "drift-blob").write_bytes(content)
            oid = _git(repo, "hash-object", "-w", str(source)).stdout.strip()
            cacheinfo = f"{mode},{oid},{relative}"
            assert _git(repo, "update-index", "--cacheinfo", cacheinfo).returncode == 0
    assert _git(repo, "commit", "-q", "-m", message).returncode == 0


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    root = tmp_path / "parent"
    (root / "out").mkdir(parents=True)
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Rework Drift Tests").returncode == 0
    # LF checkout and plain-file 120000 blobs on every host.
    assert _git(root, "config", "core.autocrlf", "false").returncode == 0
    assert _git(root, "config", "core.symlinks", "false").returncode == 0
    _commit(root, {RESULT: _bytes(), OTHER: _bytes()}, "base")
    return root


def _seed(
    repo: Path,
    tmp_path: Path,
    candidates: dict[str, bytes],
    canonical: dict[str, bytes],
    *,
    sealed: bool = False,
    canonical_mode: str = "100644",
) -> worker_workspace.WorkerWorkspace:
    scope = {"allowed_writes": [OTHER, RESULT]}
    predecessor = worker_workspace.create_workspace(
        repo, "drift-predecessor", scope, "validation"
    )
    for relative, content in candidates.items():
        (predecessor.path / relative).write_bytes(content)
    rework: dict[str, object] = {
        "schema_id": "aiworkhub.rework_predecessor.v1",
        "request_id": "drift-predecessor",
        "task_id": "task-1",
        "claim_epoch": 1,
        "workspace": predecessor.as_metadata(),
        "changed_path_hashes": {
            relative: hashlib.sha256(content).hexdigest()
            for relative, content in sorted(candidates.items())
        },
    }
    if sealed:
        rework["delta_artifact"] = worker_workspace.seal_rework_delta_artifact(
            repo,
            "task-1",
            "drift-predecessor",
            1,
            sorted(candidates.items()),
            tmp_path / "artifacts",
        )
        worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)
    try:
        _commit(repo, canonical, "canonical drift", canonical_mode)
        return worker_workspace.create_workspace(
            repo,
            "drift-successor",
            {**scope, "rework_predecessor": rework},
            "validation",
        )
    finally:
        if not sealed:
            worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)


def _notice(successor: worker_workspace.WorkerWorkspace) -> dict[str, object]:
    path = successor.home / "task_mcp_worker_runtime" / NOTICE_NAME
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("sealed", [False, True], ids=["retained", "delta_artifact"])
def test_plain_file_conflict_provisions_canonical_bytes_notice_and_diff(
    tmp_path: Path, repo: Path, sealed: bool
) -> None:
    candidates = {
        RESULT: _bytes(line1="predecessor-1\n"),
        OTHER: _bytes(line1="predecessor-1\n"),
    }
    canonical = {
        RESULT: _bytes(line1="canonical-1\n"),  # same line: a true conflict
        OTHER: _bytes(line8="canonical-8\n"),  # disjoint: merges
    }
    successor = _seed(repo, tmp_path, candidates, canonical, sealed=sealed)
    try:
        # The conflicted path holds CURRENT canonical bytes, never markers.
        assert (successor.path / RESULT).read_bytes() == canonical[RESULT]
        # The non-conflicted path merges exactly as before.
        assert (successor.path / OTHER).read_bytes() == _bytes(
            line1="predecessor-1\n", line8="canonical-8\n"
        )
        notice = _notice(successor)
        assert notice["notice"] == f"rework_base_drift_conflict:{RESULT}"
        assert notice["paths"] == [RESULT]
        (entry,) = notice["conflicts"]  # type: ignore[misc]
        assert entry["path"] == RESULT
        diff = entry["predecessor_diff"]
        assert f"--- a/{RESULT}" in diff
        assert "-line-1" in diff and "+predecessor-1" in diff
        assert "canonical-1" not in diff
        # The notice lives in HOME: the scoped worktree gains no extra file.
        assert not [p for p in successor.path.rglob("*") if p.name == NOTICE_NAME]
        # The provisioned tree passes the scope check (raises on a violation).
        assert set(worker_workspace.enforce_scope(successor)) <= {OTHER, RESULT}
        assert RESULT in successor.inherited_rework_paths
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)
    assert (repo / RESULT).read_bytes() == canonical[RESULT]


def test_non_conflicted_drift_writes_no_notice(tmp_path: Path, repo: Path) -> None:
    candidates = {
        RESULT: _bytes(line1="predecessor-1\n"),
        OTHER: _bytes(line1="predecessor-1\n"),
    }
    successor = _seed(repo, tmp_path, candidates, {OTHER: _bytes(line8="canonical-8\n")})
    try:
        assert (successor.path / RESULT).read_bytes() == candidates[RESULT]
        assert (successor.path / OTHER).read_bytes() == _bytes(
            line1="predecessor-1\n", line8="canonical-8\n"
        )
        assert not (successor.home / "task_mcp_worker_runtime" / NOTICE_NAME).exists()
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)


@pytest.mark.parametrize("sealed", [False, True], ids=["retained", "delta_artifact"])
def test_symlink_drift_still_fails_closed_with_unchanged_reason(
    tmp_path: Path, repo: Path, sealed: bool
) -> None:
    candidates = {RESULT: _bytes(line1="predecessor-1\n")}
    with pytest.raises(
        worker_workspace.WorkspaceError, match=f"^rework_base_drift:{RESULT}$"
    ):
        _seed(
            repo,
            tmp_path,
            candidates,
            {RESULT: b"line-1\n"},
            sealed=sealed,
            canonical_mode="120000",
        )
    assert _git(repo, "ls-tree", "HEAD", RESULT).stdout.startswith("120000 ")


def test_modify_delete_conflict_still_fails_closed(tmp_path: Path, repo: Path) -> None:
    """Only a content conflict is soft: theirs-deleted over moved canonical stays hard."""
    predecessor = worker_workspace.create_workspace(
        repo, "drift-predecessor", {"allowed_writes": [RESULT]}, "validation"
    )
    try:
        (predecessor.path / RESULT).unlink()
        rework = {
            "schema_id": "aiworkhub.rework_predecessor.v1",
            "request_id": "drift-predecessor",
            "task_id": "task-1",
            "claim_epoch": 1,
            "workspace": predecessor.as_metadata(),
            "changed_path_hashes": {RESULT: None},
        }
        _commit(repo, {RESULT: _bytes(line8="canonical-8\n")}, "canonical drift")
        with pytest.raises(
            worker_workspace.WorkspaceError, match=f"^rework_base_drift:{RESULT}$"
        ):
            worker_workspace.create_workspace(
                repo,
                "drift-successor",
                {"allowed_writes": [RESULT], "rework_predecessor": rework},
                "validation",
            )
    finally:
        worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)


def test_rebase_scratch_lives_under_the_repo_runtime_root_and_is_removed(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[Path] = []
    real = tempfile.TemporaryDirectory

    def spy(*args: object, **kwargs: object) -> tempfile.TemporaryDirectory:  # type: ignore[type-arg]
        directory = real(*args, **kwargs)  # type: ignore[call-overload]
        created.append(Path(directory.name))
        return directory

    monkeypatch.setattr(promotion_merge.tempfile, "TemporaryDirectory", spy)
    candidates = {RESULT: _bytes(line1="predecessor-1\n")}
    successor = _seed(repo, tmp_path, candidates, {RESULT: _bytes(line1="canonical-1\n")})
    try:
        scratch = [path for path in created if path.name.startswith("aiworkhub-rebase-")]
        assert scratch
        runtime = worker_workspace.configured_runtime_root(repo)
        for path in scratch:
            assert runtime in path.resolve(strict=False).parents
            assert not path.exists()
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)


def test_worker_facing_overlay_packet_carries_conflict_notice_outside_digest(
    tmp_path: Path, repo: Path
) -> None:
    candidates = {RESULT: _bytes(line1="predecessor-1\n")}
    successor = _seed(repo, tmp_path, candidates, {RESULT: _bytes(line1="canonical-1\n")})
    try:
        card = {
            "rework_predecessor": {
                "request_id": "drift-predecessor",
                "task_id": "task-1",
                "changed_path_hashes": {
                    RESULT: hashlib.sha256(candidates[RESULT]).hexdigest()
                },
            }
        }

        def writer(path: Path, packet: dict[str, object]) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(packet), encoding="utf-8")

        path, _packet = rework_overlay_packet.materialize_worker_rework_overlay(
            successor, "task-1", card, worker_workspace.materialize_rework_overlay,
            writer, worker_workspace.WorkspaceError, lambda *_a, **_k: "task-1",
        )
        assert path is not None
        packet = json.loads(path.read_text(encoding="utf-8"))
        notice = packet["base_drift_conflicts"]
        assert notice["notice"] == f"rework_base_drift_conflict:{RESULT}"
        (entry,) = notice["conflicts"]
        assert entry["path"] == RESULT and entry["truncated"] is False
        assert "-line-1" in entry["predecessor_diff"]
        assert "+predecessor-1" in entry["predecessor_diff"]
        # The conflict notice sits outside the digest-bound payload.
        payload = {
            key: packet[key]
            for key in (
                "successor_request_id", "successor_task_id", "predecessor_request_id",
                "predecessor_task_id", "authority_repo", "files",
            )
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        assert packet["canonical_digest"] == digest
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)


def test_notice_marks_diffs_cut_by_the_total_budget() -> None:
    half = promotion_merge._CONFLICT_DIFF_TOTAL_LIMIT * 5 // 8
    notice = promotion_merge.rework_conflict_notice(
        {"a.txt": "a" * half, "b.txt": "b" * half, "c.txt": "c" * 10}
    )
    first, second, third = notice["conflicts"]
    assert first["truncated"] is False and first["predecessor_diff"] == "a" * half
    assert second["truncated"] is True
    assert second["predecessor_diff"].endswith(promotion_merge._DIFF_TRUNCATED)
    assert third["truncated"] is True  # emptied by the budget, still marked
    assert third["predecessor_diff"] == promotion_merge._DIFF_TRUNCATED
    total = sum(len(item["predecessor_diff"]) for item in notice["conflicts"])
    assert total <= promotion_merge._CONFLICT_DIFF_TOTAL_LIMIT + 2 * len(
        promotion_merge._DIFF_TRUNCATED
    )


def test_binary_merge_refusal_stays_a_hard_conflict(tmp_path: Path, repo: Path) -> None:
    candidates = {RESULT: b"\0predecessor\n"}
    with pytest.raises(
        worker_workspace.WorkspaceError, match=f"^rework_base_drift:{RESULT}$"
    ):
        _seed(repo, tmp_path, candidates, {RESULT: b"\0canonical\n"})


class _BlobSource:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.sha256 = hashlib.sha256(content).hexdigest()
        self.size = len(content)

    def copy_into(self, sink: object) -> tuple[str, int]:
        sink.write(self._content)  # type: ignore[attr-defined]
        return self.sha256, self.size


def test_blob_carried_path_still_fails_closed_in_the_delta_artifact_lane(
    tmp_path: Path, repo: Path
) -> None:
    scope = {"allowed_writes": [RESULT]}
    predecessor = worker_workspace.create_workspace(
        repo, "drift-predecessor", scope, "validation"
    )
    content = _bytes(line1="predecessor-1\n")
    (predecessor.path / RESULT).write_bytes(content)
    rework = {
        "schema_id": "aiworkhub.rework_predecessor.v1",
        "request_id": "drift-predecessor",
        "task_id": "task-1",
        "claim_epoch": 1,
        "workspace": predecessor.as_metadata(),
        "changed_path_hashes": {RESULT: hashlib.sha256(content).hexdigest()},
        "delta_artifact": worker_workspace.seal_rework_delta_artifact(
            repo, "task-1", "drift-predecessor", 1,
            [(RESULT, _BlobSource(content))], tmp_path / "artifacts",
        ),
    }
    worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)
    _commit(repo, {RESULT: _bytes(line1="canonical-1\n")}, "canonical drift")
    with pytest.raises(
        worker_workspace.WorkspaceError,
        match=f"^rework_delta_blob_rebase_conflict:{RESULT}$",
    ):
        worker_workspace.create_workspace(
            repo, "drift-successor", {**scope, "rework_predecessor": rework}, "validation"
        )
