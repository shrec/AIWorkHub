"""NF-2026-02054 card A: the per-test impact recorder and the changed-lines selector.

Every acceptance line runs against a small git fixture repository recorded once by a
real ``pytest -p aiworkhub.test_impact_recorder`` child run: ``app.core`` (two
functions, imported by test_alpha and test_beta), ``app.other`` (a session fixture
used by both), ``app.reader`` (opens ``table.txt``), ``app.cli`` (launched as a child
interpreter by test_child) and test_plain (imports nothing). Each test rebuilds the
same tree, installs the recorded map and changes one thing.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import test_impact_recorder as recorder  # noqa: E402
from aiworkhub import test_impact_select as tis  # noqa: E402

_CHILD_TEST = '''import os
import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"


def test_child():
    env = dict(os.environ, PYTHONPATH=str(_SRC))
    subprocess.run([sys.executable, "-m", "app.cli"], check=True, env=env)
'''

_FILES = {
    "pytest.ini": "[pytest]\npythonpath = src\ntestpaths = tests\n",
    ".gitignore": ".aiworkhub/\n__pycache__/\n",
    "src/app/__init__.py": "",
    "src/app/core.py": "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n",
    "src/app/other.py": "from app import core\n\n\ndef gamma():\n    return 3\n",
    "src/app/reader.py": (
        "from pathlib import Path\n\n_TABLE = Path(__file__).with_name('table.txt')\n\n\n"
        "def load():\n    return _TABLE.read_text(encoding='utf-8').strip()\n"
    ),
    "src/app/table.txt": "x\n",
    "src/app/cli.py": "def main():\n    return 0\n\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n",
    "tests/conftest.py": (
        "import pytest\n\nfrom app import other\n\n\n"
        "@pytest.fixture(scope='session')\ndef warm():\n    return other.gamma()\n"
    ),
    "tests/test_alpha.py": "from app import core\n\n\ndef test_alpha(warm):\n    assert core.alpha() == 1\n",
    "tests/test_beta.py": "from app import core\n\n\ndef test_beta(warm):\n    assert core.beta() == 2\n",
    "tests/test_reader.py": "from app import reader\n\n\ndef test_reader():\n    assert reader.load() == 'x'\n",
    "tests/test_plain.py": "def test_plain():\n    assert True\n",
    "tests/test_child.py": _CHILD_TEST,
}

ALPHA = "tests/test_alpha.py::test_alpha"
BETA = "tests/test_beta.py::test_beta"
READER = "tests/test_reader.py::test_reader"
PLAIN = "tests/test_plain.py::test_plain"
CHILD = "tests/test_child.py::test_child"
_MAP = Path(*tis.MAP_DIRECTORY_PARTS, tis.MAP_FILE_NAME)


def _git(repo: Path, *args: str) -> str:
    argv = [
        "git", "-c", "user.name=impact", "-c", "user.email=impact@example.invalid",
        "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args,
    ]
    return subprocess.run(argv, cwd=repo, check=True, capture_output=True, text=True).stdout


def _make_repo(repo: Path, files: dict[str, str] = _FILES) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    _git(repo, "init", "-q", "--template=")
    _git(repo, "add", "--", *sorted(files))
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def _child_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "COV_")) and key != "PYTHONPATH"
    }
    # Keep what the parent inherited: the Windows AppContainer lane delivers its
    # sitecustomize on PYTHONPATH, and without it pytest's mkdir(mode=0o700) is denied.
    inherited = os.environ.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(part for part in (str(_SRC), inherited) if part)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _record(repo: Path, workers: str, *extra: str, check: bool = True) -> bytes:
    argv = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "-p", "aiworkhub.test_impact_recorder", "-n", workers, *extra, "tests",
    ]
    done = subprocess.run(argv, cwd=repo, env=_child_env(), capture_output=True, text=True)
    assert not check or done.returncode == 0, done.stdout[-4000:] + done.stderr[-4000:]
    return (repo / _MAP).read_bytes()


def _files(repo: Path) -> set[str]:
    return {
        path.relative_to(repo).as_posix()
        for path in repo.rglob("*")
        if path.is_file() and path.relative_to(repo).parts[0] != ".git"
    }


def _edit(repo: Path, relative: str, old: str, new: str) -> None:
    target = repo / relative
    text = target.read_text(encoding="utf-8")
    assert old in text
    target.write_bytes(text.replace(old, new, 1).encode("utf-8"))


@pytest.fixture(scope="module")
def recording(tmp_path_factory: pytest.TempPathFactory) -> dict:
    holder = tmp_path_factory.mktemp("impact_record")
    repo = _make_repo(holder / "repo")
    before = _files(repo)
    payload = _record(repo, "0")
    return {
        "repo": repo,
        "map": payload,
        "new_files": sorted(_files(repo) - before),
        "holder_entries": sorted(path.name for path in holder.iterdir()),
    }


@pytest.fixture
def repo(recording: dict, tmp_path: Path) -> Path:
    """A fresh copy of the recorded tree, built byte-identically, with the recorded map."""
    path = _make_repo(tmp_path / "repo")
    (path / _MAP).parent.mkdir(parents=True)
    (path / _MAP).write_bytes(recording["map"])
    return path


def _selected(repo: Path, **kwargs) -> dict[str, tuple[str, ...]]:
    selection = tis.select_tests(repo, "HEAD", **kwargs)
    assert not selection.full_suite_required, selection.full_suite_reasons
    return selection.selected


def test_the_map_records_executed_code_reads_and_children_per_test(recording: dict) -> None:
    payload = json.loads(recording["map"])
    assert payload["schema_id"] == tis.SCHEMA_ID
    tests = payload["tests"]
    assert set(tests) == {ALPHA, BETA, READER, PLAIN, CHILD}
    objects = payload["objects"]

    def code(nodeid: str) -> set[str]:
        return {objects[index] for index in tests[nodeid]["code"]}

    assert "src/app/core.py::alpha" in code(ALPHA)
    assert "src/app/core.py::alpha" not in code(BETA)
    # The session fixture ran once, inside test_alpha, but is credited to both users.
    assert "src/app/other.py::gamma" in code(ALPHA) & code(BETA)
    assert not any(item.endswith("::<module>") for item in objects)
    assert tests[READER]["reads"] == ["src/app/table.txt"]
    assert all(row["reads"] == [] for nodeid, row in tests.items() if nodeid != READER)
    assert tests[CHILD]["children"] == ["src/app/__init__.py", "src/app/cli.py"]
    assert all(not row["incomplete"] for row in tests.values())


def test_recording_writes_only_inside_the_repository_runtime_directory(recording: dict) -> None:
    assert recording["holder_entries"] == ["repo"]
    assert _MAP.as_posix() in recording["new_files"]
    prefix = "/".join(tis.MAP_DIRECTORY_PARTS) + "/"
    assert all(path.startswith(prefix) for path in recording["new_files"]), recording["new_files"]


def test_recording_with_zero_and_four_workers_produces_identical_maps(
    recording: dict, tmp_path: Path
) -> None:
    pytest.importorskip("xdist")
    # xdist workers need a basetemp; the default under the system temp directory is
    # outside the sandbox, so it goes under this test's own tmp_path.
    basetemp = f"--basetemp={tmp_path / 'basetemp'}"
    distributed = _record(_make_repo(tmp_path / "repo"), "4", basetemp)
    assert distributed == recording["map"]


@pytest.mark.parametrize(
    ("path", "old", "new", "expected"),
    [
        ("src/app/core.py", "    return 1\n", "    return 1 + 0\n", {ALPHA}),
        ("src/app/core.py", "    return 2\n", "    value = 2\n    return value\n", {BETA}),
        ("src/app/other.py", "    return 3\n", "    return 3 + 0\n", {ALPHA, BETA}),
    ],
)
def test_a_function_body_only_change_selects_only_the_tests_that_executed_it(
    repo: Path, path: str, old: str, new: str, expected: set[str]
) -> None:
    _edit(repo, path, old, new)
    selected = _selected(repo)
    assert set(selected) == expected
    assert all(reason.startswith(f"executes:{path}::") for r in selected.values() for reason in r)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("def beta():", "def beta(unused=None):"),
        ("def alpha():", "LIMIT = 3\n\n\ndef alpha():"),
    ],
)
def test_a_def_line_or_module_level_change_selects_every_importer(
    repo: Path, old: str, new: str
) -> None:
    _edit(repo, "src/app/core.py", old, new)
    assert _selected(repo) == {
        ALPHA: ("imports:src/app/core.py",),
        BETA: ("imports:src/app/core.py",),
    }


def test_a_comment_only_change_selects_nothing(repo: Path) -> None:
    _edit(repo, "src/app/core.py", "def beta():", "# beta is the second value\ndef beta():")
    assert _selected(repo) == {}


def test_a_changed_data_file_selects_only_its_readers(repo: Path) -> None:
    _edit(repo, "src/app/table.txt", "x", "y")
    assert _selected(repo) == {READER: ("reads:src/app/table.txt",)}


def test_a_changed_or_new_test_file_is_selected(repo: Path) -> None:
    _edit(repo, "tests/test_plain.py", "assert True", "assert 1")
    (repo / "tests/test_new.py").write_bytes(b"def test_new():\n    assert True\n")
    selected = _selected(repo)
    assert set(selected) == {"tests/test_plain.py", "tests/test_new.py"}
    assert "changed_test" in selected["tests/test_plain.py"]
    assert selected["tests/test_new.py"] == ("new_test",)


def test_unmapped_and_stale_tests_are_selected(repo: Path) -> None:
    (repo / "tests/test_extra.py").write_bytes(b"def test_extra():\n    assert True\n")
    _edit(repo, "tests/test_plain.py", "assert True", "assert 1")
    _git(repo, "add", "--", "tests/test_extra.py", "tests/test_plain.py")
    _git(repo, "commit", "-q", "-m", "after the recording")
    assert _selected(repo) == {
        "tests/test_extra.py": ("unmapped",),
        "tests/test_plain.py": ("stale:tests/test_plain.py",),
    }


def test_a_subprocess_opaque_test_is_selected_when_its_child_closure_changed(repo: Path) -> None:
    _edit(repo, "src/app/cli.py", "    return 0\n", "    return 0 + 0\n")
    assert _selected(repo) == {CHILD: ("subprocess:src/app/cli.py",)}


@pytest.mark.parametrize("path", ["tests/conftest.py", "pyproject.toml"])
def test_a_global_invalidator_requires_the_full_suite_with_its_reason(
    repo: Path, path: str
) -> None:
    target = repo / path
    target.write_bytes((target.read_bytes() if target.exists() else b"") + b"# changed\n")
    selection = tis.select_tests(repo, "HEAD")
    assert selection.full_suite_required
    assert selection.full_suite_reasons == (f"global_invalidator:{path}",)
    assert selection.selected == {}


def test_a_missing_map_requires_the_full_suite(repo: Path) -> None:
    (repo / _MAP).unlink()
    selection = tis.select_tests(repo, "HEAD")
    assert selection.full_suite_required
    assert selection.full_suite_reasons == ("map_missing",)


def test_the_cli_prints_each_selected_test_with_its_reason(repo: Path) -> None:
    _edit(repo, "src/app/core.py", "    return 1\n", "    return 1 + 0\n")
    _edit(repo, "src/app/table.txt", "x", "y")
    argv = [sys.executable, "-m", "aiworkhub.test_impact_select", "--base", "HEAD"]

    def run(*extra: str) -> str:
        done = subprocess.run(
            [*argv, *extra], cwd=repo, env=_child_env(), capture_output=True, text=True
        )
        assert done.returncode == 0, done.stderr
        return done.stdout

    payload = json.loads(run("--json"))
    assert payload["full_suite_required"] is False
    assert payload["selected"] == [
        {"test": ALPHA, "reasons": ["executes:src/app/core.py::alpha"]},
        {"test": READER, "reasons": ["reads:src/app/table.txt"]},
    ]
    assert f"{ALPHA}\texecutes:src/app/core.py::alpha" in run()
    limited = json.loads(run("--paths", "src/app/table.txt", "--json"))
    assert [row["test"] for row in limited["selected"]] == [READER]


def test_qualnames_match_code_objects_for_methods_nested_functions_and_lambdas() -> None:
    source = (
        "class Box:\n"
        "    def size(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "def outer():\n"
        "    def inner():\n"
        "        return 2\n"
        "    return inner\n"
    ).encode()
    scopes = {qualname for _start, _end, qualname in tis.function_scopes(ast.parse(source))}
    namespace: dict = {}
    exec(compile(source, "<fixture>", "exec"), namespace)  # noqa: S102
    inner = namespace["outer"]()
    assert scopes == {
        namespace["Box"].size.__code__.co_qualname,
        namespace["outer"].__code__.co_qualname,
        inner.__code__.co_qualname,
    }
    assert recorder.function_qualname("outer.<locals>.<lambda>") == "outer"
    assert recorder.function_qualname("outer.<locals>.<genexpr>") == "outer"


def test_no_third_party_dependency_is_added() -> None:
    allowed = set(sys.stdlib_module_names) | {"aiworkhub"}
    # pytest is the existing dev dependency the plugin hooks into; the selector needs none.
    for name, extra in (("test_impact_select.py", set()), ("test_impact_recorder.py", {"pytest"})):
        tree = ast.parse((_SRC / "aiworkhub" / name).read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        assert roots <= allowed | extra, (name, roots - allowed - extra)


# -- rework regressions: every fix errs toward selecting more ---------------------------

_INI = {"pytest.ini": _FILES["pytest.ini"], ".gitignore": _FILES[".gitignore"], "src/app/__init__.py": ""}

_IMPORT_TREE = {
    **_INI,
    "src/app/seed.py": "def helper():\n    return 1\n",
    "src/app/early.py": "from app.seed import helper\n\nVALUE = helper()\n",
    "src/app/boot.py": "def booted():\n    return 5\n\n\nVALUE = booted()\n",
    "src/app/loaded.py": (
        "from pathlib import Path\n\n"
        "TEXT = Path(__file__).with_name('seed.txt').read_text(encoding='utf-8')\n"
    ),
    "src/app/seed.txt": "x\n",
    "src/app/gone.py": "VALUE = 1\n",
    "src/app/tear.py": "def after():\n    return 1\n\n\ndef late():\n    return 2\n",
    # conftest.py loads before pytest_configure, so boot is imported that early.
    "tests/conftest.py": "from app import boot  # noqa: F401\n",
    "tests/test_boot.py": "from app import boot\n\n\ndef test_boot():\n    assert boot.VALUE == 5\n",
    "tests/test_early.py": "from app import early\n\n\ndef test_early():\n    assert early.VALUE == 1\n",
    "tests/test_loaded.py": (
        "from app import loaded\n\n\ndef test_loaded():\n    assert loaded.TEXT.strip() == 'x'\n"
    ),
    "tests/test_gone.py": "from app import gone\n\n\ndef test_gone():\n    assert gone.VALUE == 1\n",
    "tests/test_tear.py": (
        "import pytest\n\nfrom app import tear\n\n\n"
        "@pytest.fixture\ndef scoped():\n    yield\n    tear.after()\n\n\n"
        "@pytest.fixture(scope='session')\ndef wide():\n    yield\n    tear.late()\n\n\n"
        "def test_tear(scoped, wide):\n    assert True\n"
    ),
}
BOOT = "tests/test_boot.py::test_boot"
EARLY = "tests/test_early.py::test_early"
LOADED = "tests/test_loaded.py::test_loaded"
GONE = "tests/test_gone.py::test_gone"
TEAR = "tests/test_tear.py::test_tear"


_SUBPROCESS_IMPORTS = "import os\nimport subprocess\nimport sys\n\n\n"
_MISC_TREE = {
    **_INI,
    "src/app/thing.py": "def work():\n    return 1\n",
    "tests/test_ok.py": "from app import thing\n\n\ndef test_ok():\n    assert thing.work() == 1\n",
    "tests/test_fails.py": "def test_fails():\n    raise RuntimeError('stop')\n",
    "tests/test_m.py": (
        _SUBPROCESS_IMPORTS + "def test_m():\n"
        "    subprocess.run([sys.executable, '-m', 'no_such_module_here'], capture_output=True)\n"
    ),
    "tests/test_script.py": (
        _SUBPROCESS_IMPORTS + "def test_script():\n"
        "    subprocess.run([sys.executable, os.__file__], capture_output=True)\n"
    ),
    "tests/test_version.py": (
        _SUBPROCESS_IMPORTS + "def test_version():\n"
        "    subprocess.run([sys.executable, '-V'], capture_output=True)\n"
    ),
}
OK = "tests/test_ok.py::test_ok"
FAILS = "tests/test_fails.py::test_fails"
BY_MODULE = "tests/test_m.py::test_m"
BY_SCRIPT = "tests/test_script.py::test_script"
VERSION = "tests/test_version.py::test_version"


def _install_map(path: Path, files: dict[str, str], payload: bytes) -> Path:
    repo = _make_repo(path, files)
    (repo / _MAP).parent.mkdir(parents=True)
    (repo / _MAP).write_bytes(payload)
    return repo


def _code(payload: dict, nodeid: str) -> set[str]:
    return {payload["objects"][index] for index in payload["tests"][nodeid]["code"]}


@pytest.fixture(scope="module")
def import_recording(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    return _record(_make_repo(tmp_path_factory.mktemp("impact_import") / "repo", _IMPORT_TREE), "0")


@pytest.fixture
def import_repo(import_recording: bytes, tmp_path: Path) -> Path:
    return _install_map(tmp_path / "repo", _IMPORT_TREE, import_recording)


@pytest.fixture(scope="module")
def misc_recording(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    repo = _make_repo(tmp_path_factory.mktemp("impact_misc") / "repo", _MISC_TREE)
    return _record(repo, "0", check=False)  # test_fails fails on purpose


@pytest.fixture
def misc_repo(misc_recording: bytes, tmp_path: Path) -> Path:
    return _install_map(tmp_path / "repo", _MISC_TREE, misc_recording)


def test_r1_a_body_edit_of_a_function_called_only_at_module_level_selects_the_importer(
    import_repo: Path,
) -> None:
    _edit(import_repo, "src/app/seed.py", "    return 1\n", "    return 1 + 0\n")
    assert _selected(import_repo) == {EARLY: ("imports:src/app/seed.py",)}


def test_r1_import_time_code_of_a_module_conftest_imported_is_recorded(import_repo: Path) -> None:
    _edit(import_repo, "src/app/boot.py", "    return 5\n", "    return 5 + 0\n")
    assert _selected(import_repo) == {BOOT: ("imports:src/app/boot.py",)}


def test_r1_a_stale_import_time_file_selects_its_importers(import_repo: Path) -> None:
    _edit(import_repo, "src/app/seed.py", "    return 1\n", "    return 1 + 0\n")
    _git(import_repo, "add", "--", "src/app/seed.py")
    _git(import_repo, "commit", "-q", "-m", "after the recording")
    assert _selected(import_repo) == {EARLY: ("stale:src/app/seed.py",)}


def test_r2_a_data_file_read_at_import_time_selects_the_importers(import_repo: Path) -> None:
    payload = json.loads((import_repo / _MAP).read_bytes())
    assert payload["import_reads"] == {"src/app/seed.txt": ["src/app/loaded.py"]}
    _edit(import_repo, "src/app/seed.txt", "x", "y")
    assert _selected(import_repo) == {LOADED: ("import_reads:src/app/seed.txt",)}


def test_r2_a_stale_import_time_data_file_selects_the_importers(import_repo: Path) -> None:
    _edit(import_repo, "src/app/seed.txt", "x", "y")
    _git(import_repo, "add", "--", "src/app/seed.txt")
    _git(import_repo, "commit", "-q", "-m", "after the recording")
    assert _selected(import_repo) == {LOADED: ("stale:src/app/seed.txt",)}


def test_import_time_recording_is_the_same_with_zero_and_four_workers(
    import_recording: bytes, tmp_path: Path
) -> None:
    pytest.importorskip("xdist")
    basetemp = f"--basetemp={tmp_path / 'basetemp'}"
    assert _record(_make_repo(tmp_path / "repo", _IMPORT_TREE), "4", basetemp) == import_recording


def test_r3_a_child_that_cannot_be_narrowed_is_the_whole_repository(misc_repo: Path) -> None:
    tests = json.loads((misc_repo / _MAP).read_bytes())["tests"]
    assert tests[BY_MODULE]["children"] == [tis.WHOLE_REPOSITORY]
    assert tests[BY_SCRIPT]["children"] == [tis.WHOLE_REPOSITORY]
    assert tests[VERSION]["children"] == []
    _edit(misc_repo, "src/app/thing.py", "    return 1\n", "    return 1 + 0\n")
    selected = _selected(misc_repo)
    assert selected[BY_MODULE] == ("subprocess:src/app/thing.py",)
    assert selected[BY_SCRIPT] == ("subprocess:src/app/thing.py",)
    assert VERSION not in selected


@pytest.mark.parametrize("order", ["'a', 'b'", "'b', 'a'"])
def test_r4_parametrized_fixture_rows_hold_only_their_own_parameter(
    tmp_path: Path, order: str
) -> None:
    files = {
        **_INI,
        "src/app/params.py": "def a():\n    return 1\n\n\ndef b():\n    return 2\n",
        "tests/test_p.py": (
            "import pytest\n\nfrom app import params\n\n\n"
            f"@pytest.fixture(scope='module', params=[{order}])\n"
            "def which(request):\n    return getattr(params, request.param)()\n\n\n"
            "def test_p(which):\n    assert which\n"
        ),
    }
    payload = json.loads(_record(_make_repo(tmp_path / "repo", files), "0"))
    for own, other in (("a", "b"), ("b", "a")):
        code = _code(payload, f"tests/test_p.py::test_p[{own}]")
        assert f"src/app/params.py::{own}" in code
        assert f"src/app/params.py::{other}" not in code


def test_r5_function_fixture_teardown_is_recorded_and_a_wider_finalizer_is_not(
    import_repo: Path,
) -> None:
    payload = json.loads((import_repo / _MAP).read_bytes())
    assert "src/app/tear.py::after" in _code(payload, TEAR)
    # The session finalizer ran after whichever test came last: attributed to none.
    assert "src/app/tear.py::late" not in payload["objects"]


def test_r6_a_test_stopped_by_an_exception_is_incomplete_and_always_selected(misc_repo: Path) -> None:
    tests = json.loads((misc_repo / _MAP).read_bytes())["tests"]
    assert tests[FAILS]["incomplete"] is True
    assert tests[OK]["incomplete"] is False
    assert _selected(misc_repo) == {FAILS: ("incomplete_record",)}


@pytest.mark.parametrize("name", ["test_impact_recorder.py", "test_impact_select.py"])
def test_r7_the_recorder_and_selector_require_the_full_suite(repo: Path, name: str) -> None:
    target = repo / "src" / "aiworkhub" / name
    target.parent.mkdir(parents=True)
    target.write_bytes(b"VALUE = 1\n")
    selection = tis.select_tests(repo, "HEAD")
    assert selection.full_suite_required
    assert selection.full_suite_reasons == (f"global_invalidator:src/aiworkhub/{name}",)


def test_s1_deleting_a_module_selects_the_test_that_only_imports_it(import_repo: Path) -> None:
    (import_repo / "src/app/gone.py").unlink()
    assert _selected(import_repo) == {GONE: ("imports:src/app/gone.py",)}


@pytest.mark.parametrize("commit", [False, True])
def test_s2_a_deleted_test_file_is_never_selected(import_repo: Path, commit: bool) -> None:
    (import_repo / "tests/test_gone.py").unlink()
    if commit:  # the recorded digest is then stale against --base
        _git(import_repo, "rm", "-q", "--cached", "--", "tests/test_gone.py")
        _git(import_repo, "commit", "-q", "-m", "after the recording")
    assert _selected(import_repo) == {}


def test_s3_read_blobs_treats_a_missing_path_with_a_space_as_absent(repo: Path) -> None:
    blobs = tis.read_blobs(repo, "HEAD", ["no such file.py", "src/app/core.py"])
    assert blobs["no such file.py"] is None
    assert blobs["src/app/core.py"] == _FILES["src/app/core.py"].encode()


# -- second rework: D1 to D5 -------------------------------------------------------------

_CORE = _FILES["src/app/core.py"]


@pytest.mark.parametrize(
    "argv",
    [
        [r"C:\Program Files\Python 3\python.exe", "-c", "print('a b')"],
        ["python", "-c", 'print("say hi")', 'embedded "double" quotes'],
        ["python", "trailing\\", "with space and trailing\\", 'mixed \\"x\\" end\\'],
        ["python", "", "plain"],
    ],
)
def test_d1_the_windows_splitter_inverts_list2cmdline(argv: list[str]) -> None:
    assert recorder.split_windows_command_line(subprocess.list2cmdline(argv)) == argv


def test_d1_a_string_command_line_records_the_child_import(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recorder, "_split_command_line", recorder.split_windows_command_line)
    payload = "from app import core; print(core.V)"
    line = subprocess.list2cmdline([sys.executable, "-c", payload])
    sink = recorder._Sink()
    recorder.ImpactRecorder(repo, None)._on_popen(sink, (None, line, str(repo), None))
    assert "src/app/core.py" in sink.children


def test_d2_a_stale_test_file_is_picked_whole_so_a_test_added_after_the_recording_runs(
    repo: Path,
) -> None:
    target = repo / "tests/test_plain.py"
    target.write_bytes(target.read_bytes() + b"\n\ndef test_two():\n    assert True\n")
    _git(repo, "add", "--", "tests/test_plain.py")
    _git(repo, "commit", "-q", "-m", "after the recording")
    selected = _selected(repo)
    assert selected == {"tests/test_plain.py": ("stale:tests/test_plain.py",)}
    assert PLAIN not in selected


def test_d3_a_wider_fixture_reached_through_getfixturevalue_is_credited(tmp_path: Path) -> None:
    nodeid = "tests/test_dyn.py::test_dyn"
    files = {
        **_INI,
        "src/app/core.py": _CORE,
        "tests/conftest.py": (
            "import pytest\n\nfrom app import core\n\n\n"
            "@pytest.fixture(scope='session')\ndef dyn():\n    return core.alpha()\n"
        ),
        "tests/test_dyn.py": "def test_dyn(request):\n    assert request.getfixturevalue('dyn') == 1\n",
    }
    payload = json.loads(_record(_make_repo(tmp_path / "repo", files), "0"))
    assert "src/app/core.py::alpha" in _code(payload, nodeid)
    assert payload["tests"][nodeid]["incomplete"] is False


_PARAM_DEP_TREE = {
    **_INI,
    "src/app/core.py": _CORE,
    "tests/test_x.py": (
        "import pytest\n\nfrom app import core\n\n\n"
        "@pytest.fixture(scope='session', params=['a', 'b'])\n"
        "def p(request):\n    return request.param\n\n\n"
        "@pytest.fixture(scope='session')\n"
        "def dep(p):\n    return core.alpha() if p == 'a' else core.beta()\n\n\n"
        "def test_x(dep):\n    assert dep\n"
    ),
}


@pytest.fixture(scope="module")
def param_dep_recording(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    return _record(_make_repo(tmp_path_factory.mktemp("impact_param") / "repo", _PARAM_DEP_TREE), "0")


def test_d4_a_wider_fixture_over_a_parametrized_one_is_credited_per_setup(
    param_dep_recording: bytes,
) -> None:
    payload = json.loads(param_dep_recording)
    first = _code(payload, "tests/test_x.py::test_x[a]")
    second = _code(payload, "tests/test_x.py::test_x[b]")
    assert "src/app/core.py::alpha" in first
    assert "src/app/core.py::beta" not in first
    assert "src/app/core.py::beta" in second
    assert "src/app/core.py::alpha" not in second


def test_d4_a_wider_fixture_over_a_parametrized_one_is_the_same_with_two_workers(
    param_dep_recording: bytes, tmp_path: Path
) -> None:
    pytest.importorskip("xdist")
    basetemp = f"--basetemp={tmp_path / 'basetemp'}"
    assert _record(_make_repo(tmp_path / "repo", _PARAM_DEP_TREE), "2", basetemp) == param_dep_recording


def test_d5_a_child_that_cannot_be_narrowed_is_whole_and_only_no_entry_point_is_empty(
    repo: Path,
) -> None:
    impact = recorder.ImpactRecorder(repo, None)
    dynamic = "import importlib; importlib.import_module('app.core')"
    assert impact.child_entries(["-"], str(repo)) == {tis.WHOLE_REPOSITORY}
    assert impact.child_entries(["-c", dynamic], str(repo)) == {tis.WHOLE_REPOSITORY}
    assert impact.child_entries(["-V"], str(repo)) == set()
    assert impact.child_entries([], str(repo)) == {tis.WHOLE_REPOSITORY}
    assert impact.child_entries(["-B"], str(repo)) == {tis.WHOLE_REPOSITORY}


_OVERRIDE_TREE = {
    **_INI,
    "src/app/core.py": _CORE,
    "tests/conftest.py": (
        "import pytest\n\nfrom app import core\n\n\n"
        "@pytest.fixture(scope='session')\ndef dep():\n    return core.alpha()\n"
    ),
    "tests/test_root.py": "def test_root(dep):\n    assert dep is not None\n",
    "tests/zsub/conftest.py": (
        "import pytest\n\nfrom app import core\n\n\n"
        "@pytest.fixture(scope='session')\ndef dep():\n    return core.beta()\n"
    ),
    "tests/zsub/test_sub.py": "def test_sub(dep):\n    assert dep is not None\n",
}


@pytest.fixture(scope="module")
def override_recording(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    return _record(_make_repo(tmp_path_factory.mktemp("impact_override") / "repo", _OVERRIDE_TREE), "0")


def test_f1_an_overridden_fixture_credits_only_the_definition_the_test_resolves(
    override_recording: bytes,
) -> None:
    payload = json.loads(override_recording)
    root = _code(payload, "tests/test_root.py::test_root")
    sub = _code(payload, "tests/zsub/test_sub.py::test_sub")
    assert "src/app/core.py::alpha" in root and "src/app/core.py::beta" not in root
    assert "src/app/core.py::beta" in sub and "src/app/core.py::alpha" not in sub


def test_f1_an_overridden_fixture_records_the_same_map_with_two_workers(
    override_recording: bytes, tmp_path: Path
) -> None:
    pytest.importorskip("xdist")
    basetemp = f"--basetemp={tmp_path / 'basetemp'}"
    assert _record(_make_repo(tmp_path / "repo", _OVERRIDE_TREE), "2", basetemp) == override_recording


@pytest.mark.parametrize("setting", ["diff.mnemonicPrefix", "diff.noprefix"])
def test_f2_a_diff_prefix_setting_does_not_empty_the_selection(tmp_path: Path, setting: str) -> None:
    checkout = _make_repo(tmp_path / "repo")
    _git(checkout, "config", setting, "true")
    (checkout / "src/app/core.py").write_bytes(b"def alpha():\n    return 9\n")
    assert [change.path for change in tis.collect_changes(checkout, "HEAD")] == ["src/app/core.py"]


_LAZY_TREE = {
    **_INI,
    "src/app/core.py": _CORE,
    "src/app/lazy.py": "from app import core\n\nVALUE = core.alpha()\n",
    "tests/test_lazy.py": (
        "from app import core\n\n\n"
        "def test_lazy():\n    core.alpha()\n    from app import lazy\n    assert lazy.VALUE == core.alpha()\n"
    ),
}


def test_f5_a_call_already_seen_in_the_window_is_still_recorded_at_import_time(tmp_path: Path) -> None:
    payload = json.loads(_record(_make_repo(tmp_path / "repo", _LAZY_TREE), "0"))
    assert "src/app/core.py::alpha" in payload["import_time"]
