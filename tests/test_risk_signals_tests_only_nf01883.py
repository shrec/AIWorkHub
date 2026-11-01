from __future__ import annotations

import pytest

from aiworkhub import quality_evidence as qe

_PRODUCTION_SURFACE_SIGNALS = {
    "schema_migration",
    "security_sensitive",
    "authority_boundary",
    "concurrency",
}
_CARD = {"task_type": "code", "validation": ["pytest -q"]}


def _signals(paths: list[str]) -> set[str]:
    return set(qe.derive_risk_signals(_CARD, paths))


def test_tests_only_diff_derives_no_production_surface_signal() -> None:
    signals = _signals(
        [
            "tests/test_worktree_storage.py",
            "tests/test_sandbox_policy_x.py",
            "tests/test_callback_queue_y.py",
        ]
    )

    assert not signals & _PRODUCTION_SURFACE_SIGNALS
    assert "combined_change" in signals


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_worktree_storage.py",
        "tests\\test_sandbox_policy_x.py",
        "tests/sub/helper_callback.py",
        "pkg/test_token_route.py",
        "pkg/token_route_test.py",
    ],
)
def test_test_paths_alone_do_not_match_production_markers(path: str) -> None:
    assert not _signals([path]) & _PRODUCTION_SURFACE_SIGNALS


def test_test_path_keeps_code_change_and_missing_validation_rules() -> None:
    signals = set(qe.derive_risk_signals({"task_type": "code"}, ["tests/test_worktree_storage.py"]))

    assert {"code_change", "missing_validation"} <= signals
    assert "combined_change" not in signals


@pytest.mark.parametrize(
    ("path", "signal"),
    [
        ("src/aiworkhub/task_store.py", "schema_migration"),
        ("src/aiworkhub/storage.py", "schema_migration"),
        ("src/aiworkhub/sandbox_runner.py", "security_sensitive"),
        ("src/aiworkhub/sandbox_runner.py", "authority_boundary"),
        ("src/aiworkhub/callback_queue.py", "concurrency"),
    ],
)
def test_production_path_still_derives_its_signal(path: str, signal: str) -> None:
    assert signal in _signals([path])


@pytest.mark.parametrize(
    ("path", "signal"),
    [
        ("src/aiworkhub/task_store.py", "schema_migration"),
        ("src/aiworkhub/sandbox_runner.py", "authority_boundary"),
        ("src/aiworkhub/callback_queue.py", "concurrency"),
    ],
)
def test_mixed_diff_still_derives_production_signal(path: str, signal: str) -> None:
    signals = _signals([path, "tests/test_unrelated_thing.py"])

    assert signal in signals
    assert "combined_change" in signals
