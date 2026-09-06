from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from aiworkhub import toolchain_authority


def _write_registry(repo: Path, payload: dict[str, object]) -> None:
    (repo / "aiworkhub.toolchain.json").write_text(
        json.dumps(payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _registry(command: str = "python -m compileall -q src") -> dict[str, object]:
    return {
        "schema_id": toolchain_authority.REGISTRY_SCHEMA_ID,
        "version": 1,
        "baseline": ["python"],
        "tools": {
            "python": {
                "candidates": [{"executable": "python", "args": command.split()[1:]}],
                "minimum_version": "",
            }
        },
        "sandbox_capabilities": ["validation_subprocess"],
    }


def _versioned_executable(path: Path, version: str = "1.0.0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(f"#!/bin/sh\nprintf '{version}\\n'\n")
    return path


def test_project_registry_merges_baseline_with_card_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registry(tmp_path, _registry())
    calls: list[list[str]] = []

    def normalize(argv: list[str], _repo: Path) -> tuple[list[str], tuple[Path, ...]]:
        calls.append(list(argv))
        return [sys.executable, *argv[1:]], ()

    from aiworkhub import worker_workspace

    monkeypatch.setattr(
        worker_workspace,
        "_normalize_trusted_validation_executable_argv_with_roots",
        normalize,
    )
    authority = toolchain_authority.ToolchainAuthority(
        tmp_path, capability_probe=lambda _repo, _card: ()
    )

    snapshot = authority.evaluate({"validation": ["python -m pytest -q"]})

    assert snapshot.available
    assert [call[:3] for call in calls] == [
        ["python", "-m", "compileall"],
        ["python", "-m", "pytest"],
    ]
    assert snapshot.registry_fingerprint
    assert snapshot.cache_identity


def test_unchanged_registry_evaluation_reuses_local_snapshot_without_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registry(tmp_path, _registry())
    authority = toolchain_authority.ToolchainAuthority(
        tmp_path, capability_probe=lambda _repo, _card: ()
    )
    first = authority.evaluate({"validation": []})
    assert authority.repair(first)

    from aiworkhub import worker_workspace

    def fail_resolution(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("resolution repeated for unchanged registry")

    monkeypatch.setattr(
        worker_workspace,
        "_normalize_trusted_validation_executable_argv_with_roots",
        fail_resolution,
    )
    second = toolchain_authority.ToolchainAuthority(
        tmp_path, capability_probe=lambda _repo, _card: ()
    ).evaluate({"validation": []})

    assert second.digest == first.digest
    assert second.executables == first.executables


def test_registry_cache_identity_includes_card_validation_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registry(tmp_path, _registry())
    calls: list[list[str]] = []

    def normalize(argv: list[str], _repo: Path) -> tuple[list[str], tuple[Path, ...]]:
        calls.append(list(argv))
        return [sys.executable, *argv[1:]], ()

    from aiworkhub import worker_workspace

    monkeypatch.setattr(
        worker_workspace,
        "_normalize_trusted_validation_executable_argv_with_roots",
        normalize,
    )
    authority = toolchain_authority.ToolchainAuthority(
        tmp_path, capability_probe=lambda _repo, _card: ()
    )

    first = authority.evaluate({"validation": ["python -m pytest -q"]})
    second = authority.evaluate({"validation": ["python -m compileall -q tests"]})

    assert first.cache_identity != second.cache_identity
    assert second.digest != first.digest
    assert calls[-1][:3] == ["python", "-m", "compileall"]


def test_registry_metadata_drift_invalidates_cached_receipt(tmp_path: Path) -> None:
    _write_registry(tmp_path, _registry())
    authority = toolchain_authority.ToolchainAuthority(
        tmp_path, capability_probe=lambda _repo, _card: ()
    )
    first = authority.evaluate({"validation": []})
    authority.repair(first)

    registry = _registry("python -m compileall -q tests")
    _write_registry(tmp_path, registry)

    second = authority.evaluate({"validation": []})

    assert second.digest != first.digest
    assert second.registry_fingerprint != first.registry_fingerprint


def test_registry_tries_ordered_candidates_until_one_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _registry()
    tools = payload["tools"]
    assert isinstance(tools, dict)
    python = tools["python"]
    assert isinstance(python, dict)
    python["candidates"] = [
        {"executable": "missing-python"},
        {"executable": "python", "args": ["-m", "compileall", "-q", "src"]},
    ]
    _write_registry(tmp_path, payload)
    calls: list[str] = []

    def normalize(argv: list[str], _repo: Path) -> tuple[list[str], tuple[Path, ...]]:
        calls.append(argv[0])
        if argv[0] == "missing-python":
            from aiworkhub import worker_workspace

            raise worker_workspace.WorkspaceError("validation_executable_unavailable")
        return [sys.executable, *argv[1:]], ()

    from aiworkhub import worker_workspace

    monkeypatch.setattr(
        worker_workspace,
        "_normalize_trusted_validation_executable_argv_with_roots",
        normalize,
    )

    snapshot = toolchain_authority.ToolchainAuthority(
        tmp_path, capability_probe=lambda _repo, _card: ()
    ).evaluate({"validation": []})

    assert snapshot.available
    assert calls == ["missing-python", "python"]


def test_registry_minimum_version_uses_executable_version_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validator = _versioned_executable(tmp_path / "bin" / "validator", "2.4.1")
    payload = _registry()
    tools = payload["tools"]
    assert isinstance(tools, dict)
    tools["validator"] = {
        "candidates": [{"executable": "validator"}],
        "minimum_version": "2.4.0",
    }
    payload["baseline"] = ["validator"]
    _write_registry(tmp_path, payload)

    from aiworkhub import worker_workspace

    monkeypatch.setattr(
        worker_workspace,
        "_normalize_trusted_validation_executable_argv_with_roots",
        lambda argv, _repo: ([str(validator), *argv[1:]], ()),
    )

    snapshot = toolchain_authority.ToolchainAuthority(
        tmp_path, capability_probe=lambda _repo, _card: ()
    ).evaluate({"validation": []})

    assert snapshot.available
    assert snapshot.executables[0].version_fact == "2.4.1"


def test_registry_version_probe_uses_worker_validation_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validator = _versioned_executable(tmp_path / "bin" / "validator", "9.8.7")
    payload = _registry()
    tools = payload["tools"]
    assert isinstance(tools, dict)
    tools["validator"] = {
        "candidates": [{"executable": "validator"}],
        "minimum_version": "9.0.0",
    }
    payload["baseline"] = ["validator"]
    _write_registry(tmp_path, payload)

    from aiworkhub import worker_workspace

    monkeypatch.setattr(
        worker_workspace,
        "_normalize_trusted_validation_executable_argv_with_roots",
        lambda argv, _repo: ([str(validator), *argv[1:]], ()),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        worker_workspace,
        "trusted_validation_executable_version",
        lambda resolved: calls.append(resolved) or "9.8.7",
    )

    snapshot = toolchain_authority.ToolchainAuthority(
        tmp_path, capability_probe=lambda _repo, _card: ()
    ).evaluate({"validation": []})

    assert snapshot.available
    assert calls == [str(validator.resolve())]


def test_registry_structured_args_preserve_argv_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _registry()
    tools = payload["tools"]
    assert isinstance(tools, dict)
    python = tools["python"]
    assert isinstance(python, dict)
    python["candidates"] = [
        {"executable": "python", "args": ["-c", "print('two words')"]}
    ]
    _write_registry(tmp_path, payload)
    calls: list[list[str]] = []

    from aiworkhub import worker_workspace

    monkeypatch.setattr(
        worker_workspace,
        "_normalize_trusted_validation_executable_argv_with_roots",
        lambda argv, _repo: calls.append(list(argv)) or ([sys.executable, *argv[1:]], ()),
    )

    snapshot = toolchain_authority.ToolchainAuthority(
        tmp_path, capability_probe=lambda _repo, _card: ()
    ).evaluate({"validation": []})

    assert snapshot.available
    assert calls == [["python", "-c", "print('two words')"]]


def test_registry_minimum_version_rejects_actual_version_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validator = _versioned_executable(tmp_path / "bin" / "validator", "1.9.9")
    payload = _registry()
    tools = payload["tools"]
    assert isinstance(tools, dict)
    tools["validator"] = {
        "candidates": [{"executable": "validator"}],
        "minimum_version": "2.0.0",
    }
    payload["baseline"] = ["validator"]
    _write_registry(tmp_path, payload)

    from aiworkhub import worker_workspace

    monkeypatch.setattr(
        worker_workspace,
        "_normalize_trusted_validation_executable_argv_with_roots",
        lambda argv, _repo: ([str(validator), *argv[1:]], ()),
    )

    snapshot = toolchain_authority.ToolchainAuthority(
        tmp_path, capability_probe=lambda _repo, _card: ()
    ).evaluate({"validation": []})

    assert not snapshot.available
    assert ("version", "validator>=2.0.0") in {
        (item.kind, item.value) for item in snapshot.missing
    }


def test_tampered_snapshot_payload_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registry(tmp_path, _registry())
    authority = toolchain_authority.ToolchainAuthority(
        tmp_path, capability_probe=lambda _repo, _card: ()
    )
    first = authority.evaluate({"validation": []})
    assert authority.repair(first)
    payload = json.loads(authority.manifest_path.read_text(encoding="utf-8"))
    payload["missing"] = [{"kind": "module", "value": "tampered", "command": ""}]
    authority.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    calls = 0

    def normalize(argv: list[str], _repo: Path) -> tuple[list[str], tuple[Path, ...]]:
        nonlocal calls
        calls += 1
        return [sys.executable, *argv[1:]], ()

    from aiworkhub import worker_workspace

    monkeypatch.setattr(
        worker_workspace,
        "_normalize_trusted_validation_executable_argv_with_roots",
        normalize,
    )
    second = toolchain_authority.ToolchainAuthority(
        tmp_path, capability_probe=lambda _repo, _card: ()
    ).evaluate({"validation": []})

    assert second.available
    assert calls == 1


def test_malformed_registry_fails_closed_with_structured_reason(tmp_path: Path) -> None:
    (tmp_path / "aiworkhub.toolchain.json").write_text("{broken", encoding="utf-8")
    authority = toolchain_authority.ToolchainAuthority(
        tmp_path, capability_probe=lambda _repo, _card: ()
    )

    snapshot = authority.evaluate({"validation": []})

    assert not snapshot.available
    assert [(item.kind, item.value) for item in snapshot.missing] == [
        ("registry", "malformed_json")
    ]


def test_registry_rejects_host_absolute_candidate(tmp_path: Path) -> None:
    payload = _registry()
    tools = payload["tools"]
    assert isinstance(tools, dict)
    python = tools["python"]
    assert isinstance(python, dict)
    python["candidates"] = [{"executable": "/usr/bin/python"}]
    _write_registry(tmp_path, payload)

    snapshot = toolchain_authority.ToolchainAuthority(
        tmp_path, capability_probe=lambda _repo, _card: ()
    ).evaluate({"validation": []})

    assert not snapshot.available
    assert ("registry", "candidate_unavailable:python") in {
        (item.kind, item.value) for item in snapshot.missing
    }


def test_committed_registry_is_portable() -> None:
    path = Path("aiworkhub.toolchain.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    encoded = json.dumps(payload, sort_keys=True)

    assert payload["schema_id"] == toolchain_authority.REGISTRY_SCHEMA_ID
    assert not any(part in encoded for part in (str(Path.home()), os.getcwd()))
    assert "/home/" not in encoded
