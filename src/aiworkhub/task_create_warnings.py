"""Create-time advisory noise filters for ``core.create_task`` (NF-2026-01955).

Two policies, both advisory and never refusals:

* ``dedupe_runner_route_warnings``: the full runner-route advisory is reported
  once per (repo_root, runner) pair per process. Later creates for the same
  pair get one short ``runner_route_warnings_repeated:runner=<runner>`` marker.
* ``objective_path_is_scope_noise``: a path token that appears only in the
  objective prose is not a missing-scope warning when the card declares it
  read-only, or when it names no file in the repository.

``core.py`` keeps the producers and the call sites; the state and the policy
live here.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Collection

_REPORTED_LOCK = threading.Lock()
_REPORTED_RUNNER_ROUTES: set[tuple[str, str]] = set()


def dedupe_runner_route_warnings(
    repo_root: Path, runner: str, warnings: list[str]
) -> list[str]:
    """Return ``warnings`` the first time a (repo_root, runner) pair reports
    them in this process, and one repeated marker on every later create."""
    if not warnings:
        return []
    identity = str(runner or "").strip()
    key = (str(repo_root), identity)
    with _REPORTED_LOCK:
        if key not in _REPORTED_RUNNER_ROUTES:
            _REPORTED_RUNNER_ROUTES.add(key)
            return list(warnings)
    return [f"runner_route_warnings_repeated:runner={identity}"]


def reset_runner_route_dedupe_for_tests() -> None:
    with _REPORTED_LOCK:
        _REPORTED_RUNNER_ROUTES.clear()


def objective_path_is_scope_noise(
    declared: str, read_only_paths: Collection[str], repo_root: Path
) -> bool:
    """True when an objective-only path token must not become a scope warning.

    A path the card declares read-only is intentional context. A path that
    names no file in the repository is prose, not a scope reference. A path
    whose existence cannot be read is not proven absent, so it keeps its
    warning (fail toward the advisory, never toward silence).
    """
    if declared in read_only_paths:
        return True
    try:
        return not (repo_root / declared).is_file()
    except OSError:
        return False
