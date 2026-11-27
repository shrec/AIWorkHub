"""NF-2026-00976: the release cut narrows past an unmapped path and resumes a failed cut.

Measured at the 0.13.16 cut: one changed path the recorded map does not record
(.claude/hooks/aiworkhub_tool_guard.py) forced the whole suite on the host (23 min) and
in WSL (26 min), and the rerun after a one-test failure, fixed by a one-line change, ran
the whole suite again. An unmapped path now adds the test modules that name it
(``referencing_tests``: a path literal, a module import, a fixture module that loads it)
and runs the whole suite only when none does or the scan cannot decide. The rerun of a
version after a failed cut runs each suite's failed node ids plus the impact selection
since the commit its resume record names, when the record's inputs digest matches this
cut's, and the normal selection otherwise.

No test here runs pytest, npm, WSL or git: every command goes to a fake runner that
records it, the selector is injected (``Host.impact_selector``), no revision's blobs can
be read, and HEAD's commit id is a constant but where a test asks the fake runner for it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "aiworkhub_release_cut_nf00976", ROOT / "scripts" / "release_cut.py"
)
assert SPEC is not None and SPEC.loader is not None
release_cut = importlib.util.module_from_spec(SPEC)
# Its dataclasses resolve their string annotations through sys.modules.
sys.modules[SPEC.name] = release_cut
SPEC.loader.exec_module(release_cut)
Completed = release_cut.Completed
NEIGHBOURS, IMPACT = release_cut._impact_modules()
# What the autouse fixture replaces with SINCE, for the one test that runs it.
REAL_HEAD_COMMIT = release_cut.ReleaseCut._head_commit

CURRENT = "0.12.99"
VERSION = "0.13.0"
SUMMARY_HEADER = "=========================== short test summary info ============================"
LINUX_ROOT = "/mnt/z/repo"
IMPACT_BASE = "v0.12.99"
# What the fake commit step reads back as the release commit.
RELEASE_SHA = "0123456789abcdef0123456789abcdef01234567"
# HEAD when a cut that failed before its commit step recorded its host run.
SINCE = "89abcdef0123456789abcdef0123456789abcdef"
IMPACT_MODULE = "src/aiworkhub/alpha.py"
IMPACT_TEST = "tests/test_alpha.py"
IMPACT_NODE = f"{IMPACT_TEST}::test_alpha"
INVARIANT_TESTS: tuple[str, ...] = tuple(NEIGHBOURS.INVARIANT_TESTS)
# The path that forced the 0.13.16 cut's whole suite, and a test that runs it by path.
HOOK = ".claude/hooks/aiworkhub_tool_guard.py"
HOOK_TEST = "tests/test_tool_guard_hook.py"
HOST_NODE = "tests/test_a.py::test_x"
HOST_FAILURE = Completed(
    1, f"..F.\n{SUMMARY_HEADER}\nFAILED {HOST_NODE} - assert 0\n1 failed, 11 passed in 1.00s\n"
)
FAILS_ALONE_TOO = Completed(1, f"{SUMMARY_HEADER}\nFAILED {HOST_NODE} - assert 0\n1 failed\n")
WSL_NODE = "tests/test_posix_modes.py::test_mode[0o600 - owner]"
WSL_FAILURE = Completed(
    1, f"....F\n{SUMMARY_HEADER}\nFAILED {WSL_NODE} - AssertionError: 0o644\n1 failed in 1.00s\n"
)
# Why the normal selection of every resume test below is the whole suite.
FULL_REASON = "global_invalidator:conftest.py"
NORMAL = f"selector_full_suite:{FULL_REASON}"
RECORD = f".scratch/release/{VERSION}/{release_cut.RESUME_RECORD}"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _release_files(root: Path) -> None:
    """The release files of a cut from CURRENT to VERSION, which a rerun restores first."""

    def dumped(value: Any) -> str:
        return json.dumps(value, indent=2) + "\n"

    dash = "—"
    package = {"registryType": "pypi", "identifier": "aiworkhub", "version": CURRENT}
    files = {
        "src/aiworkhub/_version.py": f'__version__ = "{CURRENT}"\n',
        "vscode-extension/package.json": dumped({"name": "aiworkhub", "version": CURRENT}),
        "vscode-extension/package-lock.json": dumped(
            {
                "name": "aiworkhub",
                "version": CURRENT,
                "packages": {"": {"name": "aiworkhub", "version": CURRENT}},
            }
        ),
        "vscode-extension/extension.js": f'const EXPECTED_MCP_PACKAGE_VERSION = "{CURRENT}";\n',
        "server.json": dumped(
            {"name": "io.github.shrec/aiworkhub", "version": CURRENT, "packages": [package]}
        ),
        "CHANGELOG.md": (
            f"# Changelog\n\n## [{VERSION}] - 2026-11-07\n\n- One release cut script.\n\n"
            f"## [{CURRENT}] - 2026-11-06\n\n- Older.\n"
        ),
        "vscode-extension/CHANGELOG.md": (
            f"# Changelog\n\n## {VERSION} {dash} 2026-11-07\n\n- One release cut script.\n"
        ),
        "README.md": f"# AIWorkHub\n\n## What's new in {VERSION} {dash} 2026-11-07\n\n- Cut.\n",
        "vscode-extension/README.md": (
            f"# AIWorkHub\n\n## What's new in {VERSION} {dash} 2026-11-07\n\n- Cut.\n"
        ),
    }
    for relative, text in files.items():
        _write(root / relative, text)


def _repo(root: Path) -> Path:
    """A cut repository whose map records IMPACT_MODULE as it is on disk now.

    Every test file a selection below names exists, and none of them names a path under
    test.
    """

    _release_files(root)
    _write(root / IMPACT_MODULE, "def alpha():\n    return 1\n")
    for test in (*INVARIANT_TESTS, IMPACT_TEST, "tests/test_a.py", "tests/test_posix_modes.py"):
        _write(root / test, "def test_alpha():\n    pass\n")
    digests = {IMPACT_MODULE: IMPACT.content_digest((root / IMPACT_MODULE).read_bytes())}
    payload = {"schema_id": IMPACT.SCHEMA_ID, "digests": digests}
    _write(IMPACT.map_directory(root) / IMPACT.MAP_FILE_NAME, json.dumps(payload))
    return root


def _guarded(argv: Sequence[str]) -> tuple[str, list[str]]:
    """The directory and the command of one ``sh -c 'cd -- DIR || exit 97; exec CMD'`` call."""

    assert len(argv) == 5 and list(argv[:4]) == ["wsl", "-e", "sh", "-c"], argv
    guard, separator, command = argv[4].partition(" || exit 97; exec ")
    assert separator and guard.startswith("cd -- "), argv[4]
    (directory,) = shlex.split(guard[len("cd -- ") :])
    return directory, shlex.split(command)


def label(argv: Sequence[str]) -> str:
    """A short stable name for one command."""

    argv = list(argv)
    if argv[0] == "git":
        if argv[1] == "rev-parse" and argv[-1] == "HEAD":
            return "git rev-parse HEAD"
        return f"git {argv[1]}"
    if argv[0] == "wsl":
        inner = _guarded(argv)[1] if argv[1:3] == ["-e", "sh"] else argv[argv.index("-e") + 1 :]
        return "wsl pytest" if "pytest" in inner else f"wsl {inner[0]}"
    if argv[0] == "npm":
        return "npm package"
    if argv[1:3] == ["-m", "pytest"]:
        return "pytest"
    if len(argv) > 2 and argv[1] == "scripts/release_receipt.py":
        return f"receipt {argv[2]}"
    raise AssertionError(f"the cut ran an unexpected command: {argv}")


class FakeRunner:
    """Records every command; answers from ``responses`` by label, else as a passing cut.

    A response may be a callable of the argv: what it returns answers the command.
    """

    def __init__(self, root: Path, responses: dict[str, Any] | None = None) -> None:
        self.root = root
        self.responses = dict(responses or {})
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Any = None,
        timeout: float | None = None,
    ) -> Any:
        call = tuple(argv)
        self.calls.append(call)
        name = label(call)
        response = self.responses.get(name)
        if callable(response):
            response = response(call)
        return response if response is not None else self._passing(name, call)

    def _passing(self, name: str, call: tuple[str, ...]) -> Any:
        if name == "git symbolic-ref":
            return Completed(0, "main\n")
        if name == "git rev-parse":
            return Completed(1)  # --verify of an absent tag
        if name == "git rev-parse HEAD":
            return Completed(0, f"{RELEASE_SHA}\n")
        if name == "git describe":
            return Completed(0, f"{IMPACT_BASE}\n")
        if name == "wsl wslpath":
            windows = Path(call[-1])
            return Completed(0, f"{LINUX_ROOT}/{windows.relative_to(self.root).as_posix()}\n")
        if name == "git archive":
            output = next(token for token in call if token.startswith("--output="))
            Path(output.split("=", 1)[1]).write_bytes(b"tar")
            return Completed(0)
        if name == "npm package":
            _write(self.root / "vscode-extension" / "dist" / f"aiworkhub-{VERSION}.vsix", "vsix")
            return Completed(0)
        if name in ("pytest", "wsl pytest"):
            return Completed(0, "........\n12 passed in 1.00s\n")
        return Completed(0)

    def argv_of(self, name: str) -> list[tuple[str, ...]]:
        return [call for call in self.calls if label(call) == name]


class Selector:
    """Stands in for test_impact_select.select_tests(root, base): one answer per base."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[Path, str]] = []

    def __call__(self, root: Path, base: str) -> Any:
        self.calls.append((root, base))
        return self.answers[base]


def _selection(*, full: str = "", changed: tuple[str, ...] = (IMPACT_MODULE,)) -> Any:
    """A selector answer: the whole suite for ``full``, else IMPACT_NODE for ``changed``."""

    if full:
        return IMPACT.Selection(True, (full,), {}, changed)
    picked = {IMPACT_NODE: (f"executes:{IMPACT_MODULE}::alpha",)}
    return IMPACT.Selection(False, (), picked, changed)


def _full_then_alone(full: Any, alone: Any) -> Callable[[tuple[str, ...]], Any]:
    """A suite response: ``full`` for the xdist run, ``alone`` for the serial rerun of nodes."""

    def respond(call: tuple[str, ...]) -> Any:
        command = _guarded(call)[1] if call[0] == "wsl" else list(call)
        return alone if command[command.index("-n") + 1] == "0" else full

    return respond


def _cut(
    root: Path, *args: str, runner: FakeRunner, selector: Selector, python: str = "python"
) -> tuple[int, dict[str, Any]]:
    lines: list[str] = []
    host = release_cut.Host(
        root=root,
        runner=runner,
        python=python,
        cpu_count=16,
        env={"PATH": "fake-bin"},
        is_windows=True,
        out=lines.append,
        impact_selector=selector,
    )
    code = release_cut.main([VERSION, *args], host=host)
    return code, json.loads(lines[-1])


def _after_basetemp(argv: Sequence[str]) -> list[str]:
    """What one suite run ran: the tokens after its basetemp."""

    index = next(i for i, token in enumerate(argv) if token.startswith("--basetemp="))
    return list(argv[index + 1 :])


def _targets(runner: FakeRunner) -> tuple[list[str], list[str]]:
    """What the host suite and the WSL suite ran first."""

    host = runner.argv_of("pytest")[0]
    wsl = _guarded(runner.argv_of("wsl pytest")[0])[1]
    return _after_basetemp(host), _after_basetemp(wsl)


def _in_turn(*answers: Any) -> Callable[[tuple[str, ...]], Any]:
    """A response answering each call with the next of ``answers``, then as a passing cut."""

    pending = list(answers)

    def respond(_call: tuple[str, ...]) -> Any:
        return pending.pop(0) if pending else None

    return respond


def _entries(root: Path) -> dict[str, Any]:
    return json.loads((root / RECORD).read_text(encoding="utf-8"))["steps"]


def _failed_host_cut(root: Path) -> None:
    """Cut VERSION with a host node that fails alone too, then restore what the bump wrote.

    Its normal selection is the whole suite, as the rerun's is, so only a resume narrows.
    """

    runner = FakeRunner(root, {"pytest": _full_then_alone(HOST_FAILURE, FAILS_ALONE_TOO)})
    selector = Selector({IMPACT_BASE: _selection(full=FULL_REASON)})
    code, summary = _cut(root, runner=runner, selector=selector)
    assert code == 1 and summary["failed_step"] == "host_tests", summary["reason"]
    _release_files(root)  # the recovery line's checkout of what the bump rewrote


@pytest.fixture(autouse=True)
def _no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """No revision's blobs can be read and HEAD is SINCE, so no test reaches a real git.

    test_the_real_head_commit_asks_the_host_runner_for_head puts REAL_HEAD_COMMIT back.
    """

    def unreadable(_root: Path, rev: str, _paths: Sequence[str]) -> dict[str, bytes | None]:
        raise IMPACT.SelectionError(f"git_failed:cat-file:128:{rev}")

    monkeypatch.setattr(IMPACT, "read_blobs", unreadable)
    monkeypatch.setattr(release_cut.ReleaseCut, "_head_commit", lambda _self: SINCE)


# -- an unmapped path narrows by the tests that name it ----------------------------------


def test_an_unmapped_path_a_test_file_names_selects_that_file_not_the_whole_suite(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    _write(root / HOOK, "import sys\n")
    _write(
        root / HOOK_TEST,
        'HOOK = ROOT / ".claude" / "hooks" / "aiworkhub_tool_guard.py"\n\n\n'
        "def test_hook():\n    pass\n",
    )
    _write(root / "tests" / "test_unrelated.py", "def test_unrelated():\n    pass\n")
    runner = FakeRunner(root)
    selector = Selector({IMPACT_BASE: _selection(changed=(IMPACT_MODULE, HOOK))})

    code, summary = _cut(root, runner=runner, selector=selector)

    assert code == 0, summary["reason"]
    host, wsl = _targets(runner)
    # The map's own selection plus the one test that names the hook, on both suites.
    assert host == wsl == sorted([IMPACT_NODE, HOOK_TEST, *INVARIANT_TESTS])
    assert "tests" not in host and "tests/test_unrelated.py" not in host
    # A narrowed run never loads the recorder, which only a whole-suite run feeds.
    assert release_cut.TEST_IMPACT_RECORDER_PLUGIN not in runner.argv_of("pytest")[0]
    assert summary["test_selection"] == {
        "lane": "impact_map",
        "base_tag": IMPACT_BASE,
        "selected_count": len(host),
        "fallback_reason": f"impact_map_unmapped_referenced:{HOOK}:1",
    }


@pytest.mark.parametrize("case", ["no_test_names_it", "scan_cannot_decide"])
def test_an_unmapped_path_no_test_names_or_an_undecided_scan_runs_the_whole_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    root = _repo(tmp_path)
    _write(root / HOOK, "import sys\n")
    # Near misses name no path: another hook's file, and the stem inside a longer word.
    _write(
        root / "tests" / "test_other_hooks.py",
        'OTHER = "aiworkhub_tool_guard_v2.py"\nNAME = "my_aiworkhub_tool_guard"\n',
    )
    reason = f"impact_map_unmapped:{HOOK}"
    if case == "scan_cannot_decide":
        # A test names the hook, but a file the scan cannot read whole might name it too.
        _write(root / HOOK_TEST, f'HOOK = "{HOOK}"\n')
        monkeypatch.setattr(release_cut, "REFERENCE_SCAN_MAX_FILE_BYTES", 100)
        _write(root / "tests" / "test_large.py", "#" * 200 + "\n")
        reason = f"impact_map_unmapped_undecided:{HOOK}:over_100_bytes:tests/test_large.py"
    runner = FakeRunner(root)
    selector = Selector({IMPACT_BASE: _selection(changed=(IMPACT_MODULE, HOOK))})

    code, summary = _cut(root, runner=runner, selector=selector)

    assert code == 0, summary["reason"]
    host, wsl = _targets(runner)
    assert host == wsl == ["tests"]
    assert summary["test_selection"] == {
        "lane": "full_suite",
        "base_tag": IMPACT_BASE,
        "selected_count": None,
        "fallback_reason": reason,
    }


def test_the_reference_scan_follows_path_literals_imports_fixture_modules_and_conftests(
    tmp_path: Path,
) -> None:
    beta = "src/aiworkhub/beta.py"
    sources = {
        # A path literal, split into its parts the way tests build it.
        "tests/test_literal.py": 'HOOK = ROOT / ".claude" / "hooks" / "aiworkhub_tool_guard.py"\n',
        # A module import names the module's stem.
        "tests/test_import.py": "from aiworkhub import beta\n",
        # A fixture module that loads the hook, and the test that imports that module.
        "tests/support/guard_fixture.py": 'GUARD = "aiworkhub_tool_guard"\n',
        "tests/test_fixture_user.py": "from support import guard_fixture\n",
        # A conftest fixture that reads the hook serves every test under its directory.
        "tests/hooks/conftest.py": (
            "def guard():\n    return open('.claude/hooks/aiworkhub_tool_guard.py').read()\n"
        ),
        "tests/hooks/test_under_conftest.py": "def test_x(guard):\n    pass\n",
        # Near misses: each stem inside a longer word names neither path.
        "tests/test_near_miss.py": 'A = "aiworkhub_tool_guard_v2.py"\nB = "alphabeta my_beta"\n',
    }
    for relative, text in sources.items():
        _write(tmp_path / relative, text)

    tests, reason = release_cut.referencing_tests(tmp_path, [HOOK, beta], IMPACT.is_test_module)

    assert reason == ""
    assert tests == {
        "tests/hooks/test_under_conftest.py": (f"tests/hooks/conftest.py:references:{HOOK}",),
        "tests/test_fixture_user.py": ("references:tests/support/guard_fixture.py",),
        "tests/test_import.py": (f"references:{beta}",),
        "tests/test_literal.py": (f"references:{HOOK}",),
    }
    # Deterministic: read on several threads, the scan answers the same.
    again = release_cut.referencing_tests(
        tmp_path, [HOOK, beta], IMPACT.is_test_module, workers=4
    )
    assert again == (tests, "")


def test_a_helper_package_naming_the_path_selects_every_test_importing_it(
    tmp_path: Path,
) -> None:
    sources = {
        # The package's __init__ names the hook, and a test elsewhere imports the package.
        "tests/helpers/__init__.py": f'GUARD = "{HOOK}"\n',
        "tests/test_x.py": "from helpers import GUARD\n",
        # A test module inside the package loads its __init__ first.
        "tests/helpers/test_inside.py": "def test_inside():\n    pass\n",
        "tests/test_unrelated.py": "def test_unrelated():\n    pass\n",
        # Data naming neither the hook nor the package.
        "tests/data/notes.txt": "Fixtures for the suite.\n",
    }
    for relative, text in sources.items():
        _write(tmp_path / relative, text)

    tests, reason = release_cut.referencing_tests(tmp_path, [HOOK], IMPACT.is_test_module)

    assert reason == ""
    assert tests == {
        "tests/helpers/test_inside.py": (f"tests/helpers/__init__.py:references:{HOOK}",),
        "tests/test_x.py": ("references:tests/helpers/__init__.py",),
    }


@pytest.mark.parametrize("conftest", ["conftest.py", "tests/conftest.py"])
def test_a_conftest_every_test_loads_naming_the_path_runs_the_whole_suite(
    tmp_path: Path, conftest: str
) -> None:
    # pytest loads it for every test, so a test module naming the hook narrows nothing.
    _write(tmp_path / conftest, f"GUARD = ROOT / {HOOK!r}\n")
    _write(tmp_path / HOOK_TEST, f'HOOK = "{HOOK}"\n')

    tests, reason = release_cut.referencing_tests(tmp_path, [HOOK], IMPACT.is_test_module)

    assert (tests, reason) == (
        {},
        f"impact_map_unmapped_undecided:{HOOK}:every_test_loads:{conftest}",
    )


def test_a_file_under_tests_that_is_not_python_naming_the_path_is_undecided(
    tmp_path: Path,
) -> None:
    # A test may read this manifest or run this script; no text says which one does.
    _write(tmp_path / "tests" / "data" / "hooks.json", json.dumps({"hooks": [HOOK]}))
    _write(tmp_path / HOOK_TEST, f'HOOK = "{HOOK}"\n')

    tests, reason = release_cut.referencing_tests(tmp_path, [HOOK], IMPACT.is_test_module)

    assert (tests, reason) == (
        {},
        f"impact_map_unmapped_undecided:{HOOK}:named_outside_python:tests/data/hooks.json",
    )


def _link_or_skip(link: Path, target: Path, kind: str) -> None:
    """``link`` made a directory symlink or junction to ``target``, else the test skipped."""

    if kind == "junction" and (os.name != "nt" or not hasattr(os.DirEntry, "is_junction")):
        pytest.skip("a junction is a Windows link, told apart from Python 3.12 on")
    try:
        if kind == "junction":
            importlib.import_module("_winapi").CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this host cannot create a directory {kind}: {exc}")


@pytest.mark.parametrize("kind", ["symlink", "junction"])
@pytest.mark.parametrize("link", ["tests", "tests/linked"])
def test_a_linked_tests_tree_is_undecided_and_never_read_through(
    tmp_path: Path, link: str, kind: str
) -> None:
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere / "test_literal.py", f'HOOK = "{HOOK}"\n')
    if link != "tests":
        _write(tmp_path / HOOK_TEST, f'HOOK = "{HOOK}"\n')
    _link_or_skip(tmp_path / link, elsewhere, kind)

    tests, reason = release_cut.referencing_tests(tmp_path, [HOOK], IMPACT.is_test_module)

    assert (tests, reason) == ({}, f"impact_map_unmapped_undecided:{HOOK}:tests_linked:{link}")


def test_a_python_without_is_junction_tells_a_link_by_is_symlink_alone() -> None:
    """os.DirEntry and Path have is_junction from Python 3.12 only: no AttributeError."""

    class Entry:
        def __init__(self, symlink: bool) -> None:
            self.symlink = symlink

        def is_symlink(self) -> bool:
            return self.symlink

    assert release_cut._is_link(Entry(symlink=False)) is False
    assert release_cut._is_link(Entry(symlink=True)) is True


# -- a rerun after a failed cut resumes from its record --------------------------------


def test_a_resume_record_with_a_matching_digest_runs_the_failed_nodes_plus_the_changed_impact(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    _failed_host_cut(root)
    entries = _entries(root)
    # WSL never ran in that cut, so only the host suite has an entry. It names HEAD, as no
    # release commit was made, and the node that failed alone too, keyed by their digest.
    assert set(entries) == {"host_tests"}
    inputs = entries["host_tests"]["inputs"]
    assert (inputs["since"], inputs["failed"]) == (SINCE, [HOST_NODE])
    assert entries["host_tests"]["inputs_digest"] == release_cut._resume_digest(inputs)
    runner = FakeRunner(root)
    selector = Selector({IMPACT_BASE: _selection(full=FULL_REASON), SINCE: _selection()})

    code, summary = _cut(root, runner=runner, selector=selector)

    assert code == 0, summary["reason"]
    host, wsl = _targets(runner)
    # The failed node plus what the files changed since the recorded commit select.
    assert host == sorted([HOST_NODE, IMPACT_NODE, *INVARIANT_TESTS])
    # The WSL suite has nothing to resume from: it runs the normal selection.
    assert wsl == ["tests"]
    assert selector.calls == [(root, IMPACT_BASE), (root, SINCE)]
    assert release_cut.TEST_IMPACT_RECORDER_PLUGIN not in runner.argv_of("pytest")[0]
    assert summary["test_selection"] == {
        "lane": "resume",
        "base_tag": IMPACT_BASE,
        "selected_count": len(host),
        "fallback_reason": f"{NORMAL};host_tests_resumed:since:{SINCE[:12]}:failed:1",
    }


def _edit_the_record(root: Path) -> None:
    """Edit the entry's failed nodes and leave its digest as the failed cut wrote it."""

    path = root / RECORD
    record = json.loads(path.read_text(encoding="utf-8"))
    record["steps"]["host_tests"]["inputs"]["failed"] = []
    _write(path, json.dumps(record))


def _drop_the_record(root: Path) -> None:
    (root / RECORD).unlink()


MISMATCH = f"{NORMAL};host_tests_resume_skipped:inputs_digest_mismatch"


@pytest.mark.parametrize(
    ("arrange", "args", "python", "reason", "base", "asked"),
    [
        (_edit_the_record, (), "python", MISMATCH, IMPACT_BASE, [IMPACT_BASE]),
        (None, (), "python3.12", MISMATCH, IMPACT_BASE, [IMPACT_BASE]),
        (_drop_the_record, (), "python", NORMAL, IMPACT_BASE, [IMPACT_BASE]),
        (None, ("--full-suite",), "python", "full_suite_flag", None, []),
    ],
    ids=["edited_record", "other_interpreter", "missing_record", "full_suite_flag"],
)
def test_a_resume_record_that_does_not_match_or_is_missing_runs_the_normal_selection(
    tmp_path: Path,
    arrange: Callable[[Path], None] | None,
    args: tuple[str, ...],
    python: str,
    reason: str,
    base: str | None,
    asked: list[str],
) -> None:
    root = _repo(tmp_path)
    _failed_host_cut(root)
    if arrange is not None:
        arrange(root)
    runner = FakeRunner(root)
    selector = Selector({IMPACT_BASE: _selection(full=FULL_REASON), SINCE: _selection()})

    code, summary = _cut(root, *args, runner=runner, selector=selector, python=python)

    assert code == 0, summary["reason"]
    host, wsl = _targets(runner)
    assert host == wsl == ["tests"]
    # A record that does not match is never asked for its changed-file impact.
    assert [asked_base for _root, asked_base in selector.calls] == asked
    assert summary["test_selection"] == {
        "lane": "full_suite",
        "base_tag": base,
        "selected_count": None,
        "fallback_reason": reason,
    }


def test_a_cut_that_failed_in_wsl_resumes_both_suites_from_its_release_commit(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, {"wsl pytest": _full_then_alone(WSL_FAILURE, WSL_FAILURE)})
    selector = Selector({IMPACT_BASE: _selection(full=FULL_REASON)})
    code, summary = _cut(root, runner=runner, selector=selector)
    assert code == 1 and summary["failed_step"] == "wsl_tests", summary["reason"]
    # Both suites ran to a verdict on the release commit's tree; the host suite passed.
    recorded = {step: entry["inputs"] for step, entry in _entries(root).items()}
    assert {step: (inputs["since"], inputs["failed"]) for step, inputs in recorded.items()} == {
        "host_tests": (RELEASE_SHA, []),
        "wsl_tests": (RELEASE_SHA, [WSL_NODE]),
    }
    _release_files(root)  # the recovery line's reset and checkout
    runner = FakeRunner(root)
    selector = Selector({IMPACT_BASE: _selection(full=FULL_REASON), RELEASE_SHA: _selection()})

    code, summary = _cut(root, runner=runner, selector=selector)

    assert code == 0, summary["reason"]
    host, wsl = _targets(runner)
    assert host == sorted([IMPACT_NODE, *INVARIANT_TESTS])
    # The WSL node needs quoting, so it runs as its whole file.
    assert wsl == sorted(["tests/test_posix_modes.py", IMPACT_NODE, *INVARIANT_TESTS])
    assert summary["test_selection"]["lane"] == "resume"
    assert summary["test_selection"]["fallback_reason"] == (
        f"{NORMAL};host_tests_resumed:since:{RELEASE_SHA[:12]}:failed:0;"
        f"wsl_tests_resumed:since:{RELEASE_SHA[:12]}:failed:1"
    )


def test_a_resumed_run_that_fails_again_keeps_the_records_commit_and_adds_its_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _failed_host_cut(root)
    # HEAD moved on with the fix; the resumed run still ran only what changed since SINCE.
    monkeypatch.setattr(release_cut.ReleaseCut, "_head_commit", lambda _self: "f" * 40)
    again = Completed(1, f"{SUMMARY_HEADER}\nFAILED {IMPACT_NODE} - assert 0\n1 failed\n")
    selector = Selector({IMPACT_BASE: _selection(full=FULL_REASON), SINCE: _selection()})
    code, summary = _cut(root, runner=FakeRunner(root, {"pytest": again}), selector=selector)
    assert code == 1 and summary["failed_step"] == "host_tests", summary["reason"]
    inputs = _entries(root)["host_tests"]["inputs"]
    assert (inputs["since"], inputs["failed"]) == (SINCE, sorted([HOST_NODE, IMPACT_NODE]))
    _release_files(root)
    runner = FakeRunner(root)

    code, summary = _cut(root, runner=runner, selector=selector)

    assert code == 0, summary["reason"]
    host, _ = _targets(runner)
    assert host == sorted([HOST_NODE, IMPACT_NODE, *INVARIANT_TESTS])
    assert summary["test_selection"]["fallback_reason"] == (
        f"{NORMAL};host_tests_resumed:since:{SINCE[:12]}:failed:2"
    )


def test_a_resume_record_nested_past_jsons_depth_is_unusable_and_the_cut_still_runs(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    depth = 100_000  # far past the recursion limit of any CPython json decoder
    _write(root / RECORD, "[" * depth + "]" * depth)
    runner = FakeRunner(root)
    selector = Selector({IMPACT_BASE: _selection(full=FULL_REASON)})

    code, summary = _cut(root, runner=runner, selector=selector)

    assert code == 0, summary["reason"]
    assert _targets(runner) == (["tests"], ["tests"])
    unusable = "resume_skipped:record_unreadable:invalid_json"
    assert summary["test_selection"]["fallback_reason"] == (
        f"{NORMAL};host_tests_{unusable};wsl_tests_{unusable}"
    )


@pytest.mark.parametrize("record_stays", [False, True], ids=["record_removed", "record_stays"])
def test_a_resume_record_that_cannot_be_written_leaves_no_stale_one_to_resume_from(
    tmp_path: Path, record_stays: bool
) -> None:
    root = _repo(tmp_path)
    _failed_host_cut(root)
    record = root / RECORD
    # The atomic write cannot create its partial file: a directory holds that name.
    (record.parent / f"{release_cut.RESUME_RECORD}.partial").mkdir()
    if record_stays:
        # Nor can the stale record be removed: a directory with an entry holds its name.
        record.unlink()
        (record / "held").mkdir(parents=True)
    runner = FakeRunner(root, {"pytest": _full_then_alone(HOST_FAILURE, FAILS_ALONE_TOO)})
    selector = Selector({IMPACT_BASE: _selection(full=FULL_REASON), SINCE: _selection()})

    code, summary = _cut(root, runner=runner, selector=selector)

    assert code == 1 and summary["failed_step"] == "host_tests", summary["reason"]
    recovery = summary["recovery"]
    assert f"the resume record {RECORD} was not written (" in recovery
    if record_stays:
        assert recovery.endswith("): delete it before a rerun") and record.is_dir()
        return
    assert recovery.endswith("), so a rerun runs the normal selection")
    assert not record.exists()
    # And so it does: nothing is left to resume from.
    _release_files(root)
    runner = FakeRunner(root)

    code, summary = _cut(root, runner=runner, selector=selector)

    assert code == 0, summary["reason"]
    assert _targets(runner) == (["tests"], ["tests"])
    assert summary["test_selection"]["fallback_reason"] == NORMAL


def test_a_recorded_node_whose_file_is_gone_drops_the_entry_before_the_run(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    _failed_host_cut(root)
    (root / "tests" / "test_a.py").unlink()  # the fix removed the failed test's module
    runner = FakeRunner(root)
    selector = Selector({IMPACT_BASE: _selection(full=FULL_REASON), SINCE: _selection()})

    code, summary = _cut(root, runner=runner, selector=selector)

    assert code == 0, summary["reason"]
    assert _targets(runner) == (["tests"], ["tests"])
    # Dropped before its commit's impact is asked for, and gone from the record at once.
    assert [base for _root, base in selector.calls] == [IMPACT_BASE]
    assert _entries(root) == {}
    assert summary["test_selection"] == {
        "lane": "full_suite",
        "base_tag": IMPACT_BASE,
        "selected_count": None,
        "fallback_reason": (
            f"{NORMAL};host_tests_resume_dropped:failed_node_missing:tests/test_a.py"
        ),
    }


@pytest.mark.parametrize(
    "not_found",
    [
        Completed(4, f"ERROR: not found: {HOST_NODE}\n\nno tests ran in 0.01s\n"),
        # Under xdist the workers swallow that error and nothing is collected: exit 5.
        Completed(5, "bringing up nodes...\n\nno tests ran in 1.06s\n"),
    ],
    ids=["serial_exit_4", "xdist_exit_5"],
)
def test_a_resumed_run_pytest_finds_no_recorded_node_in_drops_the_entry_and_runs_the_normal_one(
    tmp_path: Path, not_found: Any
) -> None:
    root = _repo(tmp_path)
    _failed_host_cut(root)
    # The node's module is still there, but the fix renamed the test the node names.
    runner = FakeRunner(root, {"pytest": _in_turn(not_found)})
    selector = Selector({IMPACT_BASE: _selection(full=FULL_REASON), SINCE: _selection()})

    code, summary = _cut(root, runner=runner, selector=selector)

    assert code == 0, summary["reason"]
    resumed, normal = runner.argv_of("pytest")
    assert _after_basetemp(resumed) == sorted([HOST_NODE, IMPACT_NODE, *INVARIANT_TESTS])
    # The normal selection is the whole suite, so that run records the map as well.
    assert _after_basetemp(normal) == ["tests"]
    assert release_cut.TEST_IMPACT_RECORDER_PLUGIN in normal
    assert _entries(root) == {}
    assert summary["test_selection"] == {
        "lane": "full_suite",
        "base_tag": IMPACT_BASE,
        "selected_count": None,
        "fallback_reason": (
            f"{NORMAL};host_tests_resume_dropped:pytest_exit_{not_found.returncode}"
        ),
    }


def test_a_resumed_wsl_run_that_collects_no_recorded_node_drops_its_entry_and_reruns(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, {"wsl pytest": _full_then_alone(WSL_FAILURE, WSL_FAILURE)})
    selector = Selector({IMPACT_BASE: _selection(full=FULL_REASON)})
    code, summary = _cut(root, runner=runner, selector=selector)
    assert code == 1 and summary["failed_step"] == "wsl_tests", summary["reason"]
    _release_files(root)  # the recovery line's reset and checkout
    # The fix renamed the WSL test; under xdist pytest collects nothing and exits 5.
    collected_none = Completed(5, "bringing up nodes...\n\nno tests ran in 1.06s\n")
    runner = FakeRunner(root, {"wsl pytest": _in_turn(collected_none)})
    selector = Selector({IMPACT_BASE: _selection(full=FULL_REASON), RELEASE_SHA: _selection()})

    code, summary = _cut(root, runner=runner, selector=selector)

    assert code == 0, summary["reason"]
    resumed, normal = (_after_basetemp(_guarded(call)[1]) for call in runner.argv_of("wsl pytest"))
    assert resumed == sorted(["tests/test_posix_modes.py", IMPACT_NODE, *INVARIANT_TESTS])
    assert normal == ["tests"]
    assert "wsl_tests" not in _entries(root)
    assert summary["test_selection"]["fallback_reason"] == (
        f"{NORMAL};host_tests_resumed:since:{RELEASE_SHA[:12]}:failed:0;"
        "wsl_tests_resume_dropped:pytest_exit_5"
    )


def test_a_host_run_with_allow_dirty_paths_dirty_leaves_no_host_entry_to_resume_from(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    _failed_host_cut(root)
    responses = {
        "git status": Completed(0, "?? notes/todo.txt\0"),
        "pytest": _full_then_alone(HOST_FAILURE, FAILS_ALONE_TOO),
    }
    selector = Selector({IMPACT_BASE: _selection(full=FULL_REASON), SINCE: _selection()})

    code, summary = _cut(
        root, "--allow-dirty", "notes", runner=FakeRunner(root, responses), selector=selector
    )

    assert code == 1 and summary["failed_step"] == "host_tests", summary["reason"]
    # No commit holds the tree that run tested, so neither its entry nor the earlier one stays.
    assert _entries(root) == {}
    assert "the host suite ran with 1 --allow-dirty path(s) dirty" in summary["recovery"]
    _release_files(root)
    runner = FakeRunner(root)

    code, summary = _cut(root, runner=runner, selector=selector)

    assert code == 0, summary["reason"]
    assert _targets(runner) == (["tests"], ["tests"])
    assert summary["test_selection"]["fallback_reason"] == NORMAL


HEAD_SHA = "fedcba9876543210fedcba9876543210fedcba98"


def _runner_raises(_call: tuple[str, ...]) -> Any:
    raise OSError("cannot run git")


@pytest.mark.parametrize(
    ("repository", "answer", "since"),
    [
        (True, Completed(0, f"{HEAD_SHA}\n"), HEAD_SHA),
        (True, Completed(128, "", "fatal: Needed a single revision\n"), None),
        (True, _runner_raises, None),
        (False, Completed(0, f"{HEAD_SHA}\n"), None),
    ],
    ids=["head_named", "git_fails", "runner_raises", "no_repository"],
)
def test_the_real_head_commit_asks_the_host_runner_for_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository: bool,
    answer: Any,
    since: str | None,
) -> None:
    """A cut that failed before its commit step names HEAD's commit, asked of Host.runner."""

    monkeypatch.setattr(release_cut.ReleaseCut, "_head_commit", REAL_HEAD_COMMIT)
    root = _repo(tmp_path)
    if repository:
        (root / ".git").mkdir()
    responses = {
        "pytest": _full_then_alone(HOST_FAILURE, FAILS_ALONE_TOO),
        "git rev-parse HEAD": answer,
    }
    selector = Selector({IMPACT_BASE: _selection(full=FULL_REASON)})
    runner = FakeRunner(root, responses)

    code, summary = _cut(root, runner=runner, selector=selector)

    assert code == 1 and summary["failed_step"] == "host_tests", summary["reason"]
    # The commit step's own argv, asked only of a root that is a repository of its own.
    asked = [("git", *release_cut.HEAD_COMMIT_ARGS)] if repository else []
    assert runner.argv_of("git rev-parse HEAD") == asked
    assert _entries(root)["host_tests"]["inputs"]["since"] == since
