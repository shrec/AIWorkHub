#!/usr/bin/env python3
"""Synchronize and verify AIWorkHub release metadata projections."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Iterable


VERSION_LITERAL = re.compile(
    r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE
)
RUNTIME_LITERAL = re.compile(
    r'^(const EXPECTED_MCP_PACKAGE_VERSION\s*=\s*)["\']([^"\']+)["\'](;\s*)$',
    re.MULTILINE,
)
VALID_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


def changelog_versions(root: Path) -> set[str]:
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    return set(re.findall(r"^## \[([^]]+)\]", text, re.MULTILINE))


# Release-gating documents the extension's own static test asserts, and this
# check did not. v0.10.78 was tagged with `check` reporting ok and CI then
# failed extension-static.test.js on all four platforms, twice over: the
# extension README had no "What's new in <version>" heading and the extension
# CHANGELOG had no "## <version> —" section. A local gate that passes while the
# release gate fails is worse than no local gate, because it is trusted.
NARRATIVE_PROJECTIONS: tuple[tuple[str, str], ...] = (
    ("vscode-extension/README.md", "What's new in {version}"),
    ("vscode-extension/CHANGELOG.md", "## {version} \u2014"),
)


def narrative_mismatches(root: Path, version: str) -> dict[str, str]:
    """Report every release document that does not yet name this version."""
    missing: dict[str, str] = {}
    for relative, template in NARRATIVE_PROJECTIONS:
        needle = template.format(version=version)
        path = root / relative
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            missing[relative] = f"unreadable: {relative}"
            continue
        if needle not in text:
            missing[relative] = f"missing {needle!r}"
    return missing


def canonical_version(root: Path) -> str:
    source = (root / "src" / "aiworkhub" / "_version.py").read_text(encoding="utf-8")
    match = VERSION_LITERAL.search(source)
    if match is None:
        raise ValueError("canonical version literal is missing from src/aiworkhub/_version.py")
    version = match.group(1)
    if VALID_VERSION.fullmatch(version) is None:
        raise ValueError(f"canonical version is not release-safe: {version!r}")
    return version


def projected_versions(root: Path) -> dict[str, str]:
    package = json.loads((root / "vscode-extension" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((root / "vscode-extension" / "package-lock.json").read_text(encoding="utf-8"))
    extension_source = (root / "vscode-extension" / "extension.js").read_text(encoding="utf-8")
    runtime_match = RUNTIME_LITERAL.search(extension_source)
    return {
        "vscode-extension/package.json": str(package.get("version") or "missing"),
        "vscode-extension/package-lock.json": str(lock.get("version") or "missing"),
        "vscode-extension/package-lock.json#packages-root": str(
            ((lock.get("packages") or {}).get("") or {}).get("version") or "missing"
        ),
        "vscode-extension/extension.js": runtime_match.group(2) if runtime_match else "missing",
    }


def check(root: Path, *, tag: str = "") -> dict[str, object]:
    canonical = canonical_version(root)
    projections = projected_versions(root)
    mismatches = {
        path: version for path, version in projections.items() if version != canonical
    }
    normalized_tag = tag[1:] if tag.startswith("v") else tag
    if tag and normalized_tag != canonical:
        mismatches["release-tag"] = normalized_tag
    if canonical not in changelog_versions(root):
        mismatches["CHANGELOG.md"] = f"missing [{canonical}] section"
    mismatches.update(narrative_mismatches(root, canonical))
    return {
        "ok": not mismatches,
        "canonical_source": "src/aiworkhub/_version.py",
        "canonical_version": canonical,
        "projections": projections,
        "release_tag": tag,
        "mismatches": mismatches,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sync(root: Path) -> dict[str, object]:
    version = canonical_version(root)
    package_path = root / "vscode-extension" / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["version"] = version
    _write_json(package_path, package)

    lock_path = root / "vscode-extension" / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["version"] = version
    lock.setdefault("packages", {}).setdefault("", {})["version"] = version
    _write_json(lock_path, lock)

    extension_path = root / "vscode-extension" / "extension.js"
    extension_source = extension_path.read_text(encoding="utf-8")
    rewritten, count = RUNTIME_LITERAL.subn(
        lambda match: f'{match.group(1)}"{version}"{match.group(3)}',
        extension_source,
        count=1,
    )
    if count != 1:
        raise ValueError("extension runtime version projection is missing or ambiguous")
    extension_path.write_text(rewritten, encoding="utf-8")
    return check(root)


def sha256sums(paths: Iterable[Path], output: Path) -> dict[str, str]:
    resolved = sorted((path.resolve() for path in paths), key=lambda path: path.name)
    if not resolved:
        raise ValueError("at least one release artifact is required")
    names = [path.name for path in resolved]
    if len(names) != len(set(names)):
        raise ValueError("release artifact basenames must be unique")
    digests: dict[str, str] = {}
    for path in resolved:
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"release artifact is missing or empty: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        digests[path.name] = digest
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in digests.items()),
        encoding="utf-8",
    )
    return digests


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--tag", default="")
    subparsers.add_parser("sync")
    sums_parser = subparsers.add_parser("checksums")
    sums_parser.add_argument("--output", type=Path, required=True)
    sums_parser.add_argument("artifacts", type=Path, nargs="+")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "check":
            result = check(root, tag=args.tag)
        elif args.command == "sync":
            result = sync(root)
        else:
            result = {
                "ok": True,
                "output": str(args.output),
                "sha256": sha256sums(args.artifacts, args.output),
            }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
