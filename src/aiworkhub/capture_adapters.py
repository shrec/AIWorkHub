"""Declarative catalog of Context Graph manager-chat capture adapters.

The Context Graph records the manager conversation only.  Each supported
manager provider is described here in a single place so that the runtime status
surface (:func:`aiworkhub.context_graph.status`) and the capture
implementations agree on which providers are configured and on the exact token
the dashboard reports for each.  A provider whose transcript cannot be
projected is named ``not_configured`` rather than being silently omitted, so a
Claude-only installation is never reported as capturing nothing without a
reason.
"""

from __future__ import annotations

from typing import Mapping

# The Context Graph is a manager-only ledger; workers never enter it.
CAPTURE_SCOPE = "manager_only"

# ``state`` is the exact token the Settings/dashboard surface reports for the
# provider.  ``final_items`` means the adapter projects only completed final
# user/assistant message items -- never reasoning, tool output or streaming
# deltas.  ``not_configured`` means no capture path exists for the provider.
_ADAPTERS: dict[str, dict[str, str]] = {
    "codex": {
        "provider": "codex",
        "state": "final_items",
        "event_type": "chat_message",
        "source": "codex_app_server",
    },
    "claude": {
        "provider": "claude",
        "state": "final_items",
        "event_type": "chat_message",
        "source": "claude_transcript",
    },
    "copilot": {
        "provider": "copilot",
        "state": "not_configured",
        "event_type": "",
        "source": "",
    },
}


def status_map() -> dict[str, str]:
    """Return the ``provider -> state`` map for the runtime status surface."""
    return {name: spec["state"] for name, spec in _ADAPTERS.items()}


def is_configured(provider: str) -> bool:
    """Return True when ``provider`` has a real capture path (not ``not_configured``)."""
    spec = _ADAPTERS.get(provider)
    return spec is not None and spec["state"] != "not_configured"


def adapter(provider: str) -> Mapping[str, str] | None:
    """Return an immutable copy of the descriptor for ``provider`` if defined."""
    spec = _ADAPTERS.get(provider)
    return dict(spec) if spec is not None else None


__all__ = ["CAPTURE_SCOPE", "adapter", "is_configured", "status_map"]
