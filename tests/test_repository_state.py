from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from aiworkhub import repository_state as rs
from aiworkhub.storage_registry import load_storage_registry, resolve_database_path


def _git_init(path: Path) -> None:
    path.mkdir(parents=True)
    (path / ".git").mkdir()


def test_two_same_named_repositories_keep_distinct_ids(tmp_path: Path) -> None:
    first = tmp_path / "one" / "AIWorkHub"
    second = tmp_path / "two" / "AIWorkHub"
    _git_init(first)
    _git_init(second)

    a = rs.bootstrap_repository(first, repo_id="repo_a0000000000000000000000000000001")
    b = rs.bootstrap_repository(second, repo_id="repo_b0000000000000000000000000000002")

    assert a.root.name == b.root.name == "AIWorkHub"
    assert a.manifest.repo_id != b.manifest.repo_id
    assert rs.inspect_repository(first).manifest.repo_id == a.manifest.repo_id
    assert rs.inspect_repository(second).manifest.repo_id == b.manifest.repo_id


def test_repo_id_survives_directory_move(tmp_path: Path) -> None:
    original = tmp_path / "before" / "project"
    _git_init(original)
    state = rs.bootstrap_repository(original, repo_id="repo_move_stable_001")
    moved = tmp_path / "after" / "renamed"
    moved.parent.mkdir()
    original.rename(moved)

    inspected = rs.inspect_repository(moved)
    assert inspected.manifest.repo_id == state.manifest.repo_id
    assert inspected.root == moved.resolve()


def test_missing_and_invalid_manifests_fail_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _git_init(repo)
    with pytest.raises(rs.ManifestMissingError):
        rs.inspect_repository(repo)

    hub = repo / rs.HUB_DIRNAME
    hub.mkdir()
    manifest_path = hub / "project.json"
    manifest_path.write_text('{"schema_id":"wrong"}\n', encoding="utf-8")
    with pytest.raises(rs.ManifestInvalidError, match="manifest_schema_id_invalid"):
        rs.inspect_repository(repo)

    manifest_path.write_bytes(b"{not-json")
    with pytest.raises(rs.ManifestInvalidError, match="manifest_invalid_json"):
        rs.inspect_repository(repo)

    manifest_path.write_bytes(b"\xff\xfe")
    with pytest.raises(rs.ManifestInvalidError, match="manifest_invalid_utf8"):
        rs.inspect_repository(repo)


def test_manifest_bom_and_read_failures_preserve_fail_closed_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "C_drive" / "work" / "repo"
    _git_init(repo)
    state = rs.bootstrap_repository(repo, repo_id="repo_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    manifest_path = repo / rs.PROJECT_MANIFEST_REL
    original = manifest_path.read_bytes()
    manifest_path.write_bytes(b"\xef\xbb\xbf" + original)
    with_bom = manifest_path.read_bytes()

    assert rs.inspect_repository(repo).manifest.repo_id == state.manifest.repo_id
    assert manifest_path.read_bytes() == with_bom

    original_read = os.read
    manifest_fd: int | None = None

    def denied(fd: int, size: int) -> bytes:
        if fd == manifest_fd:
            raise PermissionError("access denied")
        return original_read(fd, size)

    original_open = os.open

    def capture_open(path: os.PathLike[str] | str, flags: int, mode: int = 0o777) -> int:
        nonlocal manifest_fd
        fd = original_open(path, flags, mode)
        if Path(path) == manifest_path:
            manifest_fd = fd
        return fd

    monkeypatch.setattr(os, "open", capture_open)
    monkeypatch.setattr(os, "read", denied)
    with pytest.raises(rs.ManifestInvalidError, match="manifest_unreadable"):
        rs.inspect_repository(repo)
    assert manifest_path.read_bytes() == with_bom


def test_manifest_replacement_between_lstat_and_open_is_rejected_without_fd_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "C_drive" / "work" / "repo"
    _git_init(repo)
    owner_id = "repo_cccccccccccccccccccccccccccccccc"
    external_id = "repo_dddddddddddddddddddddddddddddddd"
    rs.bootstrap_repository(repo, repo_id=owner_id)
    manifest_path = repo / rs.PROJECT_MANIFEST_REL
    original_bytes = manifest_path.read_bytes()
    external_manifest = tmp_path / "D_drive" / "external-project.json"
    external_manifest.parent.mkdir()
    payload = json.loads(original_bytes.decode("utf-8"))
    payload["repo_id"] = external_id
    external_manifest.write_text(json.dumps(payload), encoding="utf-8")

    original_open = os.open
    barrier_ran = False
    opened_fd: int | None = None

    def replace_then_open(path: os.PathLike[str] | str, flags: int, mode: int = 0o777) -> int:
        nonlocal barrier_ran, opened_fd
        if Path(path) == manifest_path and not barrier_ran:
            barrier_ran = True
            manifest_path.unlink()
            external_manifest.replace(manifest_path)
        opened_fd = original_open(path, flags, mode)
        return opened_fd

    monkeypatch.setattr(os, "open", replace_then_open)
    with pytest.raises(rs.ManifestInvalidError, match="manifest_unreadable"):
        rs.inspect_repository(repo)

    assert barrier_ran
    assert opened_fd is not None
    with pytest.raises(OSError):
        os.fstat(opened_fd)
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["repo_id"] == external_id
    assert owner_id != external_id


def test_resolver_precedence_explicit_env_manifest_then_git(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    env_repo = tmp_path / "env"
    cwd_repo = tmp_path / "cwd"
    for path in (explicit, env_repo, cwd_repo):
        _git_init(path)
        rs.bootstrap_repository(path, repo_id=f"repo_{path.name}_123456789")
    nested = cwd_repo / "sub" / "dir"
    nested.mkdir(parents=True)

    env = {"AIWORKHUB_REPO_ROOT": str(env_repo)}
    assert rs.resolve_repository_root(explicit, cwd=nested, env=env) == explicit.resolve()
    assert rs.resolve_repository_root(cwd=nested, env=env) == env_repo.resolve()
    assert rs.resolve_repository_root(cwd=nested, env={}) == cwd_repo.resolve()

    no_manifest_git = tmp_path / "git-only"
    _git_init(no_manifest_git)
    with pytest.raises(rs.ManifestMissingError):
        rs.resolve_repository_root(cwd=no_manifest_git, env={})
    assert rs.resolve_repository_root(cwd=no_manifest_git, env={}, require_manifest=False) == no_manifest_git.resolve()


def test_bootstrap_is_non_destructive_and_manifest_write_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _git_init(repo)
    created = rs.bootstrap_repository(repo, repo_id="repo_atomic_success")
    with pytest.raises(rs.ManifestExistsError):
        rs.bootstrap_repository(repo, repo_id=created.manifest.repo_id)
    assert rs.inspect_repository(repo).manifest.repo_id == "repo_atomic_success"

    failing = tmp_path / "failing"
    _git_init(failing)

    def boom(_src: str, _dst: str) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        rs.bootstrap_repository(failing, repo_id="repo_atomic_failure")
    assert not (failing / rs.PROJECT_MANIFEST_REL).exists()
    assert not list((failing / rs.HUB_DIRNAME).glob(".project.json.*.tmp"))


def test_expected_repo_id_blocks_silent_adoption(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _git_init(repo)
    rs.bootstrap_repository(repo, repo_id="repo_owner_a")
    with pytest.raises(rs.ManifestInvalidError):
        rs.inspect_repository(repo, expected_repo_id="repo_owner_b")
    with pytest.raises(rs.ManifestInvalidError):
        rs.bootstrap_repository(repo, repo_id="repo_owner_b")


def test_path_traversal_and_symlink_escapes_are_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _git_init(repo)
    (repo / rs.HUB_DIRNAME).symlink_to(tmp_path)
    with pytest.raises(rs.PathEscapeError):
        rs.bootstrap_repository(repo, repo_id="repo_symlink_rejected")

    other = tmp_path / "other"
    _git_init(other)
    rs.bootstrap_repository(other, repo_id="repo_layout_rejected")
    manifest_path = other / rs.PROJECT_MANIFEST_REL
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["layout"]["durable"]["kb"] = "../outside"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises((rs.ManifestInvalidError, rs.PathEscapeError)):
        rs.inspect_repository(other)


def test_broken_nearest_manifest_symlink_prevents_parent_fallback(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    _git_init(parent)
    rs.bootstrap_repository(parent, repo_id="repo_parent_authority")

    nested = parent / "nested"
    marker_dir = nested / rs.HUB_DIRNAME
    marker_dir.mkdir(parents=True)
    (marker_dir / "project.json").symlink_to(nested / "missing-project.json")

    with pytest.raises(rs.PathEscapeError, match="marker_symlink_component"):
        rs.resolve_repository_root(cwd=nested, env={})


def test_bootstrap_does_not_discover_or_adopt_a_planted_legacy_path(tmp_path: Path) -> None:
    """B878: this repository state carries no legacy-discovery feature at
    all -- a planted ``bitnnv2/data/tasking`` legacy path must be left
    completely untouched and unreferenced by bootstrap, not surfaced as a
    read-only "candidate" (that mechanism was removed; ``RepositoryState``
    has no such field)."""
    repo = tmp_path / "repo"
    _git_init(repo)
    legacy = repo / "bitnnv2" / "data" / "tasking"
    legacy.mkdir(parents=True)
    (legacy / "machine_task_cards_v1.jsonl").write_text("legacy\n", encoding="utf-8")
    before = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*"))

    state = rs.bootstrap_repository(repo, repo_id="repo_legacy_readonly")
    after = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*"))

    assert not hasattr(state, "legacy_candidates")
    assert state.manifest.to_json()["security"]["automatic_legacy_discovery"] is False
    registry = load_storage_registry(repo)
    assert "bitnnv2" not in json.dumps(registry.payload)
    # The legacy directory is neither deleted nor rewritten: bootstrap only
    # ever ADDS its own canonical .aiworkhub tree.
    assert "bitnnv2/data/tasking/machine_task_cards_v1.jsonl" in after
    assert set(before).issubset(after)


def test_bootstrap_creates_repository_local_store_registry(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _git_init(repo)
    state = rs.bootstrap_repository(repo, repo_id="repo_store_registry")

    registry = load_storage_registry(state.root, expected_repo_id=state.manifest.repo_id)
    task_db = resolve_database_path(registry, "task_queue")

    assert registry.registry_path == state.hub_dir / "config" / "storage.json"
    assert registry.payload["repo_id"] == "repo_store_registry"
    assert task_db == state.hub_dir / "tasking" / "task_queue.sqlite"
    assert str(task_db).startswith(str(state.root))


# ---------------------------------------------------------------------------
# NF833: one bounded fresh-descriptor recovery for transient manifest I/O.
# ---------------------------------------------------------------------------

_ERROR_INVALID_HANDLE = 6
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33
_ERROR_ACCESS_DENIED = 5


def _win32_error(winerror: int, message: str) -> OSError:
    """Build an OSError shaped the way CPython raises one on Win32."""
    error = PermissionError(errno.EACCES, message)
    error.winerror = winerror
    return error


def _fail_manifest_opens(
    monkeypatch: pytest.MonkeyPatch,
    manifest_path: Path,
    errors: list[OSError],
) -> list[int]:
    """Raise ``errors`` in order on the first opens of ``manifest_path``.

    Once the queue is exhausted every further open -- including the fresh
    descriptor of a retry -- behaves normally, so recovery is proven by the
    very next read rather than by any sleep, backoff or timing.  The returned
    list records one entry per attempted manifest open.
    """

    original_open = os.open
    attempts: list[int] = []
    pending = list(errors)

    def flaky_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        **kwargs: object,
    ) -> int:
        if Path(path) == manifest_path:
            attempts.append(flags)
            if pending:
                raise pending.pop(0)
        return original_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", flaky_open)
    return attempts


@pytest.mark.parametrize(
    "build_error",
    [
        lambda: _win32_error(_ERROR_INVALID_HANDLE, "The handle is invalid"),
        lambda: _win32_error(_ERROR_SHARING_VIOLATION, "used by another process"),
        lambda: _win32_error(_ERROR_LOCK_VIOLATION, "lock violation"),
        lambda: OSError(errno.EINTR, "Interrupted system call"),
    ],
    ids=["win32_invalid_handle", "win32_sharing_violation", "win32_lock_violation", "posix_eintr"],
)
def test_transient_manifest_fault_recovers_on_one_fresh_descriptor_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_error,
) -> None:
    repo = tmp_path / "C_drive" / "work" / "repo"
    _git_init(repo)
    state = rs.bootstrap_repository(repo, repo_id="repo_" + "e" * 32)
    manifest_path = repo / rs.PROJECT_MANIFEST_REL
    attempts = _fail_manifest_opens(monkeypatch, manifest_path, [build_error()])

    recovered = rs.inspect_repository(repo)

    assert recovered.manifest.repo_id == state.manifest.repo_id
    # Exactly one retry, and it opened its own descriptor with the same
    # no-follow authority as the first attempt.
    assert len(attempts) == 2
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    assert all((flags & nofollow) == nofollow for flags in attempts)


@pytest.mark.parametrize(
    "build_error",
    [
        lambda: OSError(errno.EIO, "Input/output error"),
        lambda: OSError(errno.ENODEV, "No such device"),
        lambda: PermissionError(errno.EPERM, "Operation not permitted"),
        lambda: PermissionError(errno.EACCES, "Permission denied"),
        lambda: _win32_error(_ERROR_ACCESS_DENIED, "Access is denied"),
        lambda: FileNotFoundError(errno.ENOENT, "The system cannot find the file"),
    ],
    ids=["eio", "enodev", "eperm", "eacces", "win32_access_denied", "enoent_after_prevalidation"],
)
def test_non_transient_manifest_faults_are_never_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_error,
) -> None:
    repo = tmp_path / "C_drive" / "work" / "repo"
    _git_init(repo)
    rs.bootstrap_repository(repo, repo_id="repo_" + "f" * 32)
    manifest_path = repo / rs.PROJECT_MANIFEST_REL
    attempts = _fail_manifest_opens(monkeypatch, manifest_path, [build_error()])

    with pytest.raises(rs.ManifestInvalidError, match="manifest_unreadable"):
        rs.inspect_repository(repo)

    assert len(attempts) == 1


def test_identity_change_on_the_second_attempt_stays_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "C_drive" / "work" / "repo"
    _git_init(repo)
    owner_id = "repo_" + "1" * 32
    external_id = "repo_" + "2" * 32
    rs.bootstrap_repository(repo, repo_id=owner_id)
    manifest_path = repo / rs.PROJECT_MANIFEST_REL
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["repo_id"] = external_id
    external = tmp_path / "D_drive" / "external-project.json"
    external.parent.mkdir(parents=True)
    external.write_text(json.dumps(payload), encoding="utf-8")

    original_open = os.open
    attempts: list[int] = []

    def flaky_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        **kwargs: object,
    ) -> int:
        if Path(path) == manifest_path:
            attempts.append(flags)
            if len(attempts) == 1:
                raise _win32_error(_ERROR_SHARING_VIOLATION, "used by another process")
            if len(attempts) == 2:
                # The retry has already taken its own lstat; swap a foreign
                # repository's manifest in underneath it so the descriptor it
                # is about to receive no longer carries that dev/ino.
                manifest_path.unlink()
                external.replace(manifest_path)
        return original_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", flaky_open)
    with pytest.raises(rs.ManifestInvalidError, match="manifest_unreadable"):
        rs.inspect_repository(repo)

    assert len(attempts) == 2
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["repo_id"] == external_id
    assert owner_id != external_id


def test_transient_fault_never_retries_invalid_payload_into_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _git_init(repo)
    rs.bootstrap_repository(repo, repo_id="repo_" + "3" * 32)
    manifest_path = repo / rs.PROJECT_MANIFEST_REL
    manifest_path.write_bytes(b"{not-json")
    attempts = _fail_manifest_opens(
        monkeypatch,
        manifest_path,
        [_win32_error(_ERROR_SHARING_VIOLATION, "used by another process")],
    )

    with pytest.raises(rs.ManifestInvalidError, match="manifest_invalid_json"):
        rs.inspect_repository(repo)

    assert len(attempts) == 2


def test_transient_fault_never_retries_foreign_identity_into_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _git_init(repo)
    rs.bootstrap_repository(repo, repo_id="repo_" + "4" * 32)
    manifest_path = repo / rs.PROJECT_MANIFEST_REL
    attempts = _fail_manifest_opens(
        monkeypatch,
        manifest_path,
        [_win32_error(_ERROR_LOCK_VIOLATION, "lock violation")],
    )

    with pytest.raises(rs.ManifestInvalidError, match="repo_id_mismatch"):
        rs.inspect_repository(repo, expected_repo_id="repo_" + "5" * 32)

    assert len(attempts) == 2
