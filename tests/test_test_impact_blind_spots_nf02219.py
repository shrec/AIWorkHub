"""NF-2026-02219: the five test-impact blind spots, each closed toward selecting more.

1. a child started through a shell or a console script was invisible to ``_on_popen``;
2. ``importlib.import_module``, ``runpy`` and ``mock.patch`` string targets had no edge;
3. ``merge_shards`` replaced the map even from a partial session;
4. a conftest's import-time dependency selected none of the tests under it;
5. multiprocessing and ``os.spawn*`` children raise no ``subprocess.Popen`` event.

One fixture repository is recorded once by a real ``pytest -p
aiworkhub.test_impact_recorder`` child run; each test installs that map and changes one
thing. A selection must never be narrower than before.
"""

from __future__ import annotations

import json
import multiprocessing.process
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_test_impact_nf02054 import (
    _INI,
    _MAP,
    _child_env,
    _edit,
    _install_map,
    _make_repo,
    _record,
    _selected,
    recorder,
    tis,
)

_WHOLE = tis.WHOLE_REPOSITORY
_LAUNCHES = "import multiprocessing\nimport os\nimport subprocess\nimport sys\n\n\n"
_TREE = {
    **_INI,
    "src/app/core.py": "def alpha():\n    return 1\n",
    "src/app/plug.py": "VALUE = 1\n",
    "src/app/conf_dep.py": "MARK = 1\n",
    "src/app/notes.txt": "x\n",
    "tests/test_plain.py": "from app import core\n\n\ndef test_plain():\n    assert core.alpha() == 1\n",
    "tests/test_shell.py": (
        _LAUNCHES + "def test_shell():\n    try:\n"
        "        subprocess.run('echo impact', shell=True, capture_output=True)\n"
        "    except OSError:\n        pass\n"
    ),
    "tests/test_mp.py": (
        _LAUNCHES + "def test_mp():\n"
        "    process = multiprocessing.get_context('spawn').Process()\n"
        "    try:\n        process.start()\n    except Exception:\n        return\n"
        "    process.join()\n"
    ),
    "tests/test_spawn.py": (
        _LAUNCHES + "def test_spawn():\n    try:\n"
        "        os.spawnv(os.P_WAIT, sys.executable, ['python', '-c', 'pass'])\n"
        "    except OSError:\n        pass\n"
    ),
    "tests/test_system.py": _LAUNCHES + "def test_system():\n    os.system('echo impact')\n",
    "tests/test_dyn.py": (
        "import importlib\n\n\ndef test_dyn():\n"
        "    assert importlib.import_module('app.plug').VALUE == 1\n"
    ),
    "tests/test_runpy.py": (
        "import runpy\n\n\ndef test_runpy():\n    assert runpy.run_module('app.plug')['VALUE'] == 1\n"
    ),
    "tests/test_patch.py": (
        "from unittest import mock\n\n\ndef test_patch():\n"
        "    with mock.patch('app.plug.VALUE', 2):\n        pass\n"
    ),
    # A string target outside the repository is not a repository import: no change here.
    "tests/test_stdpatch.py": (
        "from unittest import mock\n\nfrom app import core\n\n\ndef test_stdpatch():\n"
        "    with mock.patch('os.getcwd', return_value='x'):\n        assert core.alpha() == 1\n"
    ),
    "tests/sub/conftest.py": "from app import conf_dep  # noqa: F401\n",
    "tests/sub/test_under.py": "def test_under():\n    assert True\n",
}
PLAIN = "tests/test_plain.py::test_plain"
SHELL = "tests/test_shell.py::test_shell"
MP = "tests/test_mp.py::test_mp"
SPAWN = "tests/test_spawn.py::test_spawn"
SYSTEM = "tests/test_system.py::test_system"
UNOBSERVABLE = (SHELL, MP, SPAWN, SYSTEM)
DYNAMIC = ("tests/test_dyn.py", "tests/test_patch.py", "tests/test_runpy.py")


@pytest.fixture(scope="module")
def blind_recording(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    return _record(_make_repo(tmp_path_factory.mktemp("impact_blind") / "repo", _TREE), "0")


@pytest.fixture
def blind_repo(blind_recording: bytes, tmp_path: Path) -> Path:
    return _install_map(tmp_path / "repo", _TREE, blind_recording)


def _pytest(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "-p", "aiworkhub.test_impact_recorder", *args,
    ]
    return subprocess.run(argv, cwd=repo, env=_child_env(), capture_output=True, text=True)


# -- 1 and 5: a child this process cannot narrow is the whole repository ------------------


def test_bs1_bs5_unobservable_children_are_recorded_as_the_whole_repository(
    blind_recording: bytes,
) -> None:
    tests = json.loads(blind_recording)["tests"]
    for nodeid in UNOBSERVABLE:
        assert tests[nodeid]["children"] == [_WHOLE], nodeid
        assert tests[nodeid]["incomplete"] is False, nodeid
    assert tests[PLAIN]["children"] == []


def test_bs1_bs5_an_unobservable_child_is_selected_for_any_change(blind_repo: Path) -> None:
    _edit(blind_repo, "src/app/notes.txt", "x", "y")  # a data file nothing reads
    assert _selected(blind_repo) == {
        **{nodeid: ("subprocess:src/app/notes.txt",) for nodeid in UNOBSERVABLE},
        **{path: (f"dynamic_import:{path}",) for path in DYNAMIC},
    }


def test_bs1_a_shell_wrapper_or_console_script_child_is_the_whole_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    impact = recorder.ImpactRecorder(tmp_path, None)
    script = tmp_path / "console_tool"
    script.write_bytes(b"#!/usr/bin/env python3\nfrom app.cli import main\nmain()\n")
    binary = tmp_path / "native_tool"
    binary.write_bytes(b"\x7fELF\x02\x01\x01\x00")

    def children(executable: str | None, argv: object) -> set[str]:
        sink = recorder._Sink()
        impact._on_popen(sink, (executable, argv, str(tmp_path), None))
        return sink.children

    assert children("/bin/sh", ["/bin/sh", "-c", "python -m app.cli"]) == {_WHOLE}  # POSIX shell=True
    assert children(None, ["env", "python3", "-m", "app.cli"]) == {_WHOLE}
    assert children(str(script), [str(script), "--flag"]) == {_WHOLE}
    assert children(None, ["deploy.cmd", "run"]) == {_WHOLE}
    # Narrowness is kept for a native program and for one that does not exist.
    assert children(str(binary), [str(binary)]) == set()
    assert children(None, ["aiworkhub-no-such-program-nf02219"]) == set()
    monkeypatch.setattr(recorder, "_split_command_line", recorder.split_windows_command_line)
    windows_shell = r'C:\Windows\system32\cmd.exe /c "python -m app.cli"'  # Windows shell=True
    assert children(None, windows_shell) == {_WHOLE}
    monkeypatch.setattr(recorder, "_LAUNCHER_DIRECTORY_CHECK", True)
    launcher = os.path.join(os.path.dirname(sys.executable), "impact-tool.exe")  # a Windows console script
    assert children(launcher, [launcher]) == {_WHOLE}


def test_bs1_a_native_wrapper_of_a_console_script_is_the_whole_repository(tmp_path: Path) -> None:
    impact = recorder.ImpactRecorder(tmp_path, None)
    script = tmp_path / "console_tool"
    script.write_bytes(b"#!/usr/bin/env python3\nfrom app.cli import main\nmain()\n")
    for name in ("timeout", "uv", "native_tool"):
        (tmp_path / name).write_bytes(b"\x7fELF\x02\x01\x01\x00")
    timeout, uv, binary = (str(tmp_path / name) for name in ("timeout", "uv", "native_tool"))

    def children(argv: list[str]) -> set[str]:
        sink = recorder._Sink()
        impact._on_popen(sink, (None, argv, str(tmp_path), None))
        return sink.children

    assert children([timeout, "10", str(script), "--flag"]) == {_WHOLE}
    assert children([binary, "--quiet", str(script)]) == {_WHOLE}  # any native program
    # A wrapper whose program cannot be resolved here (``uv run`` finds its own) is unseen.
    assert children([uv, "run", "aiworkhub-no-such-tool-nf02219"]) == {_WHOLE}
    # A wrapper of a resolvable native program, and a native program's data words, stay narrow.
    assert children([timeout, "10", binary]) == set()
    assert children([binary, "status", "--short"]) == set()


@pytest.mark.parametrize(
    ("event", "args"),
    [
        ("os.system", (b"echo impact",)),
        ("os.fork", ()),
        ("os.forkpty", ()),
        ("os.spawn", (0, "python", ["python"], None)),
        ("os.posix_spawn", ("python", ["python"], {})),
        ("_winapi.CreateProcess", ("python", "python -c pass", None)),
    ],
)
def test_bs5_a_child_without_a_popen_event_is_the_whole_repository(
    tmp_path: Path, event: str, args: tuple
) -> None:
    impact = recorder.ImpactRecorder(tmp_path, None)
    sink = recorder._Sink()
    impact._windows.append(sink)  # the window of a running test
    impact._audit(event, args)
    assert sink.children == {_WHOLE}


def test_bs5_multiprocessing_start_is_watched_for_every_start_method() -> None:
    assert recorder._is_unaudited_launcher(multiprocessing.process.BaseProcess.start.__code__)
    assert not recorder._is_unaudited_launcher(test_bs5_multiprocessing_start_is_watched_for_every_start_method.__code__)


# -- 2: a dynamic importer is an edge and runs for any change ---------------------------


def test_bs2_scan_imports_names_dynamic_targets_and_flags_unknown_ones() -> None:
    exists = {"src/app/__init__.py", "src/app/plug.py"}.__contains__
    plug = {"src/app/__init__.py", "src/app/plug.py"}

    def scan(text: str) -> tis.ImportScan:
        found = tis.scan_imports(text, exists, origin="tests/test_x.py")
        assert found is not None
        return found

    assert scan("import importlib\nimportlib.import_module('app.plug')\n").dynamic == plug
    assert scan("__import__('app.plug')\n").dynamic == plug
    assert scan("import runpy\nrunpy.run_module('app.plug')\n").dynamic == plug
    assert scan("from unittest import mock\n@mock.patch('app.plug.VALUE', 2)\ndef f():\n    pass\n").dynamic == plug
    assert scan("from unittest.mock import patch\npatch.dict('app.plug.TABLE', {})\n").dynamic == plug
    assert scan("def t(monkeypatch):\n    monkeypatch.setattr('app.plug.VALUE', 2)\n").dynamic == plug
    assert scan("import runpy\nrunpy.run_path('tool.py')\n").opaque
    assert scan("import importlib\nimportlib.import_module(NAME)\n").opaque
    assert scan("from unittest import mock\nmock.patch(f'{BASE}.x')\n").opaque
    plain = scan(
        "from unittest import mock\nmock.patch('os.getcwd')\nmock.patch.object(obj, 'x')\n"
        "client.patch(url)\nmonkeypatch.setattr(obj, 'x', 1)\npatch.dict(os.environ, {})\n"
    )
    assert plain.dynamic == frozenset() and not plain.opaque
    graph = tis.ImportGraph(
        {
            "tests/test_x.py": b"from unittest import mock\nmock.patch('app.plug.VALUE', 2)\n",
            "src/app/__init__.py": b"",
            "src/app/plug.py": b"VALUE = 1\n",
        }
    )
    assert "tests/test_x.py" in graph.importers(["src/app/plug.py"])
    assert graph.dynamic == {"tests/test_x.py"} and graph.opaque == set()


def test_bs2_pytest_mock_and_any_patch_or_setattr_receiver_take_a_string_target() -> None:
    exists = {"src/app/__init__.py", "src/app/plug.py"}.__contains__
    plug = {"src/app/__init__.py", "src/app/plug.py"}

    def scan(text: str) -> tis.ImportScan:
        found = tis.scan_imports(text, exists, origin="tests/test_x.py")
        assert found is not None
        return found

    assert scan("def t(mocker):\n    mocker.patch('app.plug.VALUE', 2)\n").dynamic == plug
    assert scan("def t(mocker):\n    mocker.patch.dict('app.plug.TABLE', {})\n").dynamic == plug
    assert scan("def t(mocker):\n    mocker.patch.object('app.plug.Thing', 'x')\n").dynamic == plug
    assert scan("def t(self):\n    self.mp.setattr('app.plug.VALUE', 2)\n").dynamic == plug
    assert scan("def t(fx):\n    fx.helper.patch('app.plug.VALUE')\n").dynamic == plug
    # A target string this selector cannot name marks the test opaque.
    assert scan("def t(mocker):\n    mocker.patch(TARGET)\n").opaque
    assert scan("def t(fx):\n    fx.patch(f'{BASE}.VALUE')\n").opaque
    assert scan("def t(mp):\n    mp.setattr(BASE + '.VALUE', 2)\n").opaque
    # An HTTP verb and an object-form setattr are no import.
    plain = scan(
        "def t(client, mocker, mp):\n    client.patch('/api/items')\n    client.patch(f'/api/{ident}')\n"
        "    mocker.patch.object(obj, 'x')\n    mp.setattr(obj, 'x', 1)\n"
    )
    assert plain.dynamic == frozenset() and not plain.opaque


def test_bs2_a_dynamic_importer_test_is_selected_for_any_change(blind_repo: Path) -> None:
    _edit(blind_repo, "src/app/core.py", "    return 1\n", "    return 1 + 0\n")
    executes = ("executes:src/app/core.py::alpha",)
    assert _selected(blind_repo) == {
        PLAIN: executes,
        "tests/test_stdpatch.py::test_stdpatch": executes,  # os.getcwd is no repository edge
        **{nodeid: ("subprocess:src/app/core.py",) for nodeid in UNOBSERVABLE},
        **{path: (f"dynamic_import:{path}",) for path in DYNAMIC},
    }


def test_bs2_a_child_whose_closure_imports_an_unknown_target_is_the_whole_repository(
    tmp_path: Path,
) -> None:
    impact = recorder.ImpactRecorder(_make_repo(tmp_path / "repo", _TREE), None)
    payload = "from app import core; import importlib; importlib.import_module('app.plug')"
    assert impact.child_entries(["-c", payload], str(tmp_path)) == {_WHOLE}


# -- 3: the map is written only from a complete session ---------------------------------


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (("-k", "plain", "tests"), "deselected:"),
        (("tests/test_plain.py",), "explicit_paths"),
        (("--ignore=tests/test_dyn.py", "tests"), "option:--ignore"),
        (("--setup-only",), "setup_only"),  # fixtures ran, test bodies did not
        (("--setup-plan",), "setup_plan"),
    ],
)
def test_bs3_a_partial_session_leaves_the_map_unchanged_and_says_why(
    blind_recording: bytes, tmp_path: Path, args: tuple[str, ...], reason: str
) -> None:
    repo = _install_map(tmp_path / "repo", _TREE, blind_recording)
    done = _pytest(repo, *args)
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-4000:]
    assert (repo / _MAP).read_bytes() == blind_recording
    assert "test_impact: map not written (partial_session:" in done.stdout
    assert reason in done.stdout


def test_bs3_merge_shards_writes_the_map_only_from_a_complete_session(tmp_path: Path) -> None:
    map_path = tmp_path / _MAP
    map_path.parent.mkdir(parents=True)
    map_path.write_bytes(b"previous\n")
    nodeid = "tests/test_a.py::test_a"
    row = {"file": "tests/test_a.py", "code": [], "reads": [], "children": [], "incomplete": False}

    def shard(name: str, gaps: list[str]) -> None:
        payload = {
            "schema_id": recorder.SHARD_SCHEMA_ID,
            "tests": {nodeid: row} if name == "controller" else {},
            "gaps": gaps,
            "import_time": [],
            "import_reads": {},
        }
        target = recorder.shard_directory(tmp_path) / f"{name}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")

    shard("controller", [])
    assert recorder.merge_shards(tmp_path, ["deselected:3"]) == (None, "partial_session:deselected:3")
    crashed = recorder.merge_shards(tmp_path, [], [nodeid, "tests/test_b.py::test_b"])
    assert crashed == (None, "partial_session:unrecorded_tests:1")
    shard("gw1", ["collection_errors:1"])
    assert recorder.merge_shards(tmp_path, [], [nodeid]).reason == "partial_session:collection_errors:1"
    assert map_path.read_bytes() == b"previous\n"
    shard("gw1", [])
    assert recorder.merge_shards(tmp_path, [], [nodeid]) == (map_path, None)
    assert json.loads(map_path.read_bytes())["tests"] == {nodeid: row}


# -- 4: a conftest's import-time dependency selects every test under that conftest -------


def test_bs4_a_conftest_import_dependency_selects_every_test_under_its_directory(
    blind_repo: Path,
) -> None:
    _edit(blind_repo, "src/app/conf_dep.py", "MARK = 1", "MARK = 2")
    selected = _selected(blind_repo)
    via = "conftest:tests/sub/conftest.py:imports:src/app/conf_dep.py"
    assert selected["tests/sub/test_under.py"] == (via,)
    # Outside that conftest's directory nothing imports conf_dep.
    assert PLAIN not in selected and "tests/test_plain.py" not in selected
    assert "tests/test_stdpatch.py" not in selected


def test_bs4_a_conftest_importing_an_opaque_helper_marks_every_test_under_it(tmp_path: Path) -> None:
    inside, outside = "tests/opq/test_inside.py", "tests/test_outside.py"
    sources = {
        "tests/opq/conftest.py": b"import opq_helper  # noqa: F401\n",
        "tests/opq/opq_helper.py": b"import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n",
        inside: b"def test_inside():\n    pass\n",
        outside: b"def test_outside():\n    pass\n",
    }
    row = {"code": [], "reads": [], "children": [], "incomplete": False}
    payload = {
        "objects": [],
        "tests": {f"{path}::test_x": {"file": path, **row} for path in (inside, outside)},
        "digests": {},
        "import_time": [],
        "import_reads": {},
    }
    selector = tis._Selector(tmp_path, "HEAD", None, payload)
    selector.new_sources = sources
    graph = tis.ImportGraph(sources)
    assert graph.opaque == {"tests/opq/opq_helper.py"}
    selector._apply_dynamic(graph, {"src/app/core.py"})
    reason = "conftest:tests/opq/conftest.py:dynamic_import:tests/opq/opq_helper.py"
    assert selector.picked == {inside: {reason}}
