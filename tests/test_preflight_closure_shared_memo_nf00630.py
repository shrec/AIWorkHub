"""NF-2026-00630: the rows of one launch preflight share the closure path memos.

``preflight_validation_capabilities`` runs the seed closure once per validation
row; every run used to re-probe the same filesystem paths.  The scope below
shares the five per-call path memos for the duration of one preflight and
nothing longer.  No assertion uses wall-clock time: costs are counted syscalls.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest

from aiworkhub import python_import_closure
from aiworkhub.closure_probe_scope import CLOSURE_PROBE_SCOPE, shared_closure_probes
from aiworkhub.worker_workspace import (
    WorkspaceError,
    preflight_validation_capabilities,
)

_ABSENT = "nothere.py"


def _write(path: Path, source: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _tree(base: Path, seed: str = "import nothere\nfrom . import leaf\n") -> Path:
    repo = base / "repo"
    _write(repo / "src/pkg/__init__.py")
    _write(repo / "src/pkg/seed.py", seed)
    _write(repo / "src/pkg/leaf.py", "value = 1\n")
    _write(repo / "tests/test_seed.py", "from pkg import seed\n")
    return repo


def _count_lstat(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    counts: Counter[str] = Counter()
    original = os.lstat

    def counting_lstat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        counts[os.fspath(path)] += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", counting_lstat)
    return counts


def _closure(repo: Path) -> tuple[str, ...]:
    return python_import_closure.resolve_local_python_imports(
        repo, ("src/pkg/seed.py",)
    )


def test_closure_is_identical_inside_and_outside_the_scope(tmp_path: Path) -> None:
    repo = _tree(tmp_path)
    outside = _closure(repo)
    with shared_closure_probes():
        first = _closure(repo)
        second = _closure(repo)
    assert outside == first == second
    assert "src/pkg/leaf.py" in outside


def test_absent_candidate_is_lstatted_once_per_scope_not_per_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _tree(tmp_path)
    absent = os.fspath(repo.resolve() / _ABSENT)
    counts = _count_lstat(monkeypatch)

    with shared_closure_probes():
        for _ in range(4):
            _closure(repo)
    assert counts[absent] == 1

    counts.clear()
    for _ in range(4):
        _closure(repo)
    assert counts[absent] == 4


def test_preflight_rows_share_probes_and_keep_the_missing_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _tree(tmp_path)
    rows = [
        "python -m pytest -q tests/test_seed.py",
        "python -m pytest -q -x tests/test_seed.py",
        "python -m pytest -q -rA tests/test_seed.py",
        "python -m pytest -q -p no:cacheprovider tests/test_seed.py",
    ]
    base_card = {"allowed_writes": ["src/pkg/seed.py"]}
    prefix = os.fspath(repo.resolve())
    counts = _count_lstat(monkeypatch)

    combined: set[str] = set()
    for row in rows:
        combined.update(
            preflight_validation_capabilities(repo, {**base_card, "validation": [row]})
        )
    separate_lstats = sum(n for p, n in counts.items() if p.startswith(prefix))

    counts.clear()
    shared = preflight_validation_capabilities(
        repo, {**base_card, "validation": rows}
    )
    shared_lstats = sum(n for p, n in counts.items() if p.startswith(prefix))

    assert shared == tuple(sorted(combined))
    assert shared_lstats < separate_lstats


def test_memo_does_not_survive_the_scope(tmp_path: Path) -> None:
    repo = _tree(tmp_path, seed="import late\n")
    with shared_closure_probes():
        before = _closure(repo)
    assert "src/late.py" not in before

    _write(repo / "src/late.py")

    with shared_closure_probes():
        assert "src/late.py" in _closure(repo)
    assert "src/late.py" in _closure(repo)
    assert CLOSURE_PROBE_SCOPE.get() is None


def test_scope_holds_five_memos_per_resolved_repo(
    tmp_path: Path,
) -> None:
    # The scope is only for one preflight; documenting that its entries are
    # exactly the five memos, one entry per resolved repo.
    repo = _tree(tmp_path)
    with shared_closure_probes():
        _closure(repo)
        scope = CLOSURE_PROBE_SCOPE.get()
        assert scope is not None
        assert list(scope) == [str(repo.resolve())]
        assert len(scope[str(repo.resolve())]) == 5


def test_two_repos_in_one_scope_do_not_share_verdicts(tmp_path: Path) -> None:
    repo_a = _tree(tmp_path / "a", seed="import late\n")
    repo_b = _tree(tmp_path / "b", seed="import late\n")
    _write(repo_a / "src/late.py")

    with shared_closure_probes():
        closure_a = _closure(repo_a)
        closure_b = _closure(repo_b)
        scope = CLOSURE_PROBE_SCOPE.get()
        assert scope is not None
        assert set(scope) == {str(repo_a.resolve()), str(repo_b.resolve())}
        assert scope[str(repo_a.resolve())] is not scope[str(repo_b.resolve())]

    assert "src/late.py" in closure_a
    assert "src/late.py" not in closure_b


def test_symlink_refusal_is_replayed_identically_on_every_row(
    tmp_path: Path,
) -> None:
    repo = _tree(tmp_path, seed="from . import escaped\n")
    outside = tmp_path / "outside.py"
    _write(outside, "SECRET = 1\n")
    try:
        (repo / "src/pkg/escaped.py").symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(WorkspaceError) as unscoped:
        _closure(repo)

    messages: list[str] = []
    with shared_closure_probes():
        for _ in range(3):
            with pytest.raises(WorkspaceError) as caught:
                _closure(repo)
            messages.append(str(caught.value))

    assert messages == [str(unscoped.value)] * 3
    assert messages[0].startswith("symlink_path_component_forbidden:")
