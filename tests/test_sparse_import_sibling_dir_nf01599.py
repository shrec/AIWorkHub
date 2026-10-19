"""Regression coverage for the depth-1 sibling-directory sparse import fallback.

A validation test may live next to a top-level ``import helper_mod`` whose real
module sits in a depth-1 repository directory such as ``scripts/`` (the test or
pytest config puts that directory on ``sys.path``).  Sparse seeding must follow
that import, but only through regular files in non-dot depth-1 directories and
never for stdlib names, while relative imports keep failing closed.

Only depth-1 directories are searched; a deeper ``pythonpath`` entry is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import worker_workspace


def _write(repo: Path, relative: str, text: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_import_from_depth1_sibling_directory_is_seeded_transitively(tmp_path):
    repo = tmp_path
    _write(repo, "tests/test_x.py", "import helper_mod\n")
    _write(repo, "scripts/helper_mod.py", "import helper_dep\n")
    _write(repo, "scripts/helper_dep.py", "VALUE = 1\n")

    seeded = worker_workspace._resolve_local_python_imports(
        repo, ["tests/test_x.py"]
    )

    assert "scripts/helper_mod.py" in seeded
    assert "scripts/helper_dep.py" in seeded


def test_stdlib_name_is_never_seeded_from_sibling_directory(tmp_path):
    repo = tmp_path
    _write(repo, "tests/test_y.py", "import json\n")
    _write(repo, "scripts/json.py", "VALUE = 1\n")

    seeded = worker_workspace._resolve_local_python_imports(
        repo, ["tests/test_y.py"]
    )

    assert "scripts/json.py" not in seeded


def test_dot_directory_is_never_seeded_by_fallback(tmp_path):
    repo = tmp_path
    _write(repo, "tests/test_c.py", "import helper2\n")
    _write(repo, ".hidden/helper2.py", "VALUE = 1\n")

    seeded = worker_workspace._resolve_local_python_imports(
        repo, ["tests/test_c.py"]
    )

    assert ".hidden/helper2.py" not in seeded
    assert not [name for name in seeded if name.startswith(".hidden/")]


def test_src_package_module_still_resolves_without_sibling_fallback(tmp_path):
    repo = tmp_path
    _write(repo, "tests/test_d.py", "import pkgmod\n")
    _write(repo, "src/pkgmod.py", "VALUE = 1\n")
    _write(repo, "scripts/other.py", "VALUE = 2\n")

    seeded = worker_workspace._resolve_local_python_imports(
        repo, ["tests/test_d.py"]
    )

    assert "src/pkgmod.py" in seeded
    assert not [name for name in seeded if name.startswith("scripts/")]


def test_unresolved_relative_import_still_fails_closed(tmp_path):
    repo = tmp_path
    _write(repo, "tests/test_e.py", "from .missing import thing\n")

    with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
        worker_workspace._resolve_local_python_imports(repo, ["tests/test_e.py"])

    assert "validation_python_relative_import_unresolved" in str(excinfo.value)


def test_symlinked_package_under_sibling_directory_does_not_abort(tmp_path):
    repo = tmp_path
    _write(repo, "src/mypkg/__init__.py", "def helper():\n    return 1\n")
    scripts = repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    try:
        (scripts / "mypkg").symlink_to(repo / "src" / "mypkg", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unsupported on this filesystem")
    _write(repo, "tests/test_a.py", "from mypkg import helper\nimport pytest\n")

    result = worker_workspace._resolve_local_python_imports(repo, ["tests/test_a.py"])

    assert not any(Path(item).as_posix().startswith("scripts/") for item in result)
