"""Run the POSIX test suite against a committed ref and return a compact verdict.

Windows skips the POSIX-only tests and CI runs with ``-x``, so before a release
tag the suite has to be replayed on Linux. This module does that mechanically:
``git archive`` of one commit goes into a fresh temporary directory OUTSIDE the
repository, is extracted there (inside WSL on a Windows host), and
``<interpreter> -m pytest -q -p no:cacheprovider -o pythonpath=`` runs with
``PYTHONPATH=src``. It tests the committed ref only: it never reads an
uncommitted working-tree file and never writes inside the repository. The
temporary directory is removed on success and on failure.

Every subprocess is an argv list; nothing here uses a shell.

Output: one JSON object with ``state`` (``passed`` | ``failed`` |
``unavailable`` | ``error``), the resolved commit, pytest's summary counts, at
most 50 failing node ids and a 4000-character tail. Exit 0 when the suite was
run to a verdict (``passed``/``failed``), exit 2 otherwise.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..platform_io import is_macos, is_windows
from ._common import emit

SCHEMA_ID = "aiworkhub.posix_replay.v1"
MAX_FAILING_TESTS = 50
MAX_FAILING_ID_CHARS = 300
TAIL_CHARS = 4000
GIT_TIMEOUT_SECONDS = 120.0
SETUP_TIMEOUT_SECONDS = 300.0
# The recipe framework caps a run at 1800s; leave room to clean up and emit.
PYTEST_TIMEOUT_SECONDS = 1650.0
NO_VALUE = "all"

_SUMMARY = re.compile(
    r"^(?P<body>no tests ran|\d+ [a-z]+(?:, \d+ [a-z]+)*) in [0-9.]+s(?: \(.*\))?$"
)
_COUNT_KEYS = {
    "passed": "passed",
    "failed": "failed",
    "skipped": "skipped",
    "error": "errors",
    "errors": "errors",
}
_WSL_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


class Refusal(Exception):
    """A stable ``state``/``reason`` pair that ends the run."""

    def __init__(self, state: str, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Completed:
    rc: int | None
    out: str
    err: str
    timed_out: bool = False


def _is_windows() -> bool:
    from aiworkhub.platform_io import is_windows

    return is_windows()


def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    # wsl.exe reports its own errors in UTF-16LE (every other byte NUL); a
    # stray NUL in UTF-8 pytest output must not flip the whole decode.
    if raw.count(b"\x00") * 3 >= len(raw):
        try:
            return raw.decode("utf-16-le").replace("\x00", "")
        except UnicodeDecodeError:
            pass
    return raw.replace(b"\x00", b"").decode("utf-8", "replace")


def run_argv(
    argv: list[str],
    *,
    timeout: float,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> Completed:
    """Run one argv list (never a shell) and capture decoded output."""
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return Completed(None, _decode(exc.stdout), _decode(exc.stderr), timed_out=True)
    return Completed(done.returncode, _decode(done.stdout), _decode(done.stderr))


def pytest_argv(interpreter: str, tests: list[str]) -> list[str]:
    return [
        interpreter,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-o",
        "pythonpath=",
        *(tests or ["tests"]),
    ]


def wsl_argv(user: str | None, cd: str, inner: list[str]) -> list[str]:
    return ["wsl", *(["-u", user] if user else []), "--cd", cd, "-e", *inner]


def parse_summary(text: str) -> dict[str, Any]:
    """Counts from pytest's final ``N passed, M failed in 1.2s`` line."""
    counts: dict[str, Any] = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    for line in reversed(text.splitlines()):
        match = _SUMMARY.match(line.strip("= \t\r"))
        if match is None:
            continue
        for number, word in re.findall(r"(\d+) ([a-z]+)", match.group("body")):
            if word in _COUNT_KEYS:
                counts[_COUNT_KEYS[word]] += int(number)
        return {**counts, "found": True}
    return {**counts, "found": False}


def parse_failing(text: str) -> list[str]:
    """Node ids from the ``FAILED``/``ERROR`` short-summary lines, capped."""
    found: list[str] = []
    for line in text.splitlines():
        if not line.startswith(("FAILED ", "ERROR ")):
            continue
        node = line.split(" ", 1)[1].split(" - ", 1)[0].strip()
        if node and node not in found:
            found.append(node[:MAX_FAILING_ID_CHARS])
        if len(found) >= MAX_FAILING_TESTS:
            break
    return found


def parse_tests(raw: str) -> list[str]:
    tests = [] if raw.strip() in ("", NO_VALUE) else raw.split()
    for entry in tests:
        pure = PurePosixPath(entry)
        if (
            entry.startswith("-")
            or "\\" in entry
            or pure.is_absolute()
            or ".." in pure.parts
            or ":" in entry.split("::", 1)[0]
        ):
            raise Refusal("error", "invalid_tests", entry[:MAX_FAILING_ID_CHARS])
    return tests


def _check_interpreter(interpreter: str) -> str:
    if not interpreter or interpreter.startswith("-") or any(ch.isspace() for ch in interpreter):
        raise Refusal("error", "invalid_interpreter", interpreter[:80])
    return interpreter


def _check_wsl_user(raw: str) -> str | None:
    if raw.strip() in ("", NO_VALUE):
        return None
    if not _WSL_USER.match(raw):
        raise Refusal("error", "invalid_wsl_user", raw[:80])
    return raw


def _git(top: Path | None, *args: str) -> Completed:
    argv = ["git", *(["-C", str(top)] if top else []), *args]
    try:
        return run_argv(argv, timeout=GIT_TIMEOUT_SECONDS)
    except OSError as exc:
        raise Refusal("error", "git_unavailable", str(exc)) from exc


def resolve_commit(ref: str) -> tuple[Path, str]:
    """``(repository top-level, commit sha)`` for ``ref`` in the current repository."""
    if not ref or ref.startswith("-") or any(ch.isspace() for ch in ref):
        raise Refusal("error", "invalid_ref", ref[:80])
    top = _git(None, "rev-parse", "--show-toplevel")
    if top.rc != 0 or not top.out.strip():
        raise Refusal("error", "not_a_git_repository", top.err[:400])
    root = Path(top.out.strip())
    sha = _git(root, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    value = sha.out.strip()
    if sha.rc != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise Refusal("error", "ref_not_found", ref[:80])
    return root, value


def export_archive(root: Path, sha: str, target: Path) -> None:
    done = _git(root, "archive", "--format=tar", "-o", str(target), sha)
    if done.rc != 0 or not target.is_file():
        raise Refusal("error", "git_archive_failed", done.err[:400])


def safe_extract(archive: Path, dest: Path) -> None:
    """Extract ``archive`` into ``dest``, refusing any member that escapes it."""
    base = dest.resolve()
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        for member in members:
            target = (base / member.name).resolve()
            if target != base and base not in target.parents:
                raise Refusal("error", "archive_member_escapes", member.name[:200])
            if member.issym() or member.islnk():
                link = (target.parent / member.linkname).resolve()
                if member.islnk():
                    link = (base / member.linkname).resolve()
                if link != base and base not in link.parents:
                    raise Refusal("error", "archive_link_escapes", member.name[:200])
        extra = {"filter": "fully_trusted"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(base, members=members, **extra)


def remove_tree(path: Path) -> bool:
    """Delete ``path`` even if a test left read-only directories behind."""
    for current, dirs, _files in os.walk(path):
        for name in [".", *dirs]:
            try:
                os.chmod(Path(current) / name, 0o700)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def _wsl(user: str | None, cd: str, inner: list[str], timeout: float) -> Completed:
    try:
        return run_argv(wsl_argv(user, cd, inner), timeout=timeout)
    except OSError as exc:
        raise Refusal("unavailable", "wsl_not_runnable", str(exc)) from exc


def _wsl_failure(done: Completed, reason: str) -> Refusal:
    detail = (done.err or done.out)[-400:]
    return Refusal("unavailable", "wsl_timeout" if done.timed_out else reason, detail)


def _pytest_native(tree: Path, interpreter: str, tests: list[str]) -> Completed:
    env = {**os.environ, "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        return run_argv(
            pytest_argv(interpreter, tests),
            cwd=tree,
            env=env,
            timeout=PYTEST_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        raise Refusal("error", "interpreter_unavailable", str(exc)) from exc


def _verdict(done: Completed) -> dict[str, Any]:
    text = done.out + ("\n" if done.out and done.err else "") + done.err
    summary = parse_summary(text)
    payload: dict[str, Any] = {
        **{key: summary[key] for key in ("passed", "failed", "skipped", "errors")},
        "failing_tests": parse_failing(text),
        "tail": text[-TAIL_CHARS:],
    }
    if done.timed_out:
        return {**payload, "state": "error", "reason": "pytest_timeout"}
    if not summary["found"]:
        reason = "interpreter_unavailable" if done.rc == 127 else "no_pytest_summary"
        return {**payload, "state": "error", "reason": reason}
    if summary["failed"] or summary["errors"]:
        return {**payload, "state": "failed"}
    if done.rc == 0:
        return {**payload, "state": "passed"}
    reason = "no_tests_collected" if done.rc == 5 else f"pytest_exit_{done.rc}"
    return {**payload, "state": "error", "reason": reason}


def _run_in_wsl(
    archive: Path, scratch: Path, user: str | None, interpreter: str, tests: list[str]
) -> dict[str, Any]:
    made = _wsl(user, "~", ["mktemp", "-d"], SETUP_TIMEOUT_SECONDS)
    lines = [line.strip() for line in made.out.splitlines() if line.strip()]
    directory = lines[-1] if lines else ""
    if made.rc != 0 or not directory.startswith("/"):
        raise _wsl_failure(made, "wsl_error")
    try:
        unpacked = _wsl(
            user,
            str(scratch),
            ["tar", "-xf", archive.name, "-C", directory],
            SETUP_TIMEOUT_SECONDS,
        )
        if unpacked.rc != 0:
            raise _wsl_failure(unpacked, "wsl_extract_failed")
        inner = ["env", "PYTHONPATH=src", *pytest_argv(interpreter, tests)]
        done = _wsl(user, directory, inner, PYTEST_TIMEOUT_SECONDS)
        if done.rc in (-1, 0xFFFFFFFF):
            raise _wsl_failure(done, "wsl_error")
        return _verdict(done)
    finally:
        try:
            _wsl(user, "~", ["rm", "-rf", "--", directory], SETUP_TIMEOUT_SECONDS)
        except Refusal:
            pass


def replay(ref: str, tests_raw: str, interpreter: str, wsl_user_raw: str) -> dict[str, Any]:
    started = time.monotonic()
    payload: dict[str, Any] = {
        "schema_id": SCHEMA_ID,
        "state": "error",
        "ref": ref,
        "commit_sha": None,
        "platform": "windows" if is_windows() else "macos" if is_macos() else "linux",
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "failing_tests": [],
        "tail": "",
    }
    scratch: Path | None = None
    try:
        tests = parse_tests(tests_raw)
        interpreter = _check_interpreter(interpreter)
        user = _check_wsl_user(wsl_user_raw)
        windows = _is_windows()
        if windows and shutil.which("wsl") is None:
            raise Refusal("unavailable", "wsl_not_found")
        root, sha = resolve_commit(ref)
        payload["commit_sha"] = sha
        scratch = Path(tempfile.mkdtemp(prefix="aiworkhub-posix-replay-")).resolve()
        if scratch == root.resolve() or root.resolve() in scratch.parents:
            raise Refusal("error", "temp_inside_repository", str(scratch))
        archive = scratch / "archive.tar"
        export_archive(root, sha, archive)
        if windows:
            payload.update(_run_in_wsl(archive, scratch, user, interpreter, tests))
        else:
            tree = scratch / "tree"
            tree.mkdir()
            safe_extract(archive, tree)
            payload.update(_verdict(_pytest_native(tree, interpreter, tests)))
    except Refusal as refusal:
        payload.update(state=refusal.state, reason=refusal.reason)
        if refusal.detail:
            payload["tail"] = refusal.detail[-TAIL_CHARS:]
    except (OSError, tarfile.TarError) as exc:
        payload.update(state="error", reason="replay_failed", tail=str(exc)[-TAIL_CHARS:])
    finally:
        if scratch is not None:
            payload["temp_removed"] = remove_tree(scratch)
    payload["duration_seconds"] = round(time.monotonic() - started, 3)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--tests", default=NO_VALUE)
    parser.add_argument("--interpreter", default="python3")
    parser.add_argument("--wsl-user", default=NO_VALUE)
    args = parser.parse_args(argv)
    payload = replay(args.ref, args.tests, args.interpreter, args.wsl_user)
    answered = payload["state"] in ("passed", "failed")
    return emit(payload, ok=answered, code=0 if answered else 2)


if __name__ == "__main__":
    sys.exit(main())
