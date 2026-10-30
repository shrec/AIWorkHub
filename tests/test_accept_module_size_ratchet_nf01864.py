import inspect
from pathlib import Path

from aiworkhub import process_launcher_accept_review as par

CEILING = 120


def _repo(tmp_path: Path, ceiling: int | None = CEILING) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    if ceiling is not None:
        (repo / "tests" / "test_module_size_ratchet.py").write_text(
            f"MAX_AIWORKHUB_MODULE_LINES = {ceiling}\n", encoding="utf-8"
        )
    return repo


def _workspace(tmp_path: Path, files: dict[str, int]) -> Path:
    workspace = tmp_path / "ws"
    for relative, lines in files.items():
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n" * lines, encoding="utf-8")
    return workspace


def test_over_ceiling_is_refused_with_the_named_error(tmp_path):
    repo = _repo(tmp_path)
    workspace = _workspace(tmp_path, {"src/aiworkhub/x.py": CEILING + 3})
    blockers = par.module_size_ratchet_blockers(repo, workspace, ["src/aiworkhub/x.py"])
    assert [b["kind"] for b in blockers] == ["module_size_ratchet_exceeded"]
    assert blockers[0]["error"] == (
        f"module_size_ratchet_exceeded:src/aiworkhub/x.py:{CEILING + 3}>{CEILING}"
    )
    assert "module_size_ratchet_exceeded" in par.ACCEPT_BLOCKER_KINDS


def test_at_or_under_ceiling_is_not_refused(tmp_path):
    repo = _repo(tmp_path)
    workspace = _workspace(
        tmp_path, {"src/aiworkhub/a.py": CEILING, "src/aiworkhub/b.py": 5}
    )
    changed = ["src/aiworkhub/a.py", "src/aiworkhub/b.py"]
    assert par.module_size_ratchet_blockers(repo, workspace, changed) == []


def test_only_src_aiworkhub_python_files_are_measured(tmp_path):
    repo = _repo(tmp_path)
    workspace = _workspace(
        tmp_path,
        {
            "tests/test_big.py": CEILING + 50,
            "src/other/big.py": CEILING + 50,
            "src/aiworkhub/notes.md": CEILING + 50,
        },
    )
    changed = ["tests/test_big.py", "src/other/big.py", "src/aiworkhub/notes.md"]
    assert par.module_size_ratchet_blockers(repo, workspace, changed) == []


def test_deleted_candidate_file_is_not_refused(tmp_path):
    repo = _repo(tmp_path)
    workspace = _workspace(tmp_path, {})
    assert (
        par.module_size_ratchet_blockers(repo, workspace, ["src/aiworkhub/gone.py"])
        == []
    )


def test_correction_is_judged_on_its_corrected_bytes(tmp_path):
    repo = _repo(tmp_path)
    workspace = _workspace(tmp_path, {"src/aiworkhub/x.py": CEILING + 10})
    changed = ["src/aiworkhub/x.py"]
    fixed = {"src/aiworkhub/x.py": "x = 1\n" * CEILING}
    assert par.module_size_ratchet_blockers(repo, workspace, changed, fixed) == []

    workspace = _workspace(tmp_path / "other", {"src/aiworkhub/x.py": 5})
    grown = {"src/aiworkhub/x.py": "x = 1\n" * (CEILING + 1)}
    blockers = par.module_size_ratchet_blockers(repo, workspace, changed, grown)
    assert [b["error"] for b in blockers] == [
        f"module_size_ratchet_exceeded:src/aiworkhub/x.py:{CEILING + 1}>{CEILING}"
    ]


def test_no_ratchet_test_means_nothing_is_refused(tmp_path):
    repo = _repo(tmp_path, ceiling=None)
    workspace = _workspace(tmp_path, {"src/aiworkhub/x.py": 100000})
    assert par.module_size_ratchet_blockers(repo, workspace, ["src/aiworkhub/x.py"]) == []


def test_unreadable_candidate_blocks_instead_of_skipping(tmp_path):
    repo = _repo(tmp_path)
    workspace = tmp_path / "ws"
    (workspace / "src" / "aiworkhub").mkdir(parents=True)
    (workspace / "src" / "aiworkhub" / "x.py").write_bytes(b"\xff\xfe\x00bad\n")
    blockers = par.module_size_ratchet_blockers(repo, workspace, ["src/aiworkhub/x.py"])
    assert [b["kind"] for b in blockers] == ["module_size_ratchet_unreadable"]
    assert "module_size_ratchet_unreadable" in par.ACCEPT_BLOCKER_KINDS


def test_accept_review_and_preview_both_call_the_check():
    assert "module_size_ratchet_blockers(" in inspect.getsource(par.accept_review)
    assert "module_size_ratchet_blockers(" in inspect.getsource(par.accept_preview)


def test_ceiling_number_is_not_hardcoded_in_production_code():
    source = inspect.getsource(par)
    assert "13924" not in source
