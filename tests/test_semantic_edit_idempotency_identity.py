"""SCAN-E3FF: semantic_edit_apply must fail closed on identity and durability.

These tests pin the four contracts repaired for the semantic-edit pillar:

* a reused idempotency_key that points at a *different* prepared target is a
  caller error and is refused -- never answered with the first call's receipt;
* an identical repeat (same key, same target, same bytes) still replays
  without rewriting the file;
* an apply preserves the file's original mode instead of collapsing it to the
  ``mkstemp`` 0600 default;
* an apply whose authenticated audit record could not be written fails closed
  rather than returning ``ok: True``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from aiworkhub import worker_ai_tools_mcp as worker_tools


def _ctx(
    repo: Path,
    *,
    audit_ledger_path: Path | None = None,
    audit_hmac_key_path: Path | None = None,
) -> worker_tools.WorkerToolContext:
    return worker_tools.WorkerToolContext(
        task_id="TASK",
        runner="runner",
        topic="topic",
        request_id="request",
        repo=repo,
        authority_repo=repo,
        source_graph_targets=("src/a.py", "src/b.py", "src/module.py"),
        session_topic="topic",
        audit_ledger_path=audit_ledger_path,
        audit_hmac_key_path=audit_hmac_key_path,
        allowed_writes=("src/*.py",),
    )


def test_same_key_different_target_is_refused_not_replayed(tmp_path: Path) -> None:
    """The a.py/b.py reproduction from the objective must be refused."""

    repo = tmp_path / "worktree"
    (repo / "src").mkdir(parents=True)
    a = repo / "src" / "a.py"
    b = repo / "src" / "b.py"
    a.write_bytes(b"ONE\n")
    b.write_bytes(b"ALPHA\n")
    session = worker_tools.WorkerSemanticEditSession(_ctx(repo))

    prepared_a = session.prepare(file_path="src/a.py", start_line=1, end_line=1)
    first = session.apply(
        target_id=prepared_a["target_id"], new="TWO\n", idempotency_key="k"
    )
    assert first["ok"] is True
    assert first["path"] == "src/a.py"
    assert a.read_text(encoding="utf-8") == "TWO\n"

    prepared_b = session.prepare(file_path="src/b.py", start_line=1, end_line=1)
    b_inode_before = b.stat().st_ino
    second = session.apply(
        target_id=prepared_b["target_id"], new="BETA\n", idempotency_key="k"
    )

    # Fails closed: an explicit violation, never a success receipt and never
    # the receipt of the other file.
    assert second["ok"] is False
    assert second["reason"] == "semantic_edit_idempotency_key_conflict"
    assert second.get("idempotent_replay") is not True
    assert second.get("path") != "src/a.py"
    # b.py is left completely untouched.
    assert b.read_text(encoding="utf-8") == "ALPHA\n"
    assert b.stat().st_ino == b_inode_before


def test_identical_repeat_replays_without_rewriting(tmp_path: Path) -> None:
    """Same key + same target + same bytes still replays; both halves asserted."""

    repo = tmp_path / "worktree"
    (repo / "src").mkdir(parents=True)
    module = repo / "src" / "module.py"
    module.write_bytes(b"before\ndef target():\n    return 1\nafter\n")
    session = worker_tools.WorkerSemanticEditSession(_ctx(repo))

    prepared = session.prepare(file_path="src/module.py", start_line=2, end_line=3)
    first = session.apply(
        target_id=prepared["target_id"],
        new="def target():\n    return 2",
        idempotency_key="edit-1",
    )
    assert first["ok"] is True
    assert first["idempotent_replay"] is False
    after_first = module.read_bytes()
    inode_after_first = module.stat().st_ino
    mtime_after_first = module.stat().st_mtime_ns

    replay = session.apply(
        target_id=prepared["target_id"],
        new="def target():\n    return 2",
        idempotency_key="edit-1",
    )

    # Half one: it replays (the first receipt, flagged as a replay).
    assert replay["idempotent_replay"] is True
    assert replay["after_sha256"] == first["after_sha256"]
    # Half two: it does not rewrite -- same bytes, same inode, same mtime.
    assert module.read_bytes() == after_first
    assert module.stat().st_ino == inode_after_first
    assert module.stat().st_mtime_ns == mtime_after_first


def test_apply_preserves_original_file_mode(tmp_path: Path) -> None:
    """chmod 0755 then apply must leave the mode at 0755, not 0600."""

    repo = tmp_path / "worktree"
    (repo / "src").mkdir(parents=True)
    module = repo / "src" / "module.py"
    module.write_bytes(b"#!/usr/bin/env python\nprint('one')\n")
    try:
        os.chmod(module, 0o755)
    except OSError:
        pytest.skip("chmod is not permitted on this sandbox filesystem")
    if module.stat().st_mode & 0o777 != 0o755:
        pytest.skip("filesystem does not honour chmod")

    session = worker_tools.WorkerSemanticEditSession(_ctx(repo))
    prepared = session.prepare(file_path="src/module.py", start_line=2, end_line=2)
    applied = session.apply(
        target_id=prepared["target_id"],
        new="print('two')",
        idempotency_key="chmod-1",
    )

    assert applied["ok"] is True
    assert module.read_text(encoding="utf-8") == "#!/usr/bin/env python\nprint('two')\n"
    # The atomic os.replace would otherwise carry the mkstemp 0600 mode across.
    assert module.stat().st_mode & 0o777 == 0o755


def test_apply_fails_closed_when_audit_append_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bound-but-failing audit ledger must make apply report not-durable."""

    repo = tmp_path / "worktree"
    (repo / "src").mkdir(parents=True)
    module = repo / "src" / "module.py"
    module.write_bytes(b"before\ndef target():\n    return 1\nafter\n")
    ledger = tmp_path / "audit.jsonl"
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"k" * 32)
    ctx = _ctx(repo, audit_ledger_path=ledger, audit_hmac_key_path=key_path)
    session = worker_tools.WorkerSemanticEditSession(ctx)

    prepared = session.prepare(file_path="src/module.py", start_line=2, end_line=3)
    # Simulate an audit append that could not be authenticated/written.
    monkeypatch.setattr(worker_tools, "_append_audit", lambda *args, **kwargs: False)
    applied = session.apply(
        target_id=prepared["target_id"],
        new="def target():\n    return 2",
        idempotency_key="edit-fail",
    )

    assert applied["ok"] is False
    assert applied["reason"] == "semantic_edit_apply_not_durable"
    assert applied.get("idempotent_replay") is not True
    # The non-durable apply must not be cached as a replayable success: a
    # same-key retry re-enters the write path (and now fails the stale check
    # because the file already changed) instead of replaying ok: True.
    retry = session.apply(
        target_id=prepared["target_id"],
        new="def target():\n    return 2",
        idempotency_key="edit-fail",
    )
    assert retry["ok"] is False
    assert retry.get("idempotent_replay") is not True


def test_apply_without_ledger_still_succeeds(tmp_path: Path) -> None:
    """No bound ledger means audit is disabled, not a durability failure."""

    repo = tmp_path / "worktree"
    (repo / "src").mkdir(parents=True)
    module = repo / "src" / "module.py"
    module.write_bytes(b"before\ndef target():\n    return 1\nafter\n")
    session = worker_tools.WorkerSemanticEditSession(_ctx(repo))

    prepared = session.prepare(file_path="src/module.py", start_line=2, end_line=3)
    applied = session.apply(
        target_id=prepared["target_id"],
        new="def target():\n    return 2",
        idempotency_key="no-ledger-1",
    )
    assert applied["ok"] is True
    assert hashlib.sha256(module.read_bytes()).hexdigest() == applied["after_sha256"]
