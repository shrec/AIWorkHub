"""Declared worktree seeds must keep canonical bytes on Windows.

NF-2026-00970: a text-mode copy stripped CR from CRLF and stopped at the
PNG EOF marker 0x1A. The seed copy has to be a raw byte copy.
"""

from __future__ import annotations

from pathlib import Path

from aiworkhub.worker_workspace import _copy_one


def test_copy_one_preserves_crlf_and_png_eof_marker(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination_root = tmp_path / "dest"
    crlf = source / "pyproject.toml"
    crlf.write_bytes(b"name = \"aiworkhub\"\r\nversion = \"0\"\r\n")
    png = source / "icon.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00binary\x1a more")

    for name in ("pyproject.toml", "icon.png"):
        target = destination_root / name
        _copy_one(source / name, target)
        assert target.read_bytes() == (source / name).read_bytes()
