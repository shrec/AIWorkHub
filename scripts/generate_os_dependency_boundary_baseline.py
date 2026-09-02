#!/usr/bin/env python3
"""Deterministically render, but never bless, the OS boundary baseline."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Run directly, this script's sibling module is not importable either. Add its
# own directory, and let the sibling add `src` for the package it imports.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from check_os_dependency_boundary import load_manifest, scan_repository  # noqa: E402


def generated_baseline(root: Path, config: Path) -> tuple[list[dict[str, object]], int]:
    manifest = load_manifest(config)
    boundary = manifest.os_dependency_boundary
    if boundary is None:
        raise ValueError("manifest has no os_dependency_boundary")
    counts = scan_repository(root, manifest)
    baseline: list[dict[str, object]] = [
        {"path": path, "pattern": pattern, "count": count}
        for (path, pattern), count in counts.items()
    ]
    return baseline, sum(counts.values())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.root.is_symlink():
            raise ValueError("repository root must not be a symlink")
        root = args.root.resolve(strict=True)
        expected, total = generated_baseline(root, args.check)
        raw = json.loads(args.check.read_text(encoding="utf-8"))
        boundary = raw["os_dependency_boundary"]
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"baseline generation failed closed: {exc}")
        return 2
    measurement = boundary["measurement"]
    reproducible = (
        expected == boundary["baseline"]
        and total == measurement["current_total"]
        and total - measurement["reference_total"] == measurement["accepted_predecessor_delta"]
    )
    if not reproducible:
        print("canonical manifest baseline is not reproducible; generator never overwrites it")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
