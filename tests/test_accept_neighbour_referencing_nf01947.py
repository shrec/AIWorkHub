"""NF-2026-01947: accept always runs the tests that load a changed module by path
or ratchet its size.

testmap has no import edge to a test that loads ``src/.../<stem>.py`` with
``spec_from_file_location``, and a ``NAME_MAX_LINES`` ratchet on a module with
many related tests can rank below the neighbour cap. Both escaped the lane at
0.12.84; they are now a must-run category beside ``INVARIANT_TESTS``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import accept_neighbour_tests as neighbours
from aiworkhub import card_contract_advice
from aiworkhub import source_graph as sg

ENGINE = "src/tinypkg/engine.py"
RATCHET = neighbours.INVARIANT_TESTS[0]
REPO = Path(__file__).resolve().parent.parent

PATH_LOADED = (
    "import importlib.util\nfrom pathlib import Path\n\n"
    '_PATH = Path(__file__).resolve().parent.parent / "src" / "tinypkg" / "engine.py"\n'
    'spec = importlib.util.spec_from_file_location("engine_under_test", _PATH)\n\n'
    "def test_ok():\n    assert spec is not None\n"
)
RATCHETED = (
    "from tinypkg import engine\n\n"
    "# measured; the next extraction ratchets it back down.\n"
    "ENGINE_MAX_LINES = 13855\n\n"
    "def test_ok():\n    assert engine\n"
)
PLAIN = "def test_ok():\n    assert True\n"


def _write(root: Path, relative: str, text: str = PLAIN) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _testmap(
    monkeypatch: pytest.MonkeyPatch,
    related: dict[str, int] | None = None,
    *,
    freshness: BaseException | None = None,
) -> None:
    """Fake the shared testmap pass: ``related`` maps a test to its score for ENGINE."""

    def fake_states(root: Path, paths: Any) -> dict[str, str]:
        if freshness is not None:
            raise freshness
        return {path: "fresh" for path in paths}

    def fake_related(root: Path, consulted: Any, states: Any, named: Any) -> Any:
        best = {
            test: (score, ENGINE)
            for test, score in (related or {}).items()
            if not card_contract_advice._covers(named, test)
        }
        return best, [], [], len(consulted)

    monkeypatch.setattr(card_contract_advice, "_freshness_states", fake_states)
    monkeypatch.setattr(card_contract_advice, "_related_tests", fake_related)


def test_a_path_loaded_test_is_selected_when_testmap_relates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "tests/test_x.py", PATH_LOADED)
    _write(tmp_path, "tests/test_unrelated.py")
    _testmap(monkeypatch)
    selection = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [])
    assert selection.tests == ("tests/test_x.py",)
    assert selection.referencing == ("tests/test_x.py",)
    assert selection.reason == "no_related_tests"
    evidence = selection.evidence()
    assert evidence["referencing"] == ["tests/test_x.py"]
    assert evidence["referencing_omitted"] == 0
    assert evidence["selected"] == ["tests/test_x.py"]
    assert evidence["omitted"] == 0 and evidence["related_count"] == 0


def test_a_ratchet_survives_a_full_neighbour_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [f"tests/test_n{index:02d}.py" for index in range(20)]
    for path in paths:
        _write(tmp_path, path)
    _write(tmp_path, "tests/test_engine_ratchet.py", RATCHETED)
    _testmap(monkeypatch, {**{path: 90 for path in paths}, "tests/test_engine_ratchet.py": 10})
    selection = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [])
    assert selection.tests == (*paths[: neighbours.MAX_NEIGHBOUR_TESTS],
                               "tests/test_engine_ratchet.py")
    assert selection.referencing == ("tests/test_engine_ratchet.py",)
    # "omitted" still counts testmap omissions only: 21 related, 12 kept by rank.
    assert selection.evidence()["omitted"] == 9


def test_referencing_tests_already_run_are_not_duplicated_and_order_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, RATCHET, 'LIMITS = {"src/tinypkg/engine.py": 10}\n')
    _write(tmp_path, "tests/test_declared.py", PATH_LOADED)
    _write(tmp_path, "tests/test_mapped.py", PATH_LOADED)
    _write(tmp_path, "tests/test_b_loaded.py", PATH_LOADED)
    _write(tmp_path, "tests/test_a_loaded.py", PATH_LOADED)
    _write(tmp_path, "tests/test_z_ratchet.py", RATCHETED)
    _testmap(monkeypatch, {"tests/test_mapped.py": 50})
    declared = ["python -m pytest tests/test_declared.py -q"]
    first = neighbours.select_neighbour_tests(tmp_path, [ENGINE], declared)
    assert first.tests == (
        "tests/test_mapped.py",
        RATCHET,
        "tests/test_z_ratchet.py",
        "tests/test_a_loaded.py",
        "tests/test_b_loaded.py",
    )
    assert first.invariant == (RATCHET,)
    assert first.referencing == first.tests[2:]
    assert len(set(first.tests)) == len(first.tests)
    assert first.evidence()["omitted"] == 0
    for _ in range(3):
        assert neighbours.select_neighbour_tests(tmp_path, [ENGINE], declared) == first


def test_referencing_tests_have_their_own_ceiling_and_record_the_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = [f"tests/test_l{index:02d}.py" for index in range(14)]
    for path in loaded:
        _write(tmp_path, path, PATH_LOADED)
    _write(tmp_path, "tests/test_zz_ratchet.py", RATCHETED)
    _testmap(monkeypatch)
    selection = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [])
    assert neighbours.MAX_REFERENCING_TESTS == 12
    assert selection.referencing == ("tests/test_zz_ratchet.py", *loaded[:11])
    assert selection.referencing_omitted == 3
    assert selection.evidence()["referencing_omitted"] == 3


def test_referencing_tests_survive_an_unavailable_index_and_limit_zero_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, RATCHET)
    _write(tmp_path, "tests/test_x.py", PATH_LOADED)
    _testmap(monkeypatch, freshness=sg.SourceGraphError("index_missing: no db"))
    unavailable = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [])
    assert unavailable.reason == "index_missing"
    assert unavailable.tests == (RATCHET, "tests/test_x.py")
    assert unavailable.referencing == ("tests/test_x.py",)

    disabled = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [], limit=0)
    assert disabled.tests == () and disabled.reason == "neighbour_limit_zero"
    assert "referencing" not in disabled.evidence()


def test_an_unreadable_tests_dir_keeps_the_selection_and_names_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, RATCHET)
    _write(tmp_path, "tests/test_mapped.py")
    _write(tmp_path, "tests/test_x.py", PATH_LOADED)
    _testmap(monkeypatch, {"tests/test_mapped.py": 50})
    real_scandir = os.scandir

    def denied(path: Any) -> Any:
        if Path(path).name == "tests":
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(neighbours.os, "scandir", denied)
    selection = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [])
    assert selection.tests == ("tests/test_mapped.py", RATCHET)
    assert selection.referencing == ()
    assert selection.referencing_reason == "referencing_scan_failed:PermissionError"
    assert selection.evidence()["referencing_reason"] == "referencing_scan_failed:PermissionError"


def test_only_collected_word_bounded_module_level_matches_are_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "tests/helper_loader.py", PATH_LOADED)  # pytest does not collect it
    _write(tmp_path, "tests/test_other_module.py", PATH_LOADED.replace("engine.py", "my_engine.py"))
    _write(tmp_path, "tests/test_unrelated_ratchet.py",
           RATCHETED.replace("engine", "engines").replace("ENGINE_", "ENGINES_"))
    _write(tmp_path, "tests/test_nested_constant.py",
           "from tinypkg import engine\n\ndef test_ok():\n    ENGINE_MAX_LINES = 3\n")
    _write(tmp_path, "tests/sub/engine_test.py", RATCHETED)
    _testmap(monkeypatch)
    selection = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [])
    assert selection.referencing == ("tests/sub/engine_test.py",)


def test_oversized_files_and_links_out_of_the_repo_are_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    # The bound is twice the text, so CRLF translation on Windows stays under it.
    bound = 2 * len(PATH_LOADED)
    _write(root, "tests/test_big.py", PATH_LOADED + "#" * bound + "\n")
    _write(root, "tests/test_small.py", PATH_LOADED)
    outside = tmp_path / "outside"
    _write(outside, "test_escaped.py", PATH_LOADED)
    try:
        os.symlink(outside, root / "tests" / "linked", target_is_directory=True)
        os.symlink(outside / "test_escaped.py", root / "tests" / "test_linked.py")
    except (OSError, NotImplementedError):
        pass  # no symlink privilege: the size bound below is still exercised
    monkeypatch.setattr(neighbours, "REFERENCING_SCAN_MAX_BYTES", bound)
    _testmap(monkeypatch)
    selection = neighbours.select_neighbour_tests(root, [ENGINE], [])
    assert selection.referencing == ("tests/test_small.py",)


def test_the_two_measured_escapes_are_recognised_in_the_real_tree() -> None:
    cases = (
        ("tests/test_tool_guard_gitignored_nf01737.py", "tool_guard_hook", 1),
        ("tests/test_process_launcher_extraction_seams_nf01729.py", "process_launcher", 0),
    )
    for relative, stem, rank in cases:
        source = REPO / relative
        if not source.is_file():
            pytest.skip(f"{relative} is not in this checkout")
        literal, ratchet = neighbours._stem_patterns([stem])
        assert neighbours._referencing_rank(
            neighbours._read_test(source), literal, ratchet
        ) == rank, relative


def test_scan_workers_leave_headroom_and_never_drop_to_zero() -> None:
    cores = os.cpu_count() or 1
    assert neighbours._scan_workers(0) == 1
    assert neighbours._scan_workers(1) == 1
    assert 1 <= neighbours._scan_workers(10_000) <= max(1, cores * 3 // 4)


def test_a_ratchet_named_for_another_module_does_not_rank_a_stem_mention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "tests/test_other_ratchet.py", RATCHETED.replace("ENGINE_", "OTHER_"))
    _write(tmp_path, "tests/test_engine_ratchet.py", RATCHETED)
    _testmap(monkeypatch)
    selection = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [])
    assert selection.referencing == ("tests/test_engine_ratchet.py",)


def test_one_unreadable_test_file_drops_only_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "tests/test_a_locked.py", PATH_LOADED)
    _write(tmp_path, "tests/test_b_loaded.py", PATH_LOADED)
    _testmap(monkeypatch)
    real_open = open

    def denied(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path).name == "test_a_locked.py":
            raise PermissionError(13, "Access is denied", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(neighbours, "open", denied, raising=False)
    selection = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [])
    assert selection.referencing == ("tests/test_b_loaded.py",)
