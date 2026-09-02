#!/usr/bin/env python3
"""Fail-closed OS dependency boundary ratchet."""
from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from collections import Counter
from pathlib import Path

# Run directly -- `python3 scripts/check_os_dependency_boundary.py` -- this file's
# own package is not importable unless the caller happens to have exported
# PYTHONPATH. That made the CLI unusable standalone and left two of this script's
# own tests failing on a ModuleNotFoundError rather than on the exit codes they
# assert. The repository root is two levels up from here, so derive `src` from
# __file__ rather than depending on the environment.
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub.development_rules import DevelopmentRulesManifest, parse_manifest_bytes  # noqa: E402

PATTERNS: dict[str, re.Pattern[str]] = {
    "os_name_eq": re.compile(r"os\.name\s*=="),
    "os_name_ne": re.compile(r"os\.name\s*!="),
    "sys_platform": re.compile(r"sys\.platform"),
    "creationflags": re.compile(r"\bcreationflags\b"),
    "os_killpg": re.compile(r"os\.killpg\b"),
    "def_chmod_fd": re.compile(r"(?m)^\s*def\s+chmod_fd\b"),
    "def_atomic": re.compile(r"(?m)^\s*def\s+_atomic\w*\b"),
    "sqlite_connect": re.compile(r"sqlite3\.connect\s*\("),
}

AST_IMPORT_MODULES = ("fcntl", "msvcrt")
AST_IMPORT_IDENTITIES = tuple(f"import_{module}" for module in AST_IMPORT_MODULES)


def _code_only(source: str, relative: str) -> str:
    """Blank comments and string literals, preserving every offset.

    These patterns are regexes over source text, so a docstring that DESCRIBES
    an OS dependency was counted as one. Measured across src/aiworkhub: 12
    phantom matches in 10 baseline entries, including development_rules.py --
    the file that declares the rule -- recorded as violating it, and this
    script's own vocabulary counted wherever it was quoted.

    A boundary that cannot tell a description of a thing from the thing is not
    measuring the boundary. Offsets are preserved rather than the text deleted
    so line and column numbers stay usable for anything that reports them.
    """

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        # Fail closed exactly as the AST scanner does: an unreadable file is
        # never silently scanned raw, which would restore the phantom counts.
        raise ValueError(f"invalid Python syntax in scan input: {relative}: {exc}") from None
    grid = [list(line) for line in source.split("\n")]
    for token in tokens:
        if token.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        for row in range(start_row - 1, end_row):
            if row >= len(grid):
                break
            line = grid[row]
            first = start_col if row == start_row - 1 else 0
            last = end_col if row == end_row - 1 else len(line)
            for column in range(first, min(last, len(line))):
                line[column] = " "
    return "\n".join("".join(row) for row in grid)


def _ast_import_counts(source: str, relative: str) -> Counter[str]:
    try:
        tree = ast.parse(source, filename=relative)
    except SyntaxError as exc:
        raise ValueError(
            f"invalid Python syntax in scan input: {relative}:{exc.lineno}:{exc.offset}"
        ) from None
    counts: Counter[str] = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level = alias.name.partition(".")[0]
                if top_level in AST_IMPORT_MODULES:
                    counts[f"import_{top_level}"] += 1
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top_level = node.module.partition(".")[0]
            if top_level in AST_IMPORT_MODULES:
                counts[f"import_{top_level}"] += 1
    return counts


def _python_files_without_symlinks(scan_root: Path) -> list[Path]:
    """Enumerate Python files while rejecting every symlink entry fail-closed."""
    python_files: list[Path] = []
    pending = [scan_root]
    while pending:
        directory = pending.pop()
        for entry in sorted(directory.iterdir()):
            if entry.is_symlink():
                raise ValueError(f"symlink in scan input: {entry}")
            if entry.is_dir():
                pending.append(entry)
            elif entry.is_file() and entry.suffix == ".py":
                python_files.append(entry)
    return sorted(python_files)


def load_manifest(path: Path) -> DevelopmentRulesManifest:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"manifest is missing or not a regular file: {path}")
    return parse_manifest_bytes(path.read_bytes())


def scan_repository(root: Path, manifest: DevelopmentRulesManifest) -> dict[tuple[str, str], int]:
    boundary = manifest.os_dependency_boundary
    if boundary is None:
        raise ValueError("canonical manifest has no os_dependency_boundary")
    resolved_root = root.resolve(strict=True)
    declared_scan_root = resolved_root / boundary.scan_root
    if declared_scan_root.is_symlink():
        raise ValueError("declared scan root must not be a symlink")
    scan_root = declared_scan_root.resolve(strict=True)
    if resolved_root not in scan_root.parents or not scan_root.is_dir():
        raise ValueError("scan root escapes repository or is not a directory")
    scanner_identities = tuple(sorted((*PATTERNS, *AST_IMPORT_IDENTITIES)))
    if scanner_identities != boundary.patterns:
        raise ValueError("manifest pattern identities do not match authoritative scanner")
    excluded = set(boundary.sanctioned_modules)
    counts: Counter[tuple[str, str]] = Counter()
    for path in _python_files_without_symlinks(scan_root):
        resolved = path.resolve(strict=True)
        if scan_root not in resolved.parents:
            raise ValueError(f"scan path escapes root: {path}")
        relative = resolved.relative_to(resolved_root).as_posix()
        if relative in excluded:
            continue
        source = path.read_text(encoding="utf-8")
        for identity, count in _ast_import_counts(source, relative).items():
            counts[(relative, identity)] = count
        code = _code_only(source, relative)
        for identity, regex in PATTERNS.items():
            count = len(regex.findall(code))
            if count:
                counts[(relative, identity)] = count
    return dict(sorted(counts.items()))


def check(root: Path, config: Path) -> list[str]:
    manifest = load_manifest(config)
    boundary = manifest.os_dependency_boundary
    if boundary is None:
        return ["missing os_dependency_boundary"]
    baseline = {(entry.path, entry.pattern): entry.count for entry in boundary.baseline}
    current = scan_repository(root, manifest)
    failures = []
    for identity, count in current.items():
        allowed = baseline.get(identity)
        if allowed is None:
            failures.append(f"new violation identity {identity[0]}:{identity[1]}={count}")
        elif count > allowed:
            failures.append(f"violation growth {identity[0]}:{identity[1]} {allowed}->{count}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True)
        config = args.config or root / ".aiworkhub/config/development_rules.json"
        failures = check(root, config)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"os-dependency boundary check failed closed: {exc}")
        return 2
    for failure in failures:
        print(failure)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
