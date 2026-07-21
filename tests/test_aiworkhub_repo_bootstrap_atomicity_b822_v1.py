"""Atomicity tests for .aiworkhub bootstrap --- b822_v1.

Verifies that the manifest is always the final atomic seal written after all
directory and .gitignore artifacts are ready, so an interrupted first attach
cannot leave a valid-looking partial layout.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiworkhub import repository_state as rs


def _git_init(path: Path) -> None:
    path.mkdir(parents=True)
    (path / ".git").mkdir()


class TestBootstrapAtomicSeal:
    """Manifest must be the final atomic write after all other artifacts."""

    # ------------------------------------------------------------------
    # partial-directory safety
    # ------------------------------------------------------------------

    def test_partial_dirs_without_manifest_retry_cleanly(self, tmp_path: Path) -> None:
        """Orphaned dirs (no manifest) --- bootstrap completes the layout."""
        repo = tmp_path / "repo"
        _git_init(repo)
        hub = repo / rs.HUB_DIRNAME
        hub.mkdir()
        for sub in (*rs.DURABLE_LAYOUT.values(), rs.RUNTIME_DIRNAME):
            (hub / sub).mkdir(parents=True, exist_ok=True)
        assert not (repo / rs.PROJECT_MANIFEST_REL).exists()

        state = rs.bootstrap_repository(repo, repo_id="repo_retry_safe")
        assert state.manifest.repo_id == "repo_retry_safe"
        assert (repo / rs.PROJECT_MANIFEST_REL).exists()
        # all durable + runtime dirs still present
        for sub in (*rs.DURABLE_LAYOUT.values(), rs.RUNTIME_DIRNAME):
            assert (hub / sub).is_dir()

    # ------------------------------------------------------------------
    # crash-during-manifest-seal
    # ------------------------------------------------------------------

    def test_crash_during_manifest_seal_leaves_no_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """os.replace failure on the manifest write --- no manifest, clean retry."""
        repo = tmp_path / "repo"
        _git_init(repo)

        real_replace = os.replace
        call_count = [0]

        def fail_second_replace(src: str, dst: str) -> None:
            call_count[0] += 1
            # first call = .gitignore write; second = manifest seal
            if call_count[0] >= 2:
                raise OSError("injected crash during manifest seal")
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", fail_second_replace)
        with pytest.raises(OSError, match="injected crash"):
            rs.bootstrap_repository(repo, repo_id="repo_crash_seal")

        # manifest must NOT exist --- the seal was never applied
        assert not (repo / rs.PROJECT_MANIFEST_REL).exists()
        # no orphaned tmp files in hub
        assert not list((repo / rs.HUB_DIRNAME).glob(".project.json.*.tmp"))

        # retry succeeds --- no valid manifest was ever published
        monkeypatch.undo()
        state = rs.bootstrap_repository(repo, repo_id="repo_crash_seal")
        assert state.manifest.repo_id == "repo_crash_seal"

    # ------------------------------------------------------------------
    # crash during .gitignore write (before manifest)
    # ------------------------------------------------------------------

    def test_crash_during_gitignore_before_manifest_is_safe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Crash during .gitignore write --- no manifest, safe retry."""
        repo = tmp_path / "repo"
        _git_init(repo)

        def boom(_src: str, _dst: str) -> None:
            raise OSError("injected crash during gitignore")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            rs.bootstrap_repository(repo, repo_id="repo_crash_gitignore")

        assert not (repo / rs.PROJECT_MANIFEST_REL).exists()
        monkeypatch.undo()
        state = rs.bootstrap_repository(repo, repo_id="repo_retry_ok")
        assert state.manifest.repo_id == "repo_retry_ok"

    # ------------------------------------------------------------------
    # .gitignore-already-exists path
    # ------------------------------------------------------------------

    def test_gitignore_exists_is_not_overwritten(self, tmp_path: Path) -> None:
        """Existing .gitignore is preserved; manifest still sealed last."""
        repo = tmp_path / "repo"
        _git_init(repo)
        runtime_dir = repo / rs.HUB_DIRNAME / rs.RUNTIME_DIRNAME
        runtime_dir.mkdir(parents=True)
        (runtime_dir / ".gitignore").write_text("existing\n", encoding="utf-8")

        state = rs.bootstrap_repository(repo, repo_id="repo_gitignore_keep")
        assert (runtime_dir / ".gitignore").read_text(encoding="utf-8") == "existing\n"
        assert state.manifest.repo_id == "repo_gitignore_keep"

    # ------------------------------------------------------------------
    # idempotency: existing repos unchanged
    # ------------------------------------------------------------------

    def test_existing_repos_are_fully_idempotent(self, tmp_path: Path) -> None:
        """Re-bootstrap on bootstrapped repo --- ManifestExistsError, zero mutation."""
        repo = tmp_path / "repo"
        _git_init(repo)
        first = rs.bootstrap_repository(repo, repo_id="repo_idem")
        manifest_bytes = (repo / rs.PROJECT_MANIFEST_REL).read_bytes()

        with pytest.raises(rs.ManifestExistsError):
            rs.bootstrap_repository(repo, repo_id="repo_idem")

        # byte-identical manifest, same repo_id
        assert (repo / rs.PROJECT_MANIFEST_REL).read_bytes() == manifest_bytes
        second = rs.inspect_repository(repo)
        assert second.manifest.repo_id == first.manifest.repo_id
