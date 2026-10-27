"""NF-2026-01722: the needfix_list MCP tool folds filer-authored bodies by default."""

from __future__ import annotations

import pytest

from aiworkhub import core, server


def _row(**overrides):
    row = {
        "id": "NF-2026-90001",
        "title": "short title",
        "kind": "bug",
        "severity": "high",
        "status": "captured",
        "tags": ["a", "b"],
        "scope_files": ["src/aiworkhub/server.py"],
        "description": "a short description well under one kilobyte",
        "evidence": {"log": "line one", "count": 3},
    }
    row.update(overrides)
    return row


def _listing(rows, *, derived=True, reason=None):
    return core.NeedfixListing(rows, derived=derived, underived_reason=reason)


@pytest.fixture
def patch_core_list(monkeypatch):
    def install(rows, **kwargs):
        listing = _listing(rows, **kwargs)
        monkeypatch.setattr(core, "needfix_list", lambda **_kw: listing)
        return listing

    return install


def test_default_returns_compact_projection(patch_core_list):
    original = _row(converted_task_id="needfix-NF-2026-90001", updated_at="2026-10-27")
    patch_core_list([original])

    result = server.needfix_list()

    assert len(result) == 1
    folded = result[0]
    assert set(folded) == {
        "id",
        "title",
        "status",
        "severity",
        "kind",
        "converted_task_id",
        "updated_at",
    }
    for key in folded:
        assert folded[key] == original[key]
    for key in ("description", "evidence", "tags", "scope_files"):
        assert key not in folded


def test_full_returns_core_rows_unchanged(patch_core_list):
    listing = patch_core_list([_row(), _row(id="NF-2026-90002")])

    result = server.needfix_list(full=True)

    assert list(result) == list(listing)


def test_default_result_is_a_needfix_listing_with_same_derivation(patch_core_list):
    patch_core_list([_row()], derived=False, reason="no ready task store")

    result = server.needfix_list()

    assert isinstance(result, core.NeedfixListing)
    assert result.derived is False
    assert result.underived_reason == "no ready task store"


def test_absent_projection_keys_are_omitted_not_invented(patch_core_list):
    patch_core_list([{"id": "NF-2026-90003", "title": "t", "description": "x"}])

    assert server.needfix_list()[0] == {"id": "NF-2026-90003", "title": "t"}


def test_full_keeps_empty_bodies_unchanged(patch_core_list):
    patch_core_list([_row(description="", evidence={})])

    row = server.needfix_list(full=True)[0]

    assert row["description"] == ""
    assert row["evidence"] == {}


def test_bodies_bounded_default_keeps_short_body_verbatim():
    row = _row()

    assert core.needfix_bodies_bounded(row) == row


def test_bodies_bounded_max_bytes_zero_folds_short_body():
    folded = core.needfix_bodies_bounded(_row(), max_bytes=0)

    assert folded["description"]["summarized"] is True


def test_full_parameter_is_final_and_defaults_false():
    import inspect

    params = list(inspect.signature(server.needfix_list).parameters.values())

    assert params[-1].name == "full"
    assert params[-1].default is False
