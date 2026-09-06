from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from aiworkhub import toolchain_authority


def _card() -> dict[str, object]:
    # Cache/digest tests need one stable absolute executable fact, not a
    # dependency on whichever pytest runtime layout the host Python exposes.
    return {"validation": [f"{sys.executable} -m compileall -q src"]}


def _authority(tmp_path: Path, **kwargs: object) -> toolchain_authority.ToolchainAuthority:
    return toolchain_authority.ToolchainAuthority(
        tmp_path, capability_probe=lambda _repo, _card: (), **kwargs
    )


def _secret_path(repo: Path) -> Path:
    return repo / ".aiworkhub" / "toolchain-authority" / "receipt-hmac.key"


def _clear_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIWORKHUB_TOOLCHAIN_AUTHORITY_HMAC_KEY", raising=False)
    monkeypatch.delenv("AIWORKHUB_TOOLCHAIN_AUTHORITY_SECRET", raising=False)


def test_authority_secret_prefers_environment_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "AIWORKHUB_TOOLCHAIN_AUTHORITY_HMAC_KEY",
        "hex:" + ("ab" * 32),
    )

    assert toolchain_authority._authority_secret(tmp_path, create=False) == bytes.fromhex(
        "ab" * 32
    )


def test_authority_secret_rejects_symlink_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_secret_env(monkeypatch)
    target = tmp_path / "elsewhere.key"
    target.write_bytes(b"x" * 32)
    key_path = _secret_path(tmp_path)
    key_path.parent.mkdir(parents=True)
    key_path.symlink_to(target)

    assert toolchain_authority._authority_secret(tmp_path, create=False) == b""


def test_authority_secret_rejects_wrong_mode_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_secret_env(monkeypatch)
    key_path = _secret_path(tmp_path)
    key_path.parent.mkdir(parents=True)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    with os.fdopen(fd, "wb") as handle:
        handle.write(b"k" * 32)
    if key_path.stat().st_mode & 0o077 == 0:
        pytest.skip("validation_unsupported_in_sandbox:wrong_mode_key_setup")

    assert toolchain_authority._authority_secret(tmp_path, create=False) == b""


def test_authority_secret_rejects_malformed_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_secret_env(monkeypatch)
    key_path = _secret_path(tmp_path)
    key_path.parent.mkdir(parents=True)
    key_path.write_bytes(b"too-short")

    assert toolchain_authority._authority_secret(tmp_path, create=False) == b""


def test_authority_secret_handles_create_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_secret_env(monkeypatch)
    from aiworkhub import terminal_authority

    key_path = _secret_path(tmp_path)
    race_key = b"r" * 32
    real_open = terminal_authority.os.open
    raced = False

    monkeypatch.setattr(terminal_authority.os, "chmod", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(terminal_authority, "chmod_fd", lambda *_args, **_kwargs: None)

    def racing_open(path, flags, mode=0o777, *args, **kwargs):
        nonlocal raced
        if Path(path) == key_path and flags & os.O_EXCL and not raced:
            raced = True
            key_path.parent.mkdir(parents=True, exist_ok=True)
            fd = real_open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(race_key)
            raise FileExistsError(str(path))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(terminal_authority.os, "open", racing_open)

    assert toolchain_authority._authority_secret(tmp_path, create=True) == race_key
    assert raced


def test_snapshot_digest_is_deterministic_and_cached(tmp_path: Path) -> None:
    authority = _authority(tmp_path)

    first = authority.evaluate(_card())
    second = authority.evaluate(_card())

    assert first is second
    assert first.digest == second.digest
    assert first.schema_id == "aiworkhub.toolchain_authority.v1"
    assert first.executables[0].canonical_path == str(Path(sys.executable).resolve())
    assert first.executables[0].version_fact.startswith("Python ")


def test_repository_dependency_metadata_drift_rebuilds_snapshot(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    first = authority.evaluate(_card())
    (tmp_path / "pyproject.toml").write_text("[project]\nname='one'\n", encoding="utf-8")

    second = authority.evaluate(_card())

    assert second is not first
    assert second.repository_fingerprint != first.repository_fingerprint
    assert second.digest != first.digest


def test_path_change_invalidates_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority = _authority(tmp_path)
    first = authority.evaluate(_card())
    monkeypatch.setenv("PATH", os.environ.get("PATH", "") + os.pathsep + "/poison")

    second = authority.evaluate(_card())

    assert second.digest != first.digest


def test_executable_and_symlink_replacement_invalidate_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiworkhub import worker_workspace

    link = tmp_path / "validator"
    link.symlink_to("/bin/true")

    def normalize(argv: list[str], _repo: Path) -> tuple[list[str], tuple[Path, ...]]:
        return [str(link), *argv[1:]], ()

    monkeypatch.setattr(
        worker_workspace,
        "_normalize_trusted_validation_executable_argv_with_roots",
        normalize,
    )
    authority = _authority(tmp_path)
    first = authority.evaluate({"validation": ["validator --version"]})
    link.unlink()
    link.symlink_to("/bin/false")

    second = authority.evaluate({"validation": ["validator --version"]})

    assert first.executables[0].canonical_path != second.executables[0].canonical_path
    assert first.digest != second.digest


def test_missing_facts_are_exact_and_structured(tmp_path: Path) -> None:
    authority = toolchain_authority.ToolchainAuthority(
        tmp_path,
        capability_probe=lambda _repo, _card: (
            "module:pytest",
            "cwd:missing-dir",
            "executable:/missing/tool",
        ),
    )

    snapshot = authority.evaluate({"validation": []})

    assert not snapshot.available
    assert {(item.kind, item.value) for item in snapshot.missing} == {
        ("module", "pytest"),
        ("cwd", "missing-dir"),
        ("executable", "/missing/tool"),
    }


def test_repair_is_atomic_idempotent_and_scoped_to_aiworkhub(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    snapshot = authority.evaluate(_card())

    assert authority.repair(snapshot)
    assert not authority.repair(snapshot)
    payload = json.loads(authority.manifest_path.read_text(encoding="utf-8"))
    assert payload["digest"] == snapshot.digest
    assert not list(authority.manifest_path.parent.glob(".snapshot-*"))

    external = toolchain_authority.ToolchainAuthority(
        tmp_path,
        manifest_path=tmp_path / "outside.json",
        capability_probe=lambda _repo, _card: (),
    )
    assert not external.repair(snapshot)
    assert not (tmp_path / "outside.json").exists()


def test_typed_extension_boundary_is_not_an_ordinary_requirement() -> None:
    assert {item.value for item in toolchain_authority.ProvisioningDomain} == {
        "repository_overlay",
        "kernel_backend",
    }
