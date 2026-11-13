"""Dart/Flutter project facts: the one owner (NF-2026-02251, NF-2026-02258).

Two consumers read these facts and must never restate them:

* ``worker_materialize.checkout_policy`` -- a repository whose root holds
  ``pubspec.yaml`` is a Dart/Flutter project.  The sparse worker closure is
  computed from Python imports and omits ``lib/``, ``test/``, ``pubspec.*``,
  ``analysis_options.yaml`` and ``.dart_tool/package_config.json``, so
  ``flutter analyze`` there reports hundreds of false ``uri_does_not_exist``
  errors.  Such a repository therefore defaults to the full checkout unless the
  card states ``checkout`` itself.
* ``task_templates`` -- the ``dart_flutter_with_tests`` template accepts only
  ``lib/**.dart`` production and ``test/**.dart`` test paths and validates with
  exactly :data:`FLUTTER_VALIDATION`.

Pure and dependency-free: no store, lifecycle or subprocess import.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from pathlib import Path

PUBSPEC_MARKER = "pubspec.yaml"
DART_SUFFIX = ".dart"
DART_PRODUCTION_ROOT = "lib"
DART_TEST_ROOT = "test"
FLUTTER_VALIDATION: tuple[str, ...] = ("flutter analyze", "flutter test")
# Canonical work kinds the template carries; their role gates are
# ``bugfix`` -> (reproduction, regression) and ``generic`` -> (generic, generic).
DART_TEMPLATE_WORK_KINDS: frozenset[str] = frozenset({"generic", "bugfix"})


def is_dart_project(repo: str | os.PathLike[str]) -> bool:
    """Whether ``pubspec.yaml`` exists at the repository root.

    Absent (or a directory of that name) is ``False``.  Any other ``OSError``
    propagates: an unreadable root is unknown, never "not Dart".
    """
    try:
        info = os.lstat(Path(repo) / PUBSPEC_MARKER)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return not stat.S_ISDIR(info.st_mode)


def _under(path: str, root: str) -> bool:
    return path.startswith(f"{root}/") and path.endswith(DART_SUFFIX)


def is_dart_path(path: str) -> bool:
    return path.endswith(DART_SUFFIX)


def partition_dart_paths(paths: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
    """Split paths into (``lib/**.dart``, ``test/**.dart``, everything else)."""
    production: list[str] = []
    tests: list[str] = []
    other: list[str] = []
    for path in paths:
        if _under(path, DART_PRODUCTION_ROOT):
            production.append(path)
        elif _under(path, DART_TEST_ROOT):
            tests.append(path)
        else:
            other.append(path)
    return production, tests, other
