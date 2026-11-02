"""NF-2026-01981: builtin:syntax picks the parser by JavaScript dialect.

``node --check`` rejects JSX, so valid React sources failed the builtin gate.
JSX-dialect ``.js`` (nearest package.json lists react/react-scripts), ``.jsx``
and ``.tsx`` are parsed with the tree-sitter grammar; plain JavaScript keeps
``node --check``. Every test drives ``run_builtin_static_checks`` itself.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import tree_sitter_language_pack

from aiworkhub import quality_evidence

VALID_JSX = (
    "import React from 'react';\n"
    "const Comp = ({ a }) => <span>{a}</span>;\n"
    "export const App = () => (\n"
    "  <React.Fragment>\n"
    "    <Comp a={1} />\n"
    "  </React.Fragment>\n"
    ");\n"
)
MISMATCHED_JSX = "export const A = () => (<div><p>a</div>);\n"
VALID_TSX = (
    "import React from 'react';\n"
    "type Props = { label: string; count?: number };\n"
    "export const Badge = ({ label, count = 0 }: Props): JSX.Element => (\n"
    "  <span title={label}>{count}</span>\n"
    ");\n"
)
UNCLOSED_TSX = "type P = { a: number };\nexport const B = (p: P) => (<div>{p.a});\n"


def _react_package(root: Path, section: str = "dependencies", name: str = "react") -> None:
    (root / "package.json").write_text(
        json.dumps({"name": "app", section: {name: "^18.0.0"}}), encoding="utf-8"
    )


def _syntax(root: Path, relative: str) -> quality_evidence.EvidenceCheck:
    checks = quality_evidence.run_builtin_static_checks(root, changed_paths=[relative])
    return next(row for row in checks if row.check_id == f"builtin:syntax:{relative}")


def _write(root: Path, relative: str, text: str) -> str:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return relative


@pytest.mark.parametrize(
    ("section", "name"),
    [("dependencies", "react"), ("devDependencies", "react-scripts"), ("peerDependencies", "react")],
)
def test_jsx_dialect_js_passes_with_tree_sitter_javascript(tmp_path, section, name):
    _react_package(tmp_path, section, name)
    check = _syntax(tmp_path, _write(tmp_path, "src/App.js", VALID_JSX))
    assert check.status == quality_evidence.STATUS_PASSED, check.summary
    assert check.command == ("tree-sitter", "javascript")
    assert "tree-sitter javascript" in check.summary
    assert "tree-sitter-language-pack" in check.summary


@pytest.mark.parametrize("source", [MISMATCHED_JSX, "const x = ;\n"])
def test_jsx_dialect_js_real_syntax_errors_fail_with_line(tmp_path, source):
    _react_package(tmp_path)
    check = _syntax(tmp_path, _write(tmp_path, "src/Broken.js", "// header\n" + source))
    assert check.status == quality_evidence.STATUS_FAILED
    assert check.command == ("tree-sitter", "javascript")
    assert "line 2:" in check.summary
    assert "line 2:" in check.error


def test_jsx_file_passes_without_package_json(tmp_path):
    check = _syntax(tmp_path, _write(tmp_path, "Widget.jsx", VALID_JSX))
    assert check.status == quality_evidence.STATUS_PASSED, check.summary
    assert check.command == ("tree-sitter", "javascript")


def test_tsx_uses_tsx_grammar(tmp_path):
    ok = _syntax(tmp_path, _write(tmp_path, "Badge.tsx", VALID_TSX))
    assert ok.status == quality_evidence.STATUS_PASSED, ok.summary
    assert ok.command == ("tree-sitter", "tsx")
    assert "tree-sitter tsx" in ok.summary
    broken = _syntax(tmp_path, _write(tmp_path, "Broken.tsx", UNCLOSED_TSX))
    assert broken.status == quality_evidence.STATUS_FAILED
    assert broken.command == ("tree-sitter", "tsx")
    assert "line " in broken.error


def test_ts_stays_unchecked(tmp_path):
    relative = _write(tmp_path, "util.ts", "export const x: number = ;\n")
    checks = quality_evidence.run_builtin_static_checks(tmp_path, changed_paths=[relative])
    assert not any(row.check_id == f"builtin:syntax:{relative}" for row in checks)


def test_plain_js_keeps_node_check(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"express": "^4"}}), encoding="utf-8"
    )
    check = _syntax(tmp_path, _write(tmp_path, "server.js", VALID_JSX))
    assert check.command == ("node", "--check", "server.js")
    assert check.status != quality_evidence.STATUS_PASSED
    if shutil.which("node"):
        assert check.status == quality_evidence.STATUS_FAILED


@pytest.mark.parametrize("relative", ["mod.mjs", "mod.cjs"])
def test_mjs_cjs_keep_node_check_even_in_react_package(tmp_path, relative):
    _react_package(tmp_path)
    check = _syntax(tmp_path, _write(tmp_path, relative, "export const x = 1;\n"))
    assert check.command == ("node", "--check", relative)


@pytest.mark.parametrize("payload", ["{not json", "[]"])
def test_invalid_package_json_is_not_jsx_dialect(tmp_path, payload):
    (tmp_path / "package.json").write_text(payload, encoding="utf-8")
    check = _syntax(tmp_path, _write(tmp_path, "a.js", VALID_JSX))
    assert check.command == ("node", "--check", "a.js")


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, timeout=60,
    )


@pytest.mark.parametrize("sparse_mode", ["deleted", "skip_worktree"])
def test_sparse_worktree_reads_package_json_from_git(tmp_path, sparse_mode):
    if shutil.which("git") is None:
        pytest.skip("git executable not available")
    _git(tmp_path, "init", "-q")
    _react_package(tmp_path)
    _git(tmp_path, "add", "package.json")
    _git(tmp_path, "commit", "-q", "-m", "init")
    if sparse_mode == "skip_worktree":
        _git(tmp_path, "update-index", "--skip-worktree", "package.json")
    (tmp_path / "package.json").unlink()
    check = _syntax(tmp_path, _write(tmp_path, "src/App.js", VALID_JSX))
    assert check.status == quality_evidence.STATUS_PASSED, check.summary
    assert check.command == ("tree-sitter", "javascript")


def test_parser_unavailable_is_not_available_never_passed(tmp_path, monkeypatch):
    def broken_get_parser(name):
        raise LookupError(f"no grammar {name}")

    monkeypatch.setattr(tree_sitter_language_pack, "get_parser", broken_get_parser)
    check = _syntax(tmp_path, _write(tmp_path, "Widget.jsx", VALID_JSX))
    assert check.status == quality_evidence.STATUS_NOT_AVAILABLE
    assert check.summary.startswith("jsx_parser_unavailable:")
    assert check.error.startswith("jsx_parser_unavailable:")


def test_sparse_git_manifest_with_non_ascii_utf8_is_decoded(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git executable not available")
    _git(tmp_path, "init", "-q")
    # Á Í Ï Ð Ý encode as C3 81/8D/8F/90/9D, the bytes cp1252 leaves undefined:
    # on a cp1252 host a locale-codec (text=True) read raises instead of finding react.
    payload = {"name": "app", "description": "Café ÁÍÏÐÝ ünïcödé — 日本語", "dependencies": {"react": "^18"}}
    (tmp_path / "package.json").write_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    _git(tmp_path, "add", "package.json")
    _git(tmp_path, "commit", "-q", "-m", "init")
    (tmp_path / "package.json").unlink()
    check = _syntax(tmp_path, _write(tmp_path, "src/App.js", VALID_JSX))
    assert check.status == quality_evidence.STATUS_PASSED, check.summary
    assert check.command == ("tree-sitter", "javascript")


def test_on_disk_manifest_with_bom_is_jsx_dialect(tmp_path):
    body = json.dumps({"name": "app", "dependencies": {"react": "^18"}}).encode("utf-8")
    (tmp_path / "package.json").write_bytes(b"\xef\xbb\xbf" + body)
    check = _syntax(tmp_path, _write(tmp_path, "src/App.js", VALID_JSX))
    assert check.status == quality_evidence.STATUS_PASSED, check.summary
    assert check.command == ("tree-sitter", "javascript")


def test_native_parse_failure_is_not_available(tmp_path, monkeypatch):
    class _ExplodingParser:
        def parse(self, raw):
            raise RuntimeError("abi mismatch")

    monkeypatch.setattr(tree_sitter_language_pack, "get_parser", lambda name: _ExplodingParser())
    check = _syntax(tmp_path, _write(tmp_path, "Widget.jsx", VALID_JSX))
    assert check.status == quality_evidence.STATUS_NOT_AVAILABLE
    assert check.summary.startswith("jsx_parser_unavailable:")
    assert check.error.startswith("jsx_parser_unavailable:")


class _StubNode:
    def __init__(self, node_type, start_byte, children=()):
        self.type = node_type
        self.start_byte = start_byte
        self.children = list(children)
        self.has_error = True
        self.is_missing = False


def test_error_location_falls_back_to_deepest_has_error_node(monkeypatch):
    raw = b"one\ntwo\nthree\n"
    deepest = _StubNode("expression", raw.index(b"three") + 2)
    middle = _StubNode("statement", raw.index(b"two"), [deepest])
    root = _StubNode("program", 0, [middle])

    class _Tree:
        root_node = root

    class _Parser:
        def parse(self, data):
            return _Tree()

    monkeypatch.setattr(tree_sitter_language_pack, "get_parser", lambda name: _Parser())
    status, summary, error = quality_evidence._tree_sitter_syntax("javascript", raw)
    assert status == quality_evidence.STATUS_FAILED
    assert error == "syntax error at line 3:3"
    assert summary.endswith(error)
