"""NF-2026-01198: promotion-write failures are named, transient sharing
violations retry with a bounded backoff, retries are safe, and a read-only
destination the caller is already authorized to overwrite is cleared and
promoted. Covers src/aiworkhub/promotion_write.py and its use by
worker_workspace.promote.
"""

from __future__ import annotations

import ctypes
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import promotion_write, worker_workspace  # noqa: E402

_OUT_FILES = ("a.txt", "b.txt", "c.txt")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Immutable git repo fixture with three tracked output files."""
    root = tmp_path / "parent"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "NF01198 Test"], cwd=root, check=True
    )
    (root / "out").mkdir()
    for name in _OUT_FILES:
        (root / "out" / name).write_bytes(f"{name}-v1\n".encode("ascii"))
    subprocess.run(["git", "add", "out"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    return root


def _build_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path, request_id: str
) -> worker_workspace.WorkerWorkspace:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    return worker_workspace.create_workspace(
        repo,
        request_id,
        {"allowed_writes": [f"out/{name}" for name in _OUT_FILES], "read_first": []},
        "validation",
    )


def _write_candidate(workspace: worker_workspace.WorkerWorkspace, name: str, text: str) -> None:
    (workspace.path / "out" / name).write_bytes(text.encode("ascii"))


# ---------------------------------------------------------------------------
# Regression: a bare PermissionError(13) on the k-th of n replaces must be
# named, leak no absolute path, leave no temp sibling, and release the marker.
# ---------------------------------------------------------------------------


def test_regression_replace_permission_error_named_no_leftovers_marker_released(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _build_workspace(monkeypatch, tmp_path, repo, "nf01198-regression")
    for name in _OUT_FILES:
        _write_candidate(workspace, name, f"{name}-v2\n")

    real_replace = os.replace
    calls: list[str] = []

    def failing_replace(src: object, dst: object) -> None:
        calls.append(str(dst))
        if len(calls) == 2:
            raise PermissionError(13, "Permission denied")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
        worker_workspace.promote(workspace, [f"out/{name}" for name in _OUT_FILES])

    message = str(excinfo.value)
    assert message.startswith("promotion_write_failed:out/b.txt:replace:errno=13")
    assert message.endswith(":winerror=none")
    assert str(repo) not in message
    assert str(workspace.path) not in message

    leftovers = [p.name for p in (repo / "out").iterdir() if p.name.startswith(".b.txt.")]
    assert leftovers == []

    marker_dir = worker_workspace._promotion_inflight_dir(workspace.repo)
    assert not (marker_dir / workspace.request_id).exists()


# ---------------------------------------------------------------------------
# Retry-safety: a second promote() call with the holder gone promotes
# everything, including the paths that already succeeded on the first call.
# ---------------------------------------------------------------------------


def test_retry_safety_second_promote_completes_after_cleared_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _build_workspace(monkeypatch, tmp_path, repo, "nf01198-retry-safety")
    for name in _OUT_FILES:
        _write_candidate(workspace, name, f"{name}-v2\n")

    real_replace = os.replace
    calls: list[str] = []

    def failing_once(src: object, dst: object) -> None:
        calls.append(str(dst))
        if len(calls) == 2:
            raise PermissionError(13, "Permission denied")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_once)
    with pytest.raises(worker_workspace.WorkspaceError):
        worker_workspace.promote(workspace, [f"out/{name}" for name in _OUT_FILES])

    monkeypatch.setattr(os, "replace", real_replace)
    promoted = worker_workspace.promote(workspace, [f"out/{name}" for name in _OUT_FILES])

    assert sorted(promoted) == sorted(f"out/{name}" for name in _OUT_FILES)
    for name in _OUT_FILES:
        assert (repo / "out" / name).read_bytes() == f"{name}-v2\n".encode("ascii")


# ---------------------------------------------------------------------------
# Transient: a sharing violation that clears after a bounded number of
# attempts is retried and succeeds; attempts/wait are bounded by named
# constants and the sleep is injected so the test never really waits.
# ---------------------------------------------------------------------------


def test_transient_sharing_violation_retried_with_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _build_workspace(monkeypatch, tmp_path, repo, "nf01198-transient")
    _write_candidate(workspace, "a.txt", "a-v2\n")

    real_replace = os.replace
    attempt_count: list[int] = []
    sleeps: list[float] = []

    def flaky_replace(src: object, dst: object) -> None:
        attempt_count.append(1)
        if len(attempt_count) < 3:
            err = PermissionError(13, "The process cannot access the file")
            err.winerror = promotion_write.ACCESS_DENIED_WINERROR
            raise err
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    monkeypatch.setattr(promotion_write.time, "sleep", lambda s: sleeps.append(s))

    start = time.monotonic()
    promoted = worker_workspace.promote(workspace, ["out/a.txt"])
    elapsed = time.monotonic() - start

    assert promoted == ["out/a.txt"]
    assert len(attempt_count) == 3
    assert sleeps == [promotion_write.PROMOTION_WRITE_RETRY_DELAY_SECONDS] * 2
    assert len(attempt_count) <= promotion_write.PROMOTION_WRITE_RETRY_ATTEMPTS
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# Non-transient: an error that is not a sharing violation is not retried.
# ---------------------------------------------------------------------------


def test_non_transient_error_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _build_workspace(monkeypatch, tmp_path, repo, "nf01198-non-transient")
    _write_candidate(workspace, "a.txt", "a-v2\n")

    attempt_count: list[int] = []

    def always_no_space(src: object, dst: object) -> None:
        attempt_count.append(1)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", always_no_space)

    with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
        worker_workspace.promote(workspace, ["out/a.txt"])

    assert len(attempt_count) == 1
    assert str(excinfo.value) == "promotion_write_failed:out/a.txt:replace:errno=28:winerror=none"


# ---------------------------------------------------------------------------
# NF-2026-01198 rework: winerror 32 (sharing violation) and the "unlink"
# transient set were previously unexercised; winerror 5 is not in
# _TRANSIENT_WINERRORS["unlink"], so it must fail after exactly one attempt.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["replace", "unlink"])
def test_run_promotion_write_retrying_sharing_violation_winerror32_retries(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(promotion_write.time, "sleep", lambda s: sleeps.append(s))
    calls: list[int] = []

    def flaky(*args: object) -> str:
        calls.append(1)
        if len(calls) < 3:
            err = PermissionError(13, "The process cannot access the file")
            err.winerror = promotion_write.SHARING_VIOLATION_WINERROR
            raise err
        return "ok"

    result = promotion_write.run_promotion_write_retrying("x", operation, flaky)

    assert result == "ok"
    assert len(calls) == 3
    assert sleeps == [promotion_write.PROMOTION_WRITE_RETRY_DELAY_SECONDS] * 2


def test_run_promotion_write_retrying_unlink_access_denied_not_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def always_denied(*args: object) -> None:
        calls.append(1)
        err = PermissionError(13, "Access is denied")
        err.winerror = promotion_write.ACCESS_DENIED_WINERROR
        raise err

    with pytest.raises(promotion_write.PromotionWriteError) as excinfo:
        promotion_write.run_promotion_write_retrying("x", "unlink", always_denied)

    assert len(calls) == 1
    assert str(excinfo.value) == "promotion_write_failed:x:unlink:errno=13:winerror=5"


# ---------------------------------------------------------------------------
# Windows-only live tests (skipped elsewhere): real sharing violation and
# real read-only attribute, no injected errors.
# ---------------------------------------------------------------------------

_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _open_handle_without_delete_share(path: Path) -> int:
    """Open ``path`` the way a non-Python holder (editor, AV) would: no
    FILE_SHARE_DELETE, so a concurrent os.replace/unlink hits a real
    ERROR_SHARING_VIOLATION. Python's own ``open()`` grants FILE_SHARE_DELETE
    and would not reproduce the failure this is testing.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        str(path),
        _GENERIC_READ,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _close_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int32
    kernel32.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing-violation semantics only")
def test_windows_live_sharing_violation_retries_then_succeeds_after_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _build_workspace(monkeypatch, tmp_path, repo, "nf01198-win-live-sharing")
    _write_candidate(workspace, "a.txt", "a-v2\n")
    target = repo / "out" / "a.txt"

    sleeps: list[float] = []
    monkeypatch.setattr(promotion_write.time, "sleep", lambda s: sleeps.append(s))

    handle = _open_handle_without_delete_share(target)
    try:
        with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
            worker_workspace.promote(workspace, ["out/a.txt"])
        assert str(excinfo.value) == "promotion_write_failed:out/a.txt:replace:errno=13:winerror=5"
        assert len(sleeps) == promotion_write.PROMOTION_WRITE_RETRY_ATTEMPTS - 1
    finally:
        _close_handle(handle)

    promoted = worker_workspace.promote(workspace, ["out/a.txt"])
    assert promoted == ["out/a.txt"]
    assert target.read_bytes() == b"a-v2\n"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing-violation semantics only")
def test_windows_live_sharing_violation_handle_closed_during_backoff_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _build_workspace(monkeypatch, tmp_path, repo, "nf01198-win-live-mid-retry")
    _write_candidate(workspace, "a.txt", "a-v2\n")
    target = repo / "out" / "a.txt"

    holder = [_open_handle_without_delete_share(target)]
    sleeps: list[float] = []

    def sleep_and_maybe_close(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            _close_handle(holder.pop())

    monkeypatch.setattr(promotion_write.time, "sleep", sleep_and_maybe_close)

    try:
        promoted = worker_workspace.promote(workspace, ["out/a.txt"])
    finally:
        if holder:
            _close_handle(holder.pop())

    assert promoted == ["out/a.txt"]
    assert len(sleeps) == 2
    assert target.read_bytes() == b"a-v2\n"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows read-only attribute semantics only")
def test_windows_live_readonly_destination_is_cleared_and_promoted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    # Report decision (NF-2026-01198): promotion may clear the read-only
    # attribute of a destination it is already authorized to overwrite (it
    # already passed the allowed_writes scope check and the hash preflight),
    # scoped to that exact file and only on Windows -- so this must succeed,
    # not fail with a named reason. Set read-only BEFORE create_workspace so
    # the parent baseline hash captures the read-only mode as the expected
    # starting state.
    target = repo / "out" / "a.txt"
    os.chmod(target, stat.S_IREAD)
    try:
        workspace = _build_workspace(monkeypatch, tmp_path, repo, "nf01198-win-live-readonly")
        _write_candidate(workspace, "a.txt", "a-v2\n")

        promoted = worker_workspace.promote(workspace, ["out/a.txt"])

        assert promoted == ["out/a.txt"]
        assert target.read_bytes() == b"a-v2\n"
    finally:
        if target.exists():
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows read-only attribute semantics only")
def test_windows_live_readonly_destination_retry_safe_after_failed_promote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    # Measured defect (NF-2026-01198 rework): clear_readonly_if_set ran before
    # a failed replace and nothing restored the read-only bit, so the parent
    # hash preflight (which hashes mode) rejected the retry as
    # parent_changed_since_launch once the holder closed.
    target = repo / "out" / "a.txt"
    os.chmod(target, stat.S_IREAD)
    try:
        workspace = _build_workspace(
            monkeypatch, tmp_path, repo, "nf01198-win-live-readonly-retry-safe"
        )
        _write_candidate(workspace, "a.txt", "a-v2\n")
        monkeypatch.setattr(promotion_write.time, "sleep", lambda s: None)

        handle = _open_handle_without_delete_share(target)
        try:
            with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
                worker_workspace.promote(workspace, ["out/a.txt"])
            assert (
                str(excinfo.value)
                == "promotion_write_failed:out/a.txt:replace:errno=13:winerror=5"
            )
        finally:
            _close_handle(handle)

        assert not target.stat().st_mode & stat.S_IWRITE

        promoted = worker_workspace.promote(workspace, ["out/a.txt"])
        assert promoted == ["out/a.txt"]
        assert target.read_bytes() == b"a-v2\n"
    finally:
        if target.exists():
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
