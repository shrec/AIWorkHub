"""Small release gate for the runtime surface supported on every host OS.

Linux-only sandbox, ``/proc`` liveness, Unix-socket and Landlock suites remain
in the full Linux test job.  This module is intentionally the common contract
that must pass on Linux, macOS and Windows for every release.
"""

from __future__ import annotations

from aiworkhub import core, process_launcher, server, task_reconciler


def test_public_mcp_runtime_imports_on_host_platform() -> None:
    assert server is not None
    assert process_launcher.MAX_LOG_TAIL_BYTES > 0
    assert task_reconciler.DEFAULT_SCAN_INTERVAL_SECONDS > 0


def test_repository_binding_uses_native_host_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    assert core.repo_root() == tmp_path.resolve()


def test_repository_binding_fails_closed_on_cross_repo_mismatch(monkeypatch, tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(first))
    monkeypatch.setenv("AIWORKHUB_REPO", str(second))
    try:
        core.repo_root()
    except RuntimeError as exc:
        assert str(exc).startswith("repo_root_env_mismatch:")
    else:  # pragma: no cover - safety assertion
        raise AssertionError("cross-repository mismatch must fail closed")
