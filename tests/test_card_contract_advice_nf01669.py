"""NF-2026-01669: advisory Source Graph and size-ratchet hints for one task-card plan.

``contract_advice`` never rewrites ``allowed_writes`` or ``validation``. It reports the
tests Source Graph's canonical ``testmap`` relates to the card's production paths that no
validation command names, and the ``src/aiworkhub`` files close to the module-size
ratchet ceiling. A part whose evidence is missing, stale or raising says ``unavailable``
with a named reason instead of reading as an all-clear.
"""

from __future__ import annotations

import copy
import json
import runpy
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import card_contract_advice, core, needfix_store, task_store
from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository

REPO_ROOT = Path(__file__).resolve().parents[1]
RATCHET_REL = card_contract_advice.RATCHET_TEST_PATH
CEILING_NAME = card_contract_advice.RATCHET_CEILING_NAME
FLOOR = card_contract_advice.RELATED_TEST_SCORE_FLOOR
CEILING = 100
NEAR_LINES = 85
NEAR = "src/aiworkhub/near.py"
ENGINE = "src/tinypkg/engine.py"
HELPERS = "src/tinypkg/helpers.py"

TREE = {
    ENGINE: "def run_engine(value):\n    return value + 1\n",
    HELPERS: 'def format_label(value):\n    return f"<{value}>"\n',
    "tests/test_engine.py": (
        "from tinypkg.engine import run_engine\n"
        "from tinypkg.helpers import format_label\n"
        "\n\n"
        "def test_run_engine():\n"
        "    assert run_engine(1) == 2\n"
        "\n\n"
        "def test_format_label():\n"
        "    assert format_label(1) == '<1>'\n"
    ),
    "tests/test_alpha.py": (
        "from tinypkg.engine import run_engine\n"
        "\n\n"
        "def test_alpha():\n"
        "    assert run_engine(2) == 3\n"
    ),
    "tests/test_engine_extra.py": "def test_nothing():\n    assert True\n",
}

_CLAUDE_IDENTITY = {
    "provider": "claude",
    "session_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
    "window_id": "claude_vscode_4242",
}


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _lines(count: int) -> str:
    # Distinct one-line defs, so a real Source Graph index of the file has entities to scope to.
    return "".join(f"def f{index}(): pass\n" for index in range(count))


def _write_ratchet(root: Path, ceiling: int = CEILING) -> None:
    _write(root / RATCHET_REL, f"{CEILING_NAME} = {ceiling}\n")


def _seed_near(root: Path) -> None:
    _write_ratchet(root)
    _write(root / NEAR, _lines(NEAR_LINES))


def _plan(allowed_writes: Any = (), validation: Any = (), **extra: Any) -> dict[str, Any]:
    return {"allowed_writes": list(allowed_writes), "validation": list(validation), **extra}


def _advice(root: Path, **plan: Any) -> dict[str, Any]:
    return card_contract_advice.contract_advice(root, _plan(**plan))


def _near_warning(lines: int = NEAR_LINES) -> dict[str, Any]:
    return {"path": NEAR, "lines": lines, "ceiling": CEILING, "headroom": CEILING - lines}


def _index_repo(root: Path) -> Path:
    root.mkdir()
    bootstrap_repository(root, repo_name=root.name)
    for relative, text in TREE.items():
        _write(root / relative, text)
    sg.build_index(root, incremental=True)
    return root


def _testmap(repo: Path, production_path: str) -> dict[str, Any]:
    return sg.analytics_query(repo, "testmap", production_path, 40, target=production_path)


def _scores(payload: dict[str, Any]) -> dict[str, Any]:
    return {row["file_path"]: row["score"] for row in payload.get("related_tests", [])}


def _related(*rows: tuple[str, int], truncated: bool = False) -> dict[str, Any]:
    return {
        "scope": "query_matches",
        "related_tests": [
            {"file_path": path, "score": score, "reasons": []} for path, score in rows
        ],
        "truncated": truncated,
    }


def _fake_graph(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, Any]
) -> list[tuple[Any, ...]]:
    """Every consulted path is fresh; ``analytics_query`` answers from ``answers``."""
    calls: list[tuple[Any, ...]] = []

    def fake_states(root: Path, paths: list[str]) -> dict[str, str]:
        return {path: "fresh" for path in paths}

    def fake_query(
        root: Path,
        mode: str,
        query: str,
        budget: int = 64,
        *,
        target: str | None = None,
        cursor: str | None = None,
    ) -> Any:
        calls.append((mode, query, budget, target))
        answer = answers.get(target, _related())
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(card_contract_advice, "_freshness_states", fake_states)
    monkeypatch.setattr(sg, "analytics_query", fake_query)
    return calls


@pytest.fixture(scope="module")
def advice_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A tiny indexed source tree; tests that use it must not modify it."""
    return _index_repo(tmp_path_factory.mktemp("nf01669") / "advice_index")


@pytest.fixture
def mutable_index(tmp_path: Path) -> Path:
    return _index_repo(tmp_path / "mutable_index")


# --- the module surface ------------------------------------------------------------------


def test_module_exposes_only_the_advice_and_apply_functions() -> None:
    public = [
        name
        for name, value in vars(card_contract_advice).items()
        if not name.startswith("_")
        and callable(value)
        and getattr(value, "__module__", None) == card_contract_advice.__name__
    ]
    assert public == ["contract_advice", "preview_convert_applying_advice"]


@pytest.mark.parametrize("plan", [None, [], "src/aiworkhub/x.py", 7])
def test_non_mapping_plan_is_unavailable_and_never_raises(tmp_path: Path, plan: Any) -> None:
    unavailable = {"status": "unavailable", "reason": "task_plan_not_a_mapping"}
    assert card_contract_advice.contract_advice(tmp_path, plan) == {
        "advisory": True,
        "testmap": unavailable,
        "ratchet": unavailable,
    }


@pytest.mark.parametrize(
    "plan",
    [
        {},
        {"allowed_writes": "src/aiworkhub/x.py"},
        {"allowed_writes": [], "read_only": True},
        {"allowed_writes": ["tests/test_alpha.py", "docs/readme.md", "src/tinypkg/data.json"]},
    ],
)
def test_plan_without_python_source_scope_is_not_applicable(tmp_path: Path, plan: Any) -> None:
    advice = card_contract_advice.contract_advice(tmp_path, plan)
    assert advice["testmap"] == {
        "status": "not_applicable",
        "reason": "no_production_python_in_allowed_writes",
    }
    assert advice["ratchet"] == {
        "status": "not_applicable",
        "reason": "no_python_file_under_src_aiworkhub",
    }


def test_globs_and_directories_are_reported_not_expanded(tmp_path: Path) -> None:
    _seed_near(tmp_path)
    unexpanded = ["src/aiworkhub/*.py", "src/aiworkhub/sub/"]

    only = _advice(tmp_path, allowed_writes=unexpanded)
    assert only["testmap"]["status"] == "not_applicable"
    assert only["testmap"]["not_expanded"] == unexpanded
    assert only["ratchet"]["status"] == "not_applicable"
    assert only["ratchet"]["not_expanded"] == unexpanded

    mixed = _advice(tmp_path, allowed_writes=[NEAR, *unexpanded])["ratchet"]
    assert mixed["status"] == "ok", mixed
    assert mixed["not_expanded"] == unexpanded
    assert mixed["ratchet_headroom_warnings"] == [_near_warning()]


def test_malformed_plan_entries_are_ignored_and_paths_are_normalised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_graph(monkeypatch, {})
    plan = {
        "allowed_writes": [
            None,
            5,
            "",
            "../escape.py",
            "src/p/../c.py",
            "src\\p\\a.py",
            "./src/p/b.py",
        ],
        "validation": [None, 3, "pytest -q tests/test_x.py"],
    }
    part = card_contract_advice.contract_advice(tmp_path, plan)["testmap"]
    assert part["production_paths"] == ["src/p/a.py", "src/p/b.py"], part
    assert [call[3] for call in calls] == ["src/p/a.py", "src/p/b.py"]


def test_advice_reuses_the_canonical_testmap_with_a_bounded_budget(
    advice_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = sg.analytics_query
    calls: list[tuple[Any, ...]] = []

    def spy(
        root: Path,
        mode: str,
        query: str,
        budget: int = 64,
        *,
        target: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        calls.append((mode, query, budget, target))
        return real(root, mode, query, budget, target=target, cursor=cursor)

    monkeypatch.setattr(sg, "analytics_query", spy)
    part = _advice(advice_index, allowed_writes=[HELPERS, ENGINE])["testmap"]

    assert part["status"] == "ok", part
    budget = card_contract_advice.TESTMAP_BUDGET
    assert calls == [("testmap", ENGINE, budget, ENGINE), ("testmap", HELPERS, budget, HELPERS)]


# --- (a)/(b): suggested validation tests -------------------------------------------------


def test_fixture_index_relates_the_direct_tests_above_the_floor(advice_index: Path) -> None:
    payload = _testmap(advice_index, ENGINE)
    scores = _scores(payload)
    assert scores.get("tests/test_engine.py", 0) >= FLOOR, payload
    assert scores.get("tests/test_alpha.py", 0) >= FLOOR, payload
    assert scores.get("tests/test_engine_extra.py", 0) < FLOOR, payload


def test_direct_test_missing_from_validation_is_suggested_and_flagged_unwritable(
    advice_index: Path,
) -> None:
    scores = _scores(_testmap(advice_index, ENGINE))
    plan = _plan([ENGINE], ["python -m pytest -q tests/test_engine.py"])
    before = copy.deepcopy(plan)

    advice = card_contract_advice.contract_advice(advice_index, plan)
    part = advice["testmap"]

    assert part["status"] == "ok", part
    assert part["score_floor"] == FLOOR
    assert part["production_paths"] == [ENGINE]
    assert part["suggested_validation_tests"] == [
        {
            "test": "tests/test_alpha.py",
            "score": scores["tests/test_alpha.py"],
            "production_path": ENGINE,
        }
    ]
    assert part["allowed_writes_missing_tests"] == ["tests/test_alpha.py"]
    assert plan == before
    assert card_contract_advice.contract_advice(advice_index, plan) == advice
    assert json.loads(json.dumps(advice)) == advice


@pytest.mark.parametrize("covering", [["tests/test_alpha.py"], ["tests/"], ["tests/test_*.py"]])
def test_suggested_test_inside_allowed_writes_is_not_reported_missing(
    advice_index: Path, covering: list[str]
) -> None:
    part = _advice(
        advice_index,
        allowed_writes=[ENGINE, *covering],
        validation=["python -m pytest -q tests/test_engine.py"],
    )["testmap"]
    suggested = [entry["test"] for entry in part["suggested_validation_tests"]]
    assert suggested == ["tests/test_alpha.py"]
    assert part["allowed_writes_missing_tests"] == []


def test_every_direct_test_is_listed_when_validation_names_none(advice_index: Path) -> None:
    part = _advice(
        advice_index, allowed_writes=[ENGINE], validation=["python -m compileall -q src/tinypkg"]
    )["testmap"]
    assert [entry["test"] for entry in part["suggested_validation_tests"]] == [
        "tests/test_alpha.py",
        "tests/test_engine.py",
    ]
    assert {entry["production_path"] for entry in part["suggested_validation_tests"]} == {ENGINE}
    assert part["allowed_writes_missing_tests"] == ["tests/test_alpha.py", "tests/test_engine.py"]


def test_best_score_names_its_production_path(advice_index: Path) -> None:
    engine_scores = _scores(_testmap(advice_index, ENGINE))
    helper_scores = _scores(_testmap(advice_index, HELPERS))
    part = _advice(
        advice_index,
        allowed_writes=[HELPERS, ENGINE],
        validation=["python -m pytest -q tests/test_alpha.py"],
    )["testmap"]

    assert part["production_paths"] == [ENGINE, HELPERS]
    best = max(engine_scores["tests/test_engine.py"], helper_scores["tests/test_engine.py"])
    assert part["suggested_validation_tests"] == [
        {"test": "tests/test_engine.py", "score": best, "production_path": ENGINE}
    ]


@pytest.mark.parametrize(
    "validation",
    [
        ["python -m pytest -q tests/test_engine.py tests/test_alpha.py"],
        ["python -m pytest -q tests/test_engine.py", "python -m pytest -q tests/test_alpha.py"],
        ["python -m pytest -q tests"],
        ["python -m pytest -q tests/"],
        ["python -m pytest -q tests/test_*.py"],
    ],
)
def test_tests_named_in_validation_are_not_listed(
    advice_index: Path, validation: list[str]
) -> None:
    part = _advice(advice_index, allowed_writes=[ENGINE], validation=validation)["testmap"]
    assert part["status"] == "ok", part
    assert part["suggested_validation_tests"] == []
    assert part["allowed_writes_missing_tests"] == []


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest -q tests/test_alpha.py::test_alpha",
        'python -m pytest -q "tests/test_alpha.py"',
        "python -m pytest -q ./tests/test_alpha.py",
        "python -m pytest -q tests\\test_alpha.py",
    ],
)
def test_validation_path_spellings_all_count_as_naming_the_test(
    advice_index: Path, command: str
) -> None:
    part = _advice(advice_index, allowed_writes=[ENGINE], validation=[command])["testmap"]
    listed = [entry["test"] for entry in part["suggested_validation_tests"]]
    assert "tests/test_alpha.py" not in listed, part
    assert "tests/test_engine.py" in listed, part


def test_score_floor_is_the_module_constant_that_gates_suggestions(
    advice_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = {"allowed_writes": [ENGINE], "validation": ["python -m pytest -q tests/test_engine.py"]}

    monkeypatch.setattr(card_contract_advice, "RELATED_TEST_SCORE_FLOOR", 1000)
    strict = _advice(advice_index, **plan)["testmap"]
    assert strict["status"] == "ok", strict
    assert strict["score_floor"] == 1000
    assert strict["suggested_validation_tests"] == []

    monkeypatch.setattr(card_contract_advice, "RELATED_TEST_SCORE_FLOOR", 1)
    loose = _advice(advice_index, **plan)["testmap"]
    assert [entry["test"] for entry in loose["suggested_validation_tests"]] == [
        "tests/test_alpha.py",
        "tests/test_engine_extra.py",
    ]


def test_rows_below_the_floor_or_named_by_validation_are_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_graph(
        monkeypatch,
        {
            "src/p/a.py": _related(
                ("tests/keep.py", FLOOR), ("tests/low.py", FLOOR - 1), ("tests/named.py", FLOOR + 5)
            )
        },
    )
    plan = _plan(["src/p/a.py"], ["pytest -q tests/named.py"])
    part = card_contract_advice.contract_advice(tmp_path, plan)["testmap"]
    assert part["suggested_validation_tests"] == [
        {"test": "tests/keep.py", "score": FLOOR, "production_path": "src/p/a.py"}
    ]


def test_best_score_wins_and_ties_keep_the_first_production_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_graph(
        monkeypatch,
        {
            "src/p/a.py": _related(("tests/t_best.py", FLOOR), ("tests/t_tie.py", FLOOR + 1)),
            "src/p/b.py": _related(("tests/t_best.py", FLOOR + 4), ("tests/t_tie.py", FLOOR + 1)),
        },
    )
    part = _advice(tmp_path, allowed_writes=["src/p/b.py", "src/p/a.py"])["testmap"]
    assert part["suggested_validation_tests"] == [
        {"test": "tests/t_best.py", "score": FLOOR + 4, "production_path": "src/p/b.py"},
        {"test": "tests/t_tie.py", "score": FLOOR + 1, "production_path": "src/p/a.py"},
    ]


def test_suggestions_are_capped_and_unwritable_tests_are_kept_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = card_contract_advice.MAX_SUGGESTED_TESTS
    tests = [f"tests/t{index:02d}.py" for index in range(cap + 2)]
    _fake_graph(monkeypatch, {"src/p/a.py": _related(*[(test, FLOOR + 1) for test in tests])})

    part = _advice(tmp_path, allowed_writes=["src/p/a.py", *tests[:2]])["testmap"]

    assert [entry["test"] for entry in part["suggested_validation_tests"]] == tests[2:]
    assert part["allowed_writes_missing_tests"] == tests[2:]
    assert part["omitted_suggestions"] == 2


def test_production_paths_are_capped_and_the_omission_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = card_contract_advice.MAX_PRODUCTION_PATHS
    paths = [f"src/p/m{index:02d}.py" for index in range(cap + 3)]
    calls = _fake_graph(monkeypatch, {})

    part = _advice(tmp_path, allowed_writes=reversed(paths))["testmap"]

    assert len(calls) == cap
    assert part["production_paths"] == paths[:cap]
    assert part["omitted_paths"] == 3


def test_truncated_testmap_is_reported_per_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_graph(
        monkeypatch,
        {"src/p/a.py": _related(("tests/t.py", FLOOR), truncated=True), "src/p/b.py": _related()},
    )
    part = _advice(tmp_path, allowed_writes=["src/p/a.py", "src/p/b.py"])["testmap"]
    assert part["status"] == "ok", part
    assert part["truncated_paths"] == ["src/p/a.py"]


def test_no_related_tests_is_an_answer_not_an_unavailable_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_graph(monkeypatch, {"src/p/a.py": _related()})
    part = _advice(tmp_path, allowed_writes=["src/p/a.py"])["testmap"]
    assert part == {
        "status": "ok",
        "score_floor": FLOOR,
        "production_paths": ["src/p/a.py"],
        "suggested_validation_tests": [],
        "allowed_writes_missing_tests": [],
    }


# --- (d): Source Graph unavailable, stale or raising -------------------------------------


def test_unindexed_repository_reports_unavailable_never_an_all_clear(tmp_path: Path) -> None:
    _write(tmp_path / ENGINE, TREE[ENGINE])
    part = _advice(tmp_path, allowed_writes=[ENGINE])["testmap"]
    assert part["status"] == "unavailable", part
    assert part["reason"].startswith("source_graph"), part
    assert "suggested_validation_tests" not in part
    assert "allowed_writes_missing_tests" not in part


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RuntimeError("boom"), "source_graph_unavailable:RuntimeError"),
        (ValueError(), "source_graph_unavailable:ValueError"),
        (
            sg.SourceGraphError("source_graph_index_stale: reindex first"),
            "source_graph_index_stale",
        ),
        (sg.SourceGraphError("x" * 100), "x" * 64),
    ],
)
def test_engine_failure_is_unavailable_with_a_named_reason(
    advice_index: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, reason: str
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(sg, "analytics_query", boom)
    part = _advice(advice_index, allowed_writes=[ENGINE])["testmap"]
    assert part == {
        "status": "unavailable",
        "reason": reason,
        "unavailable_paths": [{"path": ENGINE, "reason": reason}],
    }


def test_database_failure_is_unavailable_with_a_named_reason(
    advice_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(*args: Any, **kwargs: Any) -> Any:
        raise sg.SourceGraphError("source_graph_index_missing: no database")

    monkeypatch.setattr(sg, "connect", missing)
    part = _advice(advice_index, allowed_writes=[ENGINE])["testmap"]
    assert part == {"status": "unavailable", "reason": "source_graph_index_missing"}


def test_first_engine_failure_ends_the_pass_and_names_every_unanswered_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reason = "source_graph_unavailable:RuntimeError"
    calls = _fake_graph(
        monkeypatch,
        {"src/p/a.py": RuntimeError("boom"), "src/p/b.py": _related(("tests/t.py", FLOOR + 5))},
    )
    part = _advice(tmp_path, allowed_writes=["src/p/b.py", "src/p/a.py"])["testmap"]

    assert len(calls) == 1
    assert part == {
        "status": "unavailable",
        "reason": reason,
        "unavailable_paths": [
            {"path": "src/p/a.py", "reason": reason},
            {"path": "src/p/b.py", "reason": reason},
        ],
    }


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"related_tests": "nope"},
        {"related_tests": [None]},
        {"related_tests": [{"file_path": "tests/t.py"}]},
        {"related_tests": [{"file_path": "tests/t.py", "score": True}]},
        {"related_tests": [{"file_path": "", "score": 9}]},
    ],
)
def test_unrecognised_payload_is_unavailable_not_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    _fake_graph(monkeypatch, {"src/p/a.py": payload})
    part = _advice(tmp_path, allowed_writes=["src/p/a.py"])["testmap"]
    reason = "testmap_payload_unrecognized"
    assert part == {
        "status": "unavailable",
        "reason": reason,
        "unavailable_paths": [{"path": "src/p/a.py", "reason": reason}],
    }


def test_empty_target_scope_names_the_path_and_keeps_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_graph(
        monkeypatch,
        {
            "src/p/a.py": _related(("tests/t.py", FLOOR)),
            "src/p/b.py": {"scope": "target_scope_empty"},
        },
    )
    part = _advice(tmp_path, allowed_writes=["src/p/a.py", "src/p/b.py"])["testmap"]
    assert part == {
        "status": "partial",
        "reason": "some_paths_unavailable",
        "score_floor": FLOOR,
        "production_paths": ["src/p/a.py", "src/p/b.py"],
        "suggested_validation_tests": [
            {"test": "tests/t.py", "score": FLOOR, "production_path": "src/p/a.py"}
        ],
        "allowed_writes_missing_tests": ["tests/t.py"],
        "unavailable_paths": [{"path": "src/p/b.py", "reason": "target_scope_empty"}],
    }


def test_stale_and_deleted_files_report_named_reasons(mutable_index: Path) -> None:
    edited = TREE[ENGINE] + "# edited after indexing\n"
    (mutable_index / ENGINE).write_text(edited, encoding="utf-8")
    (mutable_index / HELPERS).unlink()

    part = _advice(mutable_index, allowed_writes=[ENGINE, HELPERS])["testmap"]

    assert part == {
        "status": "unavailable",
        "reason": "index_stale",
        "unavailable_paths": [
            {"path": ENGINE, "reason": "index_stale"},
            {"path": HELPERS, "reason": "file_not_on_disk"},
        ],
    }


def test_unindexed_file_is_named_while_indexed_paths_still_answer(mutable_index: Path) -> None:
    extra = "src/tinypkg/extra.py"
    _write(mutable_index / extra, "def extra():\n    return 1\n")

    only = _advice(mutable_index, allowed_writes=[extra])["testmap"]
    assert only == {
        "status": "unavailable",
        "reason": "not_indexed",
        "unavailable_paths": [{"path": extra, "reason": "not_indexed"}],
    }

    mixed = _advice(
        mutable_index,
        allowed_writes=[ENGINE, extra],
        validation=["python -m pytest -q tests/test_engine.py"],
    )["testmap"]
    assert mixed["status"] == "partial", mixed
    assert mixed["reason"] == "some_paths_unavailable"
    assert mixed["unavailable_paths"] == [{"path": extra, "reason": "not_indexed"}]
    suggested = [entry["test"] for entry in mixed["suggested_validation_tests"]]
    assert suggested == ["tests/test_alpha.py"]


# --- (c): ratchet headroom ---------------------------------------------------------------


def test_warning_threshold_is_thirty_lines() -> None:
    assert card_contract_advice.RATCHET_HEADROOM_WARN_LINES == 30


def test_file_within_thirty_lines_of_the_ceiling_warns_with_exact_headroom(
    tmp_path: Path,
) -> None:
    _write_ratchet(tmp_path)
    _write(tmp_path / NEAR, _lines(80))

    part = _advice(tmp_path, allowed_writes=[NEAR])["ratchet"]

    assert part == {
        "status": "ok",
        "ceiling": CEILING,
        "warn_within_lines": 30,
        "ratchet_headroom_warnings": [
            {"path": NEAR, "lines": 80, "ceiling": CEILING, "headroom": 20}
        ],
    }


@pytest.mark.parametrize(
    ("lines", "warns"),
    [(69, False), (70, True), (99, True), (100, True), (105, True)],
)
def test_headroom_warning_boundary(tmp_path: Path, lines: int, warns: bool) -> None:
    _write_ratchet(tmp_path)
    _write(tmp_path / NEAR, _lines(lines))

    part = _advice(tmp_path, allowed_writes=[NEAR])["ratchet"]

    assert part["ratchet_headroom_warnings"] == ([_near_warning(lines)] if warns else [])


@pytest.mark.parametrize(
    "body",
    [_lines(NEAR_LINES), "x = 1\r\n" * NEAR_LINES, _lines(NEAR_LINES - 1) + "x = 1"],
    ids=["lf", "crlf", "no_trailing_newline"],
)
def test_lines_are_counted_the_way_the_ratchet_counts_them(tmp_path: Path, body: str) -> None:
    _write_ratchet(tmp_path)
    (tmp_path / NEAR).parent.mkdir(parents=True)
    (tmp_path / NEAR).write_bytes(body.encode("utf-8"))

    part = _advice(tmp_path, allowed_writes=[NEAR])["ratchet"]

    assert part["ratchet_headroom_warnings"] == [_near_warning()]


def test_ceiling_comes_from_the_ratchet_test_not_from_a_copy(tmp_path: Path) -> None:
    _write_ratchet(tmp_path, 250)
    _write(tmp_path / NEAR, _lines(230))
    _write(tmp_path / "src/aiworkhub/roomy.py", _lines(219))

    part = _advice(tmp_path, allowed_writes=[NEAR, "src/aiworkhub/roomy.py"])["ratchet"]

    assert part["ceiling"] == 250
    assert part["ratchet_headroom_warnings"] == [
        {"path": NEAR, "lines": 230, "ceiling": 250, "headroom": 20}
    ]


def test_annotated_ceiling_literal_is_read(tmp_path: Path) -> None:
    _write(tmp_path / RATCHET_REL, f"{CEILING_NAME}: int = 120\n")
    _write(tmp_path / NEAR, _lines(100))

    part = _advice(tmp_path, allowed_writes=[NEAR])["ratchet"]

    assert part["ratchet_headroom_warnings"] == [
        {"path": NEAR, "lines": 100, "ceiling": 120, "headroom": 20}
    ]


def test_only_files_named_by_the_plan_and_under_src_aiworkhub_are_measured(
    tmp_path: Path,
) -> None:
    _write_ratchet(tmp_path)
    _write(tmp_path / NEAR, _lines(99))
    _write(tmp_path / "src/aiworkhub/small.py", _lines(5))
    _write(tmp_path / "src/tinypkg/big.py", _lines(99))

    small = _advice(tmp_path, allowed_writes=["src/aiworkhub/small.py"])["ratchet"]
    assert small["status"] == "ok", small
    assert small["ratchet_headroom_warnings"] == []

    outside = _advice(tmp_path, allowed_writes=["src/tinypkg/big.py"])["ratchet"]
    assert outside == {
        "status": "not_applicable",
        "reason": "no_python_file_under_src_aiworkhub",
    }


def test_file_the_card_will_create_has_nothing_to_measure_yet(tmp_path: Path) -> None:
    _write_ratchet(tmp_path)

    part = _advice(tmp_path, allowed_writes=["src/aiworkhub/not_yet.py"])["ratchet"]

    assert part["status"] == "ok", part
    assert part["ratchet_headroom_warnings"] == []


def test_unreadable_source_file_is_named_instead_of_skipped(tmp_path: Path) -> None:
    _write_ratchet(tmp_path)
    _write(tmp_path / NEAR, _lines(90))
    bad = tmp_path / "src/aiworkhub/bad.py"
    bad.write_bytes(b"\xff\xfe\x00")

    part = _advice(tmp_path, allowed_writes=["src/aiworkhub/bad.py", NEAR])["ratchet"]

    assert part["status"] == "partial", part
    assert part["reason"] == "some_files_unreadable"
    assert part["unreadable_paths"] == ["src/aiworkhub/bad.py"]
    assert [warning["path"] for warning in part["ratchet_headroom_warnings"]] == [NEAR]


def _unreadable_ratchet(root: Path) -> None:
    target = root / RATCHET_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xfe\x00")


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (None, "ratchet_test_missing"),
        ("def broken(:\n", "ratchet_test_unparseable"),
        ("OTHER = 5\n", "ratchet_ceiling_not_found"),
        (f"{CEILING_NAME} = 'abc'\n", "ratchet_ceiling_not_found"),
        (f"{CEILING_NAME} = 12.5\n", "ratchet_ceiling_not_found"),
        (f"{CEILING_NAME} = True\n", "ratchet_ceiling_not_found"),
        (f"{CEILING_NAME} = 0\n", "ratchet_ceiling_not_found"),
        (f"{CEILING_NAME} = -5\n", "ratchet_ceiling_not_found"),
        (f"{CEILING_NAME} = compute()\n", "ratchet_ceiling_not_found"),
        (b"\xff\xfe\x00", "ratchet_test_unreadable:UnicodeDecodeError"),
    ],
)
def test_unusable_ratchet_test_is_unavailable_with_a_named_reason(
    tmp_path: Path, body: str | bytes | None, reason: str
) -> None:
    _write(tmp_path / NEAR, _lines(99))
    if isinstance(body, bytes):
        _unreadable_ratchet(tmp_path)
    elif body is not None:
        _write(tmp_path / RATCHET_REL, body)

    part = _advice(tmp_path, allowed_writes=[NEAR])["ratchet"]

    assert part == {"status": "unavailable", "reason": reason}


def test_ceiling_matches_the_real_ratchet_test(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_store(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("this test must not open the checkout's own Source Graph store")

    monkeypatch.setattr(sg, "resolve_db_path", no_store)
    expected = runpy.run_path(str(REPO_ROOT / RATCHET_REL))[CEILING_NAME]

    part = card_contract_advice.contract_advice(
        REPO_ROOT, _plan(["src/aiworkhub/card_contract_advice.py"])
    )["ratchet"]

    assert part["status"] == "ok", part
    assert part["ceiling"] == expected
    assert part["ratchet_headroom_warnings"] == []


# --- preview wiring: (d) convertible and (e) plan_digest ---------------------------------


@pytest.fixture
def needfix_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    needfix_store.initialize_repository(tmp_path)
    _seed_near(tmp_path)
    core.previewed_plan_cache_invalidate()
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    yield tmp_path
    core.previewed_plan_cache_invalidate()


def _accepted_needfix(repo: Path) -> dict[str, Any]:
    return needfix_store.add_needfix(
        repo,
        title="NF-2026-01669 contract advice",
        description="Advisory Source Graph and ratchet hints at preview and create.",
        status="accepted",
        kind="bug",
    )


def _preview_plan(**overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "runner": "opencode_go_gemini-4-argon-contributor",
        "topic": "needfix-convert",
        "allowed_writes": [NEAR],
        "required_outputs": [NEAR],
    }
    plan.update(overrides)
    return plan


def test_preview_carries_the_advice_next_to_the_roles_requirement(needfix_repo: Path) -> None:
    rec = _accepted_needfix(needfix_repo)

    preview = core.needfix_preview_convert(rec["id"], task_plan=_preview_plan())

    assert preview["convertible"] is True, preview
    assert "validation_roles_requirement" in preview
    advice = preview["contract_advice"]
    assert advice["advisory"] is True
    assert advice["ratchet"]["ratchet_headroom_warnings"] == [_near_warning()]
    assert advice["testmap"]["status"] == "unavailable"


@pytest.mark.parametrize("failure", [None, RuntimeError("boom")], ids=["no_index", "raises"])
def test_preview_stays_convertible_when_source_graph_is_unavailable(
    needfix_repo: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception | None
) -> None:
    if failure is not None:

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise failure

        monkeypatch.setattr(sg, "resolve_db_path", boom)
    rec = _accepted_needfix(needfix_repo)

    preview = core.needfix_preview_convert(rec["id"], task_plan=_preview_plan())

    assert preview["claimable"] is True
    assert preview["convertible"] is True, preview
    testmap = preview["contract_advice"]["testmap"]
    assert testmap["status"] == "unavailable", testmap
    assert testmap["reason"].startswith("source_graph"), testmap
    assert "suggested_validation_tests" not in testmap
    if failure is not None:
        assert testmap["reason"] == "source_graph_unavailable:RuntimeError"


def test_plan_digest_is_identical_with_and_without_advice(
    needfix_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _accepted_needfix(needfix_repo)

    bare = needfix_store.preview_convert(needfix_repo, rec["id"], _preview_plan())
    advised = core.needfix_preview_convert(rec["id"], task_plan=_preview_plan())

    assert "contract_advice" not in bare
    assert "contract_advice" in advised
    assert advised["plan_digest"] == bare["plan_digest"]
    assert advised["task_plan"] == bare["task_plan"]

    monkeypatch.setattr(
        card_contract_advice,
        "contract_advice",
        lambda root, plan: {"advisory": True, "marker": "a different report"},
    )
    changed = core.needfix_preview_convert(rec["id"], task_plan=_preview_plan())

    assert changed["contract_advice"] == {"advisory": True, "marker": "a different report"}
    assert changed["plan_digest"] == bare["plan_digest"]


def test_convert_from_the_previewed_digest_ignores_the_advice(
    needfix_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _accepted_needfix(needfix_repo)
    preview = core.needfix_preview_convert(rec["id"], task_plan=_preview_plan())
    created: list[dict[str, Any]] = []

    def fake_create_task(**kwargs: Any) -> dict[str, Any]:
        created.append(kwargs)
        return {"ok": True, "task_id": kwargs["task_id"]}

    monkeypatch.setattr(core, "create_task", fake_create_task)
    committed = core.needfix_convert(rec["id"], plan_digest=preview["plan_digest"])

    assert committed["converted_task_id"] == f"needfix-{rec['id']}"
    assert len(created) == 1
    assert "contract_advice" not in created[0]
    assert created[0]["allowed_writes"] == [NEAR]


class _PreviewStore:
    def __init__(self, plan: Any) -> None:
        self.plan = plan

    def preview_convert(
        self, repo_root: object, needfix_id: str, task_plan: object = None
    ) -> dict[str, Any]:
        return {
            "needfix_id": needfix_id,
            "already_converted": False,
            "claimable": True,
            "current_status": "accepted",
            "task_plan": self.plan,
            "plan_digest": "d" * 64,
        }


@pytest.fixture
def preview_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[Any], None]]:
    core.previewed_plan_cache_invalidate()
    monkeypatch.setattr(core, "repo_root", lambda: str(tmp_path))

    def install(plan: Any) -> None:
        monkeypatch.setattr(core, "_needfix_store_module", lambda: _PreviewStore(plan))

    yield install
    core.previewed_plan_cache_invalidate()


@pytest.mark.parametrize("plan", [None, {}])
def test_preview_without_a_plan_carries_no_advice(
    preview_store: Callable[[Any], None], plan: Any
) -> None:
    preview_store(plan)

    preview = core.needfix_preview_convert("NF-2026-01669")

    assert "contract_advice" not in preview
    assert preview["plan_digest_accepted_alone"] is False


def test_server_preview_receipt_passes_the_small_advice_through(
    preview_store: Callable[[Any], None],
) -> None:
    from aiworkhub import server

    validation = ["python -m pytest -q tests/test_near.py"]
    preview_store(_preview_plan(title="advice", validation=validation))

    receipt = server.needfix_preview_convert("NF-2026-01669")

    assert receipt["task_plan"]["summarized"] is True
    advice = receipt["contract_advice"]
    assert set(advice) == {"advisory", "testmap", "ratchet"}
    assert len(json.dumps(advice)) < 600


# --- create wiring -----------------------------------------------------------------------


@pytest.fixture
def coord_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "coord_repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: dict(_CLAUDE_IDENTITY))
    return root


def _create(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(
        task_id="T_ADVICE",
        title="advice card",
        runner="claude_coding",
        topic="coding",
        objective="fix the bug",
        acceptance=["it works"],
        allowed_writes=[NEAR],
        required_outputs=[NEAR],
        validation=["pytest -q tests/test_near.py"],
        callback_required=False,
        custom_template_escape="audited_custom_unclassified",
    )
    kwargs.update(overrides)
    return core.create_task(**kwargs)


def test_create_task_returns_the_advice_and_persists_nothing_new(coord_repo: Path) -> None:
    _seed_near(coord_repo)

    res = _create()

    assert res["ok"] is True, res
    assert res["created"] is True
    advice = res["contract_advice"]
    assert advice["ratchet"]["ratchet_headroom_warnings"] == [_near_warning()]
    assert advice["testmap"]["status"] == "unavailable", advice
    stored = task_store.get_task(coord_repo, "T_ADVICE")
    assert stored is not None
    assert "contract_advice" not in json.dumps(stored, default=str)


def test_create_task_reports_real_advice_without_rewriting_the_card(coord_repo: Path) -> None:
    _seed_near(coord_repo)
    for relative, text in TREE.items():
        _write(coord_repo / relative, text)
    sg.build_index(coord_repo, incremental=True)
    requested = [NEAR, ENGINE]

    res = _create(
        allowed_writes=requested,
        required_outputs=[NEAR],
        validation=["pytest -q tests/test_engine.py"],
    )

    assert res["ok"] is True, res
    testmap = res["contract_advice"]["testmap"]
    assert testmap["status"] == "ok", testmap
    suggested = [entry["test"] for entry in testmap["suggested_validation_tests"]]
    assert suggested == ["tests/test_alpha.py"]
    assert testmap["allowed_writes_missing_tests"] == ["tests/test_alpha.py"]
    assert res["contract_advice"]["ratchet"]["ratchet_headroom_warnings"] == [_near_warning()]
    stored = json.loads(res["stdout"])
    assert stored["allowed_writes"] == requested
    assert "contract_advice" not in stored


def test_create_task_hands_the_new_card_to_contract_advice(
    coord_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Any, dict[str, Any]]] = []
    sentinel = {"advisory": True, "marker": "spy"}

    def spy(root: Any, plan: dict[str, Any]) -> dict[str, Any]:
        seen.append((root, copy.deepcopy(plan)))
        return sentinel

    monkeypatch.setattr(card_contract_advice, "contract_advice", spy)

    res = _create()

    assert res["contract_advice"] == sentinel
    assert len(seen) == 1
    root, card = seen[0]
    assert Path(root).resolve() == coord_repo.resolve()
    assert card["allowed_writes"] == [NEAR]
    assert any("tests/test_near.py" in command for command in card["validation"])


def test_task_create_receipt_passes_the_advice_through(coord_repo: Path) -> None:
    from aiworkhub import server

    _seed_near(coord_repo)
    res = _create()

    receipt = server._task_create_receipt(res, echo_card=False)

    assert receipt["schema_id"] == "aiworkhub.task_create_receipt.v1"
    assert receipt["contract_advice"] == res["contract_advice"]
