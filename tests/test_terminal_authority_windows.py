from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from aiworkhub import platform_io, process_launcher, terminal_authority


def test_windows_accepts_acl_protected_key_without_posix_0600(tmp_path: Path) -> None:
    key_path = tmp_path / "authority.key"
    expected = b"k" * 32
    key_path.write_bytes(expected)
    key_path.chmod(0o644)

    assert terminal_authority.load_or_create_key(
        key_path,
        _platform_name="nt",
    ) == expected


def test_invalid_existing_key_fails_bounded_without_recursion(tmp_path: Path) -> None:
    key_path = tmp_path / "authority.key"
    key_path.write_bytes(b"short")

    with pytest.raises(RuntimeError, match="terminal_authority_key_invalid"):
        terminal_authority.load_or_create_key(
            key_path,
            _platform_name="nt",
        )


def test_new_key_is_exactly_32_bytes(tmp_path: Path) -> None:
    key_path = tmp_path / "authority.key"

    key = terminal_authority.load_or_create_key(key_path)

    assert len(key) == 32
    assert key_path.read_bytes() == key
    if os.name != "nt":
        assert key_path.stat().st_mode & 0o777 == 0o600


def test_launch_diagnostic_is_phase_bound_and_path_redacted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    try:
        raise RecursionError(f"failure in {repo}")
    except RecursionError as exc:
        diagnostic = process_launcher._bounded_launch_diagnostic(
            exc,
            phase="terminal_authority",
            repo=repo,
        )

    assert diagnostic["phase"] == "terminal_authority"
    assert diagnostic["exception_type"] == "RecursionError"
    assert str(repo) not in diagnostic["traceback"]
    assert "<repo>" in diagnostic["traceback"]
    assert len(diagnostic["traceback"]) <= 4000


def test_unexpected_pre_supervisor_failure_returns_structured_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        isolation_enabled=True,
    )
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    monkeypatch.setattr(
        manager,
        "_preflight_card",
        lambda *_args: (_ for _ in ()).throw(RecursionError("fixture recursion")),
    )
    monkeypatch.setattr(
        process_launcher.task_engine,
        "record_launch_blocker",
        lambda *_args, **_kwargs: {"ok": True},
    )

    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )

    assert result["ok"] is False
    assert result["state"] == "blocked"
    assert result["blocked_reason"].startswith(
        "unexpected_launch_error:RecursionError:fixture recursion"
    )
    assert result["diagnostic"]["phase"] == "preflight"
    assert result["diagnostic"]["exception_type"] == "RecursionError"
    event = manager._events()[-1]
    assert event["diagnostic"] == result["diagnostic"]


def _grant_everyone_inheritable(target: Path) -> bool:
    """Grant Everyone read, inherited by every file created under ``target``.

    Mirrors the shape of this repo's real, measured incident: a runtime
    directory whose DACL grants a broad principal, from before this module's
    write-side hardening existed. Returns whether the grant was actually
    applied, so a test can skip rather than assert against a setup that never
    happened.
    """

    completed = subprocess.run(
        ["icacls", str(target), "/grant", "*S-1-1-0:(OI)(CI)(R)"],
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


@pytest.mark.skipif(os.name != "nt", reason="exercises real Win32 DACL APIs")
def test_create_path_mints_a_trusted_key_under_a_permissive_parent(
    tmp_path: Path,
) -> None:
    """NF-2026-00011 follow-up, reproducing the live incident directly.

    Measured on this host: a key created under
    ``.aiworkhub/runtime/process_logs/processes`` -- whose DACL predates this
    module's read-side trust check -- inherited a grant to Authenticated
    Users, and EVERY subsequent launch refused it as
    ``terminal_authority_key_invalid``, including a freshly deleted and
    recreated one, because the create path never hardened the DACL either.
    This is the regression test for the fix: the create path must mint a key
    that is trusted even when its parent directory is exactly this permissive.
    """

    processes_dir = tmp_path / "processes"
    processes_dir.mkdir()
    if not _grant_everyone_inheritable(processes_dir):
        pytest.skip("validation_unsupported_in_sandbox:cannot_grant_everyone")
    key_path = processes_dir / "authority.key"

    key = terminal_authority.load_or_create_key(key_path, _platform_name="nt")

    assert len(key) == 32
    fd = os.open(str(key_path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        trusted, reason = platform_io.windows_descriptor_secret_trust(fd)
    finally:
        os.close(fd)
    assert trusted, reason
    # The key must also be READABLE again on the next call -- the DACL fix
    # must not have produced a key only this one code path can use.
    assert terminal_authority.load_or_create_key(key_path, _platform_name="nt") == key


@pytest.mark.skipif(os.name != "nt", reason="exercises real Win32 DACL APIs")
def test_windows_harden_owner_only_key_dacl_repairs_an_inherited_grant(
    tmp_path: Path,
) -> None:
    """The hardening primitive itself, isolated from the create path."""

    processes_dir = tmp_path / "processes"
    processes_dir.mkdir()
    if not _grant_everyone_inheritable(processes_dir):
        pytest.skip("validation_unsupported_in_sandbox:cannot_grant_everyone")
    key_path = processes_dir / "authority.key"
    key_path.write_bytes(b"k" * 32)

    fd = os.open(str(key_path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        trusted_before, _reason = platform_io.windows_descriptor_secret_trust(fd)
    finally:
        os.close(fd)
    assert not trusted_before, "setup did not actually inherit a broad grant"

    hardened, reason = platform_io.windows_harden_owner_only_key_dacl(key_path)
    assert hardened, reason

    fd = os.open(str(key_path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        trusted_after, after_reason = platform_io.windows_descriptor_secret_trust(fd)
    finally:
        os.close(fd)
    assert trusted_after, after_reason


@pytest.mark.skipif(os.name != "nt", reason="exercises real Win32 DACL APIs")
def test_windows_harden_owner_only_key_dacl_fails_closed_on_a_missing_path(
    tmp_path: Path,
) -> None:
    hardened, reason = platform_io.windows_harden_owner_only_key_dacl(
        tmp_path / "does-not-exist.key"
    )

    assert hardened is False
    assert reason


def test_untrusted_existing_key_error_names_the_reason(tmp_path: Path) -> None:
    """The refusal stays fail-closed, and now also names WHY.

    NF-2026-00011 follow-up: this host's own incident was an unexplained
    ``terminal_authority_key_invalid`` with no way to tell "someone else can
    read this key" apart from "the file is corrupt" -- both raised the exact
    same bare string. The reason is diagnostic only; it must never change
    whether an untrusted key is accepted.
    """

    key_path = tmp_path / "authority.key"
    key_path.write_bytes(b"short")  # malformed: not 32 bytes

    with pytest.raises(RuntimeError, match=r"terminal_authority_key_invalid:\S+"):
        terminal_authority.load_or_create_key(key_path, _platform_name="nt")
