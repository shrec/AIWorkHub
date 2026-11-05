"""NF-2026-02072: the Python import closure memoises probes and caches parses."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from aiworkhub import python_import_closure as closure
from aiworkhub.worker_workspace import WorkspaceError

SEEDS = ("src/pkg/seed.py",)
DYNAMIC = "src/pkg/dyn.py"


def _write(repo: Path, relative: str, source: str = "") -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _fixture(repo: Path) -> None:
    _write(repo, "src/pkg/__init__.py", "from .core import run\n")
    _write(repo, "src/pkg/core.py", "import os\nfrom . import helpers\nfrom .sub.leaf import X\n")
    _write(repo, "src/pkg/helpers.py", "VALUE = 1\n")
    _write(repo, "src/pkg/sub/__init__.py", "SUB = 1\n")
    _write(repo, "src/pkg/sub/leaf.py", "X = 1\n")
    _write(repo, DYNAMIC, "import importlib\nimportlib.import_module('pkg.hidden')\n")
    _write(repo, "src/pkg/hidden.py", "HIDDEN = 1\n")
    _write(repo, "src/pkg/seed.py", "from .core import run\nfrom . import dyn\nimport json\n")


EXPECTED = {
    "src/pkg/__init__.py",
    "src/pkg/core.py",
    "src/pkg/helpers.py",
    "src/pkg/sub/__init__.py",
    "src/pkg/sub/leaf.py",
    DYNAMIC,
    "src/pkg/hidden.py",
    "src/pkg/seed.py",
}


@pytest.fixture(autouse=True)
def _clean_process_records():
    closure.clear_process_import_records()
    yield
    closure.clear_process_import_records()


@pytest.fixture
def parsed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real = ast.parse

    def counting(source, filename="<unknown>", *args, **kwargs):
        calls.append(str(filename))
        return real(source, filename, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", counting)
    return calls


def _runtime(repo: Path) -> Path:
    runtime = repo / ".aiworkhub" / "runtime"
    runtime.mkdir(parents=True)
    return runtime / closure._CACHE_FILE


def test_cached_closure_is_identical_to_uncached_closure(tmp_path: Path) -> None:
    _fixture(tmp_path)
    uncached = closure.resolve_local_python_imports(tmp_path, SEEDS, use_cache=False)
    cold = closure.resolve_local_python_imports(tmp_path, SEEDS)
    warm = closure.resolve_local_python_imports(tmp_path, SEEDS)
    assert set(uncached) == EXPECTED
    assert uncached == cold == warm


def test_warm_run_parses_only_modules_that_bind_importlib(
    tmp_path: Path, parsed: list[str]
) -> None:
    _fixture(tmp_path)
    closure.resolve_local_python_imports(tmp_path, SEEDS)
    assert len(parsed) == len(EXPECTED)
    parsed.clear()
    closure.resolve_local_python_imports(tmp_path, SEEDS)
    assert parsed == [DYNAMIC]


def test_uncached_run_never_consults_or_fills_the_cache(
    tmp_path: Path, parsed: list[str]
) -> None:
    _fixture(tmp_path)
    target = _runtime(tmp_path)
    closure.resolve_local_python_imports(tmp_path, SEEDS, use_cache=False)
    parsed.clear()
    closure.resolve_local_python_imports(tmp_path, SEEDS, use_cache=False)
    assert len(parsed) == len(EXPECTED)
    assert not target.exists()


def test_editing_a_file_misses_the_cache_and_changes_the_closure(
    tmp_path: Path, parsed: list[str]
) -> None:
    _fixture(tmp_path)
    before = closure.resolve_local_python_imports(tmp_path, SEEDS)
    _write(tmp_path, "src/pkg/helpers.py", "from . import extra\n")
    _write(tmp_path, "src/pkg/extra.py")
    parsed.clear()
    after = closure.resolve_local_python_imports(tmp_path, SEEDS)
    assert "src/pkg/helpers.py" in parsed
    assert "src/pkg/seed.py" not in parsed
    assert "src/pkg/extra.py" in after and "src/pkg/extra.py" not in before
    assert after == closure.resolve_local_python_imports(tmp_path, SEEDS, use_cache=False)


def test_symlinked_module_is_rejected_cold_and_warm(tmp_path: Path) -> None:
    _fixture(tmp_path)
    _write(tmp_path, "src/pkg/seed.py", "from . import linked\n")
    _write(tmp_path / "outside", "escape.py")
    try:
        (tmp_path / "src/pkg/linked.py").symlink_to(tmp_path / "outside" / "escape.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")
    for _ in range(2):
        with pytest.raises(WorkspaceError, match="symlink"):
            closure.resolve_local_python_imports(tmp_path, SEEDS)


def test_symlinked_package_directory_is_rejected_cold_and_warm(tmp_path: Path) -> None:
    _fixture(tmp_path)
    _write(tmp_path, "src/pkg/seed.py", "from .linkdir import mod\n")
    _write(tmp_path / "outside", "mod.py", "M = 1\n")
    try:
        (tmp_path / "src/pkg/linkdir").symlink_to(tmp_path / "outside", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")
    for _ in range(2):
        with pytest.raises(WorkspaceError, match="symlink"):
            closure.resolve_local_python_imports(tmp_path, SEEDS)


def test_unresolved_relative_import_still_fails_closed_when_cached(tmp_path: Path) -> None:
    _fixture(tmp_path)
    _write(tmp_path, "src/pkg/seed.py", "from .missing import thing\n")
    for _ in range(2):
        with pytest.raises(WorkspaceError, match="relative_import_unresolved"):
            closure.resolve_local_python_imports(tmp_path, SEEDS)


def test_records_persist_under_runtime_and_survive_a_new_process(
    tmp_path: Path, parsed: list[str]
) -> None:
    _fixture(tmp_path)
    target = _runtime(tmp_path)
    first = closure.resolve_local_python_imports(tmp_path, SEEDS)
    payload = json.loads(target.read_bytes())
    assert payload["schema"] == closure._CACHE_SCHEMA
    assert closure._CACHE_SCHEMA.endswith("py%d.%d" % sys.version_info[:2])
    assert len(payload["entries"]) == len(EXPECTED) - 1  # dyn.py binds importlib
    assert [p.name for p in target.parent.iterdir()] == [target.name]
    closure.clear_process_import_records()
    parsed.clear()
    assert closure.resolve_local_python_imports(tmp_path, SEEDS) == first
    assert parsed == [DYNAMIC]


def test_no_runtime_directory_means_no_file_is_created(tmp_path: Path) -> None:
    _fixture(tmp_path)
    closure.resolve_local_python_imports(tmp_path, SEEDS)
    assert not (tmp_path / ".aiworkhub").exists()


@pytest.mark.parametrize(
    "garbage",
    [b"", b"{not json", b"[]", b'{"schema":"other","entries":{}}', b'{"schema":"%s","entries":[]}'],
)
def test_corrupt_cache_file_falls_back_to_parsing(tmp_path: Path, garbage: bytes) -> None:
    _fixture(tmp_path)
    target = _runtime(tmp_path)
    target.write_bytes(garbage.replace(b"%s", closure._CACHE_SCHEMA.encode()))
    result = closure.resolve_local_python_imports(tmp_path, SEEDS)
    assert set(result) == EXPECTED
    assert json.loads(target.read_bytes())["schema"] == closure._CACHE_SCHEMA


def test_malformed_record_is_ignored_and_reparsed(tmp_path: Path, parsed: list[str]) -> None:
    _fixture(tmp_path)
    target = _runtime(tmp_path)
    digest = hashlib.sha256((tmp_path / "src/pkg/seed.py").read_bytes()).hexdigest()
    bad = {digest: [[["f", None, "one", ["x"]]], ["os"]]}
    target.write_text(json.dumps({"schema": closure._CACHE_SCHEMA, "entries": bad}))
    result = closure.resolve_local_python_imports(tmp_path, SEEDS)
    assert set(result) == EXPECTED
    assert "src/pkg/seed.py" in parsed


def test_cache_is_bounded_on_disk_and_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    target = _runtime(tmp_path)
    monkeypatch.setattr(closure, "_CACHE_MAX_ENTRIES", 2)
    result = closure.resolve_local_python_imports(tmp_path, SEEDS)
    assert set(result) == EXPECTED
    assert len(json.loads(target.read_bytes())["entries"]) == 2
    assert len(closure._MEMORY_RECORDS) <= 2


def test_concurrent_writers_leave_a_valid_cache_and_identical_closures(tmp_path: Path) -> None:
    _fixture(tmp_path)
    target = _runtime(tmp_path)
    results: list[tuple[str, ...]] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(closure.resolve_local_python_imports(tmp_path, SEEDS))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(set(results)) == 1 and set(results[0]) == EXPECTED
    assert json.loads(target.read_bytes())["schema"] == closure._CACHE_SCHEMA
    assert [p.name for p in target.parent.iterdir()] == [target.name]


def test_permission_error_while_probing_is_raised_not_read_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    real = os.lstat

    def lstat(path, *args, **kwargs):
        if Path(path).name == "helpers.py":
            raise PermissionError(13, "denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(closure.os, "lstat", lstat)
    with pytest.raises(PermissionError):
        closure.resolve_local_python_imports(tmp_path, SEEDS, use_cache=False)
