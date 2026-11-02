"""NF-2026-01928: the tool guard and Source Graph agree on what is indexed.

``tool_guard_hook.index_admits`` is the one predicate: ``iter_source_files``
walks with it and the guard's ``indexed_file``/``indexed_dir``/``indexed_glob``
call it, both over the repository policy in ``.aiworkhub/config/source_graph.json``.
A gitignored file re-included through ``include_roots`` is indexed, so the
guard denies built-in Read on it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from aiworkhub import source_graph, source_graph_languages, tool_guard_hook  # noqa: E402

_GIT = shutil.which("git")
needs_git = pytest.mark.skipif(_GIT is None, reason="git executable unavailable")

# rel -> indexed by both sides
CASES = {
    "src/a.py": True,  # (a) tracked
    "vendor/kept/b.py": True,  # (b) gitignored, under an include root
    "vendor/other/c.py": False,  # (c) gitignored, outside every include root
    "logs/d.py": False,  # (d) under a default excluded dir name
    "generated/d2.py": False,  # (d) under a policy exclude_dirs name
    "src/e.bin": False,  # (e) unindexed suffix
}


def _git(root: Path, *args: str) -> None:
    subprocess.run([_GIT or "git", "-C", str(root), *args], check=True, capture_output=True, timeout=60)


def _write_policy(root: Path, **fields: object) -> None:
    config = root / ".aiworkhub" / "config" / "source_graph.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_id": source_graph.POLICY_SCHEMA_ID,
        "revision": 1,
        "exclude_dirs": [],
        "exclude_globs": [],
        "disabled_languages": [],
        **fields,
    }
    config.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    for rel in CASES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"value = 1\n")
    _write_policy(root, exclude_dirs=["generated"], include_roots=["vendor/kept"])
    (root / ".gitignore").write_text("vendor/\n.aiworkhub/\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".gitignore", "src/a.py")
    return root


def _sg_indexed(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in source_graph.iter_source_files(root)}


def _guard_rule(root: Path, tool: str, tool_input: dict[str, str]) -> str | None:
    verdict = tool_guard_hook.decide(tool, tool_input, root)
    return None if verdict is None else verdict[0]


@needs_git
def test_guard_verdict_equals_source_graph_membership(repo: Path) -> None:
    indexed = _sg_indexed(repo)

    for rel, expected in CASES.items():
        read = _guard_rule(repo, "Read", {"file_path": str(repo / rel)})
        cat = _guard_rule(repo, "Bash", {"command": f"cat {rel}"})
        assert (rel in indexed) is expected, (rel, sorted(indexed))
        assert (read == "R5") is (rel in indexed), (rel, read)
        assert (cat == "R3") is (rel in indexed), (rel, cat)


@needs_git
def test_a_gitignored_include_root_file_is_denied_to_builtin_read(repo: Path) -> None:
    assert "vendor/kept/b.py" in _sg_indexed(repo)
    assert _guard_rule(repo, "Read", {"file_path": str(repo / "vendor/kept/b.py")}) == "R5"


@needs_git
def test_directory_verdicts_follow_the_walk(repo: Path) -> None:
    # ``vendor`` is ignored but stays open: the walk reaches ``vendor/kept`` through it.
    assert _guard_rule(repo, "Bash", {"command": "tree vendor"}) == "R2"
    assert _guard_rule(repo, "Bash", {"command": "tree vendor/kept"}) == "R2"
    assert _guard_rule(repo, "Bash", {"command": "tree vendor/other"}) is None
    assert _guard_rule(repo, "Bash", {"command": "tree generated"}) is None
    assert _guard_rule(repo, "Bash", {"command": "tree logs"}) is None


def _assert_same_policy(root: Path) -> None:
    sg_policy = source_graph.load_ignore_policy(root)
    guard_policy = tool_guard_hook.load_index_policy(root.resolve())
    assert guard_policy.exclude_dirs == sg_policy.exclude_dirs
    assert guard_policy.exclude_globs == sg_policy.exclude_globs
    assert guard_policy.include_roots == sg_policy.include_roots
    assert guard_policy.indexed_extensions == sg_policy.indexed_extensions


def test_both_sides_load_the_same_policy(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / ".aiworkhub").mkdir(parents=True)
    _assert_same_policy(root)
    _write_policy(
        root,
        revision=3,
        exclude_dirs=[" generated ", "out"],
        exclude_globs=["docs/**", "docs/**"],
        include_roots=["vendor/kept/", "third"],
        disabled_languages=["xml", "documentation"],
    )
    _assert_same_policy(root)


def test_the_language_table_mirrors_source_graph() -> None:
    assert tool_guard_hook.LANGUAGE_EXTENSIONS == {
        spec.id: tuple(spec.extensions) for spec in source_graph_languages.LANGUAGE_SPECS
    }
    assert tool_guard_hook.INDEXED_SUFFIXES == frozenset(source_graph_languages.INDEXED_EXTENSIONS)


_JSON_LANGUAGE = next(spec.id for spec in source_graph_languages.LANGUAGE_SPECS if ".json" in spec.extensions)


@needs_git
@pytest.mark.parametrize("disabled", [[], [_JSON_LANGUAGE]])
def test_a_disabled_language_is_not_guarded(tmp_path: Path, disabled: list[str]) -> None:
    root = (tmp_path / "repo").resolve()
    (root / "data").mkdir(parents=True)
    (root / "data" / "x.json").write_text("{}\n", encoding="utf-8")
    _write_policy(root, disabled_languages=disabled)
    _git(root, "init", "-q")
    _git(root, "add", "data/x.json")
    indexed = "data/x.json" in _sg_indexed(root)
    assert indexed is (not disabled), sorted(_sg_indexed(root))
    read = _guard_rule(root, "Read", {"file_path": str(root / "data" / "x.json")})
    cat = _guard_rule(root, "Bash", {"command": "cat data/x.json"})
    assert (read == "R5") is indexed, read
    assert (cat == "R3") is indexed, cat


def test_an_unknown_disabled_language_leaves_the_defaults_guarded(tmp_path: Path) -> None:
    _write_policy(tmp_path, disabled_languages=["klingon"])
    policy = tool_guard_hook.load_index_policy(tmp_path)
    assert policy.indexed_extensions == tool_guard_hook.INDEXED_SUFFIXES


def test_a_malformed_policy_leaves_the_defaults_guarded(tmp_path: Path) -> None:
    config = tmp_path / ".aiworkhub" / "config" / "source_graph.json"
    config.parent.mkdir(parents=True)
    for text in ("{not json", "[]", json.dumps({"include_roots": "vendor"})):
        config.write_text(text, encoding="utf-8")
        policy = tool_guard_hook.load_index_policy(tmp_path)
        assert policy.exclude_dirs == tool_guard_hook.EXCLUDED_DIRS
        assert policy.include_roots == ()


def test_one_definition_is_shared() -> None:
    assert source_graph.index_admits is tool_guard_hook.index_admits
    assert source_graph.DEFAULT_EXCLUDE_DIR_NAMES is tool_guard_hook.EXCLUDED_DIRS
    assert source_graph.DEFAULT_CONFIG_EXCLUDE_GLOBS is tool_guard_hook.DEFAULT_EXCLUDE_GLOBS
    assert source_graph._glob_ignored is tool_guard_hook.glob_ignored
    assert source_graph._under_include_root is tool_guard_hook.under_include_root
    assert source_graph.HUB_DIRNAME in tool_guard_hook.EXCLUDED_DIRS


@pytest.mark.parametrize(
    ("rel", "is_dir", "ignored", "expected"),
    [
        ("src/a.py", False, False, True),
        ("src/a.PY", False, False, True),
        ("src/.py", False, False, False),
        ("src/a.bin", False, False, False),
        ("src/a.py", False, True, False),
        ("vendor/kept/b.py", False, True, True),
        ("vendor", True, True, True),
        ("vendor/kept", True, True, True),
        ("vendor/kept_more", True, True, False),
        ("vendor/other", True, True, False),
        ("pkg.egg-info", True, False, False),
        ("a/__pycache__", True, False, False),
        ("docs", True, False, False),
        ("docs/x.py", False, False, False),
    ],
)
def test_the_predicate(rel: str, is_dir: bool, ignored: bool, expected: bool) -> None:
    policy = tool_guard_hook.IndexPolicy(
        tool_guard_hook.EXCLUDED_DIRS, ("docs/**",), ("vendor/kept",), frozenset({".py"})
    )
    assert tool_guard_hook.index_admits(policy, rel, is_dir=is_dir, ignored=ignored) is expected
