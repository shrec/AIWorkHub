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
# A cut release is a plain MAJOR.MINOR.PATCH whose patch never passes 99:
# x.y.99 rolls over to x.(y+1).0, never to x.y.100.
RELEASE_TRIPLE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
PATCH_CEILING = 99


def _release_triple(version: str, role: str) -> tuple[int, int, int]:
    match = RELEASE_TRIPLE.fullmatch(version)
    if match is None:
        raise ValueError(f"{role} version {version!r} is not a MAJOR.MINOR.PATCH release")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def successor_version(current: str) -> str:
    """The patch release after ``current``; x.y.99 rolls over to x.(y+1).0."""
    major, minor, patch = _release_triple(current, "current")
    if patch >= PATCH_CEILING:
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def validate_successor(current: str, proposed: str) -> None:
    """Raise unless ``proposed`` is a release above ``current`` with a patch of at most 99."""
    expected = successor_version(current)
    release = _release_triple(proposed, "release")
    if release[2] > PATCH_CEILING:
        raise ValueError(
            f"release {proposed} has patch {release[2]} > {PATCH_CEILING}: x.y.{PATCH_CEILING} "
            f"rolls over to x.(y+1).0, so the release after {current} is {expected}"
        )
    if release <= _release_triple(current, "current"):
        raise ValueError(
            f"release {proposed} is not greater than the current version {current}; "
            f"the next release is {expected}"
        )


def changelog_versions(root: Path) -> set[str]:
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    return set(re.findall(r"^## \[([^]]+)\]", text, re.MULTILINE))


def tag_version(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def changelog_section(root: Path, tag: str) -> str:
    """Return the CHANGELOG.md text under the tag's own ``## [version]`` heading, or raise."""
    version = tag_version(tag)
    if VALID_VERSION.fullmatch(version) is None:
        raise ValueError(f"release tag is not release-safe: {tag!r}")
    heading = f"## [{version}]"
    lines = (root / "CHANGELOG.md").read_text(encoding="utf-8").split("\n")
    start = next((index for index, line in enumerate(lines) if line.startswith(heading)), None)
    if start is None:
        raise ValueError(f"CHANGELOG.md has no {heading!r} section for release tag {tag!r}")
    section: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## ["):
            break
        section.append(line)
    while section and not section[0].strip():
        section.pop(0)
    while section and not section[-1].strip():
        section.pop()
    if not section:
        raise ValueError(f"CHANGELOG.md section {heading!r} is empty; a release needs notes")
    return "\n".join(section)


# Release-gating documents the extension's own static test asserts, and this
# check did not. v0.10.78 was tagged with `check` reporting ok and CI then
# failed extension-static.test.js on all four platforms, twice over: the
# extension README had no "What's new in <version>" heading and the extension
# CHANGELOG had no "## <version> —" section. A local gate that passes while the
# release gate fails is worse than no local gate, because it is trusted. The
# root README's own release-current section is a narrative projection too, so
# it is gated the same way instead of drifting silently behind the changelog.
NARRATIVE_PROJECTIONS: tuple[tuple[str, str], ...] = (
    ("vscode-extension/README.md", "What's new in {version}"),
    ("vscode-extension/CHANGELOG.md", "## {version} —"),
    ("README.md", "What's new in {version}"),
)

# Each release prepended a "What's new" section, so the extension README grew to 116.
SINGLE_WHATS_NEW_READMES: tuple[str, ...] = ("README.md", "vscode-extension/README.md")
WHATS_NEW_HEADING = re.compile(r"^## What's new in ", re.MULTILINE)
# Every document a release carries that is written by hand before the cut.
RELEASE_DOCUMENTS: tuple[str, ...] = (
    "CHANGELOG.md",
    *(relative for relative, _template in NARRATIVE_PROJECTIONS),
)


def whats_new_gap(text: str, version: str) -> str | None:
    """Why a README is not exactly one ``## What's new in`` section naming ``version``."""
    headings = [
        text[match.end() :].split("\n", 1)[0].strip()
        for match in WHATS_NEW_HEADING.finditer(text)
    ]
    if len(headings) > 1:
        return (
            "more than one '## What's new in' section; keep only the current "
            "release, history lives in the CHANGELOG"
        )
    if not headings:
        return f"no '## What's new in {version}' section heading"
    named = headings[0]
    if not named.startswith(version) or named[len(version) : len(version) + 1].strip():
        return f"its '## What's new in' section names {named!r}, not {version}"
    return None


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
        elif relative in SINGLE_WHATS_NEW_READMES:
            gap = whats_new_gap(text, version)
            if gap is not None:
                missing[relative] = gap
    return missing


CANONICAL_VERSION_FILE = "src/aiworkhub/_version.py"
# The generated projections: what ``sync`` rewrites and ``projected_versions``
# reads (tests/test_release_cut.py asserts both name the same files).
PROJECTION_FILES: tuple[str, ...] = (
    "vscode-extension/package.json",
    "vscode-extension/package-lock.json",
    "vscode-extension/extension.js",
    "server.json",
)


def canonical_version(root: Path) -> str:
    source = (root / CANONICAL_VERSION_FILE).read_text(encoding="utf-8")
    match = VERSION_LITERAL.search(source)
    if match is None:
        raise ValueError(f"canonical version literal is missing from {CANONICAL_VERSION_FILE}")
    version = match.group(1)
    if VALID_VERSION.fullmatch(version) is None:
        raise ValueError(f"canonical version is not release-safe: {version!r}")
    return version


def projected_versions(root: Path) -> dict[str, str]:
    package = json.loads((root / "vscode-extension" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((root / "vscode-extension" / "package-lock.json").read_text(encoding="utf-8"))
    extension_source = (root / "vscode-extension" / "extension.js").read_text(encoding="utf-8")
    runtime_match = RUNTIME_LITERAL.search(extension_source)
    server = json.loads((root / "server.json").read_text(encoding="utf-8"))
    return {
        "vscode-extension/package.json": str(package.get("version") or "missing"),
        "vscode-extension/package-lock.json": str(lock.get("version") or "missing"),
        "vscode-extension/package-lock.json#packages-root": str(
            ((lock.get("packages") or {}).get("") or {}).get("version") or "missing"
        ),
        "vscode-extension/extension.js": runtime_match.group(2) if runtime_match else "missing",
        "server.json": str(server.get("version") or "missing"),
        "server.json#packages-0": str(
            ((server.get("packages") or [{}])[0] or {}).get("version") or "missing"
        ),
    }


def check(root: Path, *, tag: str = "") -> dict[str, object]:
    canonical = canonical_version(root)
    projections = projected_versions(root)
    mismatches = {
        path: version for path, version in projections.items() if version != canonical
    }
    normalized_tag = tag_version(tag)
    if tag and normalized_tag != canonical:
        mismatches["release-tag"] = normalized_tag
    if canonical not in changelog_versions(root):
        mismatches["CHANGELOG.md"] = f"missing [{canonical}] section"
    mismatches.update(narrative_mismatches(root, canonical))
    return {
        "ok": not mismatches,
        "canonical_source": CANONICAL_VERSION_FILE,
        "canonical_version": canonical,
        "projections": projections,
        "release_tag": tag,
        "mismatches": mismatches,
    }


def _write_text_preserving_newlines(path: Path, text: str) -> None:
    """Write ``text`` with the exact newlines it contains.

    ``Path.write_text`` opens in text mode, and on Windows that translates every
    "\\n" into "\\r\\n". Measured on this repository: one ``sync`` rewrote
    vscode-extension/extension.js, package.json and package-lock.json from LF to
    CRLF -- 11,332 line endings changed in extension.js alone -- which turned a
    three-line version bump into a whole-file diff and broke a test that matches
    the extension source with an "\\n}\\n\\nasync function" pattern. A metadata
    projection must change the version and nothing else.
    """

    path.write_text(text, encoding="utf-8", newline="")


def _write_json(path: Path, value: object) -> None:
    _write_text_preserving_newlines(
        path, json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    )


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
    _write_text_preserving_newlines(extension_path, rewritten)

    server_path = root / "server.json"
    server = json.loads(server_path.read_text(encoding="utf-8"))
    packages = server.get("packages")
    if not isinstance(packages, list) or not packages or not isinstance(packages[0], dict):
        raise ValueError("server.json packages[0] is missing")
    server["version"] = version
    packages[0]["version"] = version
    _write_json(server_path, server)
    return check(root)


def bump(root: Path, version: str) -> dict[str, object]:
    """Make ``version`` canonical, then project it the way ``sync`` does.

    Anything but a successor of the current canonical version is refused
    (``validate_successor``). Only the ``__version__`` literal changes: the rest
    of the canonical file, its newlines included, is kept byte for byte.
    """

    validate_successor(canonical_version(root), version)
    path = root / CANONICAL_VERSION_FILE
    source = path.read_bytes().decode("utf-8")
    match = VERSION_LITERAL.search(source)
    if match is None:
        raise ValueError(f"canonical version literal is missing from {CANONICAL_VERSION_FILE}")
    _write_text_preserving_newlines(
        path, source[: match.start(1)] + version + source[match.end(1) :]
    )
    return sync(root)


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


CHECKSUM_NOTE = "Verify downloaded artifacts against the attached `SHA256SUMS`."


def write_release_notes(root: Path, tag: str, output: Path) -> None:
    notes = f"{changelog_section(root, tag)}\n\n{CHECKSUM_NOTE}\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_text_preserving_newlines(output, notes)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--tag", default="")
    subparsers.add_parser("sync")
    bump_parser = subparsers.add_parser("bump")
    bump_parser.add_argument("version")
    sums_parser = subparsers.add_parser("checksums")
    sums_parser.add_argument("--output", type=Path, required=True)
    sums_parser.add_argument("artifacts", type=Path, nargs="+")
    notes_parser = subparsers.add_parser("notes")
    notes_parser.add_argument("--tag", required=True)
    notes_parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "check":
            result = check(root, tag=args.tag)
        elif args.command == "sync":
            result = sync(root)
        elif args.command == "bump":
            result = bump(root, args.version)
        elif args.command == "notes":
            write_release_notes(root, args.tag, args.output)
            result = {"ok": True, "output": str(args.output), "release_tag": args.tag}
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
