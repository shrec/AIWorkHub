"""NF-2026-02079: native build-driver cards seed the full tracked tree.

A card whose validation runs ``cmake``/``ctest``/... needs ``CMakePresets.json``,
the whole ``CMakeLists.txt`` tree, target sources no card names and
``third_party/``.  Those are seeded as immutable support from the git index;
a python-only card's closure stays exactly as sparse as before.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from aiworkhub import worker_workspace, worker_workspace_native_seed
from aiworkhub.worker_workspace_native_seed import (
    NATIVE_BUILD_DRIVERS,
    invokes_native_build_driver,
    native_build_driver,
    native_build_support,
)

_FILES = {
    "CMakePresets.json": '{"version": 3, "configurePresets": []}\n',
    "CMakeLists.txt": "cmake_minimum_required(VERSION 3.20)\nadd_subdirectory(src)\n",
    "src/CMakeLists.txt": "add_executable(app app.cpp engine.cpp)\n",
    "src/app.cpp": "int main() { return 0; }\n",
    "src/engine.cpp": "int engine() { return 1; }\n",
    "third_party/lib/lib.c": "int lib(void) { return 2; }\n",
    "src/aiworkhub_free/__init__.py": "",
    "tests/test_example.py": "def test_x():\n    assert True\n",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, timeout=60
    )


def _repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git executable is required for the tracked-tree fixture")
    repo = tmp_path / "repo"
    for relative, text in _FILES.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "--", *_FILES)
    # Present on disk but untracked: must never reach the provider worktree.
    (repo / "build").mkdir()
    (repo / "build" / "CMakeCache.txt").write_text("private\n", encoding="utf-8")
    (repo / "notes.local").write_text("private\n", encoding="utf-8")
    return repo


def test_cmake_card_seeds_presets_lists_tree_unnamed_sources_and_third_party(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    card = {
        "allowed_writes": ["src/app.cpp"],
        "validation": ["cmake --preset ci", "cd src && ctest --preset ci"],
    }

    live, support, seeded = worker_workspace._declared_workspace_seed_closure(
        repo, card, ("src/app.cpp",)
    )

    for relative in (
        "CMakePresets.json",
        "CMakeLists.txt",
        "src/CMakeLists.txt",
        "src/engine.cpp",
        "third_party/lib/lib.c",
    ):
        assert relative in support, relative
        assert relative in seeded, relative
    assert "src/app.cpp" in live
    assert "src/app.cpp" not in support
    assert "build/CMakeCache.txt" not in seeded
    assert "notes.local" not in seeded
    assert seeded == sorted(set(live) | set(support))


def test_pytest_only_card_seed_set_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    card = {
        "allowed_writes": ["tests/test_example.py"],
        "validation": ["python -m pytest -q tests/test_example.py"],
    }
    allowed = ("tests/test_example.py",)

    with_native = worker_workspace._declared_workspace_seed_closure(repo, card, allowed)

    def _no_git(*_args: object, **_kwargs: object) -> set[str]:
        raise AssertionError("a pytest-only card must not list the tracked tree")

    with monkeypatch.context() as patched:
        patched.setattr(worker_workspace, "_tracked_repository_paths", _no_git)
        assert native_build_support(repo, card["validation"]) == ()
    monkeypatch.setattr(
        worker_workspace_native_seed, "native_build_support", lambda *_a: ()
    )
    without_native = worker_workspace._declared_workspace_seed_closure(
        repo, card, allowed
    )

    assert with_native == without_native
    _live, _support, seeded = with_native
    for relative in ("CMakePresets.json", "src/engine.cpp", "third_party/lib/lib.c"):
        assert relative not in seeded


def test_native_card_without_a_tracked_listing_is_refused_by_name(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "plain"
    repo.mkdir()
    (repo / "CMakeLists.txt").write_text("project(x)\n", encoding="utf-8")
    with pytest.raises(worker_workspace.WorkspaceError) as caught:
        native_build_support(repo, ["cmake -S . -B build"])
    assert "validation_native_build_tree_unlisted" in str(caught.value)


@pytest.mark.parametrize("driver", sorted(NATIVE_BUILD_DRIVERS))
def test_detector_recognises_every_listed_driver(driver: str) -> None:
    assert NATIVE_BUILD_DRIVERS == {
        "cmake", "ctest", "ninja", "make", "nmake", "msbuild",
        "meson", "cargo", "go", "dotnet", "gradle", "mvn",
    }
    for spelling in (
        driver,
        driver.upper(),
        f"/usr/local/bin/{driver}",
        f"C:\\tools\\{driver}.exe",
        f"{driver}.cmd",
        f"{driver}.bat",
    ):
        assert native_build_driver(spelling) == driver, spelling
    assert invokes_native_build_driver([f"{driver} --version"])
    assert invokes_native_build_driver([f"cd sub && {driver} --version"])
    assert invokes_native_build_driver([f"PYTHONPATH=. {driver} --version"])


@pytest.mark.parametrize(
    "executable",
    ["mycmake", "cmake_helper", "cmake.py", "ctest2", "gopher", "makefile",
     "cargo-fuzz", "python", "pytest", "npm", "ruff", ""],
)
def test_detector_rejects_look_alikes(executable: str) -> None:
    assert native_build_driver(executable) is None


@pytest.mark.parametrize(
    "row",
    [
        "python -m cmake_helper",
        "mycmake --build build",
        "python -m pytest -q tests/test_example.py",
        "npm --prefix web test",
        "python tools/run.py cmake ctest",
    ],
)
def test_rows_that_only_mention_a_driver_are_not_native(row: str) -> None:
    assert not invokes_native_build_driver([row])


def test_tracked_path_under_a_symlinked_parent_is_not_seeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.c").write_text("int s;\n", encoding="utf-8")
    (repo / "inside.c").write_text("int i;\n", encoding="utf-8")
    try:
        (repo / "vendor").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not permitted on this host")
    monkeypatch.setattr(
        worker_workspace,
        "_tracked_repository_paths",
        lambda *_a, **_k: {"inside.c", "vendor/secret.c"},
    )

    assert native_build_support(repo, ["cmake --build build"]) == ("inside.c",)
