"""The direct-importer tier of the accept neighbour selection (NF-2026-02390).

The neighbour selection capped the testmap ranking at 12 files, and a changed
module with more importers than that lost some of them to lower-confidence
string-reference tests: the 0.13.12 release cut went red on two files that
import ``process_launcher_accept_review`` directly yet were never selected.

A collected test file is a *direct importer* of a changed ``src/`` module when
its syntax tree binds that module:

* ``import pkg.mod`` (or a submodule of it),
* ``from pkg import mod`` (any alias, any position in the name list),
* ``from pkg.mod import name``,
* or a string constant naming it by dotted path, ``"pkg.mod"`` or
  ``"pkg.mod.attr"`` -- ``monkeypatch.setattr("pkg.mod.attr", ...)``.

A module only re-exported through another module does not count: the importer
of the re-exporting module is not a direct importer (the known ceiling). The
decision is made over ``ast``, never a regex over arbitrary text; a file that
mentions the module name but cannot be read or parsed counts as a direct
importer, because an unknown binding must run more, never less.

:mod:`aiworkhub.accept_neighbour_tests` owns collection, the declared-run
filter and the worker count; this module owns only the predicate, the scan and
the cap, so it imports nothing from the selector.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# The direct tier runs outside the neighbour ``limit`` under its own ceiling;
# overflow beyond it is reported as ``direct_omitted`` with ``direct_reason``.
# Measured on this checkout (NF-2026-02390): process_launcher_accept_review has
# 42 direct importers. A cap of 40 cut two of them, one of which was a 0.13.12
# release-cut file. 64 holds all 42 with about 50% headroom. Also measured: a
# Source Graph bodygrep over tests/ returns 160+ direct-import lines for the hub
# process_launcher, so hub modules overflow 64 by design. That overflow is named
# in direct_omitted/direct_reason, and the selector orders the tier by testmap
# score before the cut, so the least-related importers are dropped first.
DIRECT_IMPORTER_CAP = 64
DIRECT_CAP_REASON = "direct_importer_cap"
# A test file larger than this is not parsed; when it mentions a changed module
# name it counts as a direct importer (fail closed).
DIRECT_SCAN_MAX_BYTES = 4 * 1024 * 1024
_SRC_PREFIX = "src/"


def changed_modules(production_paths: Iterable[str]) -> tuple[str, ...]:
    """The dotted module names of changed ``src/**.py`` paths, sorted.

    A package ``__init__.py`` is left out: every ``from pkg import x`` would
    match it. A path segment that is not an identifier names no importable module.
    """
    modules: set[str] = set()
    for path in production_paths:
        if not (path.startswith(_SRC_PREFIX) and path.endswith(".py")):
            continue
        parts = path[len(_SRC_PREFIX) : -len(".py")].split("/")
        if parts[-1] == "__init__" or not all(part.isidentifier() for part in parts):
            continue
        modules.add(".".join(parts))
    return tuple(sorted(modules))


def _within(name: str, module: str) -> bool:
    return name == module or name.startswith(f"{module}.")


def binds_module(tree: ast.AST, modules: Sequence[str]) -> bool:
    """True when ``tree`` imports one of ``modules`` or names it by dotted string."""
    wanted = frozenset(modules)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_within(alias.name, module) for alias in node.names for module in wanted):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue  # a relative import inside tests/ never reaches src/
            if any(_within(node.module, module) for module in wanted):
                return True
            if any(f"{node.module}.{alias.name}" in wanted for alias in node.names):
                return True
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(_within(node.value, module) for module in wanted):
                return True
    return False


def is_direct_importer(path: Path, modules: Sequence[str]) -> bool:
    """Whether the test file at ``path`` directly imports one of ``modules``.

    A file that does not even mention a module's last name is not parsed. One
    that does but is unreadable, oversized or not valid Python counts (fail closed).
    """
    leaves = {module.rsplit(".", 1)[-1].encode("utf-8") for module in modules}
    try:
        with open(path, "rb") as handle:
            data = handle.read(DIRECT_SCAN_MAX_BYTES + 1)
    except OSError:
        return True  # unknown content: run it rather than drop a possible importer
    if not any(leaf in data for leaf in leaves):
        return False
    if len(data) > DIRECT_SCAN_MAX_BYTES:
        return True
    try:
        tree = ast.parse(data)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return True  # a pathological file fails closed: run it
    return binds_module(tree, modules)


def scan_direct_importers(
    candidates: Sequence[tuple[str, Path]], modules: Sequence[str], *, workers: int
) -> tuple[str, ...]:
    """The repo-relative tests of ``candidates`` that directly import ``modules``.

    The result keeps ``candidates`` order. ``workers`` comes from the selector's
    core-count helper; ``pool.map`` preserves input order, so any worker count
    returns exactly the sequential result (asserted by the NF-2026-02390 test).
    Reads are I/O-bound and the substring pre-filter keeps the GIL-bound parse
    to the few files that mention a changed module, as in the referencing scan.
    """
    if not modules or not candidates:
        return ()

    def check(item: tuple[str, Path]) -> bool:
        return is_direct_importer(item[1], modules)

    if workers <= 1:
        hits = [check(item) for item in candidates]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            hits = list(pool.map(check, candidates))
    return tuple(test for (test, _path), hit in zip(candidates, hits) if hit)


def cap_direct(found: Sequence[str]) -> tuple[tuple[str, ...], int, str]:
    """``(kept, omitted, reason)``: at most ``DIRECT_IMPORTER_CAP`` tests, overflow named."""
    kept = tuple(found[:DIRECT_IMPORTER_CAP])
    omitted = len(found) - len(kept)
    reason = f"{DIRECT_CAP_REASON}:{DIRECT_IMPORTER_CAP}:omitted={omitted}" if omitted else ""
    return kept, omitted, reason
