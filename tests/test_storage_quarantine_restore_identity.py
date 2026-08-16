"""Quarantine -> restore must be identity-preserving, and already-stranded
worktrees must have an explicit, operator-only recovery path.

The bug this pins: ``quarantine`` moves a worktree directory aside, which leaves
its git registration dangling; git auto-gc (reproduced here with an explicit
``git worktree prune``) then removes that registration. The old ``restore``
moved the directory back but never reinstated the registration, so the worktree
came back on disk attributed to nobody -- counted in the footprint yet outside
every reclamation path, permanently. ``restore`` reported ``{"ok": true}`` all
the same, which is strictly worse than a loud failure.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from aiworkhub import storage_retention, task_store, worktree_storage

# terminal_runs_days defaults to 30; 31 real days pushes a worktree past the
# default policy threshold without ever touching its on-disk mtime (workers run
# under a landlock sandbox that forbids changing filesystem mtimes).
_AGED_NOW_OFFSET_DAYS = 31


def _aged_now() -> float:
    return time.time() + _AGED_NOW_OFFSET_DAYS * 86400


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def retained(tmp_path: Path) -> dict[str, object]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    base = tmp_path / "worktrees"
    base.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(tmp_path, "clone", str(remote), str(repo))
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "origin", "HEAD:refs/heads/main")
    _git(repo, "fetch", "origin")
    assert task_store.initialize_repository(repo)["ok"]
    ids = ["request-a", "request-b"]
    for wid in ids:
        entry = base / wid
        entry.mkdir()
        _git(repo, "worktree", "add", "--detach", str(entry / "worktree"), "HEAD")
        # Give each worktree enough bytes that, once stranded, the unattributed
        # footprint dominates the observed total -- so the "material share" flag
        # is exercised deterministically rather than at the mercy of how large
        # the freshly initialised .aiworkhub/runtime happens to be.
        (entry / "worktree" / "payload.bin").write_bytes(b"x" * (8 * 1024 * 1024))
    return {"repo": repo, "base": base, "ids": ids}


def _identity_snapshot(repo: Path, base: Path) -> dict[str, int]:
    """The exact three numbers the round trip must preserve."""
    reg = worktree_storage.scan_worktree_registrations(repo, base)
    fp = storage_retention.repo_storage_footprint(repo, base=base)
    return {
        "registered_count": int(reg["registered_count"]),
        "aiworkhub_registered_count": int(reg["aiworkhub_registered_count"]),
        "repository_worktree_bytes": int(fp["repository_worktree_bytes"]),
        "unattributed_or_foreign_worktree_bytes": int(
            fp["unattributed_or_foreign_worktree_bytes"]
        ),
    }


def _quarantine_batch(repo: Path, base: Path, aged: float) -> str:
    preview = storage_retention.preview(repo, base=base, now=aged)
    assert preview["candidate_count"] == 2
    moved = storage_retention.quarantine(
        repo, preview_digest=preview["preview_digest"], confirm=True, base=base, now=aged
    )
    assert moved["quarantined"] == 2
    return str(moved["batch_id"])


def test_quarantine_then_restore_preserves_registration_and_byte_identity(retained) -> None:
    repo, base = retained["repo"], retained["base"]  # type: ignore[assignment]
    aged = _aged_now()

    before = _identity_snapshot(repo, base)
    batch_id = _quarantine_batch(repo, base, aged)
    # Reproduce the real strand: auto-gc prunes the now-dangling registrations
    # while the batch sits quarantined.
    _git(repo, "worktree", "prune")

    restored = storage_retention.restore(repo, batch_id=batch_id, confirm=True, base=base)
    assert restored["restored"] == 2
    assert restored["reinstated"] == 2

    after = _identity_snapshot(repo, base)
    # The equality IS the test: the three numbers land exactly where they began.
    assert after == before


def test_restore_that_cannot_reinstate_registration_fails_loudly(retained, monkeypatch) -> None:
    repo, base = retained["repo"], retained["base"]  # type: ignore[assignment]
    aged = _aged_now()
    batch_id = _quarantine_batch(repo, base, aged)
    _git(repo, "worktree", "prune")

    # Force reinstatement to be impossible: neutralise the reinstate helper so
    # the registration cannot come back -- exactly the silent-loss condition.
    monkeypatch.setattr(storage_retention, "_reinstate_registration", lambda *a, **k: None)

    with pytest.raises(
        storage_retention.StorageRetentionError,
        match="retention_restore_registration_failed",
    ):
        storage_retention.restore(repo, batch_id=batch_id, confirm=True, base=base)

    # The files are genuinely back on disk (a loud failure, never data loss); it
    # is only the registration that could not be reinstated.
    assert (base / "request-a" / "worktree").is_dir()
    assert (base / "request-b" / "worktree").is_dir()


def _strand_batch(repo: Path, base: Path, aged: float, monkeypatch) -> None:
    """Drive the round trip into the exact already-stranded end state."""
    batch_id = _quarantine_batch(repo, base, aged)
    _git(repo, "worktree", "prune")
    monkeypatch.setattr(storage_retention, "_reinstate_registration", lambda *a, **k: None)
    with pytest.raises(storage_retention.StorageRetentionError):
        storage_retention.restore(repo, batch_id=batch_id, confirm=True, base=base)
    monkeypatch.undo()


def test_recover_stranded_worktrees_is_explicit_and_reattaches_provable(
    retained, monkeypatch
) -> None:
    repo, base = retained["repo"], retained["base"]  # type: ignore[assignment]
    aged = _aged_now()
    _strand_batch(repo, base, aged, monkeypatch)

    # They are now stranded: on disk, but attributed to nobody.
    footprint = storage_retention.repo_storage_footprint(repo, base=base)
    assert footprint["unattributed_or_foreign_worktree_count"] == 2
    assert footprint["repository_worktree_count"] == 0

    # A read-only report states what recovery would do and mutates nothing.
    report = storage_retention.recover_stranded_worktrees(repo, base=base)
    assert report["dry_run"] is True
    assert report["reattachable_count"] == 2
    assert "explicit operator action" in report["invoked_from"]
    assert storage_retention.repo_storage_footprint(repo, base=base)[
        "unattributed_or_foreign_worktree_count"
    ] == 2

    # An explicit, reasoned confirm reattaches the provable ones.
    done = storage_retention.recover_stranded_worktrees(
        repo, base=base, confirm=True, reason="post-incident reattach"
    )
    assert done["ok"] is True
    assert done["reattached_count"] == 2
    assert done["reattach_failed"] == []

    after = storage_retention.repo_storage_footprint(repo, base=base)
    assert after["unattributed_or_foreign_worktree_count"] == 0
    assert after["repository_worktree_count"] == 2


def test_recover_requires_reason_and_never_runs_on_a_read(retained, monkeypatch) -> None:
    repo, base = retained["repo"], retained["base"]  # type: ignore[assignment]
    aged = _aged_now()
    _strand_batch(repo, base, aged, monkeypatch)

    # confirm without a reason is refused; a dry run performs no mutation.
    with pytest.raises(
        storage_retention.StorageRetentionError, match="retention_recover_reason_required"
    ):
        storage_retention.recover_stranded_worktrees(repo, base=base, confirm=True, reason="  ")
    assert storage_retention.repo_storage_footprint(repo, base=base)[
        "unattributed_or_foreign_worktree_count"
    ] == 2

    # A preview read never triggers recovery as a side effect.
    storage_retention.preview(repo, base=base, now=aged)
    assert storage_retention.repo_storage_footprint(repo, base=base)[
        "unattributed_or_foreign_worktree_count"
    ] == 2


def test_preview_flags_material_unattributed_share_prominently(retained, monkeypatch) -> None:
    repo, base = retained["repo"], retained["base"]  # type: ignore[assignment]
    aged = _aged_now()
    _strand_batch(repo, base, aged, monkeypatch)

    preview = storage_retention.preview(repo, base=base, now=aged)
    alert = preview["unattributed_alert"]
    assert alert["material"] is True
    assert alert["count"] == 2
    assert alert["bytes"] == preview["unattributed_or_foreign_worktree_bytes"] > 0
    assert alert["recovery_action"] == "storage_retention.recover_stranded_worktrees"
    assert "unattributed" in alert["message"]
