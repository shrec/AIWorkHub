from pathlib import Path

import pytest

from aiworkhub import repo_policy
from aiworkhub import workspace_hygiene as hygiene
from aiworkhub.repository_state import bootstrap_repository


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    bootstrap_repository(root, repo_name="repo")
    return root


def test_pool_is_repository_isolated_and_outside_source_tree(tmp_path):
    repo_a = _repo(tmp_path / "a")
    repo_b = _repo(tmp_path / "b")
    scratch = tmp_path / "scratch"
    assert hygiene.repository_pool(repo_a, scratch_root=scratch) != hygiene.repository_pool(
        repo_b, scratch_root=scratch
    )
    with pytest.raises(hygiene.WorkspaceHygieneError):
        hygiene.repository_pool(repo_a, scratch_root=repo_a / "cache")


def test_allocate_release_and_real_size_accounting(tmp_path):
    repo = _repo(tmp_path)
    scratch = tmp_path / "scratch"
    fingerprint = hygiene.compute_fingerprint(repo, toolchain="cmake")
    allocated = hygiene.allocate(
        repo, owner="worker", task_id="T1", fingerprint=fingerprint,
        reserved_bytes=1024, scratch_root=scratch,
    )
    Path(allocated["path"]).joinpath("artifact.bin").write_bytes(b"x" * 64)
    current = hygiene.inventory(repo, scratch_root=scratch)
    assert current["slot_count"] == 1
    assert current["effective_bytes"] >= 1024
    released = hygiene.release(
        repo, allocated["slot_id"], allocated["lease_token"], scratch_root=scratch,
    )
    assert released["size_bytes"] == 64
    with pytest.raises(hygiene.WorkspaceHygieneError):
        hygiene.release(repo, allocated["slot_id"], "wrong", scratch_root=scratch)


def test_quota_admission_never_touches_live_slot(tmp_path):
    repo = _repo(tmp_path)
    scratch = tmp_path / "scratch"
    fingerprint = hygiene.compute_fingerprint(repo)
    hygiene.allocate(
        repo, owner="one", task_id="T1", fingerprint=fingerprint,
        reserved_bytes=90, quota_bytes=100, max_slots=2, scratch_root=scratch,
    )
    with pytest.raises(hygiene.WorkspaceHygieneError, match="quota_exhausted"):
        hygiene.allocate(
            repo, owner="two", task_id="T2", fingerprint={**fingerprint, "combined_hash": "b" * 64},
            reserved_bytes=20, quota_bytes=100, max_slots=2, scratch_root=scratch,
        )
    assert hygiene.inventory(repo, scratch_root=scratch)["slot_count"] == 1


def test_cleanup_is_digest_bound_write_gated_and_never_deletes_rogue_repo_dir(
    tmp_path, monkeypatch,
):
    repo = _repo(tmp_path)
    scratch = tmp_path / "scratch"
    rogue = repo / "build-debug"
    rogue.mkdir()
    (rogue / "keep.txt").write_text("keep", encoding="utf-8")
    fingerprint = hygiene.compute_fingerprint(repo)
    allocated = hygiene.allocate(
        repo, owner="worker", task_id="T1", fingerprint=fingerprint,
        ttl_seconds=1, scratch_root=scratch,
    )
    hygiene.release(repo, allocated["slot_id"], allocated["lease_token"], scratch_root=scratch)
    preview = hygiene.cleanup_preview(repo, scratch_root=scratch, now=10**12)
    assert preview["candidates"]
    assert preview["rogue_build_dirs"][0]["path"] == "build-debug"
    with pytest.raises(hygiene.WorkspaceHygieneError, match="write_gate_closed"):
        hygiene.apply_cleanup(
            repo, preview_digest=preview["preview_digest"], confirm=True,
            scratch_root=scratch, now=10**12,
        )
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    applied = hygiene.apply_cleanup(
        repo, preview_digest=preview["preview_digest"], confirm=True,
        scratch_root=scratch, now=10**12,
    )
    assert applied["removed"] and rogue.exists()


def test_cli_inventory_is_bounded_json(tmp_path, capsys):
    repo = _repo(tmp_path)
    scratch = tmp_path / "scratch"
    assert hygiene.main([
        "--repo", str(repo), "--scratch-root", str(scratch), "inventory",
    ]) == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["slot_count"] == 0


def test_preflight_surfaces_bounded_workspace_hygiene_summary(tmp_path):
    repo = _repo(tmp_path)
    report = repo_policy.build_preflight(repo)
    workspace = report["workspace_hygiene"]
    assert workspace["ok"] is True
    assert workspace["slot_count"] == 0
    assert workspace["explicit_preview_required"] is True
    assert "pool" not in workspace
