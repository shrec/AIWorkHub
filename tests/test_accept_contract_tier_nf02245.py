"""NF-2026-02245: the always-run contract tier of accept-time neighbour selection.

The 0.12.98 wave accepted many cards clean and the release-cut full suite then found
51 failures: neighbour selection follows only the changed module's importers, so
repository-wide contract tests never ran at accept. ``CONTRACT_TIER_RULES`` adds
them by a static rule table, each with a ``contract_tier:`` reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aiworkhub import accept_neighbour_tests as neighbours

SERVER = "src/aiworkhub/server.py"
METADATA = "src/aiworkhub/mcp_tool_metadata.py"
WORKSPACE = "src/aiworkhub/worker_workspace.py"
RANKED = "tests/test_ranked_neighbour.py"


def _rule(name: str) -> neighbours.ContractTierRule:
    return next(rule for rule in neighbours.CONTRACT_TIER_RULES if rule.rule == name)


TOOL_SURFACE = _rule("tool_surface").tests
README_TESTS = _rule("readme").tests
WORKSPACE_TESTS = _rule("worker_workspace_import").tests
HOST_GUARD = _rule("host_path_guard").tests


def _on_disk(root: Path, *paths: str, text: str = "") -> None:
    for relative in paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _all_rule_tests(root: Path) -> None:
    for rule in neighbours.CONTRACT_TIER_RULES:
        _on_disk(root, *rule.tests)


@pytest.fixture(autouse=True)
def _ranked_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ranked lanes return one fixed neighbour, so only the tiers decide."""

    def fake_select(
        root: Path, changed: Any, declared: Any, limit: int, candidate_root: Any
    ) -> neighbours.NeighbourSelection:
        return neighbours.NeighbourSelection(
            tests=(RANKED,), reason="", limit=limit, related_count=1
        )

    monkeypatch.setattr(neighbours, "_impact_lane", lambda *_a, **_k: (None, "stubbed"))
    monkeypatch.setattr(neighbours, "_select", fake_select)


def _reasons(selection: neighbours.NeighbourSelection) -> dict[str, str]:
    return {row["test"]: row["reason"] for row in selection.contract_tier_reasons}


@pytest.mark.parametrize("module", [SERVER, METADATA])
def test_tool_surface_change_selects_the_pin_tests_with_a_contract_tier_reason(
    tmp_path: Path, module: str
) -> None:
    _all_rule_tests(tmp_path)
    selection = neighbours.select_neighbour_tests(tmp_path, [module], [])
    for test in TOOL_SURFACE:
        assert test in selection.tests
    assert selection.contract_tier == TOOL_SURFACE
    assert set(_reasons(selection).values()) == {f"contract_tier:tool_surface:{module}"}
    evidence = selection.evidence()
    assert evidence["contract_tier"] == list(TOOL_SURFACE)
    assert {row["reason"] for row in evidence["contract_tier_reasons"]} == {
        f"contract_tier:tool_surface:{module}"
    }
    reasons = [row["reason"] for row in evidence["contract_tier_reasons"]]
    assert reasons and all(reason.startswith("contract_tier:") for reason in reasons)
    # The tier sits outside the cap: the ranked neighbour is not reported as omitted.
    assert evidence["omitted"] == 0
    assert selection.tests[0] == RANKED


@pytest.mark.parametrize("readme", ["README.md", "vscode-extension/README.md"])
def test_readme_change_selects_the_readme_contract_tests(tmp_path: Path, readme: str) -> None:
    _all_rule_tests(tmp_path)
    selection = neighbours.select_neighbour_tests(tmp_path, [readme], [])
    assert selection.contract_tier == README_TESTS
    assert set(_reasons(selection).values()) == {f"contract_tier:readme:{readme}"}
    for test in TOOL_SURFACE + WORKSPACE_TESTS + HOST_GUARD:
        assert test not in selection.tests


def test_new_test_file_selects_the_host_path_guard(tmp_path: Path) -> None:
    _all_rule_tests(tmp_path)
    selection = neighbours.select_neighbour_tests(tmp_path, ["tests/test_brand_new.py"], [])
    assert selection.contract_tier == HOST_GUARD
    assert _reasons(selection) == {
        HOST_GUARD[0]: "contract_tier:host_path_guard:tests/test_brand_new.py"
    }


def test_edited_existing_test_file_does_not_select_the_host_path_guard(tmp_path: Path) -> None:
    _all_rule_tests(tmp_path)
    _on_disk(tmp_path, "tests/test_existing.py")
    selection = neighbours.select_neighbour_tests(tmp_path, ["tests/test_existing.py"], [])
    assert selection.contract_tier == ()
    assert HOST_GUARD[0] not in selection.tests


def test_new_test_file_absent_from_the_candidate_is_not_an_addition(tmp_path: Path) -> None:
    root, candidate = tmp_path / "repo", tmp_path / "candidate"
    _all_rule_tests(root)
    candidate.mkdir()
    selection = neighbours.select_neighbour_tests(
        root, ["tests/test_gone.py"], [], candidate_root=candidate
    )
    assert selection.contract_tier == ()
    _on_disk(candidate, "tests/test_gone.py")
    selection = neighbours.select_neighbour_tests(
        root, ["tests/test_gone.py"], [], candidate_root=candidate
    )
    assert selection.contract_tier == HOST_GUARD


def test_new_relative_import_in_worker_workspace_selects_its_test(tmp_path: Path) -> None:
    root, candidate = tmp_path / "repo", tmp_path / "candidate"
    _all_rule_tests(root)
    _on_disk(root, WORKSPACE, text="from . import alpha\n")
    _on_disk(candidate, WORKSPACE, text="from . import alpha\nfrom .beta import gamma\n")
    selection = neighbours.select_neighbour_tests(root, [WORKSPACE], [], candidate_root=candidate)
    assert WORKSPACE_TESTS[0] in selection.tests
    assert _reasons(selection)[WORKSPACE_TESTS[0]] == (
        f"contract_tier:worker_workspace_import:{WORKSPACE}"
    )


def test_worker_workspace_change_without_a_new_relative_import_adds_nothing(
    tmp_path: Path,
) -> None:
    root, candidate = tmp_path / "repo", tmp_path / "candidate"
    _all_rule_tests(root)
    _on_disk(root, WORKSPACE, text="import os\nfrom . import alpha\n")
    _on_disk(candidate, WORKSPACE, text="import os\nimport sys\nfrom . import alpha\n")
    selection = neighbours.select_neighbour_tests(root, [WORKSPACE], [], candidate_root=candidate)
    assert WORKSPACE_TESTS[0] not in selection.tests
    assert selection.contract_tier == ()


@pytest.mark.parametrize("candidate_text", [None, "def broken(:\n"])
def test_unknown_import_set_selects_the_workspace_test_fail_closed(
    tmp_path: Path, candidate_text: str | None
) -> None:
    root, candidate = tmp_path / "repo", tmp_path / "candidate"
    _all_rule_tests(root)
    _on_disk(root, WORKSPACE, text="from . import alpha\n")
    if candidate_text is not None:
        _on_disk(candidate, WORKSPACE, text=candidate_text)
    selection = neighbours.select_neighbour_tests(
        root, [WORKSPACE], [], candidate_root=candidate if candidate_text is not None else None
    )
    assert WORKSPACE_TESTS[0] in selection.tests
    assert _reasons(selection)[WORKSPACE_TESTS[0]] == (
        f"contract_tier:worker_workspace_import:{WORKSPACE}#unverified"
    )


def test_rule_targets_missing_on_disk_are_skipped_never_failing_selection(
    tmp_path: Path,
) -> None:
    present, missing = README_TESTS[0], README_TESTS[1:]
    _on_disk(tmp_path, present)
    selection = neighbours.select_neighbour_tests(tmp_path, ["README.md", SERVER], [])
    assert selection.contract_tier == (present,)
    assert selection.tests == (RANKED, present)
    assert selection.contract_tier_reason == ""
    skipped = {row["test"]: row["reason"] for row in selection.contract_tier_skipped}
    for test in missing:
        assert skipped[test] == "contract_tier_missing:readme"
    for test in TOOL_SURFACE:
        assert skipped[test] == "contract_tier_missing:tool_surface"
    assert selection.evidence()["contract_tier_skipped"]


def test_nothing_on_disk_still_returns_the_ranked_selection(tmp_path: Path) -> None:
    selection = neighbours.select_neighbour_tests(tmp_path, ["README.md"], [])
    assert selection.tests == (RANKED,)
    assert selection.contract_tier == ()
    assert "contract_tier" not in selection.evidence()


def test_tier_dedupes_with_the_selection_and_the_declared_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_rule_tests(tmp_path)
    first, second = TOOL_SURFACE[0], TOOL_SURFACE[1]

    def fake_select(*_a: Any, **_k: Any) -> neighbours.NeighbourSelection:
        return neighbours.NeighbourSelection(
            tests=(RANKED, f"{second}::test_one", f"{second}::test_two"),
            reason="",
            limit=12,
            related_count=3,
            selection_mode="node",
            node_count=3,
        )

    monkeypatch.setattr(neighbours, "_select", fake_select)
    declared = [f"python -m pytest -q {first}"]
    selection = neighbours.select_neighbour_tests(tmp_path, [SERVER], declared)
    assert first not in selection.tests  # the declared run already covers it
    assert selection.tests.count(second) == 1
    assert not any(test.startswith(f"{second}::") for test in selection.tests)
    assert selection.contract_tier == (second,)
    assert selection.node_count == 1
    assert selection.evidence()["omitted"] == 0


def test_a_test_already_selected_whole_is_not_added_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_rule_tests(tmp_path)

    def fake_select(*_a: Any, **_k: Any) -> neighbours.NeighbourSelection:
        return neighbours.NeighbourSelection(
            tests=(README_TESTS[0],), reason="", limit=12, related_count=1
        )

    monkeypatch.setattr(neighbours, "_select", fake_select)
    selection = neighbours.select_neighbour_tests(tmp_path, ["README.md"], [])
    assert selection.tests == (README_TESTS[0],) + README_TESTS[1:]
    assert selection.contract_tier == README_TESTS[1:]


def test_unrelated_change_leaves_the_selection_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_rule_tests(tmp_path)
    changed = ["src/aiworkhub/unrelated_module.py", "docs/notes.md"]
    with_tier = neighbours.select_neighbour_tests(tmp_path, changed, [])
    monkeypatch.setattr(neighbours, "CONTRACT_TIER_RULES", ())
    without_tier = neighbours.select_neighbour_tests(tmp_path, changed, [])
    assert with_tier == without_tier
    assert with_tier.evidence() == without_tier.evidence()


def test_disabled_lane_adds_nothing(tmp_path: Path) -> None:
    _all_rule_tests(tmp_path)
    selection = neighbours.select_neighbour_tests(tmp_path, [SERVER, "README.md"], [], limit=0)
    assert selection.contract_tier == ()
    for rule in neighbours.CONTRACT_TIER_RULES:
        for test in rule.tests:
            assert test not in selection.tests


def test_a_failing_tier_is_named_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_rule_tests(tmp_path)

    def boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(neighbours, "_tier_trigger", boom)
    selection = neighbours.select_neighbour_tests(tmp_path, ["README.md"], [])
    assert selection.tests == (RANKED,)
    assert selection.contract_tier_reason == "contract_tier_failed:RuntimeError"
    assert selection.evidence()["contract_tier_reason"] == "contract_tier_failed:RuntimeError"


def test_selection_is_deterministic(tmp_path: Path) -> None:
    _all_rule_tests(tmp_path)
    changed = [SERVER, "README.md", "tests/test_new_a.py", "tests/test_new_b.py"]
    first = neighbours.select_neighbour_tests(tmp_path, changed, [])
    second = neighbours.select_neighbour_tests(tmp_path, list(reversed(changed)), [])
    assert first == second
    assert _reasons(first)[HOST_GUARD[0]] == (
        "contract_tier:host_path_guard:tests/test_new_a.py+1"
    )


def test_rule_table_names_only_collected_test_files() -> None:
    for rule in neighbours.CONTRACT_TIER_RULES:
        assert rule.trigger in {
            neighbours.TIER_TRIGGER_CHANGED,
            neighbours.TIER_TRIGGER_NEW_RELATIVE_IMPORT,
            neighbours.TIER_TRIGGER_ADDED_TEST,
        }
        assert rule.tests
        for test in rule.tests:
            assert neighbours._COLLECTED_TEST_PATH.fullmatch(test)
            assert neighbours._TEST_PATH.fullmatch(test)
    assert _rule("tool_surface").paths == neighbours.CONTRACT_GATE_MODULES
