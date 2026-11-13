"""scripts/release_cut.py: one ordered release cut, driven through an injected runner.

No test here runs npm, WSL or a real pytest suite: every command the cut issues
goes to a FakeRunner that records it and answers from a script. The
release-metadata steps (version, changelog, readme, bump, metadata_check) run
for real against a minimal repository under tmp_path, and one test drives the
real runner's git against a throwaway repository there as well.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "aiworkhub_release_cut", ROOT / "scripts" / "release_cut.py"
)
assert SPEC is not None and SPEC.loader is not None
release_cut = importlib.util.module_from_spec(SPEC)
# Its dataclasses resolve their string annotations through sys.modules.
sys.modules[SPEC.name] = release_cut
SPEC.loader.exec_module(release_cut)
release_metadata = release_cut.release_metadata
Completed = release_cut.Completed

CURRENT = "0.12.99"
VERSION = "0.13.0"
TAG = f"v{VERSION}"
VSIX = f"vscode-extension/dist/aiworkhub-{VERSION}.vsix"
SUMMARY_HEADER = "=========================== short test summary info ============================"
# What the fake wslpath answers: the repository root as WSL mounts it.
LINUX_ROOT = "/mnt/z/repo"
LINUX_SCRATCH = f"{LINUX_ROOT}/.scratch/release/{VERSION}"
WSL_BASETEMP = f"/tmp/aiworkhub-release-{VERSION}"
# A venv interpreter inside WSL, which --wsl-python names when pytest and xdist live there.
WSL_VENV_PYTHON = "/root/aiwh-ci-venv/bin/python"
RELEASE_SHA = "0123456789abcdef0123456789abcdef01234567"
BUMPED = [release_metadata.CANONICAL_VERSION_FILE, *release_metadata.PROJECTION_FILES]

STEP_ORDER = [
    "version",
    "changelog",
    "readme",
    "worktree",
    "branch",
    "tag_free",
    "wsl",
    "bump",
    "metadata_check",
    "host_tests",
    "package",
    "commit",
    "wsl_tests",
    "receipt",
    "tag",
    "push",
]
PREFLIGHT = STEP_ORDER[: STEP_ORDER.index("bump")]
CUT = STEP_ORDER[len(PREFLIGHT) :]
# Every command of a full cut, in order (see ``label``).
FULL_CUT = [
    "git status",
    "git symbolic-ref",
    "git rev-parse",
    "wsl python3",
    "wsl wslpath",  # the preflight's own translation of the scratch directory
    "pytest",
    "npm package",
    "git commit",
    "git rev-parse HEAD",
    "wsl wslpath",
    "wsl rm",
    "git archive",
    "wsl mkdir",
    "wsl tar",
    "wsl git",  # the WSL tree's git init, add and commit (_commit_wsl_tree)
    "wsl git",
    "wsl git",
    "wsl pytest",
    "wsl rm",
    "receipt record",
    "git tag",
    "git push",
]
READ_ONLY = ["git status", "git symbolic-ref", "git rev-parse", "wsl python3", "wsl wslpath"]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _repo(root: Path, *, current: str = CURRENT, version: str = VERSION) -> Path:
    """A repository whose projections carry ``current`` and whose documents name ``version``."""

    files = {
        "src/aiworkhub/_version.py": f'__version__ = "{current}"\n',
        "vscode-extension/package.json": json.dumps(
            {"name": "aiworkhub", "version": current}, indent=2
        )
        + "\n",
        "vscode-extension/package-lock.json": json.dumps(
            {
                "name": "aiworkhub",
                "version": current,
                "packages": {"": {"name": "aiworkhub", "version": current}},
            },
            indent=2,
        )
        + "\n",
        "vscode-extension/extension.js": f'const EXPECTED_MCP_PACKAGE_VERSION = "{current}";\n',
        "server.json": json.dumps(
            {
                "name": "io.github.shrec/aiworkhub",
                "version": current,
                "packages": [
                    {"registryType": "pypi", "identifier": "aiworkhub", "version": current}
                ],
            },
            indent=2,
        )
        + "\n",
        "CHANGELOG.md": (
            f"# Changelog\n\n## [{version}] - 2026-11-07\n\n- One release cut script.\n\n"
            f"## [{current}] - 2026-11-06\n\n- Older.\n"
        ),
        "vscode-extension/CHANGELOG.md": (
            f"# Changelog\n\n## {version} — 2026-11-07\n\n- One release cut script.\n"
        ),
        "README.md": f"# AIWorkHub\n\n## What's new in {version} — 2026-11-07\n\n- Cut script.\n",
        "vscode-extension/README.md": (
            f"# AIWorkHub\n\n## What's new in {version} — 2026-11-07\n\n- Cut script.\n"
        ),
    }
    for relative, text in files.items():
        _write(root / relative, text)
    return root


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str] | None
    timeout: float | None


def label(argv: Sequence[str]) -> str:
    """A short stable name for one command, so an expected order reads at a glance."""

    argv = list(argv)
    if argv[0] == "git":
        if argv[1] == "rev-parse" and argv[-1] == "HEAD":
            return "git rev-parse HEAD"
        return f"git {argv[1]}"
    if argv[0] == "wsl":
        # A guarded command is named by what it execs once inside its directory.
        inner = _guarded(argv)[1] if argv[1:3] == ["-e", "sh"] else argv[argv.index("-e") + 1 :]
        return "wsl pytest" if "pytest" in inner else f"wsl {inner[0]}"
    if argv[0] == "npm":
        return "npm package"
    if argv[1:3] == ["-m", "pytest"]:
        return "pytest"
    if len(argv) > 2 and argv[1] == "scripts/release_receipt.py":
        return f"receipt {argv[2]}"
    raise AssertionError(f"the cut ran an unexpected command: {argv}")


def _guarded(argv: Sequence[str]) -> tuple[str, list[str]]:
    """The directory and the command of one ``sh -c 'cd -- DIR || exit 97; exec CMD'`` call."""

    assert len(argv) == 5 and list(argv[:4]) == ["wsl", "-e", "sh", "-c"], argv
    guard, separator, command = argv[4].partition(" || exit 97; exec ")
    assert separator and guard.startswith("cd -- "), argv[4]
    (directory,) = shlex.split(guard[len("cd -- ") :])
    return directory, shlex.split(command)


class FakeRunner:
    """Records every command; answers from ``responses`` (by label) or a passing default.

    A response may be a callable of the ``Call``: what it returns answers the
    command, and None leaves the answer to the passing default.
    """

    def __init__(self, root: Path, responses: dict[str, Any] | None = None) -> None:
        self.root = root
        self.responses = dict(responses or {})
        self.calls: list[Call] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Any = None,
        timeout: float | None = None,
    ) -> Any:
        call = Call(tuple(argv), cwd, None if env is None else dict(env), timeout)
        self.calls.append(call)
        name = label(call.argv)
        response = self.responses.get(name)
        if callable(response):
            response = response(call)
        if response is not None:
            return response
        return self._passing(name, call)

    def _passing(self, name: str, call: Call) -> Any:
        if name == "git symbolic-ref":
            return Completed(0, "main\n")
        if name == "git rev-parse":
            return Completed(1)  # --verify of an absent tag
        if name == "git rev-parse HEAD":
            return Completed(0, f"{RELEASE_SHA}\n")
        if name == "wsl wslpath":
            windows = Path(call.argv[-1])
            return Completed(0, f"{LINUX_ROOT}/{windows.relative_to(self.root).as_posix()}\n")
        if name == "git archive":
            output = next(token for token in call.argv if token.startswith("--output="))
            Path(output.split("=", 1)[1]).write_bytes(b"tar")
            return Completed(0)
        if name == "npm package":
            _write(self.root / VSIX, "vsix")
            return Completed(0)
        if name in ("pytest", "wsl pytest"):
            return Completed(0, "........\n12 passed in 1.00s\n")
        return Completed(0)

    @property
    def labels(self) -> list[str]:
        return [label(call.argv) for call in self.calls]

    def argv_of(self, name: str) -> list[tuple[str, ...]]:
        return [call.argv for call in self.calls if label(call.argv) == name]


def _cut(
    root: Path,
    *args: str,
    version: str = VERSION,
    runner: FakeRunner | None = None,
    cpu_count: int | None = 16,
    windows: bool = True,
) -> tuple[int, dict[str, Any], list[str], FakeRunner]:
    runner = runner if runner is not None else FakeRunner(root)
    lines: list[str] = []
    host = release_cut.Host(
        root=root,
        runner=runner,
        python="python",
        cpu_count=cpu_count,
        env={"PATH": "fake-bin"},
        is_windows=windows,
        out=lines.append,
    )
    code = release_cut.main([version, *args], host=host)
    return code, json.loads(lines[-1]), lines, runner


def _statuses(summary: dict[str, Any]) -> dict[str, str]:
    return {step["name"]: step["status"] for step in summary["steps"]}


def _status(*entries: str) -> Any:
    return Completed(0, "".join(f"{entry}\0" for entry in entries))


def _on_call(number: int, response: Any) -> Any:
    """A response that answers only the ``number``-th call of its label (1-based)."""

    seen = 0

    def respond(_call: Call) -> Any:
        nonlocal seen
        seen += 1
        return response if seen == number else None  # None: the passing default

    return respond


# -- step order ------------------------------------------------------------


def test_a_cut_runs_every_step_in_order_through_the_injected_runner(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    code, summary, lines, runner = _cut(root, "--push")

    assert code == 0, summary["reason"]
    assert runner.labels == FULL_CUT
    assert [step["name"] for step in summary["steps"]] == STEP_ORDER
    assert set(_statuses(summary).values()) == {"ok"}
    assert summary["ok"] is True and summary["failed_step"] is None
    assert summary["current_version"] == CURRENT and summary["tag"] == TAG
    assert summary["completed_steps"] == STEP_ORDER and summary["recovery"] is None
    assert all(call.cwd == root for call in runner.calls)
    # The bump ran for real: the canonical version and every projection moved.
    assert release_metadata.canonical_version(root) == VERSION
    assert set(release_metadata.projected_versions(root).values()) == {VERSION}
    # The summary is one JSON line, and it is the last line.
    assert lines[-1] == json.dumps(summary, sort_keys=True)


@pytest.mark.parametrize(
    ("failing", "response", "step"),
    [
        ("git symbolic-ref", Completed(0, "feature/x\n"), "branch"),
        ("git symbolic-ref", Completed(1), "branch"),
        ("git rev-parse", Completed(0, "0123abcd\n"), "tag_free"),
        ("wsl python3", Completed(1, "", "ModuleNotFoundError: No module named 'xdist'\n"), "wsl"),
        (
            "pytest",
            Completed(
                1,
                f"..F.\n{SUMMARY_HEADER}\nFAILED tests/test_a.py::test_x - assert 0\n"
                "1 failed, 11 passed in 1.00s\n",
            ),
            "host_tests",
        ),
        ("npm package", Completed(1, "", "vsce: packaging failed\n"), "package"),
        ("npm package", Completed(0), "package"),
        ("git commit", Completed(1, "", "error: pathspec did not match\n"), "commit"),
        ("git rev-parse HEAD", Completed(128, "", "fatal: bad object HEAD\n"), "commit"),
        # The wsl preflight translates the scratch directory first, before any commit.
        ("wsl wslpath", Completed(1, "", "wslpath: Z:\\repo: Invalid argument\n"), "wsl"),
        ("wsl rm", Completed(97, "", f"sh: 1: cd: can't cd to {LINUX_SCRATCH}\n"), "wsl_tests"),
        ("git archive", Completed(128, "", "fatal: not a valid object name\n"), "wsl_tests"),
        ("receipt record", Completed(2, "", '{"error": "vsix not found"}\n'), "receipt"),
        ("git tag", Completed(128, "", "fatal: tag 'v0.13.0' already exists\n"), "tag"),
        ("git push", Completed(1, "", "! [rejected] main -> main (fetch first)\n"), "push"),
    ],
)
def test_the_first_failing_step_stops_the_cut_and_the_summary_names_it(
    tmp_path: Path, failing: str, response: Any, step: str
) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, {failing: response})
    code, summary, lines, _ = _cut(root, "--push", runner=runner)

    assert code == 1
    assert summary["ok"] is False and summary["failed_step"] == step
    statuses = _statuses(summary)
    index = STEP_ORDER.index(step)
    assert [statuses[name] for name in STEP_ORDER[:index]] == ["ok"] * index
    assert statuses[step] == "failed"
    assert {statuses[name] for name in STEP_ORDER[index + 1 :]} <= {"not_run"}
    # Nothing ran after the failing command but the WSL tree's own cleanup and the one
    # serial rerun of a failed node.
    expected = FULL_CUT[: FULL_CUT.index(failing) + 1]
    if failing == "git archive":
        expected.append("wsl rm")
    if failing == "pytest":
        expected.append("pytest")
    assert runner.labels == expected
    reason = summary["reason"]
    assert reason and "\n" not in reason
    assert summary["completed_steps"] == STEP_ORDER[:index]
    # A failure in the cut also names what it already changed; a preflight one has nothing.
    recovery = summary["recovery"]
    assert (recovery is None) == (step in PREFLIGHT)
    tail = [f"FAILED  {step}: {reason}", *([] if recovery is None else [f"recovery: {recovery}"])]
    assert lines[-1 - len(tail) : -1] == tail


def test_an_in_process_step_failure_stops_the_cut_too(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root / "vscode-extension" / "extension.js", "// no runtime version literal\n")
    code, summary, _, runner = _cut(root, "--push")

    assert code == 1 and summary["failed_step"] == "bump"
    assert "extension runtime version projection" in summary["reason"]
    assert runner.labels == READ_ONLY
    # The bump wrote some files before it failed, so the recovery line names them all.
    assert summary["recovery"].startswith(f"the bump may have rewritten {' '.join(BUMPED)}")


def test_a_failure_after_the_bump_names_what_changed_in_one_recovery_line(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path / "bumped")
    failing = Completed(1, f"{SUMMARY_HEADER}\nFAILED tests/test_a.py::test_x - assert 0\n")
    code, summary, lines, _ = _cut(root, runner=FakeRunner(root, {"pytest": failing}))

    assert code == 1 and summary["failed_step"] == "host_tests"
    assert summary["completed_steps"] == STEP_ORDER[: STEP_ORDER.index("host_tests")]
    assert "bump" in summary["completed_steps"]
    recovery = summary["recovery"]
    assert lines[-2] == f"recovery: {recovery}"
    assert recovery == (
        f"the bump rewrote {' '.join(BUMPED)}; a rerun must restore them first: "
        f"git checkout -- {' '.join(BUMPED)}"
    )

    root = _repo(tmp_path / "committed")
    runner = FakeRunner(root, {"receipt record": Completed(2, "", "vsix not found\n")})
    code, summary, _, _ = _cut(root, runner=runner)

    assert code == 1 and summary["failed_step"] == "receipt"
    assert "commit" in summary["completed_steps"]
    assert f"release commit {RELEASE_SHA} carries them" in summary["recovery"]
    restore = f"git reset HEAD~1 && git checkout -- {' '.join(BUMPED)}"
    assert summary["recovery"].endswith(restore)


@pytest.mark.parametrize(
    ("failing", "step", "flags"), [("git tag", "tag", ()), ("git push", "push", ("--push",))]
)
def test_a_failure_at_tag_or_push_names_the_release_ledger_in_the_recovery_hint(
    tmp_path: Path, failing: str, step: str, flags: tuple[str, ...]
) -> None:
    root = _repo(tmp_path)
    # The ledger already holds two earlier built lines. The second is still uncommitted, so
    # this cut's release commit carries it; the fake receipt appends this cut's line, as
    # release_receipt.py does.
    ledger = root / ".aiworkhub" / "releases.jsonl"
    seed = b'{"kind": "built", "version": "0.12.98"}\n{"kind": "built", "version": "0.12.99"}\n'
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(seed)

    def append_built_line(_call: Call) -> None:
        with ledger.open("ab") as handle:
            handle.write(b'{"kind": "built", "version": "0.13.0"}\n')

    runner = FakeRunner(
        root,
        {"receipt record": append_built_line, failing: Completed(1, "", "fatal: refused\n")},
    )

    code, summary, lines, _ = _cut(root, *flags, runner=runner)

    assert code == 1 and summary["failed_step"] == step
    assert "receipt" in summary["completed_steps"]
    recovery = summary["recovery"]
    assert lines[-2] == f"recovery: {recovery}"
    # The receipt appended this cut's built line after the seed the file held when it began.
    assert ledger.read_bytes().startswith(seed) and len(ledger.read_bytes()) > len(seed)
    assert "the receipt appended a built line to .aiworkhub/releases.jsonl" in recovery
    assert "drops only this cut's built line" in recovery
    restore = recovery.partition("a rerun must restore them first: ")[2].split(" && ")
    # The release commit carries the earlier cut's built line and nothing else does, so a
    # checkout of the ledger after the reset would drop it: only the bumped files are
    # checked out.
    checkouts = [part for part in restore if part.startswith("git checkout")]
    assert checkouts == [f"git checkout -- {' '.join(BUMPED)}"]
    assert not any("releases.jsonl" in part for part in checkouts)
    # The ledger is cut back to the size it had before the receipt step appended to it.
    truncate = f"import os; os.truncate('.aiworkhub/releases.jsonl', {len(seed)})"
    assert restore[-1] == f'python -c "{truncate}"'


# -- version rule -----------------------------------------------------------


def test_patch_99_rolls_over_to_the_next_minor_and_never_to_100() -> None:
    assert release_metadata.successor_version("0.12.99") == "0.13.0"
    assert release_metadata.successor_version("0.12.93") == "0.12.94"
    release_metadata.validate_successor("0.12.99", "0.13.0")
    release_metadata.validate_successor("0.12.93", "0.12.94")
    with pytest.raises(ValueError, match=r"patch 100 > 99.*0\.13\.0"):
        release_metadata.validate_successor("0.12.99", "0.12.100")
    for stale in ("0.12.99", "0.12.98", "0.11.5"):
        with pytest.raises(ValueError, match="not greater"):
            release_metadata.validate_successor("0.12.99", stale)
    for malformed in ("0.13", "v0.13.0", "0.13.0-rc1", "0.13.00", "../0.13.0"):
        with pytest.raises(ValueError, match="MAJOR.MINOR.PATCH"):
            release_metadata.validate_successor("0.12.99", malformed)


def test_the_cut_refuses_0_12_100_and_accepts_0_13_0(tmp_path: Path) -> None:
    refused = _repo(tmp_path / "refused", version="0.12.100")
    code, summary, _, runner = _cut(refused, version="0.12.100")
    assert code == 1 and summary["failed_step"] == "version"
    assert "0.13.0" in summary["reason"]
    assert runner.calls == []
    assert release_metadata.canonical_version(refused) == CURRENT

    accepted = _repo(tmp_path / "accepted")
    code, summary, _, _ = _cut(accepted)
    assert code == 0, summary["reason"]
    assert summary["steps"][0] == {"name": "version", "status": "ok", "detail": f"{CURRENT} -> {VERSION}"}


def test_bump_changes_only_the_version_literal_and_refuses_a_non_successor(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    canonical = root / "src" / "aiworkhub" / "_version.py"
    canonical.write_bytes(b'"""Version."""\r\n\r\n__version__ = "0.12.99"\r\n')

    result = release_metadata.bump(root, VERSION)

    assert result["ok"] is True, result["mismatches"]
    assert canonical.read_bytes() == b'"""Version."""\r\n\r\n__version__ = "0.13.0"\r\n'
    with pytest.raises(ValueError, match="not greater"):
        release_metadata.bump(root, VERSION)
    assert canonical.read_bytes() == b'"""Version."""\r\n\r\n__version__ = "0.13.0"\r\n'


def test_release_metadata_has_a_bump_subcommand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    assert release_metadata.main(["--root", str(root), "bump", VERSION]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is True and printed["canonical_version"] == VERSION

    assert release_metadata.main(["--root", str(root), "bump", "0.13.100"]) == 2
    assert "patch 100" in json.loads(capsys.readouterr().out)["error"]


# -- release documents -------------------------------------------------------


@pytest.mark.parametrize("readme", ["README.md", "vscode-extension/README.md"])
def test_a_readme_with_two_whats_new_sections_is_refused(tmp_path: Path, readme: str) -> None:
    root = _repo(tmp_path)
    path = root / readme
    _write(path, path.read_text(encoding="utf-8") + f"\n## What's new in {CURRENT}\n\n- Older.\n")

    code, summary, _, runner = _cut(root)

    assert code == 1 and summary["failed_step"] == "readme"
    assert readme in summary["reason"] and "more than one" in summary["reason"]
    assert runner.calls == []


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (f"# AIWorkHub\n\nWhat's new in {VERSION} is below.\n", "no '## What's new in"),
        (
            f"# AIWorkHub\n\n## What's new in {CURRENT}\n\nWhat's new in {VERSION} soon.\n",
            f"names '{CURRENT}', not {VERSION}",
        ),
        (f"# AIWorkHub\n\n## What's new in {VERSION}1\n", f"names '{VERSION}1'"),
    ],
)
def test_the_single_whats_new_section_must_name_the_version(
    tmp_path: Path, text: str, why: str
) -> None:
    root = _repo(tmp_path)
    _write(root / "README.md", text)

    code, summary, _, _ = _cut(root)

    assert code == 1 and summary["failed_step"] == "readme"
    assert summary["reason"].startswith("README.md: ") and why in summary["reason"]


def test_a_changelog_without_a_section_for_the_version_is_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root / "CHANGELOG.md", f"# Changelog\n\n## [{CURRENT}] - 2026-11-06\n\n- Older.\n")

    code, summary, _, runner = _cut(root)

    assert code == 1 and summary["failed_step"] == "changelog"
    assert f"[{VERSION}]" in summary["reason"]
    assert runner.calls == []


def test_the_extension_changelog_must_name_the_version_too(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root / "vscode-extension" / "CHANGELOG.md", f"# Changelog\n\n## {CURRENT} — x\n")

    code, summary, _, _ = _cut(root)

    assert code == 1 and summary["failed_step"] == "changelog"
    assert summary["reason"].startswith("vscode-extension/CHANGELOG.md: missing")


# -- WSL suite ----------------------------------------------------------------

WSL_FAILURE = Completed(
    1,
    "....F\n"
    "____ test_mode ____\n"
    "FAILED this line is captured test output, not the summary\n"
    f"{SUMMARY_HEADER}\n"
    "FAILED tests/test_posix_modes.py::test_mode[0o600 - owner] - AssertionError: 0o644\n"
    "1 failed, 4 passed in 1.00s\n",
)
WSL_NODE = "tests/test_posix_modes.py::test_mode[0o600 - owner]"


def test_a_wsl_failure_not_in_the_baseline_stops_the_cut(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    baseline = tmp_path / "wsl-baseline.txt"
    _write(baseline, "# known WSL failures\ntests/test_other.py::test_y\n")
    runner = FakeRunner(root, {"wsl pytest": WSL_FAILURE})

    code, summary, _, _ = _cut(root, "--push", "--wsl-baseline", str(baseline), runner=runner)

    assert code == 1 and summary["failed_step"] == "wsl_tests"
    assert f"1 WSL failure(s) not in the baseline: {WSL_NODE}" in summary["reason"]
    assert "this line is captured" not in summary["reason"]
    # The full run, the one serial rerun of the new node, and the WSL tree's own cleanup.
    assert runner.labels == [*FULL_CUT[: FULL_CUT.index("wsl pytest") + 1], "wsl pytest", "wsl rm"]
    assert (root / ".scratch" / "release" / VERSION / "wsl_tests.log").is_file()


def test_a_wsl_failure_listed_in_the_baseline_does_not_stop_the_cut(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    baseline = tmp_path / "wsl-baseline.txt"
    # A line pasted straight from pytest's short summary works as an entry.
    _write(baseline, f"FAILED {WSL_NODE} - AssertionError: 0o644\n")
    runner = FakeRunner(root, {"wsl pytest": WSL_FAILURE})

    code, summary, _, _ = _cut(root, "--push", "--wsl-baseline", str(baseline), runner=runner)

    assert code == 0, summary["reason"]
    wsl_tests = next(step for step in summary["steps"] if step["name"] == "wsl_tests")
    assert wsl_tests["status"] == "ok" and "each in the baseline" in wsl_tests["detail"]
    assert runner.labels == FULL_CUT


@pytest.mark.parametrize(
    "response",
    [
        Completed(1, "", "/usr/bin/python3: No module named pytest\n"),
        Completed(
            2,
            f"{SUMMARY_HEADER}\nERROR {WSL_NODE}\n!!! Interrupted: 1 error during collection !!!\n",
        ),
    ],
)
def test_a_wsl_run_without_a_baselined_verdict_fails_closed(tmp_path: Path, response: Any) -> None:
    root = _repo(tmp_path)
    baseline = tmp_path / "wsl-baseline.txt"
    _write(baseline, f"{WSL_NODE}\n")
    runner = FakeRunner(root, {"wsl pytest": response})

    code, summary, _, _ = _cut(root, "--wsl-baseline", str(baseline), runner=runner)

    assert code == 1 and summary["failed_step"] == "wsl_tests"


def test_the_wsl_suite_runs_on_a_git_archive_of_head_under_scratch_release(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    code, _, _, runner = _cut(root)
    assert code == 0
    scratch = root / ".scratch" / "release" / VERSION

    (archive,) = runner.argv_of("git archive")
    assert archive == ("git", "archive", "--format=tar", f"--output={scratch / 'head.tar'}", "HEAD")
    wsl_calls = [call for call in runner.calls if call.argv[0] == "wsl"]
    # The import check, wslpath twice (preflight, wsl_tests), rm, mkdir, tar, git init,
    # add and commit, pytest, rm.
    assert len(wsl_calls) == 11
    assert all(call.env is not None and call.env["MSYS_NO_PATHCONV"] == "1" for call in wsl_calls)
    (suite,) = runner.argv_of("wsl pytest")
    assert _guarded(suite)[0] == f"{LINUX_SCRATCH}/tree"
    # The archive commit is the release commit, and the host suite was over before.
    assert runner.labels.index("git commit") < runner.labels.index("git archive")
    assert runner.labels.index("pytest") < runner.labels.index("wsl pytest")
    # Only the read-only preflight check enters a directory with --cd: the root.
    (preflight,) = runner.argv_of("wsl python3")
    assert preflight[:3] == ("wsl", "--cd", str(root))


def test_every_wsl_tree_command_is_guarded_and_names_only_absolute_linux_paths(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    code, summary, _, runner = _cut(root)
    assert code == 0, summary["reason"]

    # The scratch directory is translated through the injected runner: by the read-only
    # preflight, and again by wsl_tests once the release commit exists.
    translate = ("wsl", "-e", "wslpath", "-a", str(root / ".scratch" / "release" / VERSION))
    assert runner.argv_of("wsl wslpath") == [translate, translate]
    guarded = [call.argv for call in runner.calls if call.argv[:4] == ("wsl", "-e", "sh", "-c")]
    assert [label(argv) for argv in guarded] == [
        "wsl rm",
        "wsl mkdir",
        "wsl tar",
        "wsl git",
        "wsl git",
        "wsl git",
        "wsl pytest",
        "wsl rm",
    ]
    tree, archive = f"{LINUX_SCRATCH}/tree", f"{LINUX_SCRATCH}/head.tar"
    assert guarded[0][4] == (
        f"cd -- {LINUX_SCRATCH} || exit 97; exec rm -rf -- {tree} {archive} {WSL_BASETEMP}"
    )
    assert _guarded(guarded[1]) == (LINUX_SCRATCH, ["mkdir", "--", tree])
    assert _guarded(guarded[2]) == (LINUX_SCRATCH, ["tar", "-xf", archive, "-C", tree])
    assert _guarded(guarded[3]) == (tree, ["git", "init", "-q"])
    assert _guarded(guarded[4]) == (
        tree,
        ["git", "-c", "user.name=release", "-c", "user.email=release@localhost", "add", "-A"],
    )
    assert _guarded(guarded[5]) == (
        tree,
        [
            "git",
            "-c",
            "user.name=release",
            "-c",
            "user.email=release@localhost",
            "commit",
            "-q",
            "-m",
            "release-tree",
        ],
    )
    assert _guarded(guarded[6])[0] == tree
    assert guarded[7] == guarded[0]


@pytest.mark.parametrize(
    "translated",
    [
        f"/home/user/repo/.scratch/release/{VERSION}",
        f"mnt/z/repo/.scratch/release/{VERSION}",
        "/mnt/z/repo",
        f"/mnt/z/repo/../../home/me/.scratch/release/{VERSION}",
        f"{LINUX_SCRATCH}\n/mnt/z/other",
        "",
    ],
)
def test_a_wsl_path_outside_mnt_stops_the_cut_before_anything_runs_in_it(
    tmp_path: Path, translated: str
) -> None:
    root = _repo(tmp_path)
    # The preflight accepted the first translation; wsl_tests translates again once the
    # release commit exists, and its own check refuses that second answer.
    runner = FakeRunner(root, {"wsl wslpath": _on_call(2, Completed(0, f"{translated}\n"))})

    code, summary, _, _ = _cut(root, runner=runner)

    assert code == 1 and summary["failed_step"] == "wsl_tests"
    assert "not a path under /mnt/" in summary["reason"]
    assert runner.labels[-1] == "wsl wslpath"


@pytest.mark.parametrize("flags", [("--dry-run",), ()])
def test_a_wsl_path_outside_mnt_fails_the_read_only_wsl_preflight_before_any_bump_or_commit(
    tmp_path: Path, flags: tuple[str, ...]
) -> None:
    root = _repo(tmp_path)
    outside = f"/home/user/repo/.scratch/release/{VERSION}"
    runner = FakeRunner(root, {"wsl wslpath": Completed(0, f"{outside}\n")})

    code, summary, _, _ = _cut(root, *flags, runner=runner)

    # --dry-run runs this step, so it reports what the cut used to find only after committing.
    assert code == 1 and summary["failed_step"] == "wsl"
    assert "not a path under /mnt/" in summary["reason"] and outside in summary["reason"]
    statuses = _statuses(summary)
    assert [statuses[name] for name in PREFLIGHT[:-1]] == ["ok"] * (len(PREFLIGHT) - 1)
    assert {statuses[name] for name in CUT} == {"not_run"}
    assert summary["recovery"] is None
    # Nothing past the read-only checks ran: no bump, no commit, no scratch directory.
    assert runner.labels == READ_ONLY
    assert release_metadata.canonical_version(root) == CURRENT
    assert not (root / ".scratch").exists()


@pytest.mark.parametrize(
    ("args", "python"),
    [
        ([], "python3"),
        (["--wsl-python", "python3"], "python3"),
        (["--wsl-python", WSL_VENV_PYTHON], WSL_VENV_PYTHON),
    ],
)
def test_wsl_python_names_the_interpreter_of_the_preflight_the_wsl_suite_and_the_plan(
    tmp_path: Path, args: list[str], python: str
) -> None:
    code, summary, lines, _ = _cut(_repo(tmp_path / "dry"), "--dry-run", *args)
    assert code == 0, summary["reason"]
    plan = {line.split()[1].rstrip(":"): line for line in lines if line.startswith("plan ")}
    assert f"wsl -e {python} -c 'import pytest, xdist'" in plan["wsl"]
    assert f"PYTHONPATH=src {python} -m pytest" in plan["wsl_tests"]

    root = _repo(tmp_path / "cut")
    code, summary, _, runner = _cut(root, *args)
    assert code == 0, summary["reason"]
    # The read-only preflight and the suite in the archived tree run the same interpreter.
    (preflight,) = [call.argv for call in runner.calls if call.argv[:2] == ("wsl", "--cd")]
    assert preflight[2:] == (str(root), "-e", python, "-c", "import pytest, xdist")
    (suite,) = runner.argv_of("wsl pytest")
    assert _guarded(suite)[1][:5] == ["env", "PYTHONPATH=src", python, "-m", "pytest"]


@pytest.mark.parametrize(
    "value",
    [
        "venv/bin/python",
        "./python3",
        "python",
        "C:\\Python312\\python.exe",
        "C:/Python312/python.exe",
        "\\\\wsl.localhost\\Ubuntu\\usr\\bin\\python3",
        "/root/my venv/bin/python",
        "/root/venv\\bin\\python",
        "/opt/a=b/python",
        "",
    ],
)
def test_wsl_python_refuses_anything_but_python3_or_an_absolute_posix_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    runner = FakeRunner(_repo(tmp_path))

    with pytest.raises(SystemExit) as refused:
        _cut(tmp_path, "--wsl-python", value, runner=runner)

    assert refused.value.code == 2
    error = capsys.readouterr().err
    assert "argument --wsl-python: " in error
    assert "is not python3 or an absolute POSIX path" in error
    assert runner.calls == []


@pytest.mark.parametrize("response", [None, WSL_FAILURE])
def test_the_wsl_basetemp_is_under_wsl_tmp_and_removed_after_the_run_pass_or_fail(
    tmp_path: Path, response: Any
) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, {} if response is None else {"wsl pytest": response})

    code, summary, _, _ = _cut(root, runner=runner)

    assert code == (0 if response is None else 1), summary["reason"]
    # A failed node outside the baseline is rerun alone in that basetemp, before the removal.
    suites = runner.argv_of("wsl pytest")
    assert len(suites) == (1 if response is None else 2)
    assert all(f"--basetemp={WSL_BASETEMP}" in _guarded(suite)[1] for suite in suites)
    assert runner.labels[runner.labels.index("wsl pytest") + len(suites)] == "wsl rm"
    assert WSL_BASETEMP in _guarded(runner.argv_of("wsl rm")[-1])[1]


def test_the_wsl_suite_needs_a_windows_host_unless_skipped(tmp_path: Path) -> None:
    code, summary, _, runner = _cut(_repo(tmp_path / "linux"), windows=False)
    assert code == 1 and summary["failed_step"] == "wsl"
    assert "--skip-wsl" in summary["reason"]
    assert "wsl python3" not in runner.labels

    code, summary, _, runner = _cut(_repo(tmp_path / "skipped"), "--skip-wsl", windows=False)
    assert code == 0, summary["reason"]
    assert _statuses(summary)["wsl"] == "skipped" and _statuses(summary)["wsl_tests"] == "skipped"
    assert not any(call.argv[0] == "wsl" for call in runner.calls)
    assert "git archive" not in runner.labels


def test_an_unreadable_wsl_baseline_stops_the_cut_before_anything_runs_in_wsl(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    code, summary, _, runner = _cut(root, "--wsl-baseline", str(tmp_path / "missing.txt"))

    assert code == 1 and summary["failed_step"] == "wsl"
    assert "missing.txt" in summary["reason"] and "unreadable" in summary["reason"]
    assert "wsl python3" not in runner.labels


def test_failing_node_ids_come_only_from_the_short_summary_and_are_uncapped() -> None:
    nodes = [f"tests/test_many.py::test_{index}[{'x' * 400}]" for index in range(60)]
    output = (
        "F.E\nERROR captured output before the summary\n"
        f"{SUMMARY_HEADER}\n"
        "FAILED tests/test_a.py::test_x[a - b] - assert 0\n"
        "ERROR tests/test_b.py::test_y - fixture 'db' not found\n"
        "FAILED tests/test_a.py::test_x[a - b] - assert 0\n"
        "ERROR tests/test_c.py\n"
        + "".join(f"FAILED {node} - boom\n" for node in nodes)
        + "61 failed, 1 passed, 2 errors in 0.10s\n"
    )

    found = release_cut.failing_node_ids(output)

    assert found[:3] == ["tests/test_a.py::test_x[a - b]", "tests/test_b.py::test_y", "tests/test_c.py"]
    assert found[3:] == nodes
    assert release_cut.failing_node_ids("FAILED tests/x.py::test - no summary header\n") == []


# -- one serial rerun of the failed nodes ---------------------------------------------

HOST_NODE = "tests/test_a.py::test_x"
HOST_FAILURE = Completed(
    1,
    f"..F.\n{SUMMARY_HEADER}\nFAILED {HOST_NODE} - assert 0\n1 failed, 11 passed in 1.00s\n",
)
PASSED_ALONE = Completed(0, ".\n1 passed in 0.10s\n")
OVER_THE_CAP = release_cut.LOAD_RERUN_MAX_NODES + 1
HOST_LOG = f".scratch/release/{VERSION}/host_tests.log"
HOST_RERUN_LOG = f".scratch/release/{VERSION}/host_tests_rerun.log"
WSL_LOG = f".scratch/release/{VERSION}/wsl_tests.log"
WSL_RERUN_LOG = f".scratch/release/{VERSION}/wsl_tests_rerun.log"


def _full_then_alone(full: Any, alone: Any) -> Any:
    """A suite response: ``full`` for the xdist run, ``alone`` for the serial rerun of nodes."""

    def respond(call: Call) -> Any:
        command = _guarded(call.argv)[1] if call.argv[0] == "wsl" else list(call.argv)
        return alone if command[command.index("-n") + 1] == "0" else full

    return respond


def _many_nodes(count: int) -> list[str]:
    return [f"tests/test_many.py::test_{index}" for index in range(count)]


def _many_failures(count: int) -> Completed:
    lines = "".join(f"FAILED {node} - boom\n" for node in _many_nodes(count))
    return Completed(1, f"{SUMMARY_HEADER}\n{lines}{count} failed in 1.00s\n")


def _detail(summary: dict[str, Any], name: str) -> str:
    return next(step["detail"] for step in summary["steps"] if step["name"] == name)


def test_a_green_first_run_makes_no_rerun_and_both_suite_commands_stay_byte_identical(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)

    code, summary, _, runner = _cut(root, "--push")

    assert code == 0, summary["reason"]
    assert runner.labels == FULL_CUT
    scratch = root / ".scratch" / "release" / VERSION
    assert runner.argv_of("pytest") == [
        ("python", "-m", "pytest", "-q", "-rfE", "-n", "12", f"--basetemp={scratch / 'b'}", "tests")
    ]
    assert runner.argv_of("wsl pytest") == [
        (
            "wsl",
            "-e",
            "sh",
            "-c",
            f"cd -- {LINUX_SCRATCH}/tree || exit 97; exec env PYTHONPATH=src python3 -m pytest "
            f"-q -rfE -p no:cacheprovider -o pythonpath= -n 12 --basetemp={WSL_BASETEMP} tests",
        )
    ]
    assert _detail(summary, "host_tests") == "12 passed in 1.00s with 12 worker(s) on 16 core(s)"
    assert _detail(summary, "wsl_tests") == "WSL suite passed: 12 passed in 1.00s"
    assert not (root / HOST_RERUN_LOG).exists() and not (root / WSL_RERUN_LOG).exists()


def test_a_host_node_that_passes_alone_continues_the_cut_and_the_report_names_it(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, {"pytest": _full_then_alone(HOST_FAILURE, PASSED_ALONE)})

    code, summary, _, _ = _cut(root, "--push", runner=runner)

    assert code == 0, summary["reason"]
    assert _statuses(summary)["host_tests"] == "ok"
    assert _detail(summary, "host_tests") == (
        "1 failed, 11 passed in 1.00s with 12 worker(s) on 16 core(s); "
        f"1 failed node(s) passed alone: {HOST_NODE}"
    )
    assert (root / HOST_LOG).is_file() and (root / HOST_RERUN_LOG).is_file()
    after = FULL_CUT.index("pytest") + 1
    assert runner.labels == [*FULL_CUT[:after], "pytest", *FULL_CUT[after:]]
    full, rerun = runner.argv_of("pytest")
    assert full[full.index("-n") + 1] == "12" and full[-1] == "tests"
    scratch = root / ".scratch" / "release" / VERSION
    assert rerun == (
        "python",
        "-m",
        "pytest",
        "-q",
        "-rfE",
        "-n",
        "0",
        f"--basetemp={scratch / 'br'}",
        HOST_NODE,
    )


@pytest.mark.parametrize("exit_code", [1, 2, 4, 5])
def test_a_host_node_that_does_not_pass_alone_stops_the_cut_and_names_it_and_both_logs(
    tmp_path: Path, exit_code: int
) -> None:
    root = _repo(tmp_path)
    alone = Completed(exit_code, f"{SUMMARY_HEADER}\nFAILED {HOST_NODE} - assert 0\n1 failed\n")
    runner = FakeRunner(root, {"pytest": _full_then_alone(HOST_FAILURE, alone)})

    code, summary, _, _ = _cut(root, "--push", runner=runner)

    assert code == 1 and summary["failed_step"] == "host_tests"
    assert HOST_NODE in summary["reason"]
    assert HOST_LOG in summary["reason"] and HOST_RERUN_LOG in summary["reason"]
    # Exactly one rerun, and nothing of the cut ran after it.
    assert runner.labels == [*FULL_CUT[: FULL_CUT.index("pytest") + 1], "pytest"]


@pytest.mark.parametrize(
    ("response", "last_line"),
    [
        (
            Completed(
                2,
                f"{SUMMARY_HEADER}\nERROR {HOST_NODE}\n"
                "!!! Interrupted: 1 error during collection !!!\n",
            ),
            "!!! Interrupted: 1 error during collection !!!",
        ),
        (Completed(1, "", "Fatal Python error: boom\n"), "Fatal Python error: boom"),
        (_many_failures(OVER_THE_CAP), f"{OVER_THE_CAP} failed in 1.00s"),
    ],
    ids=["exit_2", "exit_1_without_a_node_id", "over_the_cap"],
)
def test_a_host_failure_load_cannot_explain_makes_one_pytest_call_and_fails_as_before(
    tmp_path: Path, response: Any, last_line: str
) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, {"pytest": response})

    code, summary, _, _ = _cut(root, "--push", runner=runner)

    assert code == 1 and summary["failed_step"] == "host_tests"
    assert summary["reason"] == f"pytest exited {response.returncode}: {last_line} (log {HOST_LOG})"
    assert runner.labels == FULL_CUT[: FULL_CUT.index("pytest") + 1]
    assert not (root / HOST_RERUN_LOG).exists()


def test_a_wsl_node_that_passes_alone_continues_the_cut_and_the_report_names_it(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, {"wsl pytest": _full_then_alone(WSL_FAILURE, PASSED_ALONE)})

    code, summary, _, _ = _cut(root, "--push", runner=runner)

    assert code == 0, summary["reason"]
    assert _statuses(summary)["wsl_tests"] == "ok"
    assert _detail(summary, "wsl_tests") == (
        f"1 WSL failure(s) passed alone: {WSL_NODE}; "
        "0 failure(s) in the baseline: 1 failed, 4 passed in 1.00s"
    )
    assert (root / WSL_LOG).is_file() and (root / WSL_RERUN_LOG).is_file()
    # The rerun needs the tree, so it runs after the full run and before the final rm.
    after = FULL_CUT.index("wsl pytest") + 1
    assert runner.labels == [*FULL_CUT[:after], "wsl pytest", *FULL_CUT[after:]]
    full, rerun = runner.argv_of("wsl pytest")
    assert _guarded(full)[1][-1] == "tests"
    assert _guarded(rerun) == (
        f"{LINUX_SCRATCH}/tree",
        [
            "env",
            "PYTHONPATH=src",
            "python3",
            "-m",
            "pytest",
            "-q",
            "-rfE",
            "-p",
            "no:cacheprovider",
            "-o",
            "pythonpath=",
            "-n",
            "0",
            f"--basetemp={WSL_BASETEMP}",
            WSL_NODE,
        ],
    )


@pytest.mark.parametrize("exit_code", [1, 4, 5, 97])
def test_a_wsl_node_that_does_not_pass_alone_stops_the_cut_with_todays_text_and_both_logs(
    tmp_path: Path, exit_code: int
) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, {"wsl pytest": _full_then_alone(WSL_FAILURE, Completed(exit_code))})

    code, summary, _, _ = _cut(root, "--push", runner=runner)

    assert code == 1 and summary["failed_step"] == "wsl_tests"
    assert summary["reason"] == (
        f"1 WSL failure(s) not in the baseline: {WSL_NODE} (logs {WSL_LOG}, {WSL_RERUN_LOG})"
    )
    after = FULL_CUT.index("wsl pytest") + 1
    assert runner.labels == [*FULL_CUT[:after], "wsl pytest", "wsl rm"]


def test_only_the_wsl_nodes_outside_the_baseline_are_rerun(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    baseline = tmp_path / "wsl-baseline.txt"
    _write(baseline, f"{WSL_NODE}\n")
    new_node = "tests/test_new.py::test_new[a b]"
    full = Completed(
        1,
        f"{SUMMARY_HEADER}\n"
        f"FAILED {WSL_NODE} - AssertionError: 0o644\n"
        f"FAILED {new_node} - assert 0\n"
        "2 failed in 1.00s\n",
    )
    runner = FakeRunner(root, {"wsl pytest": _full_then_alone(full, PASSED_ALONE)})

    code, summary, _, _ = _cut(root, "--wsl-baseline", str(baseline), runner=runner)

    assert code == 0, summary["reason"]
    _, rerun = runner.argv_of("wsl pytest")
    command = _guarded(rerun)[1]
    assert command[-1] == new_node and WSL_NODE not in command
    assert _detail(summary, "wsl_tests") == (
        f"1 WSL failure(s) passed alone: {new_node}; "
        "1 failure(s) in the baseline: 2 failed in 1.00s"
    )


def test_a_wsl_run_whose_failures_are_all_in_the_baseline_makes_one_wsl_pytest_call(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    baseline = tmp_path / "wsl-baseline.txt"
    _write(baseline, f"{WSL_NODE}\n")
    runner = FakeRunner(root, {"wsl pytest": WSL_FAILURE})

    code, summary, _, _ = _cut(root, "--wsl-baseline", str(baseline), runner=runner)

    assert code == 0, summary["reason"]
    assert len(runner.argv_of("wsl pytest")) == 1
    assert not (root / WSL_RERUN_LOG).exists()


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            Completed(
                2,
                f"{SUMMARY_HEADER}\nERROR {WSL_NODE}\n"
                "!!! Interrupted: 1 error during collection !!!\n",
            ),
            "WSL pytest exited 2: !!! Interrupted: 1 error during collection !!!",
        ),
        (
            Completed(1, "", "/usr/bin/python3: No module named pytest\n"),
            "WSL pytest exited 1 without naming a failing node id",
        ),
        (
            _many_failures(OVER_THE_CAP),
            f"{OVER_THE_CAP} WSL failure(s) not in the baseline: "
            f"{', '.join(_many_nodes(5))} (+{OVER_THE_CAP - 5} more)",
        ),
    ],
    ids=["exit_2", "exit_1_without_a_node_id", "over_the_cap"],
)
def test_a_wsl_failure_load_cannot_explain_makes_one_wsl_pytest_call_and_fails_as_before(
    tmp_path: Path, response: Any, reason: str
) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, {"wsl pytest": response})

    code, summary, _, _ = _cut(root, runner=runner)

    assert code == 1 and summary["failed_step"] == "wsl_tests"
    assert summary["reason"] == f"{reason} (log {WSL_LOG})"
    assert len(runner.argv_of("wsl pytest")) == 1
    assert runner.labels[-2:] == ["wsl pytest", "wsl rm"]
    assert not (root / WSL_RERUN_LOG).exists()


@pytest.mark.parametrize("suite", ["pytest", "wsl pytest"])
def test_exactly_the_cap_of_failed_nodes_is_still_rerun_serially(
    tmp_path: Path, suite: str
) -> None:
    root = _repo(tmp_path)
    cap = release_cut.LOAD_RERUN_MAX_NODES
    runner = FakeRunner(root, {suite: _full_then_alone(_many_failures(cap), PASSED_ALONE)})

    code, summary, _, _ = _cut(root, runner=runner)

    assert code == 0, summary["reason"]
    _, rerun = runner.argv_of(suite)
    command = _guarded(rerun)[1] if suite == "wsl pytest" else list(rerun)
    assert command[-cap:] == _many_nodes(cap)
    assert command[command.index("-n") + 1] == "0"
    detail = _detail(summary, "host_tests" if suite == "pytest" else "wsl_tests")
    assert ", ".join(_many_nodes(cap)) in detail


@pytest.mark.parametrize("suite", ["pytest", "wsl pytest"])
@pytest.mark.parametrize(
    "node",
    [
        "--collect-only",
        "../x.py::t",
        "/abs/x.py::t",
        "C:/x.py::t",
        "tests/../x.py::t",
        "tests/..\\x.py::t",
    ],
)
def test_a_node_id_outside_tests_is_never_handed_to_pytest(
    tmp_path: Path, suite: str, node: str
) -> None:
    root = _repo(tmp_path)
    failure = Completed(1, f"{SUMMARY_HEADER}\nFAILED {node} - assert 0\n1 failed in 1.00s\n")
    runner = FakeRunner(root, {suite: failure})

    code, summary, _, _ = _cut(root, runner=runner)

    assert code == 1, summary["reason"]
    assert len(runner.argv_of(suite)) == 1


def test_a_parametrized_id_may_hold_dots_and_spaces_and_is_still_rerun(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    node = "tests/test_x.py::test_x[../a b]"
    failure = Completed(1, f"{SUMMARY_HEADER}\nFAILED {node} - assert 0\n1 failed in 1.00s\n")
    runner = FakeRunner(root, {"pytest": _full_then_alone(failure, PASSED_ALONE)})

    code, summary, _, _ = _cut(root, runner=runner)

    assert code == 0, summary["reason"]
    assert runner.argv_of("pytest")[1][-1] == node


# -- push, force, dry run ---------------------------------------------------------


def test_git_push_runs_only_with_push_and_never_forces(tmp_path: Path) -> None:
    code, summary, _, runner = _cut(_repo(tmp_path / "local"))
    assert code == 0, summary["reason"]
    assert "git push" not in runner.labels
    push = next(step for step in summary["steps"] if step["name"] == "push")
    assert push["status"] == "skipped" and "--push not given" in push["detail"]

    code, summary, _, runner = _cut(_repo(tmp_path / "pushed"), "--push")
    assert code == 0, summary["reason"]
    assert runner.argv_of("git push") == [
        ("git", "push", "--atomic", "origin", "refs/heads/main", f"refs/tags/{TAG}")
    ]
    for call in runner.calls:
        assert not {"--force", "-f", "--force-with-lease", "--mirror", "--delete"} & set(call.argv)
        assert not any(token.startswith(("+", "--force")) for token in call.argv[1:])


def test_dry_run_prints_the_plan_and_runs_no_mutating_step(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

    code, summary, lines, runner = _cut(root, "--dry-run", "--push")

    assert code == 0, summary["reason"]
    assert summary["dry_run"] is True
    assert runner.labels == READ_ONLY
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    assert not (root / ".scratch").exists()
    statuses = _statuses(summary)
    assert [statuses[name] for name in PREFLIGHT] == ["ok"] * len(PREFLIGHT)
    assert [statuses[name] for name in CUT] == ["planned"] * len(CUT)
    plan = [line for line in lines if line.startswith("plan ")]
    assert [line.split()[1].rstrip(":") for line in plan] == STEP_ORDER
    assert any("git push --atomic origin refs/heads/main" in line for line in plan)


def test_dry_run_still_stops_at_the_first_failing_check(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, {"git symbolic-ref": Completed(0, "topic\n")})

    code, summary, _, _ = _cut(root, "--dry-run", runner=runner)

    assert code == 1 and summary["failed_step"] == "branch"
    assert runner.labels == ["git status", "git symbolic-ref"]
    assert {_statuses(summary)[name] for name in CUT} == {"not_run"}


@pytest.mark.parametrize("version", ["/0.13.0", "C:/0.13.0"])
def test_a_rooted_or_drive_qualified_version_fails_the_version_step_under_dry_run(
    tmp_path: Path, version: str
) -> None:
    root = _repo(tmp_path)

    code, summary, lines, runner = _cut(root, "--dry-run", version=version)

    assert code == 1 and summary["failed_step"] == "version"
    assert "MAJOR.MINOR.PATCH" in summary["reason"]
    assert {_statuses(summary)[name] for name in STEP_ORDER[1:]} == {"not_run"}
    # No plan line, so no scratch path, is built from a VERSION the version step refused.
    assert not any(line.startswith("plan ") for line in lines)
    assert lines[-1] == json.dumps(summary, sort_keys=True)
    assert runner.calls == [] and not (root / ".scratch").exists()


# -- host suite --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cores", "workers"), [(32, 24), (16, 12), (8, 6), (4, 3), (2, 1), (1, 1), (None, 1)]
)
def test_host_workers_derive_from_cpu_count_with_a_quarter_headroom(
    cores: int | None, workers: int
) -> None:
    assert release_cut.host_test_workers(cores) == workers


@pytest.mark.parametrize(("cores", "flag"), [(16, "12"), (6, "4"), (2, "0")])
def test_the_host_suite_carries_the_derived_worker_count_and_an_in_repo_basetemp(
    tmp_path: Path, cores: int, flag: str
) -> None:
    root = _repo(tmp_path)
    code, summary, _, runner = _cut(root, cpu_count=cores)
    assert code == 0, summary["reason"]

    (argv,) = runner.argv_of("pytest")
    assert argv[:3] == ("python", "-m", "pytest") and argv[-1] == "tests"
    assert argv[argv.index("-n") + 1] == flag
    (basetemp,) = [token for token in argv if token.startswith("--basetemp=")]
    assert Path(basetemp.split("=", 1)[1]).is_relative_to(root / ".scratch" / "release")
    assert (root / ".scratch" / "release" / VERSION / "host_tests.log").is_file()


# -- worktree and release commit ------------------------------------------------------


def test_a_dirty_path_outside_the_release_documents_stops_the_cut(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, {"git status": _status(" M CHANGELOG.md", "?? docs/notes.md")})

    code, summary, _, _ = _cut(root, runner=runner)

    assert code == 1 and summary["failed_step"] == "worktree"
    assert "docs/notes.md" in summary["reason"] and "CHANGELOG.md" not in summary["reason"]
    assert runner.labels == ["git status"]


def test_release_documents_and_allow_dirty_paths_may_be_dirty_but_only_the_former_commit(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    status = _status(
        " M CHANGELOG.md",
        " M README.md",
        " M vscode-extension/README.md",
        " M .aiworkhub/releases.jsonl",
        "?? docs/notes.md",
        "R  docs/new.md",
        "docs/old.md",
    )
    runner = FakeRunner(root, {"git status": status})

    code, summary, _, _ = _cut(root, "--allow-dirty", "docs/", runner=runner)

    assert code == 0, summary["reason"]
    (commit,) = runner.argv_of("git commit")
    assert commit[:5] == ("git", "commit", "-m", f"release: {VERSION}", "--")
    assert set(commit[5:]) == {
        release_metadata.CANONICAL_VERSION_FILE,
        *release_metadata.PROJECTION_FILES,
        *release_metadata.RELEASE_DOCUMENTS,
        release_cut.RELEASE_LEDGER,
    }
    assert not {"-a", "--all", "docs/notes.md", "docs/"} & set(commit)


def test_a_generated_file_must_be_clean_even_under_allow_dirty(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, {"git status": _status(" M server.json")})

    code, summary, _, _ = _cut(root, "--allow-dirty", "server.json", runner=runner)

    assert code == 1 and summary["failed_step"] == "worktree"
    assert "generated by the bump" in summary["reason"]


@pytest.mark.parametrize("raw", ["../outside.txt", "/etc/hosts", "C:/Windows", "docs/../..", "."])
def test_allow_dirty_must_name_a_path_inside_the_repository(tmp_path: Path, raw: str) -> None:
    code, summary, _, runner = _cut(_repo(tmp_path), "--allow-dirty", raw)

    assert code == 1 and summary["failed_step"] == "worktree"
    assert "not a path inside the repository" in summary["reason"]
    assert runner.calls == []


@pytest.mark.parametrize(
    "raw",
    [
        "vscode-extension",
        "vscode-extension/media/",
        "VSCode-Extension",
        "src",
        "src/aiworkhub/recipes",
    ],
)
def test_allow_dirty_may_not_cover_what_the_package_step_builds_the_vsix_from(
    tmp_path: Path, raw: str
) -> None:
    root = _repo(tmp_path)

    code, summary, lines, runner = _cut(root, "--allow-dirty", "docs", raw)

    assert code == 1 and summary["failed_step"] == "worktree"
    assert repr(raw) in summary["reason"] and "VSIX" in summary["reason"]
    # Refused before any step ran: not even the read-only checks.
    statuses = _statuses(summary)
    assert statuses.pop("worktree") == "failed" and set(statuses.values()) == {"not_run"}
    assert runner.calls == [] and lines[-2] == f"FAILED  worktree: {summary['reason']}"


def test_dirty_paths_reads_porcelain_z_with_both_sides_of_a_rename() -> None:
    raw = " M CHANGELOG.md\0R  docs/new.md\0docs/old.md\0?? notes with space.md\0"
    assert release_cut.dirty_paths(raw) == [
        "CHANGELOG.md",
        "docs/new.md",
        "docs/old.md",
        "notes with space.md",
    ]
    with pytest.raises(ValueError, match="unparseable"):
        release_cut.dirty_paths("garbage\0")


def test_the_real_runner_keeps_the_nul_separators_of_git_status_z(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    # Only this repository's git: an inherited GIT_DIR must not redirect these commands.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    repo = tmp_path / "repo"
    _write(repo / "docs" / "first.txt", "first\n")
    _write(repo / "notes" / "second.txt", "second\n")
    init = release_cut.run_subprocess(["git", "init", "--quiet"], cwd=repo, env=env, timeout=120)
    assert init.returncode == 0, init.output

    status = release_cut.run_subprocess(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo,
        env=env,
        timeout=120,
    )

    assert status.returncode == 0, status.output
    assert status.stdout.split("\0") == ["?? docs/first.txt", "?? notes/second.txt", ""]
    # --allow-dirty docs covers the first path only, so the second still refuses the cut.
    host = release_cut.Host(
        root=repo,
        runner=release_cut.run_subprocess,
        python=sys.executable,
        cpu_count=1,
        env=env,
        is_windows=False,
        out=lambda line: None,
    )
    cut = release_cut.ReleaseCut(release_cut.Options(VERSION, allow_dirty=("docs",)), host)
    with pytest.raises(release_cut.StepFailed) as refused:
        cut._step_worktree()
    assert str(refused.value) == (
        "dirty outside the release documents and --allow-dirty: notes/second.txt"
    )


def test_only_wsl_output_drops_its_nuls() -> None:
    assert release_cut._decode(b"?? a\0?? b\0", wsl=False) == "?? a\0?? b\0"
    assert release_cut._decode("no distribution\n".encode("utf-16-le"), wsl=True) == (
        "no distribution\n"
    )
    for program in ("wsl", "wsl.exe", "WSL.EXE", "C:\\Windows\\System32\\wsl.exe"):
        assert release_cut._is_wsl(program), program
    for program in ("git", "npm", "C:\\Program Files\\Git\\cmd\\git.exe"):
        assert not release_cut._is_wsl(program), program


def test_the_receipt_is_release_receipt_record_of_the_packaged_vsix(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    code, summary, _, runner = _cut(root)
    assert code == 0, summary["reason"]
    assert runner.argv_of("receipt record") == [
        (
            "python",
            "scripts/release_receipt.py",
            "record",
            "--version",
            VERSION,
            "--vsix",
            VSIX,
            "--repo-root",
            str(root),
        )
    ]
    assert runner.labels.index("receipt record") > runner.labels.index("wsl pytest")


# -- single definitions ------------------------------------------------------------------


def test_projection_files_are_the_files_projected_versions_reads(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    read = {key.split("#", 1)[0] for key in release_metadata.projected_versions(root)}
    assert read == set(release_metadata.PROJECTION_FILES)
    assert release_metadata.check(root)["canonical_source"] == release_metadata.CANONICAL_VERSION_FILE


def test_the_release_ledger_is_the_one_release_receipt_appends_to() -> None:
    sdlc_deploy_proof = pytest.importorskip("aiworkhub.sdlc_deploy_proof")
    assert release_cut.RELEASE_LEDGER == "/".join(sdlc_deploy_proof.LEDGER_REL)


def test_the_packaged_roots_are_the_trees_package_vsix_copies_from() -> None:
    script = ROOT / "vscode-extension" / "test" / "package-vsix.js"
    if not script.is_file():
        pytest.skip("vscode-extension/test/package-vsix.js is not in this checkout")
    source = script.read_text(encoding="utf-8")
    # The extension's files are copied from vscode-extension, the runtime from src/aiworkhub.
    assert 'const PY_RUNTIME_SRC = path.join(root, "..", "src", "aiworkhub");' in source
    assert release_cut.PACKAGED_ROOTS == (release_cut.EXTENSION_DIR, "src/aiworkhub")
