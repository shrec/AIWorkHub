from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import runtime_temp, task_store, terminal_log_retention


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _write_manifest(
    directory: Path,
    request_id: str,
    *,
    pid: int,
    starttime: int,
    namespace: str = "worker",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / runtime_temp.OWNER_MANIFEST_NAME
    manifest.write_text(
        json.dumps({
            "schema_id": runtime_temp.OWNER_SCHEMA_ID,
            "request_id": request_id,
            "repo_id": runtime_temp._repo_fingerprint(Path("/unused/repo")),
            "pid": pid,
            "starttime": starttime,
            "created_at": "2026-01-01T00:00:00+00:00",
            "namespace": namespace,
        }),
        encoding="utf-8",
    )
    return directory


def test_temp_root_is_repo_local_and_fails_closed_on_symlink(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert runtime_temp.temp_root(repo) == (repo / ".aiworkhub" / "temp")

    # A real (non-symlink) hub and temp dir are acceptable.
    hub = repo / ".aiworkhub"
    hub.mkdir()
    (hub / "temp").mkdir()
    assert runtime_temp.temp_root(repo) == (repo / ".aiworkhub" / "temp")

    # A symlinked .aiworkhub hub must fail closed.
    shutil.rmtree(hub)
    os.symlink(tmp_path / "elsewhere", hub)
    with pytest.raises(runtime_temp.RuntimeTempError):
        runtime_temp.temp_root(repo)

    # A symlinked temp dir must fail closed too.
    hub.unlink()
    hub.mkdir()
    os.symlink(tmp_path / "elsewhere", hub / "temp")
    with pytest.raises(runtime_temp.RuntimeTempError):
        runtime_temp.temp_root(repo)


def test_request_dirs_are_namespaced_by_identity_and_repo(tmp_path: Path) -> None:
    repo_a = _repo(tmp_path)
    repo_b = tmp_path / "other"
    repo_b.mkdir()

    a = runtime_temp.request_dir(repo_a, "req-1", "validation")
    b = runtime_temp.request_dir(repo_b, "req-1", "validation")
    assert a == (repo_a / ".aiworkhub" / "temp" / "validation" / "req-1")
    assert b == (repo_b / ".aiworkhub" / "temp" / "validation" / "req-1")
    assert a != b  # two repositories never share mutable temp state

    with pytest.raises(runtime_temp.RuntimeTempError):
        runtime_temp.request_dir(repo_a, "../escape", "worker")


def test_provision_request_temp_layout_and_owner_manifest(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    layout = runtime_temp.provision_request_temp(
        repo, "req-provision", namespace="worker"
    )

    assert layout.root == runtime_temp.request_dir(repo, "req-provision", "worker")
    for sub in ("home", "tmp", "mypy_cache", "stdio"):
        assert (layout.root / sub).is_dir()

    manifest = runtime_temp.read_owner_manifest(layout.root)
    assert manifest is not None
    assert manifest["request_id"] == "req-provision"
    assert manifest["pid"] == os.getpid()
    assert runtime_temp.owner_alive(manifest) is True

    # A second request shares no mutable state with the first.
    other = runtime_temp.provision_request_temp(repo, "req-other", namespace="worker")
    assert other.root != layout.root
    assert list(layout.root.iterdir())  # first request dir still intact and isolated


def test_read_owner_manifest_rejects_fifo_promptly(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo") or not hasattr(os, "fork"):
        pytest.skip("requires POSIX FIFO and fork support")

    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    os.mkfifo(owner_dir / runtime_temp.OWNER_MANIFEST_NAME)

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            result = runtime_temp.read_owner_manifest(owner_dir)
            os.write(write_fd, b"1" if result is None else b"0")
        finally:
            os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    deadline = time.monotonic() + 1
    pid = 0
    status = 0
    try:
        while time.monotonic() < deadline:
            pid, status = os.waitpid(child, os.WNOHANG)
            if pid:
                break
            time.sleep(0.01)
        assert pid == child
        assert os.waitstatus_to_exitcode(status) == 0
        assert os.read(read_fd, 1) == b"1"
    finally:
        os.close(read_fd)
        if pid == 0:
            try:
                os.kill(child, 9)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child, os.WNOHANG)
            except ChildProcessError:
                pass


def test_read_owner_manifest_rejects_special_symlink_and_oversized(
    tmp_path: Path,
) -> None:
    valid = _write_manifest(tmp_path / "valid", "valid", pid=111, starttime=111)
    assert runtime_temp.read_owner_manifest(valid)["request_id"] == "valid"

    symlinked = tmp_path / "symlinked"
    symlinked.mkdir()
    os.symlink(
        valid / runtime_temp.OWNER_MANIFEST_NAME,
        symlinked / runtime_temp.OWNER_MANIFEST_NAME,
    )
    assert runtime_temp.read_owner_manifest(symlinked) is None

    directory_manifest = tmp_path / "directory-manifest"
    (directory_manifest / runtime_temp.OWNER_MANIFEST_NAME).mkdir(parents=True)
    assert runtime_temp.read_owner_manifest(directory_manifest) is None

    oversized = tmp_path / "oversized"
    oversized.mkdir()
    (oversized / runtime_temp.OWNER_MANIFEST_NAME).write_text(
        "{" + " " * runtime_temp.MAX_MANIFEST_BYTES + "}",
        encoding="utf-8",
    )
    assert runtime_temp.read_owner_manifest(oversized) is None


def test_read_owner_manifest_rejects_lstat_open_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_dir = _write_manifest(tmp_path / "race", "race", pid=111, starttime=111)
    replacement = tmp_path / "replacement.json"
    replacement.write_text(
        json.dumps({
            "schema_id": runtime_temp.OWNER_SCHEMA_ID,
            "request_id": "replacement",
        }),
        encoding="utf-8",
    )
    original = owner_dir / runtime_temp.OWNER_MANIFEST_NAME
    assert original.stat().st_ino != replacement.stat().st_ino
    real_open = os.open

    def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
        os.replace(replacement, owner_dir / runtime_temp.OWNER_MANIFEST_NAME)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(runtime_temp.os, "open", swap_before_open)
    assert runtime_temp.read_owner_manifest(owner_dir) is None


def test_read_owner_manifest_rejects_post_read_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_dir = _write_manifest(
        tmp_path / "post-read-race", "race", pid=111, starttime=111
    )
    replacement_dir = _write_manifest(
        tmp_path / "post-read-replacement", "replacement", pid=222, starttime=222
    )
    original = owner_dir / runtime_temp.OWNER_MANIFEST_NAME
    replacement = replacement_dir / runtime_temp.OWNER_MANIFEST_NAME
    assert original.stat().st_ino != replacement.stat().st_ino
    real_read = os.read
    replaced = False

    def swap_after_read(fd: int, n: int) -> bytes:
        nonlocal replaced
        raw = real_read(fd, n)
        if not replaced:
            os.replace(replacement, original)
            replaced = True
        return raw

    monkeypatch.setattr(runtime_temp.os, "read", swap_after_read)
    assert runtime_temp.read_owner_manifest(owner_dir) is None
    assert replaced is True


def test_read_owner_manifest_windows_identity_accepts_valid_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_dir = _write_manifest(
        tmp_path / "windows-valid", "windows-valid", pid=111, starttime=111
    )
    identity = (10, 20, 30, 1, 99)

    monkeypatch.setattr(runtime_temp, "_is_windows", lambda: True)
    monkeypatch.setattr(runtime_temp, "_windows_path_identity", lambda path: identity)
    monkeypatch.setattr(runtime_temp, "_windows_fd_identity", lambda fd: identity)

    manifest = runtime_temp.read_owner_manifest(owner_dir)
    assert manifest is not None
    assert manifest["request_id"] == "windows-valid"


def test_read_owner_manifest_windows_uses_stable_identity_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_dir = _write_manifest(
        tmp_path / "windows-pre-read-stable", "race", pid=111, starttime=111
    )
    replacement_dir = _write_manifest(
        tmp_path / "windows-pre-read-replacement", "replacement", pid=222, starttime=222
    )
    original = owner_dir / runtime_temp.OWNER_MANIFEST_NAME
    replacement = replacement_dir / runtime_temp.OWNER_MANIFEST_NAME
    original_identity_from_path = (10, 20, 30, 1, original.stat().st_size)
    original_identity_from_fd = (10, 20, 128, 1, original.stat().st_size)
    replacement_identity = (10, 21, 128, 1, replacement.stat().st_size)
    real_read = os.read
    replaced = False

    def path_identity(path: Path) -> tuple[int, ...]:
        return replacement_identity if replaced else original_identity_from_path

    def swap_after_read(fd: int, n: int) -> bytes:
        nonlocal replaced
        raw = real_read(fd, n)
        os.replace(replacement, original)
        replaced = True
        return raw

    monkeypatch.setattr(runtime_temp, "_is_windows", lambda: True)
    monkeypatch.setattr(runtime_temp, "_windows_path_identity", path_identity)
    monkeypatch.setattr(
        runtime_temp, "_windows_fd_identity", lambda fd: original_identity_from_fd
    )
    monkeypatch.setattr(runtime_temp.os, "read", swap_after_read)

    assert runtime_temp.read_owner_manifest(owner_dir) is None
    assert replaced is True


def test_read_owner_manifest_windows_rejects_post_read_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_dir = _write_manifest(
        tmp_path / "windows-post-read-race", "race", pid=111, starttime=111
    )
    original_identity = (10, 20, 30, 1, 99)
    replacement_identity = (10, 21, 30, 1, 99)
    replaced = False
    real_read = os.read

    def path_identity(path: Path) -> tuple[int, ...]:
        return replacement_identity if replaced else original_identity

    def swap_after_read(fd: int, n: int) -> bytes:
        nonlocal replaced
        raw = real_read(fd, n)
        replaced = True
        return raw

    monkeypatch.setattr(runtime_temp, "_is_windows", lambda: True)
    monkeypatch.setattr(runtime_temp, "_windows_path_identity", path_identity)
    monkeypatch.setattr(
        runtime_temp, "_windows_fd_identity", lambda fd: original_identity
    )
    monkeypatch.setattr(runtime_temp.os, "read", swap_after_read)

    assert runtime_temp.read_owner_manifest(owner_dir) is None
    assert replaced is True


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows file identity")
def test_read_owner_manifest_native_windows_rejects_post_read_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_dir = _write_manifest(
        tmp_path / "native-windows-post-read-race", "race", pid=111, starttime=111
    )
    replacement_dir = _write_manifest(
        tmp_path / "native-windows-post-read-replacement",
        "replacement",
        pid=222,
        starttime=222,
    )
    original = owner_dir / runtime_temp.OWNER_MANIFEST_NAME
    replacement = replacement_dir / runtime_temp.OWNER_MANIFEST_NAME
    real_read = os.read
    replaced = False

    def swap_after_read(fd: int, n: int) -> bytes:
        nonlocal replaced
        raw = real_read(fd, n)
        if not replaced:
            os.replace(replacement, original)
            replaced = True
        return raw

    monkeypatch.setattr(runtime_temp.os, "read", swap_after_read)

    assert runtime_temp.read_owner_manifest(owner_dir) is None
    assert replaced is True


def test_owner_alive_live_dead_reused_and_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = {
        "schema_id": runtime_temp.OWNER_SCHEMA_ID,
        "request_id": "req-x",
        "pid": 111,
        "starttime": 111,
    }
    monkeypatch.setattr(runtime_temp, "_pid_alive", lambda pid: True)
    assert runtime_temp.owner_alive(manifest) is True

    # Dead PID with matching start identity -> dead.  The reuse check reads the
    # current start ticks through the single cross-platform seam
    # ``process_start_ticks`` (Linux /proc, Darwin sysctl, Windows
    # GetProcessTimes); mocking the Linux-only ``_proc_stat_starttime`` would
    # leave that seam live on macOS/Windows, where a fake low PID resolves to a
    # real or absent process and the assertion becomes platform-dependent.
    monkeypatch.setattr(runtime_temp, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(runtime_temp, "process_start_ticks", lambda pid: 111)
    assert runtime_temp.owner_alive(manifest) is False

    # Dead PID but start identity mismatch (PID reuse) -> fail closed (live).
    monkeypatch.setattr(runtime_temp, "process_start_ticks", lambda pid: 999)
    assert runtime_temp.owner_alive(manifest) is True

    # Unknown owner (no pid) -> never deleted.
    assert runtime_temp.owner_alive({"schema_id": runtime_temp.OWNER_SCHEMA_ID}) is True


def test_identify_dead_owner_dirs_skips_live_and_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    temp = runtime_temp.temp_root(repo)
    live = _write_manifest(
        temp / "validation" / "live", "live", pid=111, starttime=111, namespace="validation"
    )
    dead = _write_manifest(
        temp / "validation" / "dead", "dead", pid=222, starttime=222, namespace="validation"
    )
    unknown = temp / "validation" / "unknown"
    unknown.mkdir(parents=True)  # no owner manifest -> never deleted

    monkeypatch.setattr(runtime_temp, "_pid_alive", lambda pid: {111: True, 222: False}.get(pid))
    # Mock the cross-platform identity seam, not the Linux-only /proc reader, so
    # the reuse check is deterministic on Linux, macOS and Windows alike.
    monkeypatch.setattr(runtime_temp, "process_start_ticks", lambda pid: pid)

    dead_dirs = runtime_temp.identify_dead_owner_dirs(repo)
    paths = {Path(item["path"]) for item in dead_dirs}
    assert paths == {dead}
    assert live not in paths
    assert unknown not in paths


def test_dispose_request_temp_removes_exact_and_rejects_escape(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    layout = runtime_temp.provision_request_temp(repo, "req-dispose", namespace="worker")
    root = layout.root

    assert runtime_temp.dispose_request_temp(
        root, repo=repo, expected_request_id="req-dispose"
    ) is True
    assert not root.exists()

    # A path that escapes the repo temp authority must fail closed.
    outsider = tmp_path / "outside"
    outsider.mkdir()
    with pytest.raises(runtime_temp.RuntimeTempError):
        runtime_temp.dispose_request_temp(outsider, repo=repo)

    # Identity mismatch fails closed and leaves the directory in place.
    other = runtime_temp.provision_request_temp(repo, "req-keep", namespace="worker")
    with pytest.raises(runtime_temp.RuntimeTempError):
        runtime_temp.dispose_request_temp(
            other.root, repo=repo, expected_request_id="req-wrong"
        )
    assert other.root.exists()


def test_quota_is_bounded(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for index in range(3):
        runtime_temp.provision_request_temp(repo, f"req-{index}", namespace="worker")

    info = runtime_temp.quota(repo)
    assert info["dirs"] == 3
    assert info["bytes"] >= 0
    assert info["max_dirs"] > 0
    assert info["within_quota"] is True


def test_enforce_gc_removes_only_exact_dead_owner_temp_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    assert task_store.initialize_repository(repo)["ok"]

    temp = runtime_temp.temp_root(repo)
    dead = _write_manifest(
        temp / "validation" / "dead-gc",
        "dead-gc",
        pid=222,
        starttime=222,
        namespace="validation",
    )
    live = _write_manifest(
        temp / "validation" / "live-gc",
        "live-gc",
        pid=111,
        starttime=111,
        namespace="validation",
    )
    unknown = temp / "validation" / "unknown-gc"
    unknown.mkdir(parents=True)

    monkeypatch.setattr(runtime_temp, "_pid_alive", lambda pid: {111: True, 222: False}.get(pid))
    # Mock the cross-platform identity seam, not the Linux-only /proc reader, so
    # the GC reclaims only provably-dead owners identically on every platform.
    monkeypatch.setattr(runtime_temp, "process_start_ticks", lambda pid: pid)

    result = terminal_log_retention.enforce(repo)
    assert result["ok"] is True
    assert result["temp_gc_count"] == 1
    assert not dead.exists()
    assert live.exists()
    assert unknown.exists()


def test_now_utc_is_injectable_and_manifest_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retention/manifest timestamps come from the injectable clock, never from
    filesystem mtimes (which the outer validation sandbox denies mutating)."""
    frozen = datetime(2036, 5, 4, 3, 2, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime_temp, "now_utc", lambda: frozen)
    assert runtime_temp.now_utc() == frozen

    repo = _repo(tmp_path)
    layout = runtime_temp.provision_request_temp(repo, "req-clock", namespace="worker")
    manifest = runtime_temp.read_owner_manifest(layout.root)
    assert manifest is not None
    assert manifest["created_at"] == frozen.isoformat()


def test_worker_workspace_direct_script_resolves_sibling_runtime_temp() -> None:
    """worker_workspace.py is executed directly as the Landlock wrapper, so its
    bootstrap must resolve the authenticated sibling runtime_temp.py rather than
    a package-relative or installed fallback."""
    from aiworkhub import worker_workspace

    original_runtime_temp = sys.modules.get("aiworkhub.runtime_temp")
    ws_py = Path(worker_workspace.__file__).resolve()
    spec = importlib.util.spec_from_file_location("_direct_worker_workspace", ws_py)
    assert spec is not None and spec.loader is not None
    direct = importlib.util.module_from_spec(spec)
    sys.modules["_direct_worker_workspace"] = direct
    try:
        spec.loader.exec_module(direct)
        assert direct.runtime_temp is sys.modules["aiworkhub.runtime_temp"]
        assert Path(direct.runtime_temp.__file__).resolve() == ws_py.parent / "runtime_temp.py"
        assert direct.runtime_temp.TEMP_ROOT_ENV == "AIWORKHUB_TEMP_ROOT"
    finally:
        if original_runtime_temp is not None:
            sys.modules["aiworkhub.runtime_temp"] = original_runtime_temp
        else:
            sys.modules.pop("aiworkhub.runtime_temp", None)
        sys.modules.pop("_direct_worker_workspace", None)
