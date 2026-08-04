from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiworkhub import process_launcher, terminal_authority


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
