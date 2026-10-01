"""Tests for the shared conftest fixture pinning AIWORKHUB_TOOLCHAIN_AUTHORITY_HMAC_KEY (NF-2026-01213)."""

from __future__ import annotations

import os
from pathlib import Path

from aiworkhub import toolchain_authority

_PINNED_HEX = "hex:" + "11" * 32
_PINNED_BYTES = bytes.fromhex("11" * 32)


def test_autouse_fixture_pins_the_authority_secret_by_default() -> None:
    assert os.environ["AIWORKHUB_TOOLCHAIN_AUTHORITY_HMAC_KEY"] == _PINNED_HEX


def test_a_test_can_opt_out_via_monkeypatch_delenv(monkeypatch) -> None:
    monkeypatch.delenv("AIWORKHUB_TOOLCHAIN_AUTHORITY_HMAC_KEY", raising=False)

    assert "AIWORKHUB_TOOLCHAIN_AUTHORITY_HMAC_KEY" not in os.environ


def test_pinned_secret_resolves_from_environment_without_creating_a_file(
    tmp_path: Path,
) -> None:
    secret = toolchain_authority._authority_secret(tmp_path, create=True)

    assert secret == _PINNED_BYTES
    assert list(tmp_path.iterdir()) == []
