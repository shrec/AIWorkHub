"""Validation argv path rules for hosted, non-Python toolchains (NF-2026-01017).

The one owner of two narrow exceptions to the validation path checks:

* ``-DNAME=<absolute host path>`` (a CMake cache definition such as
  ``-DCMAKE_PREFIX_PATH=C:/Qt/6.8.0/msvc2022_64``) is accepted by the
  create-time token check only when the value is equal to or beneath one of
  the card's declared ``read_roots``.  Those roots were already validated by
  ``card_read_roots.validate_read_roots`` / ``linux_read_roots.
  validate_read_surface``; the creator binds them for exactly the
  classification it runs with :func:`declared_read_roots`.  With no bound
  roots nothing changes: every absolute path stays refused.
* A repo-relative validation operand need not pre-exist at workspace build
  when an EARLIER validation row of the same card names it as a build output
  directory (cmake ``-B <dir>`` / ``-B<dir>`` / ``--build <dir>``, ninja/meson
  ``-C <dir>``).  The ``-B`` operand of a row is that row's own output, never
  an input.  The same row or a later row creating a consumer's directory does
  not count, and ``.git``/``.aiworkhub`` or symlink-escaping paths never
  qualify, so those keep the existing ``workspace_required_input_missing``.

``worker_workspace`` imports this module lazily (it is at its size ratchet);
its path primitives are in turn imported lazily here to avoid an import cycle.
"""

from __future__ import annotations

import contextvars
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

_FORBIDDEN_COMPONENTS = frozenset({".git", ".aiworkhub"})
_DEFINE_RE = re.compile(r"-D([A-Za-z_][A-Za-z0-9_]*)(?::[A-Za-z]+)?=(.+)", re.DOTALL)
# Build-dir options per normalized command head: option -> creates the dir.
_BUILD_DIR_OPTIONS: Mapping[str, Mapping[str, bool]] = {
    "cmake": {"-B": True, "--build": False},
    "ninja": {"-C": False},
    "meson": {"-C": False},
}
_READ_ROOTS: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "aiworkhub_validation_read_roots", default=()
)


@contextmanager
def declared_read_roots(roots: Sequence[str] | None) -> Iterator[None]:
    """Bind a card's already-validated ``read_roots`` for one classification.

    ``None`` keeps the enclosing binding (a nested classification inside an
    outer bound one must not drop it); a sequence, even empty, replaces it.
    """
    if roots is None:
        yield
        return
    token = _READ_ROOTS.set(tuple(str(root) for root in roots))
    try:
        yield
    finally:
        _READ_ROOTS.reset(token)


def _key(path: str) -> str:
    from .card_read_roots import _key as read_root_key

    return read_root_key(path)


def host_define_beneath_read_root(
    token: str, read_roots: Sequence[str] | None = None
) -> bool:
    """Whether ``token`` is ``-DNAME[:TYPE]=<abs path>`` inside a declared root.

    ``read_roots`` defaults to the roots bound by :func:`declared_read_roots`.
    Purely lexical: a relative, UNC/device, NUL-bearing, ``..``-bearing or
    list-valued (``;``) value is never accepted, and no root means no match.
    """
    from .card_read_roots import _check_syntax, _contains

    roots = _READ_ROOTS.get() if read_roots is None else tuple(read_roots)
    match = _DEFINE_RE.fullmatch(token) if roots else None
    if match is None:
        return False
    value = match.group(2)
    if ";" in value:
        return False
    try:
        _check_syntax(value)  # NUL, UNC/device, not-absolute (drive required on Windows)
    except ValueError:
        return False
    normalized = value.replace("\\", "/")
    if ".." in PurePosixPath(normalized).parts:
        return False
    key = _key(value)
    return any(_contains(_key(root), key) for root in roots)


def _head(tokens: Sequence[str]) -> str:
    if not tokens:
        return ""
    return tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")


def build_dir_operands(tokens: Sequence[str]) -> tuple[tuple[int, str, bool], ...]:
    """``(token_index, raw_dir, created_here)`` for each build-dir operand."""
    options = _BUILD_DIR_OPTIONS.get(_head(tokens), {})
    rows: list[tuple[int, str, bool]] = []
    for index in range(1, len(tokens)):
        token = tokens[index]
        if token in options and index + 1 < len(tokens):
            value = tokens[index + 1]
            if value and not value.startswith("-"):
                rows.append((index + 1, value, options[token]))
        elif options.get("-B") and token.startswith("-B") and len(token) > 2:
            rows.append((index, token[2:], True))
    return tuple(rows)


def _safe_build_dir(repo: Path, relative: str) -> bool:
    from .worker_workspace import WorkspaceError, _require_beneath

    if any(part.casefold() in _FORBIDDEN_COMPONENTS for part in PurePosixPath(relative).parts):
        return False
    try:
        _require_beneath(repo, repo / relative)
    except WorkspaceError:
        return False
    return True


def _repo_relative(raw: str, cd_relative: str | None) -> str | None:
    from .worker_workspace import WorkspaceError, _relative_repo_path

    try:
        relative = _relative_repo_path(raw.rstrip("/\\") or raw)
        if cd_relative:
            relative = _relative_repo_path(
                (PurePosixPath(cd_relative) / PurePosixPath(relative)).as_posix()
            )
    except WorkspaceError:
        return None
    return relative


def validation_operand_is_build_dir(
    repo: Path,
    validation: Sequence[Any],
    command_index: int,
    tokens: Sequence[str],
    token_index: int,
    relative: str,
) -> bool:
    """Whether a missing validation operand is a build dir, not an input.

    True when ``tokens[token_index]`` is this row's own ``-B`` output operand,
    or when a row strictly before ``command_index`` names ``relative`` as a
    build output dir.  The path must stay beneath ``repo``, outside
    ``.git``/``.aiworkhub`` and free of symlink components (fail closed).
    """
    from .worker_workspace import WorkspaceError, _parse_validation_command_detailed

    if not _safe_build_dir(repo, relative):
        return False
    if any(index == token_index and created for index, _raw, created in build_dir_operands(tokens)):
        return True
    for row in validation[: max(command_index, 0)]:
        if not isinstance(row, str) or not row.strip():
            continue
        try:
            row_tokens, _components, _tmpdir, cd_relative = _parse_validation_command_detailed(row)
        except WorkspaceError:
            continue
        for _index, raw, _created in build_dir_operands(row_tokens):
            if _repo_relative(raw, cd_relative) == relative:
                return True
    return False
