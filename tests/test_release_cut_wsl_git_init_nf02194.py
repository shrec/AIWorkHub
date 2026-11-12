"""NF-2026-02194: the release cut's WSL tree is a git repository before its suite runs.

``_step_wsl_tests`` runs the suite on an extracted ``git archive`` of HEAD, which
carries no .git, so the suite's tests that run git against the checkout (the 18 of
tests/test_retry_finalization_host_lane_nf01030.py) failed as noise on every cut.
The step now runs ``git init -q``, ``git add -A`` and ``git commit -q -m
release-tree`` in the extracted tree, after the extraction and before pytest,
through the same guarded WSL invocation as its other commands and with the
identity given by -c, so WSL needs no git config. A failing git command fails
the step and names its stderr.

No test here runs WSL or git: the step's command runner, ``Host.runner``, is a
fake that records every command and answers it.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "aiworkhub_release_cut_nf02194", ROOT / "scripts" / "release_cut.py"
)
assert SPEC is not None and SPEC.loader is not None
release_cut = importlib.util.module_from_spec(SPEC)
# Its dataclasses resolve their string annotations through sys.modules.
sys.modules[SPEC.name] = release_cut
SPEC.loader.exec_module(release_cut)
Completed = release_cut.Completed

VERSION = "0.12.98"
# What the fake wslpath answers: the repository root as WSL mounts it.
LINUX_ROOT = "/mnt/z/repo"
SCRATCH = f"{LINUX_ROOT}/.scratch/release/{VERSION}"
TREE = f"{SCRATCH}/tree"
IDENTITY = "-c user.name=release -c user.email=release@localhost"
# The three git commands as the sh -c text WSL runs: each inside the tree's cd guard.
GIT_IN_TREE = (
    f"cd -- {TREE} || exit 97; exec git init -q",
    f"cd -- {TREE} || exit 97; exec git {IDENTITY} add -A",
    f"cd -- {TREE} || exit 97; exec git {IDENTITY} commit -q -m release-tree",
)
EXTRACT = release_cut._wsl_guarded_script(
    PurePosixPath(SCRATCH), ("tar", "-xf", f"{SCRATCH}/head.tar", "-C", TREE)
)
CLEAR = release_cut._wsl_guarded_script(
    PurePosixPath(SCRATCH),
    ("rm", "-rf", "--", TREE, f"{SCRATCH}/head.tar", f"/tmp/aiworkhub-release-{VERSION}"),
)


def _command(script: str) -> str:
    """The command a guarded script execs once inside its directory."""

    return script.partition(" || exit 97; exec ")[2]


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str] | None
    timeout: float | None

    @property
    def script(self) -> str | None:
        """The ``sh -c`` text of a guarded WSL command, else None."""

        if len(self.argv) == 5 and self.argv[:4] == ("wsl", "-e", "sh", "-c"):
            return self.argv[4]
        return None


class RecordingRunner:
    """The step's command runner: records every call and answers it.

    ``answers`` maps the sh -c text of a guarded WSL command to what that command
    returns; every other command gets a passing default.
    """

    def __init__(self, root: Path, answers: dict[str, Any] | None = None) -> None:
        self.root = root
        self.answers = dict(answers or {})
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
        if call.argv[:2] == ("git", "archive"):
            output = next(token for token in call.argv if token.startswith("--output="))
            Path(output.split("=", 1)[1]).write_bytes(b"tar")
            return Completed(0)
        assert call.argv[0] == "wsl", f"the step ran an unexpected command: {call.argv}"
        if call.argv[1:3] == ("-e", "wslpath"):
            windows = Path(call.argv[-1])
            return Completed(0, f"{LINUX_ROOT}/{windows.relative_to(self.root).as_posix()}\n")
        script = call.script
        assert script is not None, f"a WSL command outside a cd guard: {call.argv}"
        if script in self.answers:
            return self.answers[script]
        if " -m pytest " in script:
            return Completed(0, "........\n8 passed in 1.00s\n")
        return Completed(0)

    @property
    def scripts(self) -> list[str | None]:
        return [call.script for call in self.calls]


def _cut(root: Path, runner: RecordingRunner) -> tuple[Any, list[str]]:
    lines: list[str] = []
    host = release_cut.Host(
        root=root,
        runner=runner,
        python="python",
        cpu_count=4,
        env={"PATH": "fake-bin"},
        is_windows=True,
        out=lines.append,
    )
    return release_cut.ReleaseCut(release_cut.Options(version=VERSION), host), lines


def test_git_init_add_and_commit_run_in_the_extracted_tree_before_pytest(tmp_path: Path) -> None:
    runner = RecordingRunner(tmp_path)
    cut, _ = _cut(tmp_path, runner)

    result = cut._run_step("wsl_tests")

    assert result.status == "ok", result.detail
    assert result.detail == "WSL suite passed: 8 passed in 1.00s"
    scripts = runner.scripts
    extract = scripts.index(EXTRACT)
    suite = scripts.index(
        release_cut._wsl_guarded_script(PurePosixPath(TREE), cut._wsl_test_command())
    )
    # Each once, in this order, right after the extraction and right before the suite.
    git = [scripts.index(script) for script in GIT_IN_TREE]
    assert [extract, *git, suite] == list(range(extract, extract + 5))
    assert [scripts.count(script) for script in GIT_IN_TREE] == [1, 1, 1]
    assert scripts[-1] == CLEAR
    # The same invocation as every other WSL command of the step.
    for index in git:
        call = runner.calls[index]
        assert call.cwd == tmp_path
        assert call.env is not None and call.env["MSYS_NO_PATHCONV"] == "1"
    # --dry-run names them as well.
    plan = cut._plan("wsl_tests")
    assert all(_command(script) in plan for script in GIT_IN_TREE)


@pytest.mark.parametrize(
    ("failing", "answer"),
    [
        pytest.param(
            0,
            Completed(
                128,
                "",
                "error: could not lock config file .git/config: Permission denied\n"
                "fatal: could not set 'core.repositoryformatversion' to '0'\n",
            ),
            id="init",
        ),
        pytest.param(
            1,
            Completed(
                128,
                "",
                f"fatal: detected dubious ownership in repository at '{TREE}'\n"
                "To add an exception for this directory, call:\n"
                "\n"
                f"\tgit config --global --add safe.directory {TREE}\n",
            ),
            id="add",
        ),
        pytest.param(
            2,
            Completed(
                128,
                "",
                "error: gpg failed to sign the data\nfatal: failed to write commit object\n",
            ),
            id="commit",
        ),
    ],
)
def test_a_failing_git_command_fails_the_step_with_its_stderr(
    tmp_path: Path, failing: int, answer: Any
) -> None:
    script = GIT_IN_TREE[failing]
    runner = RecordingRunner(tmp_path, {script: answer})
    cut, lines = _cut(tmp_path, runner)

    result = cut._run_step("wsl_tests")

    assert result.status == "failed"
    assert result.detail.startswith(f"{_command(script)} in the WSL tree failed (exit 128): ")
    # The whole stderr, so the line that names the cause too, not only the last line.
    stderr = [line.strip() for line in answer.stderr.splitlines() if line.strip()]
    assert len(stderr) >= 2 and all(line in result.detail for line in stderr)
    assert lines[-1] == f"FAILED  wsl_tests: {result.detail}"
    # Not ignored: the git commands before it ran after the extraction, and after it
    # only the cleanup did, so neither a later git command nor the suite.
    scripts = runner.scripts
    at = scripts.index(script)
    assert scripts[at - failing - 1 : at + 1] == [EXTRACT, *GIT_IN_TREE[: failing + 1]]
    assert scripts[at + 1 :] == [CLEAR]
    assert not any(text is not None and " -m pytest " in text for text in scripts)


def test_a_git_failure_without_stderr_names_the_last_line_of_its_output(tmp_path: Path) -> None:
    script = GIT_IN_TREE[2]
    nothing = 'nothing to commit (create/copy files and use "git add" to track)'
    answer = Completed(1, f"On branch master\n\nInitial commit\n\n{nothing}\n", "")
    runner = RecordingRunner(tmp_path, {script: answer})
    cut, _ = _cut(tmp_path, runner)

    result = cut._run_step("wsl_tests")

    assert result.status == "failed"
    assert result.detail == f"{_command(script)} in the WSL tree failed (exit 1): {nothing}"
    assert runner.scripts[-1] == CLEAR
