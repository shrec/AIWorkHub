"""Identity-path bubblewrap binds for the validation interpreter runtime.

NF-2026-00448 rewrites a bare ``python``/``python3`` validation head to
``sys.executable``.  Under bubblewrap only ``/usr``, ``/etc`` and a few
alias-mounted roots exist, so a coordinator interpreter living elsewhere (a
venv, a hosted toolcache Python, a bundled runtime) would fail ``execvp`` with
ENOENT (NF-2026-02327).  This module computes the read-only binds that publish
that runtime at its *identity* paths, so an unresolved venv ``bin/python``
symlink and ``pyvenv.cfg`` ``home`` keep resolving inside the sandbox.

Landlock, AppContainer and vscode_lm never come through here.
"""

from __future__ import annotations

import sys
from pathlib import Path

_USR = Path("/usr")


def _refuse(code: str) -> None:
    from .worker_workspace import WorkspaceError

    raise WorkspaceError(code)


def _is_anchor(path: Path) -> bool:
    return path == Path(path.anchor) or path.parent == path


def _candidate_roots(
    executable: str, explicit_root: Path | None, adapter_id: str
) -> list[Path]:
    roots: list[Path] = []
    coordinator = adapter_id == "validation" and executable == sys.executable
    if explicit_root is None and not coordinator:
        return roots
    raw_base = Path(sys.base_prefix)
    # Refuse a filesystem root lexically, before any resolve() can touch it.
    if _is_anchor(raw_base) or (explicit_root is not None and _is_anchor(explicit_root)):
        _refuse("validation_python_runtime_identity_root_untrusted")
    base = raw_base.resolve(strict=True)
    if explicit_root is not None:
        resolved = explicit_root.resolve(strict=True)
        if _is_anchor(resolved) or resolved != base:
            _refuse("validation_python_runtime_identity_root_untrusted")
        roots.append(resolved)
    if coordinator:
        prefix = Path(sys.prefix)
        if _is_anchor(prefix):
            _refuse("validation_python_runtime_identity_root_untrusted")
        if prefix.resolve(strict=False) != base:
            if not prefix.is_dir():
                _refuse("validation_python_runtime_identity_root_untrusted")
            roots.append(prefix)
        roots.append(base)
    return roots


def identity_runtime_binds(
    adapter_id: str, executable: str, explicit_root: Path | None = None
) -> list[str]:
    """Return ``--dir``/``--ro-bind`` args publishing the interpreter runtime.

    ``explicit_root`` is the version probe's base prefix; for a validation run
    whose final head is the coordinator ``sys.executable`` the venv root
    (``sys.prefix``, when it differs) and ``sys.base_prefix`` are published.
    Roots already covered by the ``/usr`` bind, duplicates and roots nested in
    another published root are skipped so nothing is bound twice.
    """
    candidates = _candidate_roots(executable, explicit_root, adapter_id)
    if not candidates:
        return []
    published: list[Path] = []
    for root in candidates:
        if root.is_relative_to(_USR) or any(
            root.is_relative_to(other) for other in published
        ):
            continue
        published.append(root)
    if not published:
        return []
    lexical = Path(executable)
    resolved = lexical.resolve(strict=False)
    # Path.is_relative_to is lexical: a ".." component could climb out of a
    # root while still matching it, so such a head is refused outright.
    if ".." in lexical.parts or not any(
        lexical.is_relative_to(root) or resolved.is_relative_to(root)
        for root in published
    ):
        _refuse("validation_python_runtime_executable_outside_root")
    binds: list[str] = []
    seen_dirs: set[str] = set()
    for root in published:
        for parent in reversed(root.parents):
            text = str(parent)
            if _is_anchor(parent) or text in seen_dirs:
                continue
            binds.extend(("--dir", text))
            seen_dirs.add(text)
        binds.extend(("--ro-bind", str(root), str(root)))
    return binds
