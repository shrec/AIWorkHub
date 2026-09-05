from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from aiworkhub.worker_workspace import (
    cleanup_workspace,
    create_combined_validation_workspace,
    create_workspace,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_combined_tree_contains_current_canonical_delta_and_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "AIWorkHub Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    (repo / "shared.txt").write_text("old\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")

    worktree_root = tmp_path / "worktrees"
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(worktree_root))
    card = {
        "allowed_writes": ["shared.txt", "feature.txt"],
        "read_first": [],
        "immutable_inputs": [],
        "required_outputs": [],
    }
    candidate = create_workspace(repo, "candidate_request", card, "validation")
    try:
        (candidate.path / "shared.txt").write_text("candidate\n", encoding="utf-8")
        (candidate.path / "feature.txt").write_text("new\n", encoding="utf-8")
        (repo / "base.txt").write_text("concurrent canonical\n", encoding="utf-8")

        combined, evidence = create_combined_validation_workspace(
            candidate,
            card,
            ["shared.txt", "feature.txt"],
        )
        try:
            assert (combined.path / "base.txt").read_text(encoding="utf-8") == (
                "concurrent canonical\n"
            )
            assert (combined.path / "shared.txt").read_text(encoding="utf-8") == (
                "candidate\n"
            )
            assert (combined.path / "feature.txt").read_text(encoding="utf-8") == "new\n"
            assert evidence == {
                "schema_id": "aiworkhub.combined_tree.v1",
                "candidate_paths": ["feature.txt", "shared.txt"],
                "canonical_delta_paths": ["base.txt"],
                "observed_candidate_paths": ["feature.txt", "shared.txt"],
            }
        finally:
            cleanup_workspace(combined.repo, combined.path, combined.home)
    finally:
        cleanup_workspace(candidate.repo, candidate.path, candidate.home)


def test_combined_tree_imports_unchanged_tracked_module_outside_sparse_card(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "AIWorkHub Test")
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    # Unchanged tracked helper module that no card input references.  It is
    # outside the sparse candidate/import closure, so before the full-union fix
    # it was absent from the combined worktree and ``import`` failed with
    # "No module named ..." exactly as the NF554 acceptance reported.
    (pkg / "declared_invariants.py").write_text(
        "VALUE = 'union'\n", encoding="utf-8"
    )
    (pkg / "feature.py").write_text("old = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")

    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    card = {
        "allowed_writes": ["pkg/feature.py"],
        "read_first": [],
        "immutable_inputs": [],
        "required_outputs": [],
    }
    candidate = create_workspace(repo, "candidate_union_request", card, "validation")
    try:
        (candidate.path / "pkg" / "feature.py").write_text(
            "old = 2\n", encoding="utf-8"
        )
        # The unchanged helper is genuinely absent from the sparse candidate
        # tree; the combined workspace must materialize it from the union.
        assert not (candidate.path / "pkg" / "declared_invariants.py").exists()

        combined, evidence = create_combined_validation_workspace(
            candidate,
            card,
            ["pkg/feature.py"],
        )
        try:
            assert (combined.path / "pkg" / "declared_invariants.py").is_file()
            # The unchanged helper is not misreported as a candidate change.
            assert evidence["observed_candidate_paths"] == ["pkg/feature.py"]
            # Real subprocess import from the union, mirroring the canonical
            # ``python -m`` acceptance check that a mock would bypass.
            env = dict(os.environ)
            env["PYTHONPATH"] = str(combined.path)
            proc = subprocess.run(
                [sys.executable, "-c", "import pkg.declared_invariants"],
                cwd=combined.path,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert proc.returncode == 0, proc.stderr
        finally:
            cleanup_workspace(combined.repo, combined.path, combined.home)
    finally:
        cleanup_workspace(candidate.repo, candidate.path, candidate.home)


def test_combined_tree_preserves_current_canonical_deletion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "AIWorkHub Test")
    (repo / "removed.txt").write_text("remove me\n", encoding="utf-8")
    (repo / "candidate.txt").write_text("old\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")

    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    card = {
        "allowed_writes": ["candidate.txt"],
        "read_first": [],
        "immutable_inputs": [],
        "required_outputs": [],
    }
    candidate = create_workspace(repo, "candidate_delete_request", card, "validation")
    try:
        (candidate.path / "candidate.txt").write_text("new\n", encoding="utf-8")
        (repo / "removed.txt").unlink()
        combined, evidence = create_combined_validation_workspace(
            candidate,
            card,
            ["candidate.txt"],
        )
        try:
            assert not (combined.path / "removed.txt").exists()
            assert evidence["canonical_delta_paths"] == ["removed.txt"]
        finally:
            cleanup_workspace(combined.repo, combined.path, combined.home)
    finally:
        cleanup_workspace(candidate.repo, candidate.path, candidate.home)
