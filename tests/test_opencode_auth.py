from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from aiworkhub import opencode_auth


_CREDENTIAL_SENTINEL = "credential-value-7f32b98a"


def _source(
    tmp_path: Path,
    document: bytes = b'{"openai":{"token":"credential-value-7f32b98a"}}\n',
) -> Path:
    source = tmp_path / "source-home" / ".local" / "share" / "opencode" / "auth.json"
    source.parent.mkdir(parents=True, mode=0o700)
    source.write_bytes(document)
    source.chmod(0o600)
    return source


def test_project_opencode_auth_is_exact_private_and_secret_free(tmp_path: Path) -> None:
    source = _source(tmp_path)
    host_root = source.parents[3]
    (host_root / ".config" / "opencode").mkdir(parents=True)
    (host_root / ".config" / "opencode" / "opencode.json").write_text("config")
    (source.parent / "opencode.db").write_text("database")
    (source.parent / "log").mkdir()
    isolated = tmp_path / "isolated"

    receipt = opencode_auth.project_opencode_auth(source, isolated)

    destination = isolated / opencode_auth.OPENCODE_AUTH_RELATIVE_PATH
    assert destination.read_bytes() == source.read_bytes()
    assert receipt.byte_count == source.stat().st_size
    assert _CREDENTIAL_SENTINEL not in repr(receipt)
    assert not (isolated / ".config").exists()
    assert not (destination.parent / "opencode.db").exists()
    assert not (destination.parent / "log").exists()
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
        for directory in (isolated, isolated / ".local", isolated / ".local/share", destination.parent):
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_missing_source_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(
        opencode_auth.OpenCodeAuthSourceError,
        match="^opencode_auth_source_unavailable$",
    ):
        opencode_auth.project_opencode_auth(tmp_path / "missing", tmp_path / "isolated")


def test_source_symlink_fails_closed(tmp_path: Path) -> None:
    target = _source(tmp_path)
    symlink = tmp_path / "auth-link.json"
    symlink.symlink_to(target)
    with pytest.raises(opencode_auth.OpenCodeAuthSourceError):
        opencode_auth.project_opencode_auth(symlink, tmp_path / "isolated")


def test_wrong_owner_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = _source(tmp_path)
    monkeypatch.setattr(opencode_auth, "stat_owned_by_current_user", lambda _metadata: False)
    with pytest.raises(
        opencode_auth.OpenCodeAuthSourceError,
        match="^opencode_auth_source_wrong_owner$",
    ):
        opencode_auth.project_opencode_auth(source, tmp_path / "isolated")


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode authority")
def test_group_or_world_writable_source_fails_closed(tmp_path: Path) -> None:
    source = _source(tmp_path)
    source.chmod(0o620)
    with pytest.raises(
        opencode_auth.OpenCodeAuthSourceError,
        match="^opencode_auth_source_writable_by_others$",
    ):
        opencode_auth.project_opencode_auth(source, tmp_path / "isolated")


def test_oversized_source_fails_closed(tmp_path: Path) -> None:
    source = _source(tmp_path, b"{" + b"x" * opencode_auth.MAX_SOURCE_BYTES + b"}")
    with pytest.raises(
        opencode_auth.OpenCodeAuthSourceError,
        match="^opencode_auth_source_size_invalid$",
    ):
        opencode_auth.project_opencode_auth(source, tmp_path / "isolated")


def test_source_replacement_during_read_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _source(tmp_path)
    original_read = opencode_auth.os.read
    moved = source.with_name("opened-auth.json")
    replaced = False

    def replace_then_read(fd: int, size: int) -> bytes:
        nonlocal replaced
        if not replaced:
            replaced = True
            source.replace(moved)
            source.write_bytes(b'{"openai":{"token":"replacement"}}\n')
            source.chmod(0o600)
        return original_read(fd, size)

    monkeypatch.setattr(opencode_auth.os, "read", replace_then_read)
    # On Windows the swap can never complete: _read_verified_source's plain
    # os.open descriptor requests no FILE_SHARE_DELETE, so source.replace()
    # above itself raises before the swap lands -- propagating as a bare
    # PermissionError instead of ever reaching the identity re-check below.
    # Both outcomes are fail-closed; Windows just blocks the race one layer
    # earlier, at the OS level, before production's own check is even needed.
    if os.name == "nt":
        with pytest.raises(PermissionError):
            opencode_auth.project_opencode_auth(source, tmp_path / "isolated")
    else:
        with pytest.raises(
            opencode_auth.OpenCodeAuthSourceError,
            match="^opencode_auth_source_identity_changed$",
        ):
            opencode_auth.project_opencode_auth(source, tmp_path / "isolated")
    assert not (tmp_path / "isolated").exists()


def test_existing_destination_symlink_fails_closed(tmp_path: Path) -> None:
    source = _source(tmp_path)
    isolated = tmp_path / "isolated"
    destination = isolated / opencode_auth.OPENCODE_AUTH_RELATIVE_PATH
    destination.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    destination.symlink_to(outside)
    with pytest.raises(
        opencode_auth.OpenCodeAuthDestinationError,
        match="^opencode_auth_destination_unsafe$",
    ):
        opencode_auth.project_opencode_auth(source, isolated)
    assert not outside.exists()
