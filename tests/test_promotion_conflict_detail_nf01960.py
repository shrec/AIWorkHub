"""promotion_conflict names path, expected/observed/candidate sha256 (NF-2026-01960)."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
from aiworkhub import process_launcher_validation as plv  # noqa: E402
from aiworkhub import promotion_conflict_detail as pcd  # noqa: E402
from aiworkhub import promotion_merge  # noqa: E402
from aiworkhub import worker_workspace as ww  # noqa: E402

BASE_TEXT = b"alpha\nbravo\ncharlie\ndelta\necho\nfoxtrot\n"
# Longer than the ~58 characters at which a fixed-width bound once cut tail
# fields, yet short enough for Windows MAX_PATH under pytest's tmp_path.
LONG_PATH = "deep/" + "seg_" * 8 + "/" + "long_name_" * 5 + ".txt"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "shared.txt").write_bytes(BASE_TEXT)
    (root / LONG_PATH).parent.mkdir(parents=True)
    (root / LONG_PATH).write_bytes(BASE_TEXT)
    _git(root, "add", "shared.txt", LONG_PATH)
    _git(root, "commit", "-qm", "base")
    return root


def _make_workspace(monkeypatch, tmp_path: Path, repo: Path, name: str) -> ww.WorkerWorkspace:
    monkeypatch.setattr(ww, "configured_worktree_root", lambda r: tmp_path / "worktrees")
    monkeypatch.setattr(ww, "chmod_path", lambda path, mode: None)
    monkeypatch.setattr(ww, "_refuse_if_promotion_in_flight", lambda r: None)
    monkeypatch.setattr(
        ww,
        "_credential_home",
        lambda home, adapter, r: home.mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(ww, "provision_isolated_task_queue_db", lambda r, home: None)
    monkeypatch.setattr(ww, "seal_worker_contract_packet", lambda workspace, card: None)
    return ww.create_workspace(
        repo, name, {"allowed_writes": ["shared.txt", LONG_PATH]}, "test"
    )


def _assert_detail(text: str, prefix: str, rel: str, expected: str, observed: str, candidate: str) -> None:
    assert text.startswith(f"{prefix}:{rel}")
    assert f":expected_sha256={expected}" in text
    assert f":observed_sha256={observed}" in text
    assert f":candidate_sha256={candidate}" in text
    assert plv.terminal_state_for_workspace_error(ww.WorkspaceError(text)) == "promotion_conflict"


@pytest.mark.parametrize("rel", ["shared.txt", LONG_PATH])
def test_parent_changed_since_launch_names_path_and_three_real_hashes(
    monkeypatch, tmp_path, repo, rel
):
    workspace = _make_workspace(monkeypatch, tmp_path, repo, "nf1960a1")
    workspace = ww.replace(workspace, parent_baseline_blob={})  # no three-way merge
    candidate = BASE_TEXT.replace(b"bravo\n", b"bravo_worker\n")
    manager_edit = BASE_TEXT.replace(b"bravo\n", b"bravo_manager\n")
    (workspace.path / rel).write_bytes(candidate)
    (repo / rel).write_bytes(manager_edit)

    with pytest.raises(ww.WorkspaceError) as caught:
        ww.promote(workspace, [rel])
    text = str(caught.value)
    _assert_detail(
        text, "parent_changed_since_launch", rel,
        _sha(BASE_TEXT), _sha(manager_edit), _sha(candidate),
    )
    # Nothing promoted: canonical keeps the manager's bytes.
    assert (repo / rel).read_bytes() == manager_edit

    # The accept reply carries the full string and the structured entry.
    reply = pcd.promotion_conflict_reply(text)
    assert reply is not None
    assert reply["error"] == text
    assert reply["error_class"] == "promotion_conflict"
    assert reply["promotion_conflicts"] == [
        {
            "path": rel,
            "expected_sha256": _sha(BASE_TEXT),
            "observed_sha256": _sha(manager_edit),
            "candidate_sha256": _sha(candidate),
        }
    ]
    assert reply["promotion_conflicts_truncated"] == 0


def test_parent_changed_during_promotion_names_three_real_hashes(monkeypatch, tmp_path, repo):
    workspace = _make_workspace(monkeypatch, tmp_path, repo, "nf1960b1")
    candidate = BASE_TEXT.replace(b"echo\n", b"echo_worker\n")
    racing = BASE_TEXT.replace(b"echo\n", b"echo_race\n")
    (workspace.path / "shared.txt").write_bytes(candidate)
    original_begin = ww._promotion_begin

    def racing_begin(ws):
        (repo / "shared.txt").write_bytes(racing)
        return original_begin(ws)

    monkeypatch.setattr(ww, "_promotion_begin", racing_begin)
    with pytest.raises(ww.WorkspaceError) as caught:
        ww.promote(workspace, ["shared.txt"])
    text = str(caught.value)
    _assert_detail(
        text, "parent_changed_during_promotion", "shared.txt",
        _sha(BASE_TEXT), _sha(racing), _sha(candidate),
    )
    # Mid-write conflicts are not answered as a pre-write accept refusal.
    assert pcd.promotion_conflict_reply(text) is None


def test_merge_conflict_keeps_conflicts_field_and_adds_three_real_hashes(
    monkeypatch, tmp_path, repo
):
    workspace = _make_workspace(monkeypatch, tmp_path, repo, "nf1960c1")
    candidate = BASE_TEXT.replace(b"charlie\n", b"charlie_A\n")
    canonical = BASE_TEXT.replace(b"charlie\n", b"charlie_B\n")
    (workspace.path / "shared.txt").write_bytes(candidate)
    (repo / "shared.txt").write_bytes(canonical)

    with pytest.raises(
        ww.WorkspaceError, match=r"^promotion_merge_conflict:shared.txt:conflicts=\d+:expected_sha256="
    ) as caught:
        promotion_merge.promote_merged(workspace, ["shared.txt"])
    text = str(caught.value)
    _assert_detail(
        text, "promotion_merge_conflict", "shared.txt",
        _sha(BASE_TEXT), _sha(canonical), _sha(candidate),
    )
    reply = pcd.promotion_conflict_reply(text)
    assert reply is not None
    assert reply["promotion_conflicts"][0]["observed_sha256"] == _sha(canonical)


def test_hash_tokens_are_shape_checked():
    digest = _sha(b"x")
    assert pcd.hash_token(None) == "none"
    assert pcd.hash_token(f"file:644:{digest}") == digest
    assert pcd.hash_token(digest) == digest
    assert pcd.hash_token("file:644:not-a-digest:observed_sha256=0") == "invalid"
    assert re.fullmatch(r"symlink-[0-9a-f]{64}", pcd.hash_token("symlink:target"))
    assert pcd.conflict_detail(None, None, None) == (
        ":expected_sha256=none:observed_sha256=none:candidate_sha256=none"
    )


def test_conflict_entries_are_capped_with_a_truncated_count():
    digest = _sha(b"y")
    errors = [
        f"parent_changed_since_launch:f{i}.txt" + pcd.conflict_detail(None, digest, digest)
        for i in range(25)
    ]
    result = pcd.conflict_entries(errors + ["parent_changed_since_launch:bare"])
    assert len(result["promotion_conflicts"]) == pcd.MAX_CONFLICT_ENTRIES == 20
    assert result["promotion_conflicts_truncated"] == 5
    assert result["promotion_conflicts"][0]["path"] == "f0.txt"


def test_prefix_mapping_is_single_definition():
    assert pcd.PROMOTION_CONFLICT_PREFIXES == (
        "parent_changed",
        "promotion_scope",
        "promotion_merge",
        "promotion_canonical_dirty_at_seed",  # NF-2026-01997
    )
    assert plv.PROMOTION_CONFLICT_PREFIXES is pcd.PROMOTION_CONFLICT_PREFIXES
