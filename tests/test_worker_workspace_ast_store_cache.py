"""Regression and replay evidence for the parsed-tree Store/Del descendant cache.

NF-2026-00630/NF-2026-01302: ``_resolve_local_python_imports`` calls
``stores_name`` on every module-level statement while classifying dynamic-
import trust.  The baseline implementation walks the same statement subtree
once per queried name, so a single statement with many descendants is
scanned several times.  Candidate behaviour indexes the Store/Del name
descendants once per parsed module tree and never repeats the walk.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from aiworkhub.worker_workspace import _resolve_local_python_imports


def _write(repo: Path, relative: str, source: str = "") -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _package(repo: Path, source: str) -> None:
    _write(repo, "src/pkg/__init__.py")
    _write(repo, "src/pkg/seed.py", source)


def _closure(repo: Path) -> tuple[str, ...]:
    return _resolve_local_python_imports(repo, ("src/pkg/seed.py",))


def _pathological_source(blocks: int = 40) -> str:
    """Deterministic source whose top-level statements each contain many
    Store/Del descendants in nested scopes."""
    lines: list[str] = ["import importlib"]
    for index in range(blocks):
        if index % 4 == 0:
            lines.append("__package__ = 'pkg'")
        if index % 5 == 0:
            lines.append("del getattr")
        lines.extend(
            [
                "if True:",
                "    importlib = importlib",
                "    for item in ():",
                "        import_module = item",
                "        del import_module",
                '    importlib.import_module("pkg.abs")',
            ]
        )
    return "\n".join(lines) + "\n"


def test_sensitive_store_and_del_keep_dynamic_import_taints_identical(
    tmp_path: Path,
) -> None:
    """Store and Del descendants must invalidate trust identically."""
    for sensitive in ("importlib = safe_thing", "del importlib"):
        repo = tmp_path / sensitive.replace(" ", "_").replace("=", "_")
        _package(repo, f"{sensitive}\nimportlib.import_module('pkg.abs')\n")
        _write(repo, "src/pkg/abs.py")
        assert "src/pkg/abs.py" not in _closure(repo)


def test_nested_shadowing_and_disjoint_trees_do_not_share_store_cache(
    tmp_path: Path,
) -> None:
    """Nested Store taint blocks trust; parsed-tree caches cannot leak."""
    blocked = tmp_path / "blocked"
    _package(
        blocked,
        "import importlib\n"
        "if True:\n"
        "    def inner():\n"
        "        importlib = None\n"
        "importlib.import_module('pkg.abs')\n",
    )
    _write(blocked, "src/pkg/abs.py")
    allowed = tmp_path / "allowed"
    _package(
        allowed,
        "import importlib\n"
        "importlib.import_module('pkg.abs')\n",
    )
    _write(allowed, "src/pkg/abs.py")

    def closure(repo: Path) -> tuple[str, ...]:
        return _resolve_local_python_imports(repo, ("src/pkg/seed.py",))

    blocked_closure = closure(blocked)
    allowed_closure = closure(allowed)
    assert "src/pkg/abs.py" not in blocked_closure
    assert "src/pkg/abs.py" in allowed_closure
    assert allowed_closure == closure(allowed)
    assert blocked_closure == closure(blocked)

def test_changed_source_reuses_nothing_across_files(tmp_path: Path) -> None:
    repo = tmp_path / "changed"
    _package(repo, "import importlib\ndel importlib\nimportlib.import_module('pkg.abs')\n")
    _write(repo, "src/pkg/abs.py")
    before = _closure(repo)
    assert "src/pkg/abs.py" not in before
    with open(repo / "src/pkg/seed.py", "w", encoding="utf-8") as handle:
        handle.write("import importlib\nimportlib.import_module('pkg.abs')\n")
    after = _closure(repo)
    assert "src/pkg/abs.py" in after

def test_ast_walk_reduced_for_repeated_descendant_store_lookups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The candidate must avoid walking the same statements once per name."""
    from aiworkhub import worker_workspace
    import ast as _ast

    walk_calls = 0
    original_walk = _ast.walk

    def counting_walk(node, *args, **kwargs):
        nonlocal walk_calls
        walk_calls += 1
        yield from original_walk(node, *args, **kwargs)

    monkeypatch.setattr(worker_workspace.ast, "walk", counting_walk)
    repo = tmp_path / "walk"
    _package(repo, _pathological_source())
    _write(repo, "src/pkg/abs.py")
    _closure(repo)
    assert walk_calls > 0
    # Baseline would have walked once per queried name per top-level
    # statement (6 queries + complex_store scan).  Candidate indexes once
    # per statement and only a tiny number of statements should need a
    # separate initial top-level sweep.
    expected_worst_case = 7 * 40
    assert walk_calls < expected_worst_case, (
        f"walk_calls={walk_calls}"
    )


# Historical NF-2026-00630/NF-2026-01302 qualification constants (KB
# nf630-frozen-m1-replay-20261003-v1).  They document the manager-measured
# original-M1 paired replay and are NOT a default oracle against any live or
# foreign repository tree: reproducing that replay is explicit opt-in below
# and requires a verified immutable snapshot with a per-file content digest
# manifest, so a drifted or evolving checkout is reported honestly instead of
# being silently retimed against historic numbers.
_FROZEN_EXPECTED_COUNTS = (25, 167, 192)
_FROZEN_EXPECTED_DIGEST = (
    "078758d761e4e10f50a68d2854b881adec7d81bc2f71a87f36962e3c53186093"
)
_FROZEN_BASELINE_WALL_SECONDS = (21.578, 20.203, 19.328)
_FROZEN_SNAPSHOT_ROOT_ENV = "AIWORKHUB_NF630_FROZEN_M1_ROOT"
_FROZEN_SNAPSHOT_MANIFEST_ENV = "AIWORKHUB_NF630_FROZEN_M1_MANIFEST"
_FROZEN_CARD: dict[str, object] = {
    "allowed_writes": [
        "src/aiworkhub/manager_vscode_lm.py",
        "src/aiworkhub/manager_loop.py",
        "src/aiworkhub/manager_loop_backends.py",
        "src/aiworkhub/manager_loop_service.py",
        "src/aiworkhub/server.py",
        "src/aiworkhub/core.py",
        "tests/test_manager_vscode_lm.py",
        "tests/test_manager_loop.py",
        "tests/test_manager_loop_backends.py",
        "tests/test_manager_loop_service.py",
        "tests/test_server.py",
        "tests/test_aiworkhub_manager_ai_tools.py",
        "vscode-extension/extension.js",
        "vscode-extension/media/app.js",
        "vscode-extension/test/manager-chat-panel.test.js",
        "vscode-extension/test/manager-vscode-lm.test.js",
    ],
    "read_first": [
        "AGENTS.md",
        "docs/superpowers/plans/2026-10-03-manager-chat-handoff.md",
        "src/aiworkhub/manager_loop.py",
        "src/aiworkhub/manager_loop_service.py",
        "src/aiworkhub/manager_loop_backends.py",
        "src/aiworkhub/core.py",
        "src/aiworkhub/manager_ai_tools.py",
        "src/aiworkhub/callback_store.py",
        "src/aiworkhub/runtime_adapters.py",
        "vscode-extension/extension.js",
        "vscode-extension/media/app.js",
        "vscode-extension/media/app.css",
        "vscode-extension/test/manager-chat-panel.test.js",
        "vscode-extension/test/glm-vscode-lm-bridge.test.js",
        "tests/test_manager_semantic_edit.py",
    ],
    "immutable_inputs": ["tests/test_manager_semantic_edit.py"],
    "required_outputs": [
        "src/aiworkhub/manager_vscode_lm.py",
        "tests/test_manager_vscode_lm.py",
        "vscode-extension/test/manager-vscode-lm.test.js",
        "src/aiworkhub/manager_loop_backends.py",
        "src/aiworkhub/manager_loop_service.py",
        "src/aiworkhub/server.py",
        "src/aiworkhub/core.py",
        "vscode-extension/extension.js",
        "vscode-extension/media/app.js",
    ],
    "validation": [
        "python -m pytest -q tests/test_manager_vscode_lm.py -n 0",
        "python -m pytest -q tests/test_manager_loop.py tests/test_manager_loop_backends.py tests/test_manager_loop_service.py tests/test_server.py tests/test_aiworkhub_manager_ai_tools.py tests/test_manager_semantic_edit.py -n 0",
        "node --test vscode-extension/test/manager-chat-panel.test.js vscode-extension/test/manager-vscode-lm.test.js",
        "node vscode-extension/test/glm-vscode-lm-bridge.test.js",
    ],
}

_PORTABLE_CARD: dict[str, object] = {
    "allowed_writes": ["src/pkg/tool.py", "tests/test_tool.py"],
    "read_first": ["src/pkg/seed.py"],
    "immutable_inputs": [],
    "required_outputs": ["src/pkg/tool.py"],
    "validation": ["python -m pytest -q tests/test_tool.py -n 0"],
}


def _portable_repo(tmp_path: Path) -> Path:
    """Deterministic tracked fixture that exercises the production closure."""
    repo = tmp_path / "portable"
    _write(repo, "src/pkg/__init__.py")
    _write(repo, "src/pkg/seed.py", "from pkg import helper\n")
    _write(repo, "src/pkg/helper.py", "VALUE = 1\n")
    _write(repo, "src/pkg/tool.py", "from pkg import helper\n")
    _write(
        repo,
        "tests/test_tool.py",
        "from pkg import helper\n\n\ndef test_value() -> None:\n    assert helper.VALUE == 1\n",
    )
    return repo


def test_production_seed_closure_portable_fixture_is_deterministic(
    tmp_path: Path,
) -> None:
    """Default regression: run the real production seed closure portably.

    This replaces the former default replay against a hardcoded live root.
    Expected membership, repeat parity, and a stable result digest are
    asserted on a fixture this file controls, so legitimate repository
    growth on any machine cannot invalidate the gate.
    """
    from aiworkhub import worker_workspace

    repo = _portable_repo(tmp_path)
    allowed = tuple(str(item) for item in _PORTABLE_CARD["allowed_writes"])
    first = worker_workspace._declared_workspace_seed_closure(
        repo, _PORTABLE_CARD, allowed
    )
    live, support, seeded = first
    assert "src/pkg/seed.py" in live
    assert "src/pkg/tool.py" in live
    assert "src/pkg/helper.py" in support
    assert "tests/test_tool.py" in seeded
    assert "src/pkg/__init__.py" in seeded
    assert seeded == sorted(set(live) | set(support))
    second = worker_workspace._declared_workspace_seed_closure(
        repo, _PORTABLE_CARD, allowed
    )
    assert second == first
    first_digest = hashlib.sha256(
        json.dumps(first, sort_keys=True).encode("utf-8")
    ).hexdigest()
    second_digest = hashlib.sha256(
        json.dumps(second, sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert first_digest == second_digest
    print("portable_closure_counts", tuple(len(rows) for rows in first))
    print("portable_closure_digest", first_digest)


def _frozen_manifest_mismatches(root: Path, manifest: dict[str, str]) -> list[str]:
    """Verify an opt-in snapshot still matches its recorded content manifest."""
    problems: list[str] = []
    required: set[str] = set()
    for key in (
        "allowed_writes",
        "read_first",
        "immutable_inputs",
        "required_outputs",
    ):
        required.update(str(item) for item in _FROZEN_CARD.get(key) or [])
    for relative in sorted(required - set(manifest)):
        problems.append(f"manifest_missing:{relative}")
    for relative in sorted(manifest):
        candidate = root / relative
        if not candidate.is_file():
            problems.append(f"snapshot_missing:{relative}")
            continue
        recorded = str(manifest[relative])
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != recorded:
            problems.append(f"content_mismatch:{relative}")
    return problems


def test_optional_frozen_m1_replay_requires_verified_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit opt-in replay of the measured original-M1 qualification.

    Default runs skip: the historic counts and digest are only meaningful
    against the verified immutable snapshot that produced them, never against
    an arbitrary live checkout.  Opt in by setting both
    AIWORKHUB_NF630_FROZEN_M1_ROOT (snapshot checkout) and
    AIWORKHUB_NF630_FROZEN_M1_MANIFEST (JSON mapping relative path to the
    sha256 of file bytes for the card inputs).  A requested-but-drifted
    snapshot fails with the exact mismatching entries instead of retiming.
    """
    import os

    root_raw = os.environ.get(_FROZEN_SNAPSHOT_ROOT_ENV, "").strip()
    manifest_raw = os.environ.get(_FROZEN_SNAPSHOT_MANIFEST_ENV, "").strip()
    if not root_raw or not manifest_raw:
        pytest.skip(
            "opt-in frozen M1 replay not requested; the historic paired "
            "baseline/candidate measurements remain documented qualification "
            "evidence, not a portable CI oracle"
        )
    root = Path(root_raw)
    manifest_path = Path(manifest_raw)
    if not root.is_dir() or not manifest_path.is_file():
        pytest.fail(
            "frozen M1 replay requested but snapshot root or manifest path "
            f"is absent: {root_raw} / {manifest_raw}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems = _frozen_manifest_mismatches(root, manifest)
    if problems:
        pytest.fail("frozen M1 snapshot mismatch: " + ", ".join(problems))
    from aiworkhub import worker_workspace
    import ast as _ast

    walk_calls = 0
    original_walk = _ast.walk

    def counting_walk(node, *args, **kwargs):
        nonlocal walk_calls
        walk_calls += 1
        yield from original_walk(node, *args, **kwargs)

    monkeypatch.setattr(worker_workspace.ast, "walk", counting_walk)
    wall_reps: list[float] = []
    cpu_reps: list[float] = []
    walk_reps: list[int] = []
    playbacks: list[tuple[tuple[int, ...], str]] = []
    previous_walk_calls = 0
    for _ in range(3):
        start_wall = time.perf_counter()
        start_cpu = time.process_time()
        result = worker_workspace._declared_workspace_seed_closure(
            root, _FROZEN_CARD, tuple(str(v) for v in _FROZEN_CARD["allowed_writes"])
        )
        wall_reps.append(time.perf_counter() - start_wall)
        cpu_reps.append(time.process_time() - start_cpu)
        walk_reps.append(walk_calls - previous_walk_calls)
        previous_walk_calls = walk_calls
        counts = tuple(len(rows) for rows in result)
        digest = hashlib.sha256(
            json.dumps(result, sort_keys=True).encode("utf-8")
        ).hexdigest()
        playbacks.append((counts, digest))
        assert counts == _FROZEN_EXPECTED_COUNTS
        assert digest == _FROZEN_EXPECTED_DIGEST
    print("frozen_snapshot_root", str(root))
    print(
        "frozen_manifest_digest",
        hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    )
    print("baseline_wall_reps_seconds_disclosed", _FROZEN_BASELINE_WALL_SECONDS)
    print("candidate_wall_reps_seconds", wall_reps)
    print("candidate_cpu_reps_seconds", cpu_reps)
    print("candidate_ast_walk_calls_per_rep", walk_reps)
    print("closure_counts", playbacks[0][0])
    print("closure_digest", playbacks[0][1])
    assert len(wall_reps) == 3
    assert len(cpu_reps) == 3
    assert len(walk_reps) == 3
    assert all(
        counts == _FROZEN_EXPECTED_COUNTS and digest == _FROZEN_EXPECTED_DIGEST
        for counts, digest in playbacks
    )


