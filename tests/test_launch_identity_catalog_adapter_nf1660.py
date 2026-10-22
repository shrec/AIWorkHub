"""NF-2026-01660: catalog fallback for adapter derivation.

A runner whose family prefix has no entry in ``adapter_identity_tuple``
(e.g. ``xai_grok-4.6``) derives its adapter from
``workforce_catalog.catalog_launch_identities`` when that catalog maps the
runner to a non-empty ``route_adapter_id``.
"""
from __future__ import annotations

import re

import pytest

try:  # repo layout: tests run with the repo root on sys.path
    from src.aiworkhub import process_launcher
    from src.aiworkhub import workforce_catalog
except ImportError:  # installed-package layout
    from aiworkhub import process_launcher  # type: ignore[no-redef]
    from aiworkhub import workforce_catalog  # type: ignore[no-redef]


def _card(runner: str) -> dict:
    return {
        "task_id": "T_NF1660",
        "runner": runner,
        "topic": "coding",
        "task_type": "code",
        "allowed_writes": ["src/a.py"],
    }


def _allow_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        process_launcher.repo_policy,
        "validate_launch",
        lambda repo, card, adapter_id: {"ok": True},
    )


def test_xai_runner_derives_catalog_adapter(tmp_path, monkeypatch) -> None:
    _allow_all(monkeypatch)
    monkeypatch.setattr(
        workforce_catalog,
        "catalog_launch_identities",
        lambda repo: {
            "xai_grok-4.6": {
                "route_adapter_id": "opencode_cli",
                "model": "xai/grok-4.6",
            }
        },
    )
    derived = process_launcher.derive_launch_identity(tmp_path, _card("xai_grok-4.6"))
    assert derived["adapter_id"] == "opencode_cli"
    assert derived["model"] == "xai/grok-4.6"
    assert derived["derived_from"]["adapter_id"] == "catalog_launch_identities"
    assert derived["adapter_candidates"] == ["opencode_cli"]


def test_runner_without_tuple_or_catalog_row_still_underivable(
    tmp_path, monkeypatch
) -> None:
    _allow_all(monkeypatch)
    monkeypatch.setattr(
        workforce_catalog, "catalog_launch_identities", lambda repo: {}
    )
    with pytest.raises(
        process_launcher.LaunchRejected,
        match=re.escape(
            "launch_adapter_underivable:runner=zzz_unknown_123:"
            "no adapter tuple for this runner family"
        ),
    ):
        process_launcher.derive_launch_identity(tmp_path, _card("zzz_unknown_123"))


def test_explicit_adapter_id_ignores_catalog(tmp_path, monkeypatch) -> None:
    def _boom(repo):
        raise AssertionError("catalog must not be read for adapter")

    monkeypatch.setattr(workforce_catalog, "catalog_launch_identities", _boom)
    derived = process_launcher.derive_launch_identity(
        tmp_path,
        _card("xai_grok-4.6"),
        adapter_id="opencode_cli",
        model="xai/grok-4.6",
    )
    assert derived["adapter_id"] == "opencode_cli"
    assert derived["model"] == "xai/grok-4.6"
    assert derived["derived_from"].get("adapter_id") != "catalog_launch_identities"


def test_family_tuple_runner_ignores_catalog_adapter(tmp_path, monkeypatch) -> None:
    _allow_all(monkeypatch)
    monkeypatch.setattr(
        workforce_catalog,
        "catalog_launch_identities",
        lambda repo: {
            "opencode_foo": {
                "route_adapter_id": "vscode_lm",
                "model": "other/model",
            }
        },
    )
    derived = process_launcher.derive_launch_identity(
        tmp_path, _card("opencode_foo"), model="pinned-model"
    )
    assert derived["adapter_id"] == "opencode_cli"
    assert derived["derived_from"].get("adapter_id") != "catalog_launch_identities"
