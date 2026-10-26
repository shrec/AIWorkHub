"""NF-2026-01702: the AppContainer shim's PermissionError fallback also resolves
a path that already carries the ``\\\\?\\`` extended-length prefix."""

import importlib.util
import os

import pytest

from aiworkhub import windows_appcontainer as wac

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the shim binds kernel32")

SAME = os.stat_result((0o040777, 7, 11, 1, 0, 0, 0, 0, 0, 0))
OTHER = os.stat_result((0o040777, 8, 11, 1, 0, 0, 0, 0, 0, 0))


def _site():
    path = os.path.join(wac.APPCONTAINER_PYTHON_SITE, "sitecustomize.py")
    spec = importlib.util.spec_from_file_location("aiworkhub_site_nf01702", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _denied(path):
    raise PermissionError(5, "Access is denied", path)


@pytest.fixture
def shim(monkeypatch):
    site = _site()
    seen = []

    def _relative(path):
        seen.append(path)
        return "\\a\\b"

    def _stat(path):
        return SAME if path in (r"\\?\X:\a\b", r"X:\a\b") else OTHER

    monkeypatch.setattr(site, "_getfinalpathname_host", _denied)
    monkeypatch.setattr(site, "_volume_relative_name", _relative)
    monkeypatch.setattr(site, "_stat", _stat)
    site.seen = seen
    return site


def test_a_prefixed_drive_path_resolves_through_the_fallback_candidate(shim):
    assert shim._getfinalpathname("\\\\?\\X:\\a\\b") == "\\\\?\\X:\\a\\b"
    assert shim.seen == ["X:\\a\\b"]


def test_a_plain_drive_path_resolves_as_before(shim):
    assert shim._getfinalpathname("X:\\a\\b") == "\\\\?\\X:\\a\\b"
    assert shim.seen == ["X:\\a\\b"]


def test_a_prefixed_unc_path_still_raises(shim):
    with pytest.raises(PermissionError):
        shim._getfinalpathname("\\\\?\\UNC\\server\\share\\x")
    assert shim.seen == []


def test_a_prefixed_path_without_a_matching_candidate_still_raises(shim, monkeypatch):
    monkeypatch.setattr(shim, "_stat", lambda path: SAME if path == "\\\\?\\X:\\a\\b" else OTHER)
    with pytest.raises(PermissionError):
        shim._getfinalpathname("\\\\?\\X:\\other")
    assert shim.seen == ["X:\\other"]


def test_the_samestat_check_compares_against_the_original_prefixed_path(shim, monkeypatch):
    prefixed = "\\\\?\\X:\\a\\b"
    monkeypatch.setattr(shim, "_stat", lambda path: SAME if path == prefixed else OTHER)
    assert shim._getfinalpathname(prefixed) == prefixed
    assert shim.seen == ["X:\\a\\b"]


def test_a_single_backslash_question_mark_prefix_has_no_drive_and_still_raises(shim, monkeypatch):
    stated = []
    monkeypatch.setattr(shim, "_stat", lambda path: stated.append(path) or SAME)
    with pytest.raises(PermissionError):
        shim._getfinalpathname("\\?\\" + "X:\\a")
    assert shim.seen == []
    assert stated == []
