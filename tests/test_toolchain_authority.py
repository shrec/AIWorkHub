from __future__ import annotations

import json
import os
import subprocess
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


def _host_executable(path: Path) -> Path:
    """One real file this host treats as executable, on POSIX and on Windows."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(path, 0o755)
    return path



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


def _expose_key_to_everyone(key_path: Path) -> bool:
    """Make the key readable beyond its owner, in this host's own vocabulary.

    POSIX exposure is a mode bit. Windows has no such bit -- ``0o666`` there only
    means "not read-only" and exposes nothing -- so the equivalent exposure is an
    explicit Everyone (S-1-1-0) ACE. Returns whether the exposure was actually
    created, so the test refuses to assert against a setup that did not happen.
    """

    if os.name != "nt":
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"k" * 32)
        return key_path.stat().st_mode & 0o077 != 0
    key_path.write_bytes(b"k" * 32)
    completed = subprocess.run(
        ["icacls", str(key_path), "/grant", "*S-1-1-0:(R)"],
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def test_authority_secret_rejects_wrong_mode_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_secret_env(monkeypatch)
    key_path = _secret_path(tmp_path)
    key_path.parent.mkdir(parents=True)
    if not _expose_key_to_everyone(key_path):
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


def test_request_6ba3c9b1_cache_and_receipt_bind_repository_input_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "AIWORKHUB_TOOLCHAIN_AUTHORITY_HMAC_KEY",
        "hex:" + ("ac" * 32),
    )
    card = {
        **_card(),
        "task_id": "TASK_REPLAY_INPUT_IDENTITY",
        "request_id": "6ba3c9b1f18d431cb048f640b37f4f30",
        "immutable_inputs": ["inputs/one.json"],
        "rework_predecessor": {
            "schema_id": "aiworkhub.rework_predecessor.v1",
            "request_id": "predecessor-one",
            "changed_path_hashes": {"out/result.json": "a" * 64},
        },
    }
    immutable_change = {
        **card,
        "immutable_inputs": ["inputs/two.json"],
    }
    rework_change = {
        **card,
        "rework_predecessor": {
            **card["rework_predecessor"],
            "changed_path_hashes": {"out/result.json": "b" * 64},
        },
    }
    authority = _authority(tmp_path)

    snapshot = authority.evaluate(card)
    immutable_snapshot = authority.evaluate(immutable_change)
    rework_snapshot = authority.evaluate(rework_change)

    assert len(
        {
            snapshot.cache_identity,
            immutable_snapshot.cache_identity,
            rework_snapshot.cache_identity,
        }
    ) == 3
    assert len(
        {
            toolchain_authority._receipt_card_identity(candidate)
            for candidate in (card, immutable_change, rework_change)
        }
    ) == 3

    receipt = toolchain_authority.authority_receipt(snapshot, card)
    assert toolchain_authority.verify_authority_receipt(receipt, tmp_path, card)
    for mismatched in (immutable_change, rework_change):
        with pytest.raises(
            ValueError,
            match="validation_toolchain_authority_receipt_card_identity_mismatch",
        ):
            toolchain_authority.verify_authority_receipt(receipt, tmp_path, mismatched)


def test_executable_and_symlink_replacement_invalidate_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiworkhub import worker_workspace

    # Two REAL, distinct executables. "/bin/true" and "/bin/false" do not exist
    # on Windows, where both sides then resolved to nothing and the assertion
    # proved nothing about symlink replacement.
    targets = tmp_path / "targets"
    targets.mkdir()
    first_target = _host_executable(targets / "first")
    second_target = _host_executable(targets / "second")
    link = tmp_path / "validator"
    link.symlink_to(first_target)

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
    link.symlink_to(second_target)

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


def test_authenticated_external_validation_head_is_not_required_from_worktree(
    tmp_path: Path, monkeypatch
) -> None:
    from aiworkhub import worker_workspace

    declared = tmp_path / ".venv" / "bin" / "python"
    declared.parent.mkdir(parents=True)
    declared.write_text("untracked declaration", encoding="utf-8")
    test_file = tmp_path / "tests" / "test_x.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_x(): pass\n", encoding="utf-8")
    monkeypatch.setattr(
        toolchain_authority,
        "repository_tracked_paths",
        lambda _repo: frozenset({"tests/test_x.py"}),
    )
    monkeypatch.setattr(
        worker_workspace,
        "_normalize_trusted_validation_executable_argv_with_roots",
        lambda argv, _repo: ([sys.executable, *argv[1:]], ()),
    )
    monkeypatch.setattr(
        toolchain_authority, "_read_executable_version", lambda _path: "Python test"
    )

    snapshot = _authority(tmp_path).evaluate(
        {"validation": [".venv/bin/python -m pytest -q tests/test_x.py"]}
    )

    assert snapshot.available
    assert snapshot.executables[0].requested == ".venv/bin/python"
    assert not [item for item in snapshot.missing if item.kind == "worker_workspace"]


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


def test_declared_dash_m_module_reads_only_the_interpreter_option() -> None:
    # NF-2026-00010: the requirement behind ``python -m ruff`` is Ruff, so the
    # module has to be identified before a version fact is attributed to it.
    assert (
        toolchain_authority._declared_dash_m_module(["python", "-m", "ruff", "check"])
        == "ruff"
    )
    assert (
        toolchain_authority._declared_dash_m_module(["python", "-I", "-m", "pytest"])
        == "pytest"
    )
    assert toolchain_authority._declared_dash_m_module(["node", "--version"]) == ""
    assert toolchain_authority._declared_dash_m_module([]) == ""
    # A ``-m`` that belongs to the SCRIPT's own argument vector is not a module.
    assert (
        toolchain_authority._declared_dash_m_module(["python", "script.py", "-m", "x"])
        == ""
    )
    assert toolchain_authority._declared_dash_m_module(["python", "-m"]) == ""
    assert toolchain_authority._declared_dash_m_module(["python", "-m", "-q"]) == ""
