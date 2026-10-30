import pytest

from aiworkhub.core import _paths_overlap, task_card_path_conflicts


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("StressTester/*.cpp", "StressTester/tools/x.py", False),
        ("StressTester/*.cpp", "StressTester/a.cpp", True),
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/sub/a.py", False),
        ("src/*.py", "src/a*.py", True),
        ("src/", "src/*.py", True),
        ("StressTester", "StressTester/*.cpp", True),
    ],
)
def test_paths_overlap_glob_aware(left, right, expected):
    assert _paths_overlap(left, right, glob_aware=True) is expected
    assert _paths_overlap(right, left, glob_aware=True) is expected


def test_card_with_disjoint_glob_and_forbidden_has_no_conflict():
    card = {
        "allowed_writes": ["StressTester/*.cpp"],
        "forbidden": ["StressTester/tools/x.py"],
    }
    assert task_card_path_conflicts(card) == []
