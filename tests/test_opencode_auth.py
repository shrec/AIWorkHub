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


def test_opencode_go_key_is_returned_as_env_from_the_projected_file(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        b'{"opencode-go":{"type":"api","key":" credential-value-7f32b98a "},'
        b'"openai":{"type":"oauth","access":"other"}}\n',
    )
    isolated = tmp_path / "isolated"
    opencode_auth.project_opencode_auth(source, isolated)

    assert opencode_auth.opencode_go_api_key_env(isolated) == {
        "OPENCODE_API_KEY": _CREDENTIAL_SENTINEL
    }


@pytest.mark.parametrize(
    "document",
    [
        b'{"openai":{"token":"credential-value-7f32b98a"}}\n',
        b'{"opencode-go":{"type":"api","key":""}}\n',
        b'{"opencode-go":{"type":"api","key":7}}\n',
        b'{"opencode-go":"credential-value-7f32b98a"}\n',
    ],
)
def test_opencode_go_key_env_is_empty_without_a_usable_key(
    tmp_path: Path, document: bytes
) -> None:
    isolated = tmp_path / "isolated"
    opencode_auth.project_opencode_auth(_source(tmp_path, document), isolated)
    assert opencode_auth.opencode_go_api_key_env(isolated) == {}


def test_opencode_go_key_env_is_empty_without_a_projected_file(tmp_path: Path) -> None:
    assert opencode_auth.opencode_go_api_key_env(tmp_path / "isolated") == {}


def _credential_db(db_path: Path, rows: list[tuple[str, str, int, int]]) -> Path:
    import sqlite3 as _sqlite_module

    db_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    connection = _sqlite_module.connect(db_path.as_posix())
    try:
        connection.execute(
            "CREATE TABLE credential (id TEXT, integration_id TEXT, label TEXT, "
            "value TEXT, connector_id TEXT, method_id TEXT, active INTEGER, "
            "time_created INTEGER, time_updated INTEGER)"
        )
        for index, (integration_id, value, active, updated) in enumerate(rows):
            connection.execute(
                "INSERT INTO credential (id, integration_id, label, value, "
                "connector_id, method_id, active, time_created, time_updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"id-{index}",
                    integration_id,
                    f"label-{index}",
                    value,
                    "connector",
                    "method",
                    active,
                    updated,
                    updated,
                ),
            )
        connection.commit()
    finally:
        connection.close()
    if os.name != "nt":
        db_path.chmod(0o600)
    return db_path


def test_credential_store_active_newest_wins_without_auth_json(tmp_path: Path) -> None:
    import json

    source = tmp_path / "source-home" / ".local" / "share" / "opencode" / "auth.json"
    db_path = source.parent / "opencode.db"
    inactive_value = json.dumps(
        {"key": "credential-value-inactive-3d9b1f04", "type": "key"}, sort_keys=True
    )
    active_value = json.dumps(
        {"key": _CREDENTIAL_SENTINEL, "type": "key"}, sort_keys=True
    )
    _credential_db(
        db_path,
        [
            ("opencode-go", inactive_value, 0, 3000),
            ("opencode-go", active_value, 1, 2000),
        ],
    )
    assert not source.exists()
    isolated = tmp_path / "isolated"

    receipt = opencode_auth.project_opencode_auth(source, isolated)

    destination = isolated / opencode_auth.OPENCODE_AUTH_RELATIVE_PATH
    assert json.loads(destination.read_bytes()) == {
        "opencode-go": {"key": _CREDENTIAL_SENTINEL, "type": "api"}
    }
    assert opencode_auth.opencode_go_api_key_env(isolated) == {
        "OPENCODE_API_KEY": _CREDENTIAL_SENTINEL
    }
    assert _CREDENTIAL_SENTINEL not in repr(receipt)


def test_credential_store_wins_over_auth_json(tmp_path: Path) -> None:
    import json

    auth_document = (
        b'{"opencode-go": {"type": "api", '
        b'"key": "credential-value-auth-json-6e2a8c10"}}\n'
    )
    source = _source(tmp_path, auth_document)
    active_value = json.dumps(
        {"key": _CREDENTIAL_SENTINEL, "type": "key"}, sort_keys=True
    )
    oauth_value = json.dumps(
        {"type": "oauth", "access": "a", "refresh": "r", "expires": 1, "methodID": "m"},
        sort_keys=True,
    )
    _credential_db(
        source.parent / "opencode.db",
        [
            ("opencode-go", active_value, 1, 2000),
            ("openai", oauth_value, 1, 2000),
        ],
    )
    isolated = tmp_path / "isolated"

    opencode_auth.project_opencode_auth(source, isolated)

    destination = isolated / opencode_auth.OPENCODE_AUTH_RELATIVE_PATH
    assert json.loads(destination.read_bytes()) == {
        "opencode-go": {"key": _CREDENTIAL_SENTINEL, "type": "api"},
        "openai": {
            "type": "oauth",
            "access": "a",
            "refresh": "r",
            "expires": 1,
            "methodID": "m",
        },
    }
    assert opencode_auth.opencode_go_api_key_env(isolated) == {
        "OPENCODE_API_KEY": _CREDENTIAL_SENTINEL
    }


def test_credential_store_without_table_falls_back_to_missing_source(
    tmp_path: Path,
) -> None:
    import sqlite3

    source = tmp_path / "source-home" / ".local" / "share" / "opencode" / "auth.json"
    source.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    db_path = source.parent / "opencode.db"
    connection = sqlite3.connect(db_path.as_posix())
    try:
        connection.execute("CREATE TABLE other (id INTEGER)")
        connection.commit()
    finally:
        connection.close()
    if os.name != "nt":
        db_path.chmod(0o600)
    assert not source.exists()
    with pytest.raises(
        opencode_auth.OpenCodeAuthSourceError,
        match="^opencode_auth_source_unavailable$",
    ):
        opencode_auth.project_opencode_auth(source, tmp_path / "isolated")


def _store_row() -> tuple[str, str, int, int]:
    import json

    return ("opencode-go", json.dumps({"key": _CREDENTIAL_SENTINEL, "type": "key"}), 1, 1)


def test_relative_or_dotdot_source_is_refused_before_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credential_db(tmp_path / "rel" / "opencode.db", [_store_row()])
    monkeypatch.chdir(tmp_path)
    for source in (
        Path("rel") / "auth.json",
        tmp_path / "x" / ".." / "rel" / "auth.json",
    ):
        with pytest.raises(
            opencode_auth.OpenCodeAuthSourceError,
            match="^opencode_auth_source_path_invalid$",
        ):
            opencode_auth.project_opencode_auth(source, tmp_path / "isolated")
    assert not (tmp_path / "isolated").exists()


def test_credential_store_without_rows_projects_auth_json(tmp_path: Path) -> None:
    source = _source(tmp_path)
    _credential_db(source.parent / "opencode.db", [])
    opencode_auth.project_opencode_auth(source, tmp_path / "isolated")
    destination = (tmp_path / "isolated") / opencode_auth.OPENCODE_AUTH_RELATIVE_PATH
    assert destination.read_bytes() == source.read_bytes()


def test_oversize_credential_store_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    _credential_db(source.parent / "opencode.db", [_store_row()])
    monkeypatch.setattr(opencode_auth, "MAX_SOURCE_BYTES", 16)
    with pytest.raises(
        opencode_auth.OpenCodeAuthSourceError,
        match="^opencode_auth_source_size_invalid$",
    ) as excinfo:
        opencode_auth.project_opencode_auth(source, tmp_path / "isolated")
    assert _CREDENTIAL_SENTINEL not in str(excinfo.value)


@pytest.mark.skipif(
    not opencode_auth.posix_path_modes_supported(), reason="POSIX modes unavailable"
)
def test_credential_store_writable_by_others_is_refused(tmp_path: Path) -> None:
    source = _source(tmp_path)
    db_path = _credential_db(source.parent / "opencode.db", [_store_row()])
    for mode in (0o620, 0o602):
        db_path.chmod(mode)
        with pytest.raises(
            opencode_auth.OpenCodeAuthSourceError,
            match="^opencode_auth_source_writable_by_others$",
        ) as excinfo:
            opencode_auth.project_opencode_auth(source, tmp_path / "isolated")
        assert _CREDENTIAL_SENTINEL not in str(excinfo.value)
    destination = (tmp_path / "isolated") / opencode_auth.OPENCODE_AUTH_RELATIVE_PATH
    assert not destination.exists()


@pytest.mark.skipif(
    not opencode_auth.posix_path_modes_supported(), reason="POSIX modes unavailable"
)
def test_symlinked_credential_store_is_refused(tmp_path: Path) -> None:
    source = _source(tmp_path)
    real = _credential_db(tmp_path / "elsewhere" / "real.db", [_store_row()])
    (source.parent / "opencode.db").symlink_to(real)
    with pytest.raises(
        opencode_auth.OpenCodeAuthSourceError,
        match="^opencode_auth_source_not_regular$",
    ) as excinfo:
        opencode_auth.project_opencode_auth(source, tmp_path / "isolated")
    assert _CREDENTIAL_SENTINEL not in str(excinfo.value)
    destination = (tmp_path / "isolated") / opencode_auth.OPENCODE_AUTH_RELATIVE_PATH
    assert not destination.exists()
