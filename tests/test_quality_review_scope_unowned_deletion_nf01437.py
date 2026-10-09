from __future__ import annotations

from pathlib import Path

import pytest

from test_quality_review_scope import (
    _candidate_file,
    _index_canonical,
    _scope,
    _targets,
    _unknown_ids,
)


def test_unowned_deletion_of_import_and_blank_lines_is_attributed_to_module_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _index_canonical(
        tmp_path,
        monkeypatch,
        {
            "src/mod.py": (
                "import os\n"
                "\n"
                "\n"
                "def keep():\n"
                "    return os.getpid()\n"
                "\n"
                "\n"
                "def doomed():\n"
                "    return 2\n"
            )
        },
    )
    candidate, digest = _candidate_file(
        tmp_path,
        "src/mod.py",
        "def keep():\n    return os.getpid()\ndef doomed():\n    return 2\n",
    )
    deleted_import = {
        "kind": "delete",
        "candidate_start_line": 1,
        "candidate_end_line": 4,
        "changed_start_line": 1,
        "changed_end_line": 1,
        "baseline_start_line": 1,
        "baseline_end_line": 3,
    }
    deleted_blank_gap = {
        "kind": "delete",
        "candidate_start_line": 1,
        "candidate_end_line": 4,
        "changed_start_line": 3,
        "changed_end_line": 3,
        "baseline_start_line": 6,
        "baseline_end_line": 7,
    }

    wrapped = _scope(
        canonical,
        candidate,
        "src/mod.py",
        digest,
        {"segments": [deleted_import, deleted_blank_gap]},
    )

    assert "deleted-symbols-unresolved" not in _unknown_ids(wrapped)
    assert _targets(wrapped) == {"src/mod.py"}
