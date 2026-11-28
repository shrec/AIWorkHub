"""The AppContainer stat broker: entries inside a brokered ancestor directory.

pytest's rootdir search stats ``pyproject.toml`` / ``setup.cfg`` / ``tox.ini``
in every ancestor of the invocation directory.  Inside the container those
ancestors are real directories it may not look into, so a denied stat of a
direct child of a brokered ancestor must read as not found (ENOENT), while
every other denial stays a PermissionError.
"""

import ctypes
import errno
import ntpath
import os

import pytest

import aiworkhub.windows_appcontainer as wac

pytestmark = pytest.mark.skipif(
    not hasattr(ctypes, "WinDLL"),
    reason="the AppContainer site module loads kernel32 through ctypes.WinDLL",
)

_ANCESTOR = "C:\\work\\repo"


def _appcontainer_site(name="aiworkhub_appcontainer_site_ancestor_entries"):
    """The shim, loaded under a name other than ``sitecustomize``: its
    functions, without it patching this test process."""
    import importlib.util

    path = os.path.join(wac.APPCONTAINER_PYTHON_SITE, "sitecustomize.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recorded():
    return os.stat_result((0o40777, 1, 2, 1, 0, 0, 0, 0, 0, 0))


def _denied(path, *args, **kwargs):
    raise PermissionError(errno.EACCES, "Access is denied", path)


def _broker():
    site = _appcontainer_site()
    recorded = _recorded()
    facts = {ntpath.normcase(ntpath.abspath(_ANCESTOR)): recorded}
    return site._brokered(_denied, facts), recorded


def _is_file(stat, path):
    """``pathlib.Path.is_file``'s semantics over ``stat``."""
    try:
        stat(path)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return False
        raise
    return True


def test_denied_direct_child_of_brokered_ancestor_reads_as_not_found():
    call, _ = _broker()
    child = _ANCESTOR + "\\pyproject.toml"
    with pytest.raises(FileNotFoundError) as caught:
        call(child)
    assert caught.value.errno == errno.ENOENT
    assert caught.value.filename == child
    assert isinstance(caught.value.__cause__, PermissionError)
    assert _is_file(call, child) is False
    assert _is_file(call, "c:\\WORK\\Repo\\setup.cfg") is False


def test_brokered_ancestor_itself_returns_recorded_facts():
    call, recorded = _broker()
    assert call(_ANCESTOR) is recorded
    assert call("c:\\WORK\\repo\\") is recorded


@pytest.mark.parametrize(
    "path",
    [
        _ANCESTOR + "\\sub\\pyproject.toml",
        "D:\\Other\\pyproject.toml",
        "E:\\unrelated",
    ],
)
def test_other_denials_stay_permission_errors(path):
    call, _ = _broker()
    with pytest.raises(PermissionError):
        call(path)


def test_dir_fd_call_stays_permission_error():
    call, _ = _broker()
    with pytest.raises(PermissionError):
        call("pyproject.toml", dir_fd=3)
    with pytest.raises(PermissionError):
        call(_ANCESTOR, dir_fd=3)
