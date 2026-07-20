"""Regression coverage for transient sideband ``not_ready`` callbacks."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from geoai_task_mcp.callback_bridge import (  # noqa: E402
    BusyThreadError,
    SidebandNotReadyError,
    SidebandUnavailableError,
)


def test_not_ready_uses_durable_busy_park_classification() -> None:
    assert issubclass(SidebandNotReadyError, BusyThreadError)
    assert not issubclass(SidebandNotReadyError, SidebandUnavailableError)
