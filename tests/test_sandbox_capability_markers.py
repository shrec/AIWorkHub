"""NF-2026-01138: capability-gated skip markers for sandbox-denied OS features.

The windows_appcontainer worker sandbox denies named pipes (multiprocessing)
and symlinks (``os.symlink`` / ``Path.symlink_to``). ``tests/conftest.py``
probes both once per session and skips only the tests marked as needing them,
with an explicit ``sandbox_capability_denied:<capability>`` reason -- never
unconditionally, so a capable host still runs every marked test.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

import conftest


class _FakeItem:
    """Duck-types the two ``pytest.Item`` methods the hook under test calls."""

    def __init__(self, *markers: object) -> None:
        self._markers = list(markers)

    def get_closest_marker(self, name: str):
        for marker in reversed(self._markers):
            if marker.name == name:
                return marker
        return None

    def add_marker(self, marker: object) -> None:
        self._markers.append(marker)


def _item_with(marker_name: str) -> _FakeItem:
    return _FakeItem(getattr(pytest.mark, marker_name))


@pytest.mark.parametrize(
    ("marker_name", "probe_name", "capability"),
    [
        ("requires_symlink", "can_create_symlink", "symlink"),
        ("requires_named_pipe", "can_create_named_pipe", "named_pipe"),
    ],
)
def test_capability_denied_skips_marked_item_with_explicit_reason(
    monkeypatch: pytest.MonkeyPatch, marker_name: str, probe_name: str, capability: str
) -> None:
    monkeypatch.setattr(conftest, "can_create_symlink", lambda: False)
    monkeypatch.setattr(conftest, "can_create_named_pipe", lambda: False)
    item = _item_with(marker_name)

    conftest.pytest_collection_modifyitems(items=[item])

    skip_marker = item.get_closest_marker("skip")
    assert skip_marker is not None
    assert skip_marker.kwargs["reason"] == f"sandbox_capability_denied:{capability}"


@pytest.mark.parametrize(
    ("marker_name", "probe_name"),
    [
        ("requires_symlink", "can_create_symlink"),
        ("requires_named_pipe", "can_create_named_pipe"),
    ],
)
def test_capability_available_adds_no_skip_marker(
    monkeypatch: pytest.MonkeyPatch, marker_name: str, probe_name: str
) -> None:
    monkeypatch.setattr(conftest, "can_create_symlink", lambda: True)
    monkeypatch.setattr(conftest, "can_create_named_pipe", lambda: True)
    item = _item_with(marker_name)

    conftest.pytest_collection_modifyitems(items=[item])

    assert item.get_closest_marker("skip") is None


def test_unmarked_item_is_never_skipped_regardless_of_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conftest, "can_create_symlink", lambda: False)
    monkeypatch.setattr(conftest, "can_create_named_pipe", lambda: False)
    item = _FakeItem()

    conftest.pytest_collection_modifyitems(items=[item])

    assert item.get_closest_marker("skip") is None


def test_capability_probes_are_cached_across_calls() -> None:
    conftest.can_create_named_pipe.cache_clear()
    conftest.can_create_named_pipe()
    conftest.can_create_named_pipe()
    assert conftest.can_create_named_pipe.cache_info().hits >= 1

    conftest.can_create_symlink.cache_clear()
    conftest.can_create_symlink()
    conftest.can_create_symlink()
    assert conftest.can_create_symlink.cache_info().hits >= 1


def test_symlink_denial_is_converted_to_a_capability_denial_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(conftest, "can_create_symlink", lambda: False)

    conftest._install_symlink_skip_guard(monkeypatch)

    def _raise_denied(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EPERM, "denied")

    monkeypatch.setattr(conftest.os, "symlink", _raise_denied)

    with pytest.raises(conftest.SymlinkCapabilityDenied) as excinfo:
        (tmp_path / "source").symlink_to(tmp_path / "target")
    assert excinfo.value.errno == errno.EPERM
    assert excinfo.value.strerror == "denied"
    # NF-2026-01163: an ``Exception``, never pytest's ``BaseException`` skip,
    # so pytest's own ``except Exception`` around tmp_path still catches it.
    assert isinstance(excinfo.value, Exception)
    assert not isinstance(excinfo.value, pytest.skip.Exception)


def test_symlink_capability_available_installs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conftest, "can_create_symlink", lambda: True)
    original_symlink_to = Path.symlink_to
    original_os_symlink = conftest.os.symlink

    conftest._install_symlink_skip_guard(monkeypatch)

    assert Path.symlink_to is original_symlink_to
    assert conftest.os.symlink is original_os_symlink


def test_non_capability_oserror_from_symlink_primitive_is_never_converted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(conftest, "can_create_symlink", lambda: False)

    def _raise_exists(*_args: object, **_kwargs: object) -> None:
        raise FileExistsError(errno.EEXIST, "exists")

    monkeypatch.setattr(conftest.os, "symlink", _raise_exists)
    conftest._install_symlink_skip_guard(monkeypatch)

    with pytest.raises(FileExistsError):
        conftest.os.symlink(tmp_path / "target", tmp_path / "source")


def test_symlink_primitive_capability_denial_errno_is_converted_to_denial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(conftest, "can_create_symlink", lambda: False)

    def _raise_denied(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EPERM, "denied")

    monkeypatch.setattr(conftest.os, "symlink", _raise_denied)
    conftest._install_symlink_skip_guard(monkeypatch)

    with pytest.raises(conftest.SymlinkCapabilityDenied) as excinfo:
        conftest.os.symlink(tmp_path / "target", tmp_path / "source")
    assert excinfo.value.errno == errno.EPERM
    # NF-2026-01163: an ``Exception``, never pytest's ``BaseException`` skip,
    # so pytest's own ``except Exception`` around tmp_path still catches it.
    assert isinstance(excinfo.value, Exception)
    assert not isinstance(excinfo.value, pytest.skip.Exception)
