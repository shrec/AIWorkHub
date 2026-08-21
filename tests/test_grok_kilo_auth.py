"""Focused tests for the isolated Kilo HOME xAI credential boundary."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub.kilo_auth import (  # noqa: E402
    KILO_AUTH_RELATIVE_PATH,
    KiloAuthDestinationError,
    KiloAuthError,
    KiloAuthProviderMissing,
    KiloAuthSourceError,
    MAX_SOURCE_BYTES,
    project_xai_auth,
    resolve_kilo_auth_source,
)

XAI_ACCESS_TOKEN = "xai-access-token-DO-NOT-PRINT-0001"
XAI_REFRESH_TOKEN = "xai-refresh-token-DO-NOT-PRINT-0002"
FOREIGN_TOKENS = {
    "kilo": "kilo-refresh-token-DO-NOT-PRINT-0003",
    "openai": "openai-api-key-DO-NOT-PRINT-0004",
}


def _xai_record():
    return {
        "accessToken": XAI_ACCESS_TOKEN,
        "refreshToken": XAI_REFRESH_TOKEN,
        "expiresAt": "2031-01-01T00:00:00Z",
    }


def _write_source(tmp_path, providers):
    source = tmp_path / "kilo-auth.json"
    source.write_text(json.dumps(providers), encoding="utf-8")
    return source


def _valid_source(tmp_path):
    return _write_source(
        tmp_path,
        {
            "kilo": {"refreshToken": FOREIGN_TOKENS["kilo"]},
            "openai": {"apiKey": FOREIGN_TOKENS["openai"]},
            "xai": _xai_record(),
        },
    )


def test_projects_only_the_exact_xai_record(tmp_path):
    source = _valid_source(tmp_path)
    home = tmp_path / "isolated-home"

    receipt = project_xai_auth(source, home)

    dest = home / KILO_AUTH_RELATIVE_PATH
    payload = json.loads(dest.read_text(encoding="utf-8"))
    assert set(payload) == {"xai"}
    assert payload["xai"] == _xai_record()
    raw = dest.read_bytes()
    assert XAI_ACCESS_TOKEN.encode() in raw
    for secret in FOREIGN_TOKENS.values():
        assert secret.encode() not in raw
    assert [entry.name for entry in home.iterdir()] == [".local"]
    assert Path(receipt.destination) == home / KILO_AUTH_RELATIVE_PATH
    assert receipt.provider == "xai"


def test_receipt_carries_only_safe_metadata(tmp_path):
    source = _valid_source(tmp_path)
    home = tmp_path / "isolated-home"

    receipt = project_xai_auth(source, home)

    dest_raw = (home / KILO_AUTH_RELATIVE_PATH).read_bytes()
    material = json.dumps(asdict(receipt)) + repr(receipt)
    for secret in (XAI_ACCESS_TOKEN, XAI_REFRESH_TOKEN, *FOREIGN_TOKENS.values()):
        assert secret not in material
    assert receipt.status == "projected"
    assert receipt.source == str(source)
    assert receipt.destination == str(home / KILO_AUTH_RELATIVE_PATH)
    assert receipt.source_bytes == source.stat().st_size
    assert receipt.source_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert receipt.destination_bytes == len(dest_raw)
    assert receipt.destination_sha256 == hashlib.sha256(dest_raw).hexdigest()


@pytest.mark.parametrize("platform_name", ["posix", "nt"])
def test_resolve_auth_source_uses_explicit_portable_default(tmp_path, platform_name):
    home = (tmp_path / "home").resolve()
    assert resolve_kilo_auth_source(
        home=home, platform_name=platform_name
    ) == home / ".local" / "share" / "kilo" / "auth.json"


def test_resolve_auth_source_honors_explicit_xdg_root(tmp_path):
    home = (tmp_path / "home").resolve()
    data = (tmp_path / "xdg-data").resolve()
    assert resolve_kilo_auth_source(
        home=home, xdg_data_home=data
    ) == data / "kilo" / "auth.json"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"home": "relative"}, "home must be an absolute normalized path"),
        (
            {"home": "/safe", "xdg_data_home": "relative"},
            "XDG data home must be an absolute normalized path",
        ),
        ({"home": "/safe", "platform_name": "other"}, "unsupported platform name"),
    ],
)
def test_resolve_auth_source_fails_closed_without_ambient_reads(kwargs, reason):
    with pytest.raises(KiloAuthSourceError, match=reason):
        resolve_kilo_auth_source(**kwargs)


def test_missing_source_fails_closed(tmp_path):
    home = tmp_path / "isolated-home"

    with pytest.raises(KiloAuthError):
        project_xai_auth(tmp_path / "absent.json", home)

    assert not home.exists()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("", KiloAuthSourceError),
        ("not json at all", KiloAuthSourceError),
        ('{"xai": "not-an-object"}', KiloAuthSourceError),
        ('{"kilo": {"refreshToken": "k"}}', KiloAuthProviderMissing),
        ("[1, 2, 3]", KiloAuthSourceError),
    ],
)
def test_invalid_source_content_fails_closed(tmp_path, content, expected):
    source = tmp_path / "kilo-auth.json"
    source.write_text(content, encoding="utf-8")
    home = tmp_path / "isolated-home"

    with pytest.raises(expected):
        project_xai_auth(source, home)

    assert not home.exists()


def test_oversized_source_fails_closed(tmp_path):
    source = tmp_path / "kilo-auth.json"
    source.write_bytes(b"0" * (MAX_SOURCE_BYTES + 1))
    home = tmp_path / "isolated-home"

    with pytest.raises(KiloAuthSourceError, match="size bound"):
        project_xai_auth(source, home)

    assert not home.exists()


def test_symlinked_source_fails_closed(tmp_path):
    source = _valid_source(tmp_path)
    link = tmp_path / "linked-auth.json"
    link.symlink_to(source)
    home = tmp_path / "isolated-home"

    with pytest.raises(KiloAuthSourceError, match="symlink"):
        project_xai_auth(link, home)

    assert json.loads(source.read_text(encoding="utf-8"))["xai"] == _xai_record()


def test_relative_source_is_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _valid_source(tmp_path)

    with pytest.raises(KiloAuthSourceError, match="absolute"):
        project_xai_auth("kilo-auth.json", tmp_path / "isolated-home")


def test_destination_traversal_is_rejected(tmp_path):
    source = _valid_source(tmp_path)
    escape = tmp_path / "escape"

    with pytest.raises(KiloAuthDestinationError, match="contain"):
        project_xai_auth(source, tmp_path / "home" / ".." / escape.name)

    assert not escape.exists()


def test_relative_isolated_home_is_rejected(tmp_path):
    source = _valid_source(tmp_path)

    with pytest.raises(KiloAuthDestinationError, match="absolute"):
        project_xai_auth(source, "relative-home")

    assert not (tmp_path / "relative-home").exists()


def test_symlinked_isolated_home_is_rejected(tmp_path):
    source = _valid_source(tmp_path)
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home)

    with pytest.raises(KiloAuthDestinationError, match="symlink"):
        project_xai_auth(source, linked_home)

    assert not (real_home / KILO_AUTH_RELATIVE_PATH).exists()


def test_planted_destination_symlink_is_not_followed(tmp_path):
    source = _valid_source(tmp_path)
    home = tmp_path / "isolated-home"
    destination_dir = (home / KILO_AUTH_RELATIVE_PATH).parent
    destination_dir.mkdir(parents=True)
    canary = tmp_path / "canary.json"
    canary.write_text("canary", encoding="utf-8")
    (home / KILO_AUTH_RELATIVE_PATH).symlink_to(canary)

    with pytest.raises(KiloAuthDestinationError, match="symlink"):
        project_xai_auth(source, home)

    assert canary.read_text(encoding="utf-8") == "canary"
    assert (home / KILO_AUTH_RELATIVE_PATH).is_symlink()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_intermediate_kilo_data_symlink_is_rejected(tmp_path):
    source = _valid_source(tmp_path)
    home = tmp_path / "isolated-home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".local").symlink_to(outside, target_is_directory=True)

    with pytest.raises(KiloAuthDestinationError, match="non-symlink"):
        project_xai_auth(source, home)

    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_destination_and_home_get_restrictive_modes(tmp_path):
    source = _valid_source(tmp_path)
    home = tmp_path / "loose-parent" / "isolated-home"
    home.mkdir(parents=True)
    home.chmod(0o755)

    project_xai_auth(source, home)

    dest = home / KILO_AUTH_RELATIVE_PATH
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    for directory in (
        home,
        home / ".local",
        home / ".local" / "share",
        home / ".local" / "share" / "kilo",
    ):
        assert stat.S_IMODE(directory.stat().st_mode) & 0o077 == 0


def test_stale_destination_is_atomically_replaced(tmp_path):
    source = _valid_source(tmp_path)
    home = tmp_path / "isolated-home"
    stale = home / KILO_AUTH_RELATIVE_PATH
    stale.parent.mkdir(parents=True)
    stale.write_text('{"xai": {"accessToken": "stale"}}', encoding="utf-8")

    project_xai_auth(source, home)

    assert json.loads(stale.read_text(encoding="utf-8"))["xai"] == _xai_record()
    assert [entry.name for entry in home.iterdir()] == [".local"]


def test_source_file_is_immutable(tmp_path):
    providers = {
        "kilo": {"refreshToken": FOREIGN_TOKENS["kilo"]},
        "xai": _xai_record(),
    }
    source = _write_source(tmp_path, providers)
    before = source.read_bytes()
    stat_before = source.stat()

    project_xai_auth(source, tmp_path / "home-a")
    project_xai_auth(source, tmp_path / "home-b")

    stat_after = source.stat()
    assert source.read_bytes() == before
    assert json.loads(before) == providers
    assert stat_after.st_size == stat_before.st_size
    assert stat_after.st_mtime_ns == stat_before.st_mtime_ns


def test_ambient_home_is_never_consulted(tmp_path, monkeypatch):
    decoy = tmp_path / "ambient-home"
    decoy.mkdir()
    monkeypatch.setenv("HOME", str(decoy))
    monkeypatch.setenv("USERPROFILE", str(decoy))
    source = _valid_source(tmp_path)

    receipt = project_xai_auth(source, tmp_path / "project-home")

    assert Path(receipt.destination) == (
        tmp_path / "project-home" / KILO_AUTH_RELATIVE_PATH
    )
    assert list(decoy.iterdir()) == []


def test_failures_never_echo_secret_material(tmp_path):
    home = tmp_path / "isolated-home"
    corrupted = tmp_path / "corrupted.json"
    corrupted.write_text('{"xai": ' + XAI_ACCESS_TOKEN, encoding="utf-8")

    with pytest.raises(KiloAuthSourceError) as excinfo:
        project_xai_auth(corrupted, home)

    assert XAI_ACCESS_TOKEN not in str(excinfo.value)
    assert excinfo.value.reason == "source is not valid JSON"
