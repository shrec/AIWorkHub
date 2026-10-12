"""Regression test for NF-2026-01474.

``provision_opencode_worker_config`` must merge
``opencode_auth.opencode_go_api_key_env`` into the launch env it hands the
OpenCode child, so an ``opencode-go`` worker receives ``OPENCODE_API_KEY``
instead of failing with ``provider.no-route``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from aiworkhub import opencode_auth  # noqa: E402
from test_opencode_workforce_integration import (  # noqa: E402
    _OC_ENV,
    _OC_NO_PROJECT,
    _opencode_request,
    _provision_opencode,
)

_API_KEY_ENV = opencode_auth.OPENCODE_API_KEY_ENV


def _seed_opencode_go_key(home: Path, key: str = "sk-test") -> None:
    destination = home / opencode_auth.OPENCODE_AUTH_RELATIVE_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps({"opencode-go": {"type": "api", "key": key}}),
        encoding="utf-8",
    )


@pytest.mark.parametrize("backend", ["landlock", "bubblewrap"])
def test_provision_opencode_worker_config_carries_the_projected_api_key(
    tmp_path: Path, backend: str
) -> None:
    if backend == "bubblewrap" and os.name == "nt":
        pytest.skip("bubblewrap is a Linux sandbox")
    workspace, runtime, authority = _opencode_request(tmp_path, backend)
    _seed_opencode_go_key(workspace.home)

    delivered = _provision_opencode(workspace, runtime, authority, backend)

    assert delivered[_API_KEY_ENV] == "sk-test"
    assert set(delivered) == {_OC_ENV, _OC_NO_PROJECT, _API_KEY_ENV}


def test_provision_opencode_worker_config_omits_the_key_without_a_projected_file(
    tmp_path: Path,
) -> None:
    workspace, runtime, authority = _opencode_request(tmp_path)

    delivered = _provision_opencode(workspace, runtime, authority)

    assert _API_KEY_ENV not in delivered
    assert set(delivered) == {_OC_ENV, _OC_NO_PROJECT}
    assert delivered[_OC_NO_PROJECT] == "1"
