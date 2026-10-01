"""Shared test helpers (NF-2026-00299).

A test that spawns ``sys.executable`` inside a worker's worktree gets a child
with no worktree path on ``sys.path``: the child imports ``aiworkhub`` through
the editable install -- the CANONICAL tree -- so the assertion is made against
code the candidate was supposed to change. That breaks the evidence chain in
both directions (a correct candidate can fail, and a broken candidate can pass
when the canonical tree happens to satisfy the assertion, silently).

These helpers make a spawned child import the tree under test by putting the
worktree's ``src`` ahead of everything else on ``PYTHONPATH``, plus a probe that
reports which ``aiworkhub`` a child actually loaded and an assertion that it is
beneath the worktree root.
"""

from __future__ import annotations

import errno
import functools
import multiprocessing.connection
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# A minimal child that imports aiworkhub and reports the file it resolved to,
# so the caller can prove WHICH tree the child loaded.
_CHILD_PROBE = "import aiworkhub, sys; sys.stdout.write(getattr(aiworkhub, '__file__', '') or '')"


def observed_cores() -> int:
    """Cores this process may actually run on, never a hardcoded constant.

    ``sched_getaffinity`` is the honest number under a cpuset or a container;
    ``os.cpu_count()`` is the fallback where the platform has no affinity mask.
    """

    getaffinity = getattr(os, "sched_getaffinity", None)
    if getaffinity is not None:
        try:
            return max(1, len(getaffinity(0)))
        except OSError:
            pass
    return max(1, os.cpu_count() or 1)


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    """Worker count for ``-n auto`` (NF-2026-00639).

    The count is derived from the observed core count and leaves headroom, so
    a full-suite run cannot starve the interactive MCP server sharing this
    host. Below four cores the reservation would cost more throughput than the
    headroom is worth -- and a single-worker xdist run is slower than a plain
    serial one -- so every core is used there instead.

    Returning ``None`` defers to xdist's own resolution, which is what honours
    an explicit ``PYTEST_XDIST_AUTO_NUM_WORKERS`` operator override.
    """

    if os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"):
        return None
    cores = observed_cores()
    if cores < 4:
        return cores
    return cores - max(1, cores // 8)


@pytest.fixture(autouse=True)
def deterministic_process_launch_capacity(monkeypatch):
    """Keep launcher tests independent of the CI host's instantaneous free RAM."""

    module = sys.modules.get("aiworkhub.process_launcher")
    if module is not None and hasattr(module, "_available_memory_bytes"):
        monkeypatch.setattr(
            module,
            "_available_memory_bytes",
            lambda: module.MEMORY_LAUNCH_REQUIRED_BYTES,
        )


@pytest.fixture(autouse=True)
def pinned_toolchain_authority_secret(monkeypatch):
    """A contained validation lane cannot create the on-disk authority key, so pin it."""

    monkeypatch.setenv("AIWORKHUB_TOOLCHAIN_AUTHORITY_HMAC_KEY", "hex:" + "11" * 32)


def is_beneath(path: os.PathLike[str] | str, root: os.PathLike[str] | str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True


def worktree_pythonpath_env(
    worktree_root: os.PathLike[str] | str,
    *,
    base_env: dict[str, str] | None = None,
    extra_src: list[os.PathLike[str] | str] | None = None,
) -> dict[str, str]:
    """Env whose ``PYTHONPATH`` makes a spawned child import the ``aiworkhub``
    tree checked out under ``worktree_root`` (its ``src`` dir), ahead of any
    editable/canonical install already importable in the parent interpreter.
    """
    src = Path(worktree_root).resolve() / "src"
    env = dict(os.environ if base_env is None else base_env)
    components = [str(src)]
    for extra in extra_src or ():
        components.append(str(Path(extra).resolve()))
    existing = env.get("PYTHONPATH", "")
    if existing:
        components.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(components)
    return env


def child_aiworkhub_file(
    env: dict[str, str], *, code: str = _CHILD_PROBE
) -> Path | None:
    """Run a fresh interpreter that imports ``aiworkhub`` under ``env`` and
    return the resolved path the CHILD actually loaded, or ``None`` if it could
    not import it. Not run under ``-I``/``-E`` on purpose: those ignore
    ``PYTHONPATH`` and would defeat the point of the helper.
    """
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    printed = (result.stdout or "").strip()
    if result.returncode != 0 or not printed:
        return None
    return Path(printed).resolve()


def assert_child_imports_worktree(
    env: dict[str, str], worktree_root: os.PathLike[str] | str
) -> Path:
    """Assert a spawned child resolves ``aiworkhub`` to a file beneath
    ``worktree_root``. Returns the child's ``aiworkhub.__file__``.

    This is the regression's teeth: it fails if the child resolves the package
    outside the worktree (the NF-2026-00299 failure mode).
    """
    child_file = child_aiworkhub_file(env)
    assert child_file is not None, "child could not import aiworkhub at all"
    assert is_beneath(child_file, worktree_root), (
        f"child imported aiworkhub from {child_file}, which is NOT beneath the "
        f"worktree root {Path(worktree_root).resolve()}"
    )
    return child_file


def make_aiworkhub_tree(root: os.PathLike[str] | str, sentinel: str) -> Path:
    """Create ``<root>/src/aiworkhub/__init__.py`` carrying a sentinel, standing
    in for one checked-out ``aiworkhub`` tree (a worktree copy or a canonical
    install copy). Returns the package ``__init__.py`` path.
    """
    package = Path(root) / "src" / "aiworkhub"
    package.mkdir(parents=True, exist_ok=True)
    init = package / "__init__.py"
    init.write_text(f"WORKTREE_SENTINEL = {sentinel!r}\n", encoding="utf-8")
    return init


@pytest.fixture
def worktree_import_env():
    return worktree_pythonpath_env


@pytest.fixture
def spawn_child_aiworkhub_file():
    return child_aiworkhub_file


@pytest.fixture
def assert_imports_from_worktree():
    return assert_child_imports_worktree


@pytest.fixture
def make_aiworkhub_worktree():
    return make_aiworkhub_tree


# ``ERROR_PRIVILEGE_NOT_HELD``: the token lacks SeCreateSymbolicLinkPrivilege,
# which is what a Windows AppContainer validation token looks like (NF-2026-01042).
_WINERROR_PRIVILEGE_NOT_HELD = 1314


def symlink_or_skip(
    src: os.PathLike[str] | str, dst: os.PathLike[str] | str
) -> None:
    """``os.symlink`` or skip when -- and only when -- the privilege is missing.

    Every other ``OSError`` propagates: a skip must name a missing capability,
    never hide a broken fixture.
    """
    try:
        os.symlink(src, dst)
    except OSError as exc:
        if getattr(exc, "winerror", None) == _WINERROR_PRIVILEGE_NOT_HELD:
            pytest.skip(
                "symlink privilege not held (WinError 1314, "
                "SeCreateSymbolicLinkPrivilege missing from this token)"
            )
        if sys.platform != "win32" and exc.errno == errno.EPERM:
            pytest.skip("symlink creation not permitted here (EPERM)")
        raise


@pytest.fixture
def make_symlink():
    return symlink_or_skip


# The AppContainer validation lane cannot open the drive root of its temp tree
# (measured: exactly winerror=5 in all 20 V1 lane failures, NF-2026-01071).
# The open failure matches by equality so winerror=50/53/500 still propagate.
# The lane fails at the drive-root open first, so a final-path failure is never
# a lane-capability skip: if one appears it fails loudly, and its measured
# winerror is to be named here before any skip is considered.
# A message alone cannot tell a missing capability from a regression, so the
# skip is also gated on the process actually running in an AppContainer; that
# check fails closed to False, so the host never skips (the same pattern as
# NF-2026-00964 in tests/test_windows_appcontainer.py).
_DIRECTORY_AUTHORITY_OPEN_DENIED = "windows directory handle open failed: winerror=5"


def directory_authority_or_skip(path: os.PathLike[str] | str) -> None:
    """Open the anchored directory authority for ``path`` or skip when -- and
    only when -- an AppContainer token lacks the capability (NF-2026-01071).

    Enters the drive-root authority and the directory authority the way
    production does. Every other error propagates, and outside an
    AppContainer every error propagates.
    """
    from aiworkhub import platform_io, runtime_temp

    if not platform_io.is_windows():
        return
    from aiworkhub import windows_appcontainer

    target = Path(path)
    try:
        with runtime_temp.WindowsDirectoryAuthority(Path(target.anchor)):
            pass
        with runtime_temp.WindowsDirectoryAuthority(target):
            pass
    except runtime_temp.RuntimeTempError as exc:
        message = str(exc)
        if (
            message == _DIRECTORY_AUTHORITY_OPEN_DENIED
            and windows_appcontainer.current_process_is_appcontainer()
        ):
            pytest.skip(
                "windows directory authority unavailable to this token "
                f"({message}; NF-2026-01071)"
            )
        raise


@pytest.fixture
def require_directory_authority():
    return directory_authority_or_skip


# NF-2026-01138: the windows_appcontainer worker sandbox denies the OS
# capabilities probed below (named pipes via multiprocessing, os.symlink /
# Path.symlink_to). Tests that need one carry @pytest.mark.requires_named_pipe
# or @pytest.mark.requires_symlink and skip with an explicit reason where the
# sandbox denies it, so card validation is never failed by the environment
# instead of the code under test. A capable host runs every marked test.


@functools.cache
def can_create_named_pipe() -> bool:
    """Whether this process may open multiprocessing's IPC connection primitive."""
    try:
        parent_conn, child_conn = multiprocessing.connection.Pipe()
    except OSError:
        return False
    parent_conn.close()
    child_conn.close()
    return True


@functools.cache
def can_create_symlink() -> bool:
    """Whether this process may create a filesystem symlink."""
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            target = Path(tmp_dir) / "target"
            target.write_text("probe", encoding="utf-8")
            link = Path(tmp_dir) / "link"
            link.symlink_to(target)
    except OSError:
        return False
    return True


def _skip_unless_capable(
    items: list[pytest.Item], marker_name: str, capable: bool, capability: str
) -> None:
    if capable:
        return
    skip_marker = pytest.mark.skip(reason=f"sandbox_capability_denied:{capability}")
    for item in items:
        if item.get_closest_marker(marker_name) is not None:
            item.add_marker(skip_marker)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    _skip_unless_capable(items, "requires_named_pipe", can_create_named_pipe(), "named_pipe")
    _skip_unless_capable(items, "requires_symlink", can_create_symlink(), "symlink")


# NF-2026-01150: an unmarked test that calls ``Path.symlink_to``/``os.symlink``
# directly (rather than going through a ``@pytest.mark.requires_symlink`` item)
# still hits the sandbox's raw ``OSError`` as a hard failure. Wrap both
# primitives so a denial becomes the same explicit skip instead; a capable
# host leaves both primitives untouched.
#
# NF-2026-01163: the wrappers must never raise a ``BaseException``.
# ``pytest.skip`` raises ``Skipped``, which is one, and pytest itself calls
# ``Path.symlink_to`` while building every ``tmp_path``
# (``_pytest.pathlib._force_symlink`` guards it with ``except Exception``), so
# the skip escaped that handler and EVERY test that merely requested
# ``tmp_path`` was reported ``sandbox_capability_denied:symlink`` while the
# session still exited 0. The wrappers raise ``SymlinkCapabilityDenied``
# instead -- a plain ``OSError`` -- so pytest's own handler, and every
# production ``except OSError``, behaves exactly as it does without the guard.
# The conversion to a skip happens in ``pytest_runtest_makereport`` below,
# which sees only the denials that actually escaped a test.

# The reason ``_skip_unless_capable`` builds for the marker path, reused so
# both routes name the denied capability identically.
SYMLINK_CAPABILITY_DENIED_REASON = "sandbox_capability_denied:symlink"


class SymlinkCapabilityDenied(OSError):
    """A symlink primitive the sandbox denied, raised as a plain ``OSError``."""


def _as_symlink_capability_denial(exc: OSError) -> SymlinkCapabilityDenied:
    """Rebuild ``exc`` as a ``SymlinkCapabilityDenied``, keeping everything a
    caller inspecting the error would read: ``errno``, ``strerror``,
    ``filename``, ``filename2`` and, where the platform has one, ``winerror``.
    """

    denied = SymlinkCapabilityDenied(exc.errno, exc.strerror, exc.filename)
    denied.filename2 = exc.filename2
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        denied.winerror = winerror
    return denied


def _install_symlink_skip_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    if can_create_symlink():
        return

    original_symlink_to = Path.symlink_to
    original_os_symlink = os.symlink

    def _is_symlink_capability_denial(exc: OSError) -> bool:
        return getattr(exc, "winerror", None) == 1314 or exc.errno in {
            errno.EPERM,
            errno.EACCES,
            errno.ENOSYS,
        }

    # ``Path.symlink_to`` reaches ``os.symlink``, so a denial the other wrapper
    # already converted propagates unchanged instead of being wrapped twice.
    def _symlink_to(self, *args, **kwargs):
        try:
            return original_symlink_to(self, *args, **kwargs)
        except SymlinkCapabilityDenied:
            raise
        except OSError as exc:
            if not _is_symlink_capability_denial(exc):
                raise
            raise _as_symlink_capability_denial(exc) from exc

    def _os_symlink(*args, **kwargs):
        try:
            return original_os_symlink(*args, **kwargs)
        except SymlinkCapabilityDenied:
            raise
        except OSError as exc:
            if not _is_symlink_capability_denial(exc):
                raise
            raise _as_symlink_capability_denial(exc) from exc

    monkeypatch.setattr(Path, "symlink_to", _symlink_to)
    monkeypatch.setattr(os, "symlink", _os_symlink)


@pytest.fixture(autouse=True)
def _skip_on_symlink_capability_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_symlink_skip_guard(monkeypatch)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """Report a symlink capability denial that ESCAPED a test as the skip.

    The wrappers raise an ``OSError`` so pytest's own ``tmp_path`` symlink and
    every production ``except OSError`` keep working (NF-2026-01163); a denial
    that survived to the report is one the test itself needed the capability
    for, which is exactly the case NF-2026-01150 skips.
    """

    report = yield
    if report.when not in {"setup", "call"} or not report.failed:
        return report
    if call.excinfo is None or not isinstance(
        call.excinfo.value, SymlinkCapabilityDenied
    ):
        return report
    path, line = item.reportinfo()[:2]
    report.outcome = "skipped"
    # The 3-tuple ``longrepr`` pytest itself builds for a skip, so the reason
    # reaches ``-rs``, the JUnit report and every other report consumer.
    report.longrepr = (
        os.fspath(path),
        (line or 0) + 1,
        f"Skipped: {SYMLINK_CAPABILITY_DENIED_REASON}",
    )
    return report
