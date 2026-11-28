"""NF-2026-01017: hosted non-Python validation argv paths.

(a) ``-DNAME=<abs>`` is accepted only beneath a declared (bound) read_root.
(b) A build dir named by an EARLIER row (cmake ``-B``) need not pre-exist.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiworkhub import task_templates, worker_workspace
from aiworkhub.validation_argv_paths import (
    build_dir_operands,
    declared_read_roots,
    host_define_beneath_read_root,
)

_UNSAFE = "invalid_validation_path_unsafe_token"


def _qt_root(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "Qt"
    prefix = root / "6.8.0" / "msvc2022_64"
    prefix.mkdir(parents=True)
    return root, f"-DCMAKE_PREFIX_PATH={prefix.as_posix()}"


def _cmake_row(define: str) -> str:
    return f"cmake -S shell -B shell/build-win {define}"


def test_define_beneath_bound_read_root_is_accepted_at_create(tmp_path: Path) -> None:
    root, define = _qt_root(tmp_path)
    with declared_read_roots([str(root)]):
        task_templates.validate_custom_validation_roles([_cmake_row(define)], None)
    # Equal to the root is accepted as well.
    with declared_read_roots([str(root)]):
        task_templates.validate_custom_validation_roles(
            [_cmake_row(f"-DCMAKE_PREFIX_PATH={root.as_posix()}")], None
        )
    # A define as the first argument is accepted too (reviewer finding).
    with declared_read_roots([str(root)]):
        task_templates.validate_custom_validation_roles(
            [f"cmake {define} -S shell -B shell/build-win"], None
        )


def test_define_without_matching_read_root_stays_refused(tmp_path: Path) -> None:
    root, define = _qt_root(tmp_path)
    other = tmp_path / "Other"
    other.mkdir()
    with pytest.raises(task_templates.TaskTemplateError, match=_UNSAFE):
        task_templates.validate_custom_validation_roles([_cmake_row(define)], None)
    with declared_read_roots([str(other)]), pytest.raises(
        task_templates.TaskTemplateError, match=_UNSAFE
    ):
        task_templates.validate_custom_validation_roles([_cmake_row(define)], None)
    # The binding is scoped: after the block the root no longer applies.
    with declared_read_roots([str(root)]):
        pass
    assert not host_define_beneath_read_root(define)


def test_define_value_shapes_fail_closed(tmp_path: Path) -> None:
    root, define = _qt_root(tmp_path)
    roots = [str(root)]
    assert host_define_beneath_read_root(define, roots)
    assert host_define_beneath_read_root(
        define.replace("=", ":PATH=", 1), roots
    )
    escape = f"-DCMAKE_PREFIX_PATH={root.as_posix()}/../Other"
    assert not host_define_beneath_read_root(escape, roots)
    assert not host_define_beneath_read_root(define + ";" + define[20:], roots)
    assert not host_define_beneath_read_root("-DCMAKE_PREFIX_PATH=rel/dir", roots)
    assert not host_define_beneath_read_root("-DCMAKE_PREFIX_PATH=//host/share", roots)
    assert not host_define_beneath_read_root(define, [])
    sibling = f"-DX={root.as_posix()}Evil/lib"
    assert not host_define_beneath_read_root(sibling, roots)


def test_non_python_card_classifies_at_create_only_with_read_root(tmp_path: Path) -> None:
    root, define = _qt_root(tmp_path)
    card = {
        "allowed_writes": ["shell/main.cpp"],
        "required_outputs": ["shell/main.cpp"],
        "validation": [_cmake_row(define), "ctest --test-dir shell/build-win"],
        "work_kind": "generic",
    }
    with declared_read_roots([str(root)]):
        provenance = task_templates.classify_task_card(**card)
    assert provenance["template_name"] == task_templates.CALLER_VALIDATION_TEMPLATE_NAME
    with pytest.raises(task_templates.TaskTemplateError):
        task_templates.classify_task_card(**card)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "shell").mkdir(parents=True)
    (repo / "shell" / "CMakeLists.txt").write_text("project(x)\n", encoding="utf-8")
    return repo


def _operands(repo: Path, validation: list[str]) -> tuple[str, ...]:
    return worker_workspace._validation_file_operands(
        repo, {"validation": validation}, ()
    )


def test_ctest_dir_created_by_earlier_cmake_row_is_accepted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    define = f"-DCMAKE_PREFIX_PATH={(tmp_path / 'Qt').as_posix()}"
    assert _operands(
        repo, [_cmake_row(define), "ctest --test-dir shell/build-win"]
    ) == ()
    assert _operands(
        repo, ["cmake -S shell -Bshell/build-win", "cmake --build shell/build-win",
               "ctest --test-dir shell/build-win/"]
    ) == ()
    assert not (repo / "shell" / "build-win").exists()


@pytest.mark.parametrize(
    ("rows", "index"),
    [
        # A later row creating the dir does not count.
        (["ctest --test-dir shell/build-win", "cmake -S shell -B shell/build-win"], 0),
        # The same row naming it as --build (not a creation) does not count.
        (["cmake -B shell/build-win --build shell/build-win"], 0),
        (["cmake --build shell/build-win"], 0),
        # Repository control dirs never qualify.
        (["cmake -S shell -B .git/build", "ctest --test-dir .git/build"], 0),
        (["cmake -S shell -B .aiworkhub/b", "ctest --test-dir .aiworkhub/b"], 0),
    ],
)
def test_build_dir_not_created_earlier_is_refused(
    tmp_path: Path, rows: list[str], index: int
) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(worker_workspace.WorkspaceError) as info:
        _operands(repo, rows)
    assert str(info.value).startswith(
        f"workspace_required_input_missing:field=validation:index={index}:"
    )


def test_build_dir_escaping_the_repo_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(worker_workspace.WorkspaceError):
        _operands(repo, ["cmake -S shell -B ../outside", "ctest --test-dir ../outside"])


def test_build_dir_through_symlink_component_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, repo / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable on this host")
    with pytest.raises(worker_workspace.WorkspaceError):
        _operands(repo, ["cmake -S shell -B link/build", "ctest --test-dir link/build"])


def test_build_dir_operands_vocabulary() -> None:
    assert build_dir_operands(["cmake", "-S", "s", "-B", "b"]) == ((4, "b", True),)
    assert build_dir_operands(["cmake.exe", "-Bb"]) == ((1, "b", True),)
    assert build_dir_operands(["cmake", "--build", "b"]) == ((2, "b", False),)
    assert build_dir_operands(["ninja", "-C", "b"]) == ((2, "b", False),)
    assert build_dir_operands(["meson", "-C", "b"]) == ((2, "b", False),)
    assert build_dir_operands(["ctest", "-B", "b"]) == ()
    assert build_dir_operands(["cmake", "-B"]) == ()
