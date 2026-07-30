"""AIWorkHub MCP server package.

Coordinator credential scrubbing runs here, at package-init time, instead of
inside a single submodule such as ``core.py``. Python always finishes
executing a package's ``__init__.py`` before importing any of its submodules
(``core``, ``dashboard``, ``server``, ``worker_workspace``, ...), so popping
``BITNN_TASKCTL_COORDINATOR_TOKEN`` / ``BITNN_TASKCTL_COORDINATOR_TOKEN_FILE``
out of ``os.environ`` right here closes the import-order TOCTOU a
submodule-local pop cannot guarantee on its own: no matter which submodule a
caller imports first, this file's top-level code always runs first and the
secret is gone from ``os.environ`` before ANY submodule body executes or has
a chance to copy the process environment (e.g. ``os.environ.copy()``).
``core.py`` reads the scrubbed value through ``coordinator_config()`` /
``refresh_coordinator_config()`` below; it keeps no separate copy of the pop.
"""

from __future__ import annotations

import os
import threading
import importlib

from ._version import __version__

__all__ = [
    "__version__",
    "COORDINATOR_TOKEN_ENV",
    "COORDINATOR_TOKEN_FILE_ENV",
    "coordinator_config",
    "refresh_coordinator_config",
    "project_context",
]

COORDINATOR_TOKEN_ENV = "BITNN_TASKCTL_COORDINATOR_TOKEN"
COORDINATOR_TOKEN_FILE_ENV = "BITNN_TASKCTL_COORDINATOR_TOKEN_FILE"

_coordinator_lock = threading.Lock()
_coordinator_token = ""
_coordinator_token_file = ""


def refresh_coordinator_config() -> None:
    """Pop coordinator env vars out of ``os.environ`` into a private cache.

    Idempotent and safe to call repeatedly: once the env vars are gone from
    ``os.environ`` (already scrubbed), later calls are no-ops that keep the
    previously cached values instead of clearing them.
    """
    global _coordinator_token, _coordinator_token_file
    with _coordinator_lock:
        token = os.environ.pop(COORDINATOR_TOKEN_ENV, None)
        token_file = os.environ.pop(COORDINATOR_TOKEN_FILE_ENV, None)
        if token is not None:
            _coordinator_token = token
        if token_file is not None:
            _coordinator_token_file = token_file


def coordinator_config() -> tuple[str, str]:
    """Late-bound read of the scrubbed coordinator token / token-file path."""
    with _coordinator_lock:
        return _coordinator_token, _coordinator_token_file


# Runs as the very first statement of package init -- before any submodule of
# this package is imported and before any of them can observe the raw
# coordinator env vars via os.environ.
refresh_coordinator_config()


def __getattr__(name: str):
    """Lazily expose selected submodules without import-order side effects."""

    if name == "project_context":
        return importlib.import_module(".project_context", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
