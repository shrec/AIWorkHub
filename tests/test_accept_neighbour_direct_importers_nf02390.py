"""NF-2026-02390: direct importers of a changed module are never cut by the 12-file cap.

Measured on main e7b554a1 with no candidate tree,
``select_neighbour_tests('.', ['src/aiworkhub/process_launcher_accept_review.py'], [])``
kept 12 testmap-ranked files, 2 invariants and 5 tests that only spell the module's
path, while 16+ test files import the module directly. The 0.13.12 release cut went
red on two of the dropped importers:

* ``tests/test_accept_single_validation_nf01317.py`` does
  ``from aiworkhub import process_launcher_accept_review as accept_review`` (line 13).
* ``tests/test_reviewer_reconcile_moot_home_nf01800.py`` does
  ``from aiworkhub import process_launcher, process_launcher_accept_review, runtime_temp``
  (line 23). It was missed for two reasons at once: the testmap ranked it below the
  cap, and the referencing scan only matches the ``process_launcher_accept_review.py``
  file-name literal or a ``*_MAX_LINES`` ratchet, neither of which the file spells.
  It does NOT reach the module only through ``process_launcher``'s re-export -- the
  name is bound directly in a multi-name ``from aiworkhub import`` -- so the new
  ``direct`` tier selects it. The known ceiling is a test that reaches a changed
  module only through a re-exporting module: that importer is not a direct importer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aiworkhub import accept_neighbour_direct as direct_mod
from aiworkhub import accept_neighbour_tests as neighbours
from aiworkhub import card_contract_advice
from aiworkhub import source_graph as sg

MODULE = "src/aiworkhub/target_mod.py"
REPO_ROOT = Path(__file__).resolve().parents[1]
ACCEPT_REVIEW = "src/aiworkhub/process_launcher_accept_review.py"

# Fifteen spellings of a direct binding of ``aiworkhub.target_mod``.
_IMPORT_FORMS = (
    "from aiworkhub import target_mod\n",
    "from aiworkhub import target_mod as tm\n",
    "from aiworkhub import other, target_mod, third\n",
    "import aiworkhub.target_mod\n",
    "import aiworkhub.target_mod as tm\n",
    "from aiworkhub.target_mod import VALUE\n",
    "from aiworkhub.target_mod import (\n    VALUE,\n    OTHER,\n)\n",
    "def test_inner(monkeypatch):\n    from aiworkhub import target_mod\n",
    "def test_patch(monkeypatch):\n"
    "    monkeypatch.setattr('aiworkhub.target_mod.VALUE', 2)\n",
    "import importlib\nMOD = importlib.import_module('aiworkhub.target_mod')\n",
    "import pytest\nfrom aiworkhub import target_mod  # noqa: F401\n",
    "if True:\n    import aiworkhub.target_mod\n",
    "try:\n    from aiworkhub import target_mod\nexcept ImportError:\n    target_mod = None\n",
    "PATCH = \"aiworkhub.target_mod.helper\"\n",
    "class TestX:\n    def test_y(self):\n        from aiworkhub.target_mod import helper\n",
)
IMPORTERS = tuple(f"tests/test_imp{index:02d}.py" for index in range(len(_IMPORT_FORMS)))
# Five files that only mention the module's path string: the referencing lane's job.
MENTIONS = tuple(f"tests/test_mention{index}.py" for index in range(5))
# Neither a re-export importer nor a look-alike name is a direct importer.
NOT_DIRECT = {
    "tests/test_reexport.py": "from aiworkhub import reexporter\n",
    "tests/test_lookalike.py": "from aiworkhub import target_mod_extra\nX = 'target_mod'\n",
    "tests/test_prefix.py": "import aiworkhub.target_modular\n",
}


def _write(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n\ndef test_ok():\n    assert True\n", encoding="utf-8")


def _repo(root: Path) -> None:
    _write(root, MODULE, "VALUE = 1")
    for path, form in zip(IMPORTERS, _IMPORT_FORMS):
        _write(root, path, form)
    for path in MENTIONS:
        _write(root, path, "SOURCE = 'src/aiworkhub/target_mod.py'")
    for path, text in NOT_DIRECT.items():
        _write(root, path, text)


def _fake_graph(monkeypatch: pytest.MonkeyPatch, related: dict[str, Any]) -> None:
    def fake_states(root: Path, paths: Any) -> dict[str, str]:
        return {path: "fresh" for path in paths}

    def fake_query(root: Path, mode: str, query: str, budget: int = 64, **_kw: Any) -> Any:
        rows = related.get(_kw.get("target"), ())
        return {
            "scope": "query_matches",
            "related_tests": [
                {"file_path": path, "score": score, "reasons": []} for path, score in rows
            ],
            "truncated": False,
        }

    monkeypatch.setattr(card_contract_advice, "_freshness_states", fake_states)
    monkeypatch.setattr(sg, "analytics_query", fake_query)


def test_all_fifteen_direct_importers_are_selected_with_limit_twelve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path)
    _write(tmp_path, "tests/test_ranked_a.py", "")
    _write(tmp_path, "tests/test_ranked_b.py", "")
    # The testmap ranks three importers and two other files. The ranked lane keeps
    # its own order unchanged; the twelve importers it does not hold are appended.
    _fake_graph(
        monkeypatch,
        {MODULE: (
            (IMPORTERS[0], 90), (IMPORTERS[1], 80), (IMPORTERS[2], 70),
            ("tests/test_ranked_a.py", 60), ("tests/test_ranked_b.py", 50),
        )},
    )
    selection = neighbours.select_neighbour_tests(tmp_path, [MODULE], [], limit=12)

    assert selection.direct == IMPORTERS
    assert set(IMPORTERS) <= set(selection.tests)
    assert selection.direct_omitted == 0 and selection.direct_reason == ""
    assert selection.tests[:5] == (
        *IMPORTERS[:3], "tests/test_ranked_a.py", "tests/test_ranked_b.py"
    )
    assert selection.tests[-12:] == IMPORTERS[3:]
    assert selection.direct_added == 12
    assert len(selection.tests) == len(set(selection.tests))
    assert set(selection.referencing) == set(MENTIONS)
    assert not set(NOT_DIRECT) & set(selection.direct)
    record = selection.evidence()
    assert record["direct"] == list(IMPORTERS)
    assert record["direct_omitted"] == 0
    assert record["omitted"] == 0


def test_declared_tests_are_not_repeated_in_the_direct_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path)
    _fake_graph(monkeypatch, {})
    selection = neighbours.select_neighbour_tests(
        tmp_path, [MODULE], [f"python -m pytest {IMPORTERS[4]} -q"]
    )
    assert IMPORTERS[4] not in selection.tests
    assert selection.direct == IMPORTERS[:4] + IMPORTERS[5:]


def test_overflow_beyond_the_cap_is_counted_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path)
    _fake_graph(monkeypatch, {})
    monkeypatch.setattr(direct_mod, "DIRECT_IMPORTER_CAP", 10)
    selection = neighbours.select_neighbour_tests(tmp_path, [MODULE], [], limit=12)
    assert selection.direct == IMPORTERS[:10]
    assert selection.direct_omitted == 5
    assert selection.direct_reason == "direct_importer_cap:10:omitted=5"
    record = selection.evidence()
    assert record["direct_omitted"] == 5
    assert record["direct_reason"] == selection.direct_reason


def test_overflow_drops_the_least_related_importer_not_the_alphabetically_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cut follows testmap relevance: the last file by path scores highest and stays."""
    _repo(tmp_path)
    # Distinct scores rising with path order: IMPORTERS[-1] is highest, IMPORTERS[0] lowest.
    scored = tuple((path, 10 + index) for index, path in enumerate(IMPORTERS))
    _fake_graph(monkeypatch, {MODULE: scored})
    cap = len(IMPORTERS) - 2
    monkeypatch.setattr(direct_mod, "DIRECT_IMPORTER_CAP", cap)
    selection = neighbours.select_neighbour_tests(tmp_path, [MODULE], [], limit=12)

    assert IMPORTERS[-1] in selection.direct
    assert selection.direct[0] == IMPORTERS[-1]
    assert IMPORTERS[0] not in selection.direct and IMPORTERS[1] not in selection.direct
    assert set(selection.direct) == set(IMPORTERS[2:])
    assert selection.direct_omitted == 2
    assert selection.direct_reason == f"direct_importer_cap:{cap}:omitted=2"
    # Additive: the ranked lane keeps its 12 highest; only the uncovered importer is appended.
    assert selection.tests[:12] == tuple(reversed(IMPORTERS[3:]))
    assert selection.tests[-1] == IMPORTERS[2] and selection.direct_added == 1


def test_the_direct_cap_is_its_own_named_ceiling() -> None:
    assert direct_mod.DIRECT_IMPORTER_CAP == 64
    assert direct_mod.DIRECT_IMPORTER_CAP > neighbours.MAX_NEIGHBOUR_TESTS


def test_parallel_scan_returns_exactly_the_sequential_result(tmp_path: Path) -> None:
    _repo(tmp_path)
    candidates = neighbours._collected_tests(tmp_path, bounded=False)
    modules = direct_mod.changed_modules([MODULE])
    assert modules == ("aiworkhub.target_mod",)
    sequential = direct_mod.scan_direct_importers(candidates, modules, workers=1)
    assert sequential == IMPORTERS
    for workers in (2, 4, 8, neighbours._scan_workers(len(candidates))):
        assert direct_mod.scan_direct_importers(candidates, modules, workers=workers) == sequential


def test_unparseable_file_that_names_the_module_counts_fail_closed(tmp_path: Path) -> None:
    broken = tmp_path / "tests" / "test_broken.py"
    broken.parent.mkdir(parents=True)
    broken.write_text("from aiworkhub import target_mod\ndef (:\n", encoding="utf-8")
    silent = tmp_path / "tests" / "test_silent.py"
    silent.write_text("def (:\n", encoding="utf-8")
    modules = ("aiworkhub.target_mod",)
    assert direct_mod.is_direct_importer(broken, modules) is True
    assert direct_mod.is_direct_importer(silent, modules) is False


def test_package_init_and_non_src_paths_name_no_module() -> None:
    assert direct_mod.changed_modules(
        ["src/aiworkhub/__init__.py", "tools/x.py", "src/aiworkhub/sub/deep.py"]
    ) == ("aiworkhub.sub.deep",)


def test_measured_reproduction_selects_the_direct_importers_of_accept_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The release-cut misses are direct and selected outside the capped lanes."""
    must = [
        path
        for path in (
            "tests/test_accept_single_validation_nf01317.py",
            "tests/test_reviewer_reconcile_moot_home_nf01800.py",
            "tests/test_process_launcher_accept_review.py",
        )
        if (REPO_ROOT / path).is_file()
    ]
    assert (REPO_ROOT / ACCEPT_REVIEW).is_file()
    assert "tests/test_accept_single_validation_nf01317.py" in must
    _fake_graph(monkeypatch, {})
    selection = neighbours.select_neighbour_tests(REPO_ROOT, [ACCEPT_REVIEW], [])
    for path in must:
        assert path in selection.direct
        assert path in selection.tests
    assert len(selection.direct) + selection.direct_omitted >= len(must)


def test_the_tier_is_additive_and_never_replaces_a_node_id() -> None:
    base = neighbours.NeighbourSelection(
        tests=("tests/test_a.py::test_f", "tests/test_c.py"),
        reason="",
        limit=12,
        related_count=2,
        selection_mode="node",
        node_count=1,
    )
    direct = ("tests/test_a.py", "tests/test_b.py", "tests/test_c.py")
    merged = neighbours._with_direct(base, direct, 0, "")
    assert merged.tests == ("tests/test_a.py::test_f", "tests/test_c.py", "tests/test_b.py")
    assert merged.direct == direct and merged.direct_added == 1
    assert merged.related_count == 2 and merged.node_count == 1
    assert merged.evidence()["omitted"] == 0


def test_a_failed_direct_scan_keeps_the_selection_and_names_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path)
    _fake_graph(monkeypatch, {MODULE: ((IMPORTERS[0], 90),)})
    baseline = neighbours.select_neighbour_tests(tmp_path, [MODULE], [], limit=12)

    def boom(*_args: Any, **_kw: Any) -> Any:
        raise RuntimeError("scan")

    monkeypatch.setattr(neighbours, "_direct_importers", boom)
    failed = neighbours.select_neighbour_tests(tmp_path, [MODULE], [], limit=12)
    assert failed.tests == baseline.tests[: len(baseline.tests) - baseline.direct_added]
    assert failed.tests[0] == IMPORTERS[0]
    assert failed.direct == () and failed.direct_reason == "direct_scan_failed:RuntimeError"


def test_an_unanswered_testmap_keeps_scan_order_and_says_unranked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path)
    _fake_graph(monkeypatch, {})
    monkeypatch.setattr(direct_mod, "DIRECT_IMPORTER_CAP", 10)
    monkeypatch.setattr(
        neighbours, "_testmap_related", lambda *_a, **_k: (None, {}, [], False)
    )
    kept, omitted, reason = neighbours._direct_importers(tmp_path, [MODULE], [], 12)
    assert kept == IMPORTERS[:10] and omitted == 5
    assert reason == "direct_importer_cap:10:omitted=5:unranked:testmap_not_answered"
