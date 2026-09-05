from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import sync_aiworkhub_tool_policy as policy_sync  # noqa: E402


def copy_policy_tree(tmp_path: Path) -> None:
    for relative_path in (policy_sync.POLICY_SOURCE, *policy_sync.HOST_FILES):
        destination = tmp_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative_path, destination)


def write_policy_tree_with_host_prose(tmp_path: Path) -> dict[Path, tuple[bytes, bytes]]:
    canonical = (REPO_ROOT / policy_sync.POLICY_SOURCE).read_bytes()
    (tmp_path / policy_sync.POLICY_SOURCE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / policy_sync.POLICY_SOURCE).write_bytes(canonical)

    surrounding = {
        Path("AGENTS.md"): (b"agents prefix\n", b"\nagents suffix\n"),
        Path("CLAUDE.md"): (b"claude prefix\n", b"\nclaude suffix\n"),
        Path(".github/copilot-instructions.md"): (b"copilot prefix\n", b"\ncopilot suffix\n"),
    }
    drifted = policy_sync.generated_block(canonical.replace(b"Stop at Codex review.\n", b"Stop at drift review.\n"))
    for relative_path, (prefix, suffix) in surrounding.items():
        destination = tmp_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(prefix + drifted + suffix)
    return surrounding


def staged_policy_artifacts(root: Path) -> list[Path]:
    return [
        path
        for directory in [root, root / ".github"]
        for path in directory.iterdir()
        if ".aiworkhub-policy-sync." in path.name
    ]


def test_repository_policy_blocks_match_canonical_source() -> None:
    assert policy_sync.check(REPO_ROOT) == []


def test_check_detects_drift_without_writing(tmp_path: Path) -> None:
    copy_policy_tree(tmp_path)

    drifted_path = tmp_path / policy_sync.HOST_FILES[0]
    original = drifted_path.read_bytes()
    drifted_path.write_bytes(original.replace(b"Stop at Codex review.", b"Stop at manager review."))

    errors = policy_sync.check(tmp_path)

    assert errors == [f"{policy_sync.HOST_FILES[0]}: policy block differs from {policy_sync.POLICY_SOURCE}"]
    assert drifted_path.read_bytes() != original


def test_check_rejects_host_symlink_without_opening_target(tmp_path: Path, monkeypatch) -> None:
    copy_policy_tree(tmp_path)

    host = policy_sync.HOST_FILES[0]
    host_path = tmp_path / host
    target_path = tmp_path / "host-target.md"
    target_path.write_bytes(host_path.read_bytes())
    host_path.unlink()
    host_path.symlink_to(target_path)

    real_open = policy_sync.os.open
    opened_paths = []

    def record_open(path: Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        opened_paths.append(Path(path))
        if Path(path) == target_path:
            raise AssertionError("host symlink target must not be opened")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(policy_sync.os, "open", record_open)

    assert policy_sync.check(tmp_path) == [f"{host}: refusing to sync through symlink"]
    assert target_path not in opened_paths
    assert Path(host.name) in opened_paths


def test_check_fails_closed_without_nofollow_before_opening_target(
    tmp_path: Path, monkeypatch
) -> None:
    copy_policy_tree(tmp_path)
    host = policy_sync.HOST_FILES[0]
    monkeypatch.setattr(policy_sync, "platform_io", None)
    monkeypatch.delattr(policy_sync.os, "O_NOFOLLOW", raising=False)

    real_open = policy_sync.os.open
    opened_paths = []

    def reject_open(path: Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        opened_paths.append(Path(path))
        if flags & getattr(os, "O_DIRECTORY", 0):
            return real_open(path, flags, mode, dir_fd=dir_fd)
        raise AssertionError("path must not be opened without no-follow enforcement")

    monkeypatch.setattr(policy_sync.os, "open", reject_open)

    assert policy_sync.check(tmp_path, (host,)) == [
        (
            f"{policy_sync.POLICY_SOURCE}: cannot authenticate parent directory "
            "without an enforceable no-follow open primitive"
        )
    ]
    assert Path("docs") not in opened_paths


def test_check_routes_missing_nofollow_symlink_rejection_through_platform_io(
    tmp_path: Path, monkeypatch
) -> None:
    copy_policy_tree(tmp_path)
    host = policy_sync.HOST_FILES[0]
    monkeypatch.delattr(policy_sync.os, "O_NOFOLLOW", raising=False)

    real_open = policy_sync.os.open
    opened_paths = []
    platform_calls = []

    class PlatformIO:
        @staticmethod
        def read_regular_file_snapshot(path: Path, relative_path: Path) -> policy_sync.FileSnapshot:
            platform_calls.append((path, relative_path))
            raise OSError(policy_sync.errno.ELOOP, "reparse point")

    def reject_open(path: Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        opened_paths.append(Path(path))
        if flags & getattr(os, "O_DIRECTORY", 0):
            return real_open(path, flags, mode, dir_fd=dir_fd)
        raise AssertionError("platform_io rejection must not fall back to os.open")

    monkeypatch.setattr(policy_sync, "platform_io", PlatformIO)
    monkeypatch.setattr(policy_sync.os, "open", reject_open)

    assert policy_sync.check(tmp_path, (host,)) == [
        (
            f"{policy_sync.POLICY_SOURCE}: cannot authenticate parent directory "
            "without an enforceable no-follow open primitive"
        )
    ]
    assert platform_calls == []
    assert Path("docs") not in opened_paths


def test_check_does_not_rewrite_host_prefix_or_suffix_prose(tmp_path: Path) -> None:
    write_policy_tree_with_host_prose(tmp_path)
    originals = {
        relative_path: (tmp_path / relative_path).read_bytes() for relative_path in policy_sync.HOST_FILES
    }

    errors = policy_sync.check(tmp_path)

    assert errors == [
        f"{relative_path}: policy block differs from {policy_sync.POLICY_SOURCE}"
        for relative_path in policy_sync.HOST_FILES
    ]
    assert {
        relative_path: (tmp_path / relative_path).read_bytes() for relative_path in policy_sync.HOST_FILES
    } == originals


def test_sync_preserves_host_prefix_and_suffix_prose(tmp_path: Path, monkeypatch) -> None:
    surrounding = write_policy_tree_with_host_prose(tmp_path)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")

    changed = policy_sync.sync(tmp_path)

    assert changed == list(policy_sync.HOST_FILES)
    canonical = policy_sync.read_canonical(tmp_path)
    generated = policy_sync.generated_block(canonical)
    for relative_path, (prefix, suffix) in surrounding.items():
        assert (tmp_path / relative_path).read_bytes() == prefix + generated + suffix


def test_sync_preserves_surrounding_host_prose_when_migrating_marker_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    canonical = (REPO_ROOT / policy_sync.POLICY_SOURCE).read_bytes()
    (tmp_path / policy_sync.POLICY_SOURCE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / policy_sync.POLICY_SOURCE).write_bytes(canonical)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")

    for relative_path in policy_sync.HOST_FILES:
        destination = tmp_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            b"host prose before\n"
            + policy_sync.START_MARKER_BYTES
            + b"\nmanaged drift\n"
            + policy_sync.END_MARKER_BYTES
            + b"\nhost prose after\n"
        )

    changed = policy_sync.sync(tmp_path)

    assert changed == list(policy_sync.HOST_FILES)
    for relative_path in policy_sync.HOST_FILES:
        data = (tmp_path / relative_path).read_bytes()
        assert data.startswith(b"host prose before\n")
        assert data.endswith(b"\nhost prose after\n")
        assert data == (
            b"host prose before\n"
            + policy_sync.generated_block(canonical)
            + b"\nhost prose after\n"
        )


def test_sync_preserves_host_file_mode(tmp_path: Path, monkeypatch) -> None:
    copy_policy_tree(tmp_path)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")

    drifted_path = tmp_path / policy_sync.HOST_FILES[0]
    original = drifted_path.read_bytes()
    drifted = original.replace(b"Stop at Codex review.", b"Stop at manager review.")
    drifted_path.unlink()
    fd = policy_sync.open_new_staged_file(drifted_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as drifted_file:
        drifted_file.write(drifted)

    changed = policy_sync.sync(tmp_path)

    assert changed == [policy_sync.HOST_FILES[0]]
    assert drifted_path.read_bytes() == original
    assert drifted_path.stat().st_mode & 0o7777 == 0o644


def test_open_new_staged_file_does_not_change_process_umask(tmp_path: Path) -> None:
    original_umask = os.umask(0o077)
    os.umask(original_umask)

    path = tmp_path / "staged"
    fd = policy_sync.open_new_staged_file(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    os.close(fd)

    after_umask = os.umask(original_umask)
    os.umask(after_umask)
    assert after_umask == original_umask
    assert path.stat().st_mode & 0o7777 == 0o644


def test_open_new_staged_file_does_not_relax_concurrent_file_creation_permissions(
    tmp_path: Path,
) -> None:
    created_path = tmp_path / "concurrent"
    start = threading.Event()
    done = threading.Event()
    observed_mode = None

    def create_concurrent_file() -> None:
        nonlocal observed_mode
        start.wait(timeout=5)
        fd = os.open(created_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        os.close(fd)
        observed_mode = created_path.stat().st_mode & 0o7777
        done.set()

    original_umask = os.umask(0o077)
    try:
        worker = threading.Thread(target=create_concurrent_file)
        worker.start()
        staged_fd = policy_sync.open_new_staged_file(
            tmp_path / "staged", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644
        )
        try:
            start.set()
            assert done.wait(timeout=5)
        finally:
            os.close(staged_fd)
            worker.join(timeout=5)
    finally:
        os.umask(original_umask)

    assert observed_mode == 0o600


def test_check_detects_crlf_policy_block_byte_drift_without_writing(tmp_path: Path) -> None:
    copy_policy_tree(tmp_path)

    drifted_path = tmp_path / policy_sync.HOST_FILES[0]
    original = drifted_path.read_bytes()
    block = policy_sync.extract_block(original, policy_sync.HOST_FILES[0])
    drifted_block = original[block.start : block.end].replace(b"\n", b"\r\n")
    drifted_path.write_bytes(original[: block.start] + drifted_block + original[block.end :])

    errors = policy_sync.check(tmp_path)

    assert errors == [f"{policy_sync.HOST_FILES[0]}: policy block differs from {policy_sync.POLICY_SOURCE}"]
    assert drifted_path.read_bytes() != original


def test_check_detects_single_newline_after_start_marker_byte_drift_without_writing(
    tmp_path: Path,
) -> None:
    copy_policy_tree(tmp_path)

    drifted_path = tmp_path / policy_sync.HOST_FILES[0]
    original = drifted_path.read_bytes()
    block = policy_sync.extract_block(original, policy_sync.HOST_FILES[0])
    marker_end = block.start + len(policy_sync.START_MARKER_BYTES)
    drifted = original[:marker_end] + b"\r\n" + original[marker_end + 1 :]
    drifted_path.write_bytes(drifted)

    errors = policy_sync.check(tmp_path)

    assert errors == [f"{policy_sync.HOST_FILES[0]}: policy block differs from {policy_sync.POLICY_SOURCE}"]
    assert drifted_path.read_bytes() == drifted


def test_check_and_sync_reject_canonical_source_without_terminal_newline(tmp_path: Path) -> None:
    copy_policy_tree(tmp_path)

    source_path = tmp_path / policy_sync.POLICY_SOURCE
    original_source = source_path.read_bytes()
    assert original_source.endswith(b"\n")
    source_path.write_bytes(original_source.rstrip(b"\n"))
    originals = {relative_path: (tmp_path / relative_path).read_bytes() for relative_path in policy_sync.HOST_FILES}

    expected_error = f"{policy_sync.POLICY_SOURCE}: canonical policy source must end with newline"

    assert policy_sync.check(tmp_path) == [expected_error]
    try:
        policy_sync.sync(tmp_path)
    except policy_sync.PolicySyncError as exc:
        assert str(exc) == expected_error
    else:
        raise AssertionError("sync should reject a canonical source without a terminal newline")

    assert {relative_path: (tmp_path / relative_path).read_bytes() for relative_path in policy_sync.HOST_FILES} == originals


def test_check_detects_missing_duplicate_and_reordered_markers(tmp_path: Path) -> None:
    canonical = (REPO_ROOT / policy_sync.POLICY_SOURCE).read_bytes()
    (tmp_path / policy_sync.POLICY_SOURCE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / policy_sync.POLICY_SOURCE).write_bytes(canonical)

    fixtures = {
        Path("missing.md"): b"no generated block\n",
        Path("duplicate.md"): (
            policy_sync.generated_block(canonical)
            + b"\n"
            + policy_sync.generated_block(canonical)
            + b"\n"
        ),
        Path("reordered.md"): (
            policy_sync.END_MARKER_BYTES
            + b"\n"
            + canonical
            + policy_sync.START_MARKER_BYTES
            + b"\n"
        ),
    }
    for relative_path, data in fixtures.items():
        (tmp_path / relative_path).write_bytes(data)

    errors = policy_sync.check(tmp_path, tuple(fixtures))

    assert errors == [
        "missing.md: expected one policy block, found 0 start marker(s) and 0 end marker(s)",
        "duplicate.md: expected one policy block, found 2 start marker(s) and 2 end marker(s)",
        "reordered.md: policy markers are reordered",
    ]


def test_sync_preflights_all_hosts_before_writing(tmp_path: Path) -> None:
    copy_policy_tree(tmp_path)
    originals = {relative_path: (tmp_path / relative_path).read_bytes() for relative_path in policy_sync.HOST_FILES}

    first_host = tmp_path / policy_sync.HOST_FILES[0]
    first_host.write_bytes(originals[policy_sync.HOST_FILES[0]].replace(b"Stop at Codex review.", b"Stop at manager review."))
    originals[policy_sync.HOST_FILES[0]] = first_host.read_bytes()

    malformed_host = tmp_path / policy_sync.HOST_FILES[2]
    malformed_host.write_bytes(originals[policy_sync.HOST_FILES[2]].replace(policy_sync.END_MARKER_BYTES, b""))
    originals[policy_sync.HOST_FILES[2]] = malformed_host.read_bytes()

    try:
        policy_sync.sync(tmp_path)
    except policy_sync.PolicySyncError as exc:
        assert str(exc) == (
            ".github/copilot-instructions.md: expected one policy block, found "
            "1 start marker(s) and 0 end marker(s)"
        )
    else:
        raise AssertionError("sync should reject malformed later host before writing")

    assert {relative_path: (tmp_path / relative_path).read_bytes() for relative_path in policy_sync.HOST_FILES} == originals


def test_sync_requires_write_gate_before_mutating_valid_drift(tmp_path: Path, monkeypatch) -> None:
    copy_policy_tree(tmp_path)
    monkeypatch.delenv(policy_sync.WRITE_GATE_ENV, raising=False)
    env = os.environ.copy()
    env.pop(policy_sync.WRITE_GATE_ENV, None)

    drifted_path = tmp_path / policy_sync.HOST_FILES[0]
    original = drifted_path.read_bytes()
    drifted_path.write_bytes(original.replace(b"Stop at Codex review.", b"Stop at manager review."))
    drifted = drifted_path.read_bytes()

    try:
        policy_sync.sync(tmp_path)
    except policy_sync.PolicySyncError as exc:
        assert str(exc) == f"--sync requires {policy_sync.WRITE_GATE_ENV}=1"
    else:
        raise AssertionError("sync should require an explicit write gate")

    assert drifted_path.read_bytes() == drifted

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/sync_aiworkhub_tool_policy.py"), "--sync", "--root", str(tmp_path)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    assert f"- --sync requires {policy_sync.WRITE_GATE_ENV}=1\n" in result.stdout
    assert f"Run: {policy_sync.WRITE_GATE_ENV}=1 python scripts/sync_aiworkhub_tool_policy.py --sync\n" in result.stdout
    assert drifted_path.read_bytes() == drifted


def test_sync_rolls_back_all_hosts_after_second_staged_write_failure(tmp_path: Path, monkeypatch) -> None:
    copy_policy_tree(tmp_path)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")

    originals = {}
    for relative_path in policy_sync.HOST_FILES:
        path = tmp_path / relative_path
        original = path.read_bytes()
        path.write_bytes(original.replace(b"Stop at Codex review.", b"Stop at manager review."))
        originals[relative_path] = path.read_bytes()

    real_replace = policy_sync.os.replace
    host_names = {relative_path.name for relative_path in policy_sync.HOST_FILES}
    staged_write_count = 0

    def fail_second_staged_write(src: Path, dst: Path, *args, **kwargs) -> None:
        nonlocal staged_write_count
        src_path = Path(src)
        dst_path = Path(dst)
        if ".aiworkhub-policy-sync." in src_path.name and src_path.name.endswith(".tmp") and dst_path.name in host_names:
            staged_write_count += 1
            if staged_write_count == 2:
                raise OSError("injected second write failure")
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(policy_sync.os, "replace", fail_second_staged_write)

    try:
        policy_sync.sync(tmp_path)
    except policy_sync.PolicySyncError as exc:
        assert str(exc) == "sync failed before all hosts were updated: injected second write failure"
    else:
        raise AssertionError("sync should roll back after a later staged write failure")

    assert staged_write_count == 2
    assert {relative_path: (tmp_path / relative_path).read_bytes() for relative_path in policy_sync.HOST_FILES} == originals
    assert staged_policy_artifacts(tmp_path) == []


def test_sync_rollback_preserves_concurrent_edit_after_applied_host_changes(
    tmp_path: Path, monkeypatch
) -> None:
    copy_policy_tree(tmp_path)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")

    originals = {}
    for relative_path in policy_sync.HOST_FILES:
        path = tmp_path / relative_path
        original = path.read_bytes()
        path.write_bytes(original.replace(b"Stop at Codex review.", b"Stop at manager review."))
        originals[relative_path] = path.read_bytes()

    first_host = tmp_path / policy_sync.HOST_FILES[0]
    concurrent = originals[policy_sync.HOST_FILES[0]].replace(
        b"Stop at manager review.", b"Stop at concurrent review."
    )
    real_replace = policy_sync.os.replace
    host_names = {relative_path.name for relative_path in policy_sync.HOST_FILES}
    staged_write_count = 0

    def change_first_host_then_fail_second_install(src: Path, dst: Path, *args, **kwargs) -> None:
        nonlocal staged_write_count
        src_path = Path(src)
        dst_path = Path(dst)
        if ".aiworkhub-policy-sync." in src_path.name and src_path.name.endswith(".tmp") and dst_path.name in host_names:
            staged_write_count += 1
            if staged_write_count == 2:
                raise OSError("injected second write failure")
            real_replace(src, dst, *args, **kwargs)
            first_host.write_bytes(concurrent)
            return
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(policy_sync.os, "replace", change_first_host_then_fail_second_install)

    try:
        policy_sync.sync(tmp_path)
    except policy_sync.PolicySyncError as exc:
        message = str(exc)
    else:
        raise AssertionError("sync should fail closed when rollback sees a concurrent edit")

    assert "sync failed before all hosts were updated: injected second write failure" in message
    assert (
        f"{policy_sync.HOST_FILES[0]}: rollback conflict; installed file changed after sync failure"
        in message
    )
    assert staged_write_count == 2
    assert first_host.read_bytes() == concurrent


def test_sync_rejects_concurrent_host_edit_before_backup_without_overwriting(
    tmp_path: Path, monkeypatch
) -> None:
    copy_policy_tree(tmp_path)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")

    host = policy_sync.HOST_FILES[0]
    host_path = tmp_path / host
    original = host_path.read_bytes()
    drifted = original.replace(b"Stop at Codex review.", b"Stop at manager review.")
    concurrent = original.replace(b"Stop at Codex review.", b"Stop at concurrent review.")
    host_path.write_bytes(drifted)
    replacement_path = tmp_path / "replacement.md"
    replacement_path.write_bytes(concurrent)

    real_verify_staged_identity = policy_sync.verify_staged_identity
    verify_count = 0

    def edit_host_after_preflight(
        path: Path,
        expected: policy_sync.StagedFileIdentity,
        root: Path | None = None,
        relative_path: Path | None = None,
    ) -> None:
        nonlocal verify_count
        real_verify_staged_identity(path, expected, root, relative_path)
        verify_count += 1
        if verify_count == 2:
            host_path.write_bytes(concurrent)

    monkeypatch.setattr(policy_sync, "verify_staged_identity", edit_host_after_preflight)

    try:
        policy_sync.sync(tmp_path, (host,))
    except policy_sync.PolicySyncError as exc:
        message = str(exc)
    else:
        raise AssertionError("sync should reject a concurrent host edit before backup")

    assert message.startswith("sync failed before all hosts were updated: ")
    assert f"{host}: file changed after preflight; aborting without overwrite" in message
    assert host_path.read_bytes() == concurrent
    assert staged_policy_artifacts(tmp_path) == []


def test_sync_cleans_staged_temp_when_backup_reservation_fails(
    tmp_path: Path, monkeypatch
) -> None:
    copy_policy_tree(tmp_path)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")

    host = policy_sync.HOST_FILES[0]
    host_path = tmp_path / host
    original = host_path.read_bytes()
    drifted = original.replace(b"Stop at Codex review.", b"Stop at manager review.")
    host_path.write_bytes(drifted)

    def fail_backup_reservation(path: Path):
        raise policy_sync.PolicySyncError(f"{path}: injected backup reservation failure")

    monkeypatch.setattr(policy_sync, "reserve_private_backup_path", fail_backup_reservation)

    try:
        policy_sync.sync(tmp_path, (host,))
    except policy_sync.PolicySyncError as exc:
        assert str(exc) == f"{host_path}: injected backup reservation failure"
    else:
        raise AssertionError("sync should fail when backup reservation fails")

    assert host_path.read_bytes() == drifted
    assert staged_policy_artifacts(tmp_path) == []


def test_sync_cleans_staged_temp_when_backup_reservation_raises_oserror(
    tmp_path: Path, monkeypatch
) -> None:
    copy_policy_tree(tmp_path)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")

    host = policy_sync.HOST_FILES[0]
    host_path = tmp_path / host
    original = host_path.read_bytes()
    drifted = original.replace(b"Stop at Codex review.", b"Stop at manager review.")
    host_path.write_bytes(drifted)

    def fail_backup_reservation(path: Path):
        raise OSError("injected backup reservation os error")

    monkeypatch.setattr(policy_sync, "reserve_private_backup_path", fail_backup_reservation)

    try:
        policy_sync.sync(tmp_path, (host,))
    except policy_sync.PolicySyncError as exc:
        assert str(exc) == "injected backup reservation os error"
    else:
        raise AssertionError("sync should fail when backup reservation raises OSError")

    assert host_path.read_bytes() == drifted
    assert staged_policy_artifacts(tmp_path) == []


def test_sync_does_not_delete_replaced_staged_temp_after_backup_reservation_failure(
    tmp_path: Path, monkeypatch
) -> None:
    copy_policy_tree(tmp_path)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")

    host = policy_sync.HOST_FILES[0]
    host_path = tmp_path / host
    original = host_path.read_bytes()
    drifted = original.replace(b"Stop at Codex review.", b"Stop at manager review.")
    host_path.write_bytes(drifted)
    attacker_data = b"attacker replacement\n"

    staged_paths = []
    real_create_private_staged_file = policy_sync.create_private_staged_file

    def record_staged_path(path: Path, data: bytes, mode: int):
        staged_path, identity = real_create_private_staged_file(path, data, mode)
        staged_paths.append(staged_path)
        return staged_path, identity

    def replace_temp_then_fail_backup_reservation(path: Path):
        staged_paths[0].unlink()
        staged_paths[0].write_bytes(attacker_data)
        raise policy_sync.PolicySyncError(f"{path}: injected backup reservation failure")

    monkeypatch.setattr(policy_sync, "create_private_staged_file", record_staged_path)
    monkeypatch.setattr(policy_sync, "reserve_private_backup_path", replace_temp_then_fail_backup_reservation)

    try:
        policy_sync.sync(tmp_path, (host,))
    except policy_sync.PolicySyncError as exc:
        assert str(exc) == f"{host_path}: injected backup reservation failure"
    else:
        raise AssertionError("sync should fail when backup reservation fails")

    assert host_path.read_bytes() == drifted
    assert staged_paths[0].read_bytes() == attacker_data


def test_sync_rejects_host_swap_between_fd_read_and_path_snapshot_without_overwriting(
    tmp_path: Path, monkeypatch
) -> None:
    copy_policy_tree(tmp_path)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")

    host = policy_sync.HOST_FILES[0]
    host_path = tmp_path / host
    original = host_path.read_bytes()
    drifted = original.replace(b"Stop at Codex review.", b"Stop at manager review.")
    concurrent = original.replace(b"Stop at Codex review.", b"Stop at concurrent review.")
    host_path.write_bytes(drifted)
    replacement_path = tmp_path / "replacement.md"
    replacement_path.write_bytes(concurrent)

    real_stat = policy_sync.os.stat
    stat_calls = 0

    def swap_host_before_snapshot(path: Path, *args, **kwargs):
        nonlocal stat_calls
        if Path(path) == Path(host.name):
            stat_calls += 1
            if stat_calls == 1:
                os.replace(replacement_path, host_path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(policy_sync.os, "stat", swap_host_before_snapshot)

    try:
        policy_sync.sync(tmp_path, (host,))
    except policy_sync.PolicySyncError as exc:
        assert str(exc) == f"{host}: file identity changed while authenticating"
    else:
        raise AssertionError("sync should reject a host replacement during preflight snapshot")

    assert host_path.read_bytes() == concurrent
    assert staged_policy_artifacts(tmp_path) == []


def test_sync_succeeds_when_final_backup_cleanup_fails(tmp_path: Path, monkeypatch) -> None:
    copy_policy_tree(tmp_path)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")

    originals = {}
    for relative_path in policy_sync.HOST_FILES:
        path = tmp_path / relative_path
        original = path.read_bytes()
        path.write_bytes(original.replace(b"Stop at Codex review.", b"Stop at manager review."))
        originals[relative_path] = original

    real_authenticated_unlink = policy_sync.authenticated_unlink
    backup_unlink_attempts = []

    def fail_backup_unlink(
        path: Path,
        root: Path | None = None,
        relative_path: Path | None = None,
    ) -> None:
        if ".aiworkhub-policy-sync." in path.name and path.name.endswith(".bak") and path.exists():
            backup_unlink_attempts.append(path)
            raise OSError("injected final backup cleanup failure")
        real_authenticated_unlink(path, root, relative_path)

    monkeypatch.setattr(policy_sync, "authenticated_unlink", fail_backup_unlink)

    changed = policy_sync.sync(tmp_path)

    assert changed == list(policy_sync.HOST_FILES)
    assert len(backup_unlink_attempts) == len(policy_sync.HOST_FILES)
    assert all(".aiworkhub-policy-sync." in path.name for path in backup_unlink_attempts)
    assert all(path.name.endswith(".bak") for path in backup_unlink_attempts)
    assert {relative_path: (tmp_path / relative_path).read_bytes() for relative_path in policy_sync.HOST_FILES} == originals
    for relative_path, backup_path in zip(policy_sync.HOST_FILES, backup_unlink_attempts, strict=True):
        assert backup_path.read_bytes() != (tmp_path / relative_path).read_bytes()


def test_sync_does_not_follow_existing_staged_path_symlink(tmp_path: Path, monkeypatch) -> None:
    copy_policy_tree(tmp_path)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")

    drifted_path = tmp_path / policy_sync.HOST_FILES[0]
    original = drifted_path.read_bytes()
    drifted_path.write_bytes(original.replace(b"Stop at Codex review.", b"Stop at manager review."))

    victim_path = tmp_path / "victim"
    victim_data = b"do not overwrite\n"
    victim_path.write_bytes(victim_data)

    attack_path = drifted_path.with_name(f".{drifted_path.name}.aiworkhub-policy-sync.attack.tmp")
    attack_path.symlink_to(victim_path)
    tokens = iter(("attack", "safe-temp", "safe-backup"))
    monkeypatch.setattr(policy_sync.secrets, "token_hex", lambda _size: next(tokens))

    changed = policy_sync.sync(tmp_path)

    assert changed == [policy_sync.HOST_FILES[0]]
    assert drifted_path.read_bytes() == original
    assert victim_path.read_bytes() == victim_data
    assert attack_path.is_symlink()


def test_sync_rejects_swapped_staged_temp_before_install_without_altering_host(
    tmp_path: Path, monkeypatch
) -> None:
    copy_policy_tree(tmp_path)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")

    host = policy_sync.HOST_FILES[0]
    host_path = tmp_path / host
    original = host_path.read_bytes()
    host_path.write_bytes(original.replace(b"Stop at Codex review.", b"Stop at manager review."))
    drifted = host_path.read_bytes()

    victim_path = tmp_path / "victim"
    victim_data = b"do not overwrite\n"
    victim_path.write_bytes(victim_data)

    staged_paths = []
    real_create_private_staged_file = policy_sync.create_private_staged_file

    def record_staged_path(path: Path, data: bytes, mode: int):
        staged_path, identity = real_create_private_staged_file(path, data, mode)
        staged_paths.append(staged_path)
        return staged_path, identity

    reject_call_count = 0
    real_reject_symlink_host = policy_sync.reject_symlink_host

    def swap_staged_path_before_install(update: policy_sync.HostUpdate) -> None:
        nonlocal reject_call_count
        reject_call_count += 1
        if reject_call_count == 2:
            staged_paths[0].unlink()
            staged_paths[0].symlink_to(victim_path)
        real_reject_symlink_host(update)

    monkeypatch.setattr(policy_sync, "create_private_staged_file", record_staged_path)
    monkeypatch.setattr(policy_sync, "reject_symlink_host", swap_staged_path_before_install)

    try:
        policy_sync.sync(tmp_path, (host,))
    except policy_sync.PolicySyncError as exc:
        assert str(exc).endswith(": refusing to authenticate staged symlink")
    else:
        raise AssertionError("sync should reject a swapped staged pathname before install")

    assert host_path.read_bytes() == drifted
    assert victim_path.read_bytes() == victim_data
    assert staged_paths[0].is_symlink()


def test_sync_rejects_swapped_staged_symlink_to_hardlink_without_nofollow(
    tmp_path: Path, monkeypatch
) -> None:
    copy_policy_tree(tmp_path)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")
    monkeypatch.delattr(policy_sync.os, "O_NOFOLLOW", raising=False)

    host = policy_sync.HOST_FILES[0]
    host_path = tmp_path / host
    original = host_path.read_bytes()
    host_path.write_bytes(original.replace(b"Stop at Codex review.", b"Stop at manager review."))
    drifted = host_path.read_bytes()

    staged_paths = []
    real_create_private_staged_file = policy_sync.create_private_staged_file
    real_lstat = os.lstat
    real_open = os.open

    class PlatformIO:
        @staticmethod
        def read_regular_file_snapshot(path: Path, relative_path: Path) -> policy_sync.FileSnapshot:
            file_stat = real_lstat(path)
            if policy_sync.stat.S_ISLNK(file_stat.st_mode):
                raise OSError(policy_sync.errno.ELOOP, "symlink")
            if not policy_sync.stat.S_ISREG(file_stat.st_mode):
                raise OSError(policy_sync.errno.EINVAL, "not regular")
            fd = real_open(path, os.O_RDONLY)
            try:
                digest = policy_sync.hashlib.sha256()
                chunks = []
                with os.fdopen(fd, "rb") as source_file:
                    for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                        chunks.append(chunk)
                        digest.update(chunk)
                return policy_sync.FileSnapshot(
                    data=b"".join(chunks),
                    identity=policy_sync.FileIdentity(
                        device=file_stat.st_dev,
                        inode=file_stat.st_ino,
                        mode=file_stat.st_mode,
                        is_regular=True,
                        nlink=getattr(file_stat, "st_nlink", None),
                        uid=getattr(file_stat, "st_uid", None),
                        gid=getattr(file_stat, "st_gid", None),
                        size=file_stat.st_size,
                        digest=digest.hexdigest(),
                    ),
                )
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise

    def record_staged_path(path: Path, data: bytes, mode: int):
        staged_path, identity = real_create_private_staged_file(path, data, mode)
        staged_paths.append(staged_path)
        return staged_path, identity

    reject_call_count = 0
    real_reject_symlink_host = policy_sync.reject_symlink_host

    def swap_staged_path_to_symlinked_hardlink(update: policy_sync.HostUpdate) -> None:
        nonlocal reject_call_count
        reject_call_count += 1
        if reject_call_count == 2:
            hardlink_path = tmp_path / "staged-hardlink"
            os.link(staged_paths[0], hardlink_path)
            staged_paths[0].unlink()
            staged_paths[0].symlink_to(hardlink_path)
        real_reject_symlink_host(update)

    monkeypatch.setattr(policy_sync, "platform_io", PlatformIO)
    monkeypatch.setattr(policy_sync, "create_private_staged_file", record_staged_path)
    monkeypatch.setattr(policy_sync, "reject_symlink_host", swap_staged_path_to_symlinked_hardlink)

    try:
        policy_sync.sync(tmp_path, (host,))
    except policy_sync.PolicySyncError as exc:
        assert str(exc) == (
            f"{policy_sync.POLICY_SOURCE}: cannot authenticate parent directory "
            "without an enforceable no-follow open primitive"
        )
    else:
        raise AssertionError("sync should reject parent traversal without O_NOFOLLOW")

    assert host_path.read_bytes() == drifted
    assert staged_paths == []


def test_sync_refuses_to_replace_host_symlink(tmp_path: Path, monkeypatch) -> None:
    copy_policy_tree(tmp_path)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")

    host = policy_sync.HOST_FILES[0]
    host_path = tmp_path / host
    target_path = tmp_path / "host-target.md"
    target_original = host_path.read_bytes().replace(b"Stop at Codex review.", b"Stop at manager review.")
    target_path.write_bytes(target_original)
    host_path.unlink()
    host_path.symlink_to(target_path)

    try:
        policy_sync.sync(tmp_path)
    except policy_sync.PolicySyncError as exc:
        assert str(exc) == f"{host}: refusing to sync through symlink"
    else:
        raise AssertionError("sync should reject host symlinks before replacing")

    assert host_path.is_symlink()
    assert target_path.read_bytes() == target_original


def test_sync_rejects_symlinked_parent_directory_without_modifying_external_victim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    canonical = (REPO_ROOT / policy_sync.POLICY_SOURCE).read_bytes()
    source_path = tmp_path / policy_sync.POLICY_SOURCE
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(canonical)

    host = Path(".github/copilot-instructions.md")
    external_parent = tmp_path / "external-github"
    external_parent.mkdir()
    external_victim = external_parent / host.name
    victim_original = (
        b"external prefix\n"
        + policy_sync.generated_block(
            canonical.replace(b"Stop at Codex review.\n", b"Stop at external victim review.\n")
        )
        + b"\nexternal suffix\n"
    )
    external_victim.write_bytes(victim_original)
    (tmp_path / ".github").symlink_to(external_parent)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")

    try:
        policy_sync.sync(tmp_path, (host,))
    except policy_sync.PolicySyncError as exc:
        assert str(exc) == f"{host}: refusing to sync through symlink"
    else:
        raise AssertionError("sync should reject a symlinked parent before touching the victim")

    assert (tmp_path / ".github").is_symlink()
    assert external_victim.read_bytes() == victim_original
    assert sorted(path.name for path in external_parent.iterdir()) == [host.name]


def test_sync_writes_after_explicit_write_gate(tmp_path: Path, monkeypatch) -> None:
    copy_policy_tree(tmp_path)
    monkeypatch.setenv(policy_sync.WRITE_GATE_ENV, "1")

    drifted_path = tmp_path / policy_sync.HOST_FILES[0]
    original = drifted_path.read_bytes()
    drifted_path.write_bytes(original.replace(b"Stop at Codex review.", b"Stop at manager review."))

    changed = policy_sync.sync(tmp_path)

    assert changed == [policy_sync.HOST_FILES[0]]
    assert drifted_path.read_bytes() == original


def test_missing_host_file_is_structured_check_error_and_cli_failure(tmp_path: Path) -> None:
    copy_policy_tree(tmp_path)
    missing_path = tmp_path / policy_sync.HOST_FILES[1]
    missing_path.unlink()

    assert policy_sync.check(tmp_path) == ["CLAUDE.md: file is missing"]

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/sync_aiworkhub_tool_policy.py"), "--check", "--root", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "- CLAUDE.md: file is missing\n" in result.stdout
    assert "Traceback" not in result.stderr
