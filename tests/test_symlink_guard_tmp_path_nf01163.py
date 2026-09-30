"""NF-2026-01163: the symlink capability guard must not skip ``tmp_path`` tests.

``tests/conftest.py`` wraps ``os.symlink``/``Path.symlink_to`` so a sandbox
capability denial becomes an explicit ``sandbox_capability_denied:symlink``
skip (NF-2026-01150). pytest itself calls ``Path.symlink_to`` while building
every ``tmp_path`` (``_pytest.pathlib._force_symlink``, which guards it with
``except Exception``), so a wrapper that raised ``Skipped`` -- a
``BaseException`` -- escaped that handler and EVERY test that merely requested
``tmp_path`` was reported skipped while the session still exited 0.

The outcomes asserted here are read out of an INNER pytest session run as a
subprocess. That forces the symlink-incapable branch on any host without
patching this session's own primitives, so the assertions are identical on a
symlink-capable host and inside the incapable worker sandbox. The inner
conftest imports the real guard out of ``tests/conftest.py``, so what runs is
the shipped guard rather than a copy of it.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple

import pytest

import conftest

GUARD_CONFTEST = Path(__file__).resolve().parent / "conftest.py"

TMP_PATH_ONLY = "test_tmp_path_only_is_never_skipped"
OS_SYMLINK_DENIED = "test_direct_os_symlink_denial"
SYMLINK_TO_DENIED = "test_direct_symlink_to_denial"
NON_DENIAL_OSERROR = "test_non_denial_oserror"

# No addopts: the inner session must not inherit this repo's pytest options.
_INNER_INI = """[pytest]
addopts =
"""

_INNER_CONFTEST = '''"""Inner session: the real guard over symlink primitives that always fail."""

from __future__ import annotations

import errno
import importlib.util
import os
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "nf01163_guard", os.environ["NF01163_GUARD_CONFTEST"]
)
guard = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = guard
_spec.loader.exec_module(guard)


def _denial(link) -> OSError:
    """The error the simulated syscall reports for ``link``.

    A missing parent directory is a real filesystem error rather than a
    capability denial, which is how the ENOENT case reaches a test.
    """

    code = errno.EPERM if Path(link).parent.is_dir() else errno.ENOENT
    return OSError(code, os.strerror(code), str(link))


def _deny_os_symlink(src, dst, *_args, **_kwargs):
    raise _denial(dst)


def _deny_symlink_to(self, target, *_args, **_kwargs):
    raise _denial(self)


# Installed BEFORE the guard captures its originals, so these are what the
# wrappers wrap. The probe is pinned as well, so the incapable branch is taken
# on a symlink-capable host too.
os.symlink = _deny_os_symlink
Path.symlink_to = _deny_symlink_to
guard.can_create_symlink = lambda: False

# Re-exported so the inner session installs exactly the guard under test.
_skip_on_symlink_capability_denied = guard._skip_on_symlink_capability_denied
pytest_runtest_makereport = guard.pytest_runtest_makereport
'''

_INNER_TEST = '''"""The outcomes the guard owes inside a symlink-incapable session."""

from __future__ import annotations

import os


def test_tmp_path_only_is_never_skipped(tmp_path):
    # pytest builds tmp_path through Path.symlink_to, so the guard must not
    # turn a test that only requests it into a skip.
    assert tmp_path.is_dir()


def test_direct_os_symlink_denial(tmp_path):
    os.symlink(tmp_path / "target", tmp_path / "link")


def test_direct_symlink_to_denial(tmp_path):
    (tmp_path / "link").symlink_to(tmp_path / "target")


def test_non_denial_oserror(tmp_path):
    # ENOENT from the missing parent is not a capability denial.
    os.symlink(tmp_path / "target", tmp_path / "absent" / "link")
'''


class _InnerSession(NamedTuple):
    """One inner session: each test's outcome plus its output for diagnostics."""

    outcomes: dict[str, tuple[str, str]]
    output: str


def _outcome_of(case: ET.Element) -> tuple[str, str]:
    for kind in ("error", "failure", "skipped"):
        node = case.find(kind)
        if node is not None:
            return kind, node.get("message") or ""
    return "passed", ""


def _run_inner_session(work: Path) -> _InnerSession:
    # One-letter directory names: this path is a prefix of the inner session's
    # own tmp_path tree, which has Windows' MAX_PATH to live within.
    inner = work / "i"
    inner.mkdir()
    (inner / "pytest.ini").write_text(_INNER_INI, encoding="utf-8")
    (inner / "conftest.py").write_text(_INNER_CONFTEST, encoding="utf-8")
    (inner / "test_inner_guard.py").write_text(_INNER_TEST, encoding="utf-8")
    temproot = work / "t"
    temproot.mkdir()
    report = work / "report.xml"

    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    env["NF01163_GUARD_CONFTEST"] = str(GUARD_CONFTEST)
    env["PYTEST_DEBUG_TEMPROOT"] = str(temproot)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "-q",
            f"--junitxml={report}",
            "test_inner_guard.py",
        ],
        cwd=inner,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=600,
    )
    output = completed.stdout or ""
    assert report.is_file(), (
        "the inner pytest session wrote no JUnit report "
        f"(returncode={completed.returncode}):\n{output}"
    )
    outcomes = {
        case.get("name") or "": _outcome_of(case)
        for case in ET.parse(report).iter("testcase")
    }
    expected = {
        TMP_PATH_ONLY,
        OS_SYMLINK_DENIED,
        SYMLINK_TO_DENIED,
        NON_DENIAL_OSERROR,
    }
    assert expected <= set(outcomes), (
        f"the inner session reported {sorted(outcomes)}, expected "
        f"{sorted(expected)}:\n{output}"
    )
    return _InnerSession(outcomes=outcomes, output=output)


@pytest.fixture(scope="module")
def inner(tmp_path_factory: pytest.TempPathFactory) -> _InnerSession:
    """One inner session shared by every assertion below."""

    return _run_inner_session(tmp_path_factory.mktemp("nf01163"))


def test_a_tmp_path_only_test_still_passes_under_the_denial(
    inner: _InnerSession,
) -> None:
    outcome, message = inner.outcomes[TMP_PATH_ONLY]

    assert outcome == "passed", f"{message}\n{inner.output}"


def test_direct_os_symlink_denial_is_skipped_with_the_capability_reason(
    inner: _InnerSession,
) -> None:
    outcome, message = inner.outcomes[OS_SYMLINK_DENIED]

    assert outcome == "skipped", f"{message}\n{inner.output}"
    assert conftest.SYMLINK_CAPABILITY_DENIED_REASON in message


def test_direct_symlink_to_denial_is_skipped_with_the_capability_reason(
    inner: _InnerSession,
) -> None:
    outcome, message = inner.outcomes[SYMLINK_TO_DENIED]

    assert outcome == "skipped", f"{message}\n{inner.output}"
    assert conftest.SYMLINK_CAPABILITY_DENIED_REASON in message


def test_a_non_denial_oserror_from_a_symlink_call_still_fails(
    inner: _InnerSession,
) -> None:
    outcome, message = inner.outcomes[NON_DENIAL_OSERROR]

    assert outcome == "failure", f"{message}\n{inner.output}"
    assert os.strerror(errno.ENOENT) in message
    assert "sandbox_capability_denied" not in message
