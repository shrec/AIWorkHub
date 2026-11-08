"""Toolchain refusals name the token and the toolchains.json remedy (NF-2026-02166)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from aiworkhub import task_templates, toolchain_authority
from aiworkhub.toolchain_authority import MissingRequirement
from aiworkhub.toolchain_registry import REGISTRY_RELATIVE_PATH
from aiworkhub.validation_spawn_diagnosis import registry_entry_remedy

_PREFIX = "task_contract_unwinnable:"


def _fake_compiler_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``<tmp>/path/clang++-20`` -> ``<tmp>/llvm-20/bin/clang++`` on PATH.

    The target is created executable with ``os.open`` so no chmod is needed.
    """
    install_bin = tmp_path / "llvm-20" / "bin"
    install_bin.mkdir(parents=True)
    target = install_bin / "clang++"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT, 0o755)
    os.close(descriptor)
    path_dir = tmp_path / "path"
    path_dir.mkdir()
    try:
        (path_dir / "clang++-20").symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.setenv("PATH", str(path_dir))
    return install_bin.resolve().parent


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable bit on PATH")
def test_unregistered_bare_head_on_path_gets_symlink_resolved_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_root = _fake_compiler_on_path(tmp_path, monkeypatch)

    message = toolchain_authority.unwinnable_refusal(
        (MissingRequirement("executable", "clang++-20"),)
    )

    assert message.startswith(_PREFIX + '["executable:clang++-20"]; ')
    assert REGISTRY_RELATIVE_PATH.as_posix() in message
    assert message.endswith(registry_entry_remedy("clang++-20", str(install_root)))
    assert str(install_root) in message or install_root.as_posix() in message


def test_unregistered_bare_head_remedy_is_exact_on_every_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_bin = tmp_path / "llvm-20" / "bin"
    install_bin.mkdir(parents=True)
    compiler = install_bin / "clang++-20"
    compiler.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        toolchain_authority.shutil, "which", lambda name, *a, **k: str(compiler)
    )

    message = toolchain_authority.unwinnable_refusal(
        (MissingRequirement("executable", "clang++-20"),)
    )

    root = str((tmp_path / "llvm-20").resolve())
    assert message == (
        _PREFIX
        + '["executable:clang++-20"]; '
        + registry_entry_remedy("clang++-20", root)
    )
    assert REGISTRY_RELATIVE_PATH.as_posix() in message


def test_bare_name_not_on_path_keeps_plain_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    message = toolchain_authority.unwinnable_refusal(
        (MissingRequirement("executable", "no-such-compiler-nf02166"),)
    )

    assert message == _PREFIX + '["executable:no-such-compiler-nf02166"]'


def test_absolute_missing_path_and_other_kinds_keep_plain_text() -> None:
    message = toolchain_authority.unwinnable_refusal(
        (
            MissingRequirement("executable", "/missing-validator"),
            MissingRequirement("module", "pytest"),
        )
    )

    assert message == _PREFIX + '["executable:/missing-validator","module:pytest"]'


def test_embedded_path_refusal_names_token_and_reason_for_non_head() -> None:
    with pytest.raises(task_templates.TaskTemplateError) as caught:
        task_templates.validate_custom_validation_roles(
            ["g++ -fsyntax-only /etc/shadow.cpp"], None
        )

    assert (
        str(caught.value)
        == "invalid_validation_embedded_path:/etc/shadow.cpp:invalid_validation_path_absolute"
    )


def test_embedded_path_refusal_for_absolute_head_adds_registry_remedy() -> None:
    head = "/usr/lib/llvm-20/bin/clang++"

    with pytest.raises(task_templates.TaskTemplateError) as caught:
        task_templates.validate_custom_validation_roles(
            [f"{head} -fsyntax-only src/a.cpp"], None
        )

    expected_prefix = (
        f"invalid_validation_embedded_path:{head}:invalid_validation_path_absolute"
        f"; register the bare name in {REGISTRY_RELATIVE_PATH.as_posix()}: "
    )
    assert str(caught.value) == expected_prefix + registry_entry_remedy(
        "clang++", "/usr/lib/llvm-20"
    )


def test_embedded_path_refusal_bounds_the_token() -> None:
    long_head = "/" + "a" * 400

    with pytest.raises(task_templates.TaskTemplateError) as caught:
        task_templates.validate_custom_validation_roles([long_head], None)

    assert str(caught.value) == (
        f"invalid_validation_embedded_path:{long_head[:200]}"
        ":invalid_validation_path_absolute"
    )


def test_embedded_path_refusal_non_absolute_head_gets_no_remedy() -> None:
    error = task_templates._embedded_path_refusal(
        "a:b", "invalid_validation_path_other", head=True
    )

    assert str(error) == "invalid_validation_embedded_path:a:b:invalid_validation_path_other"
