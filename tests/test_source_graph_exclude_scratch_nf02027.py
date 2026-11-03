"""NF-2026-02027: discovery answers leave out ``.scratch/`` include-root copies.

``.scratch/`` stays an include_root of the Source Graph policy (NF-2026-01928):
its files are indexed and readable through ``mode="file"`` with an exact path.
What changes is discovery -- body, bodygrep, focus, slice and testmap without a
``.scratch`` target return only canonical rows, so a manager overlay copy of
``src/`` and ``tests/`` no longer multiplies every reply. The same queries
targeted under ``.scratch/`` still return the scratch copies.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from aiworkhub import source_graph as sg  # noqa: E402
from aiworkhub.repository_state import bootstrap_repository  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FN = "scratch_probe_fn"
SCRATCH = ".scratch/s1"
SCRATCH_SRC = f"{SCRATCH}/ov/src/m.py"
SCRATCH_TEST = f"{SCRATCH}/ov/tests/test_m.py"
# NF-2026-02041: a gitignored, non-scratch include root that discovery keeps.
VENDOR_ROOT = "vendor/lib"
VENDOR_SRC = f"{VENDOR_ROOT}/v.py"
VENDOR_FN = "vendored_probe_fn"
MODULE = f"def {FN}(value):\n    return value + 1\n"
TEST = (
    f"from m import {FN}\n\n\n"
    f"def test_{FN}():\n    assert {FN}(1) == 2\n"
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A git repo with this repository's Source Graph policy and one overlay."""

    root = (tmp_path_factory.mktemp("nf02027") / "repo").resolve()
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    bootstrap_repository(root, repo_name="repo")
    # The policy shape of this repository's own .aiworkhub/config/source_graph.json
    # (".scratch" an include_root), written here so a sparse checkout runs too.
    policy_path = sg.ignore_config_path(root)
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(json.dumps({
        "schema_id": sg.POLICY_SCHEMA_ID,
        "revision": 3,
        "exclude_dirs": [],
        "exclude_globs": list(sg.DEFAULT_CONFIG_EXCLUDE_GLOBS),
        "disabled_languages": [],
        "include_roots": [".scratch", VENDOR_ROOT],
    }), encoding="utf-8")
    ignore = root / ".gitignore"
    existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    ignore.write_text(existing + "\n/.scratch/\n/vendor/\n", encoding="utf-8")
    _write(root / "src" / "m.py", MODULE)
    _write(root / "tests" / "test_m.py", TEST)
    _write(root / SCRATCH_SRC, MODULE)
    _write(root / SCRATCH_TEST, TEST)
    _write(root / VENDOR_SRC, f"def {VENDOR_FN}():\n    return 2\n")
    assert ".scratch" in sg.load_ignore_policy(root).include_roots
    sg.build_index(root)
    return root


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _strings(item)


def _scratch_mentions(payload: Any) -> list[str]:
    return [text for text in _strings(payload) if text.startswith(".scratch/")]


def _paths(payload: dict[str, Any]) -> set[str]:
    return {str(row["file_path"]) for row in payload.get("matches", [])}


def test_scratch_copies_are_indexed(repo: Path) -> None:
    indexed = {path.relative_to(repo).as_posix() for path in sg.iter_source_files(repo)}
    assert {SCRATCH_SRC, SCRATCH_TEST, "src/m.py", "tests/test_m.py"} <= indexed


def test_body_returns_only_the_canonical_definition(repo: Path) -> None:
    payload = sg.body_query(repo, FN, 16)
    assert _paths(payload) == {"src/m.py"}
    assert _scratch_mentions(payload) == []
    assert payload[sg.INCLUDE_ROOT_OMITTED_KEY] == 1
    assert "ambiguous_count" not in payload

    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        matches, total = sg.body_matches(conn, repo, FN, 8)
    finally:
        conn.close()
    assert [row["file_path"] for row in matches] == ["src/m.py"]
    assert total == 1


def test_body_under_a_scratch_target_returns_the_scratch_copy(repo: Path) -> None:
    payload = sg.body_query(repo, FN, 16, path_prefix=SCRATCH)
    assert _paths(payload) == {SCRATCH_SRC}
    assert sg.INCLUDE_ROOT_OMITTED_KEY not in payload


def test_bodygrep_skips_scratch_unless_targeted(repo: Path) -> None:
    payload = sg.bodygrep_query(repo, FN, 32)
    assert _paths(payload) == {"src/m.py", "tests/test_m.py"}
    assert _scratch_mentions(payload) == []

    scoped = sg.bodygrep_query(repo, FN, 32, target=SCRATCH)
    assert _paths(scoped) == {SCRATCH_SRC, SCRATCH_TEST}


@pytest.mark.parametrize("mode", ["focus", "slice"])
def test_focus_and_slice_list_only_canonical_rows(repo: Path, mode: str) -> None:
    query = sg.focus if mode == "focus" else sg.slice_
    payload = query(repo, FN, 32)
    assert "src/m.py" in _paths(payload)
    assert _scratch_mentions(payload) == []
    assert payload[sg.INCLUDE_ROOT_OMITTED_KEY] >= 1

    # focus has no target parameter: a lookup naming a path under .scratch/
    # is its scope; slice takes the explicit target.
    if mode == "focus":
        scoped = sg.focus(repo, SCRATCH, 32)
    else:
        scoped = sg.slice_(repo, FN, 32, target=SCRATCH)
    assert _scratch_mentions(scoped)
    assert sg.INCLUDE_ROOT_OMITTED_KEY not in scoped


@pytest.mark.parametrize("target", [None, "src"])
def test_testmap_lists_no_scratch_test_copies(repo: Path, target: str | None) -> None:
    payload = sg.analytics_query(repo, "testmap", FN, 32, target=target)
    assert _scratch_mentions(payload) == []


def test_testmap_under_a_scratch_target_keeps_the_scratch_rows(repo: Path) -> None:
    payload = sg.analytics_query(repo, "testmap", FN, 32, target=SCRATCH)
    assert _scratch_mentions(payload)


def test_file_mode_on_an_exact_scratch_path_returns_its_lines(repo: Path) -> None:
    payload = sg.file_query(repo, SCRATCH_SRC, 64)
    assert SCRATCH_SRC in _paths(payload)
    previews = [
        str(context.get("source_preview") or "") for context in payload.get("contexts", [])
    ]
    assert any(f"def {FN}(value):" in preview for preview in previews)


def test_only_the_include_root_holding_the_target_is_walked(repo: Path) -> None:
    roots = (".scratch", "overlay_root")
    # NF-2026-02041: only the scratch include root is ever omitted.
    assert sg._discovery_omit_roots(repo, f"{SCRATCH}/ov", roots) == ()
    assert sg._discovery_omit_roots(repo, "src", roots) == (".scratch",)
    assert sg._discovery_omit_roots(repo, None, (".scratch/s1", "vendor/lib")) == (".scratch/s1",)
    assert sg._discovery_omit_roots(repo, None, (".scratchy",)) == ()
    assert sg._row_under_include_roots({"file_path": r".scratch\s1\m.py"}, roots)


def test_non_scratch_ignored_include_root_stays_discoverable(repo: Path) -> None:
    """NF-2026-02041: a gitignored vendored include root is not a scratch copy."""

    payload = sg.body_query(repo, VENDOR_FN, 16)
    assert _paths(payload) == {VENDOR_SRC}
    assert sg.INCLUDE_ROOT_OMITTED_KEY not in payload
    assert VENDOR_SRC in _paths(sg.bodygrep_query(repo, VENDOR_FN, 32))
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        vendored = sg.body(conn, repo, VENDOR_FN)
    finally:
        conn.close()
    assert vendored is not None and vendored["untracked"] is True
