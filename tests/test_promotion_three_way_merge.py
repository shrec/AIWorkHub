"""Promotion three-way merge tests (NF-2026-01381 part A)."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
from aiworkhub import promotion_merge  # noqa: E402
from aiworkhub import worker_workspace as ww  # noqa: E402

BASE_TEXT = b"alpha\nbravo\ncharlie\ndelta\necho\nfoxtrot\n"
BASE_OTHER = b"other-base\n"


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
    (root / "other.txt").write_bytes(BASE_OTHER)
    _git(root, "add", "shared.txt", "other.txt")
    _git(root, "commit", "-qm", "base")
    return root


def _make_workspace(
    monkeypatch, tmp_path: Path, repo: Path, name: str
) -> ww.WorkerWorkspace:
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
        repo, name, {"allowed_writes": ["other.txt", "shared.txt"]}, "test"
    )


def test_two_workspaces_disjoint_edits_promote_to_sequential_apply(
    monkeypatch, tmp_path, repo
):
    ws_a = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aA1")
    ws_b = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aB1")
    a_bytes = BASE_TEXT.replace(b"bravo\n", b"bravo_A\n")
    b_bytes = BASE_TEXT.replace(b"delta\n", b"delta_B\n")
    (ws_a.path / "shared.txt").write_bytes(a_bytes)
    (ws_b.path / "shared.txt").write_bytes(b_bytes)

    assert ww.promote(ws_a, ["shared.txt"]) == ["shared.txt"]
    assert (repo / "shared.txt").read_bytes() == a_bytes

    assert promotion_merge.promote_merged(ws_b, ["shared.txt"]) == ["shared.txt"]
    expected = a_bytes.replace(b"delta\n", b"delta_B\n")
    assert (repo / "shared.txt").read_bytes() == expected


def test_overlapping_same_line_edits_conflict_and_write_nothing(
    monkeypatch, tmp_path, repo
):
    ws_a = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aA2")
    ws_b = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aB2")
    a_bytes = BASE_TEXT.replace(b"bravo\n", b"bravo_A\n")
    b_bytes = BASE_TEXT.replace(b"bravo\n", b"bravo_B\n")
    (ws_a.path / "shared.txt").write_bytes(a_bytes)
    (ws_b.path / "shared.txt").write_bytes(b_bytes)
    (ws_b.path / "other.txt").write_bytes(b"other_B\n")
    ww.promote(ws_a, ["shared.txt"])

    with pytest.raises(
        ww.WorkspaceError,
        match=r"promotion_merge_conflict:shared.txt:conflicts=\d+",
    ):
        promotion_merge.promote_merged(ws_b, ["other.txt", "shared.txt"])
    assert (repo / "shared.txt").read_bytes() == a_bytes
    assert (repo / "other.txt").read_bytes() == BASE_OTHER


def test_legacy_workspace_without_blobs_keeps_parent_changed_error(
    monkeypatch, tmp_path, repo
):
    ws_a = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aA3")
    ws_b = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aB3")
    a_bytes = BASE_TEXT.replace(b"bravo\n", b"bravo_A\n")
    b_bytes = BASE_TEXT.replace(b"delta\n", b"delta_B\n")
    (ws_a.path / "shared.txt").write_bytes(a_bytes)
    (ws_b.path / "shared.txt").write_bytes(b_bytes)
    ww.promote(ws_a, ["shared.txt"])
    legacy = replace(ws_b, parent_baseline_blob={})

    with pytest.raises(
        ww.WorkspaceError, match=r"parent_changed_since_launch:shared.txt"
    ):
        promotion_merge.promote_merged(legacy, ["shared.txt"])


def test_binary_parent_keeps_parent_changed_error(monkeypatch, tmp_path, repo):
    ws_a = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aA4")
    ws_b = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aB4")
    a_bytes = BASE_TEXT.replace(b"bravo\n", b"brav\x00o_A\n")
    b_bytes = BASE_TEXT.replace(b"delta\n", b"delta_B\n")
    (ws_a.path / "shared.txt").write_bytes(a_bytes)
    (ws_b.path / "shared.txt").write_bytes(b_bytes)
    ww.promote(ws_a, ["shared.txt"])

    with pytest.raises(
        ww.WorkspaceError, match=r"parent_changed_since_launch:shared.txt"
    ):
        promotion_merge.promote_merged(ws_b, ["shared.txt"])


def test_deleted_candidate_keeps_parent_changed_error(monkeypatch, tmp_path, repo):
    ws_a = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aA5")
    ws_b = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aB5")
    a_bytes = BASE_TEXT.replace(b"bravo\n", b"bravo_A\n")
    (ws_a.path / "shared.txt").write_bytes(a_bytes)
    ww.promote(ws_a, ["shared.txt"])
    (ws_b.path / "shared.txt").unlink()

    with pytest.raises(
        ww.WorkspaceError, match=r"parent_changed_since_launch:shared.txt"
    ):
        promotion_merge.promote_merged(ws_b, ["shared.txt"])


def test_base_blob_digest_mismatch_keeps_parent_changed_error(
    monkeypatch, tmp_path, repo
):
    ws_a = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aA6")
    ws_b = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aB6")
    a_bytes = BASE_TEXT.replace(b"bravo\n", b"bravo_A\n")
    b_bytes = BASE_TEXT.replace(b"delta\n", b"delta_B\n")
    (ws_a.path / "shared.txt").write_bytes(a_bytes)
    (ws_b.path / "shared.txt").write_bytes(b_bytes)
    ww.promote(ws_a, ["shared.txt"])
    tampered = replace(
        ws_b,
        parent_baseline={
            "shared.txt": "file:100644:"
            + hashlib.sha256(b"wrong base").hexdigest()
        },
    )

    with pytest.raises(
        ww.WorkspaceError, match=r"parent_changed_since_launch:shared.txt"
    ):
        promotion_merge.promote_merged(tampered, ["shared.txt"])


def test_metadata_round_trips_parent_baseline_blob(monkeypatch, tmp_path, repo):
    ws = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aMeta")
    assert ws.parent_baseline_blob
    payload = ws.as_metadata()
    assert payload["parent_baseline_blob"] == ws.parent_baseline_blob
    restored = ww.WorkerWorkspace.from_metadata(payload)
    assert restored.parent_baseline_blob == ws.parent_baseline_blob

    legacy_payload = {
        key: value for key, value in payload.items() if key != "parent_baseline_blob"
    }
    legacy = ww.WorkerWorkspace.from_metadata(legacy_payload)
    assert legacy.parent_baseline_blob == {}


def test_combined_validation_workspace_holds_merged_bytes_and_reports_merged_paths(
    monkeypatch, tmp_path, repo
):
    ws_a = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aA7")
    ws_b = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aB7")
    a_bytes = BASE_TEXT.replace(b"bravo\n", b"bravo_A\n")
    b_bytes = BASE_TEXT.replace(b"delta\n", b"delta_B\n")
    (ws_a.path / "shared.txt").write_bytes(a_bytes)
    (ws_b.path / "shared.txt").write_bytes(b_bytes)
    ww.promote(ws_a, ["shared.txt"])

    combined, report = ww.create_combined_validation_workspace(
        ws_b, {"allowed_writes": ["other.txt", "shared.txt"]}, ["shared.txt"]
    )
    try:
        expected = a_bytes.replace(b"delta\n", b"delta_B\n")
        assert (combined.path / "shared.txt").read_bytes() == expected
        assert report["merged_paths"] == {
            "shared.txt": hashlib.sha256(expected).hexdigest()
        }
        assert promotion_merge.promote_merged(
            ws_b, ["shared.txt"], merged_hashes=report["merged_paths"]
        ) == ["shared.txt"]
        assert (repo / "shared.txt").read_bytes() == expected
    finally:
        ww.cleanup_workspace(repo, combined.path, combined.home)


def test_merged_hashes_mismatch_raises_promotion_merge_changed_since_validation(
    monkeypatch, tmp_path, repo
):
    ws_a = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aA8")
    ws_b = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aB8")
    a_bytes = BASE_TEXT.replace(b"bravo\n", b"bravo_A\n")
    b_bytes = BASE_TEXT.replace(b"delta\n", b"delta_B\n")
    (ws_a.path / "shared.txt").write_bytes(a_bytes)
    (ws_b.path / "shared.txt").write_bytes(b_bytes)
    ww.promote(ws_a, ["shared.txt"])

    with pytest.raises(
        ww.WorkspaceError, match="promotion_merge_changed_since_validation"
    ):
        promotion_merge.promote_merged(
            ws_b,
            ["shared.txt"],
            merged_hashes={
                "shared.txt": hashlib.sha256(b"stale merge").hexdigest()
            },
        )
    assert (repo / "shared.txt").read_bytes() == a_bytes


def test_canonical_write_after_merge_fails_closed_without_clobbering(
    monkeypatch, tmp_path, repo
):
    ws_a = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aA9")
    ws_b = _make_workspace(monkeypatch, tmp_path, repo, "nf1381aB9")
    a_bytes = BASE_TEXT.replace(b"bravo\n", b"bravo_A\n")
    (ws_a.path / "shared.txt").write_bytes(a_bytes)
    (ws_b.path / "shared.txt").write_bytes(BASE_TEXT.replace(b"delta\n", b"delta_B\n"))
    ww.promote(ws_a, ["shared.txt"])
    concurrent = a_bytes.replace(b"alpha", b"alpha_C")
    real_plan = promotion_merge.plan_merges

    def racing_plan(workspace, changed, observed=None):
        merges = real_plan(workspace, changed, observed)
        (repo / "shared.txt").write_bytes(concurrent)
        return merges

    monkeypatch.setattr(promotion_merge, "plan_merges", racing_plan)
    with pytest.raises(
        ww.WorkspaceError, match="promotion_merge_parent_changed:shared.txt"
    ):
        promotion_merge.promote_merged(ws_b, ["shared.txt"])
    assert (repo / "shared.txt").read_bytes() == concurrent


def test_promotion_merge_errors_map_to_promotion_conflict():
    from aiworkhub.process_launcher_validation import terminal_state_for_workspace_error

    for error in (
        "promotion_merge_conflict:a:conflicts=1",
        "promotion_merge_changed_since_validation",
        "promotion_merge_parent_changed:a",
    ):
        assert terminal_state_for_workspace_error(ww.WorkspaceError(error)) == "promotion_conflict"


def test_merge_runs_outside_an_unreadable_caller_repository(monkeypatch, tmp_path, repo):
    # NF-2026-01401: inside the AppContainer the validation cwd is a worker
    # worktree whose gitdir the sandbox cannot read; git merge-file died with 128.
    ws_a = _make_workspace(monkeypatch, tmp_path, repo, "nf1401A1")
    ws_b = _make_workspace(monkeypatch, tmp_path, repo, "nf1401B1")
    a_bytes = BASE_TEXT.replace(b"bravo\n", b"bravo_A\n")
    (ws_a.path / "shared.txt").write_bytes(a_bytes)
    (ws_b.path / "shared.txt").write_bytes(BASE_TEXT.replace(b"delta\n", b"delta_B\n"))
    ww.promote(ws_a, ["shared.txt"])
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / ".git").write_text(f"gitdir: {tmp_path / 'missing' / '.git'}\n", encoding="utf-8")
    monkeypatch.chdir(caller)

    assert promotion_merge.promote_merged(ws_b, ["shared.txt"]) == ["shared.txt"]
    assert (repo / "shared.txt").read_bytes() == a_bytes.replace(b"delta\n", b"delta_B\n")


def test_git_die_exit_is_never_a_conflict_count(monkeypatch, tmp_path):
    for code in (128, 129, 255):
        monkeypatch.setattr(
            promotion_merge.subprocess,
            "run",
            lambda argv, *a, _code=code, **k: subprocess.CompletedProcess(argv, _code, b"", b""),
        )
        assert promotion_merge._merge_file(tmp_path, tmp_path / "p", b"", tmp_path / "c") == (
            None,
            b"",
        )
