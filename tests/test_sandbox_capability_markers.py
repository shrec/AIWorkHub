"""NF-2026-01138: capability-gated skip markers for sandbox-denied OS features.

The windows_appcontainer worker sandbox denies named pipes (multiprocessing)
and symlinks (``os.symlink`` / ``Path.symlink_to``). ``tests/conftest.py``
probes both once per session and skips only the tests marked as needing them,
with an explicit ``sandbox_capability_denied:<capability>`` reason -- never
unconditionally, so a capable host still runs every marked test.
"""

from __future__ import annotations

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
