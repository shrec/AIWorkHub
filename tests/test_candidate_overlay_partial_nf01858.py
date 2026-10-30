from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import (  # noqa: E402
    core,
    manager_ai_tools,
    process_launcher,
    source_graph,
    source_graph_partition,
    storage_registry,
    task_store,
)

REQUEST_ID = "d" * 32
TASK_ID = "NF-CANDIDATE-CPP"
CPP_MARKER = "cpp_candidate_marker_nf01858"
HDR_MARKER = "hdr_candidate_marker_nf01858"
CANONICAL_HDR_MARKER = "hdr_canonical_only_marker_nf01858"
CPP_BODY = f"int {CPP_MARKER}() {{\n    return 2;\n}}\n"
HDR_BODY_LF = f"int {HDR_MARKER}();\n"
CANONICAL_CPP = "int cpp_canonical_marker_nf01858() {\n    return 1;\n}\n"
CANONICAL_HDR = f"int {CANONICAL_HDR_MARKER}();\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Fixture:
    def __init__(self, repo: Path, worktree: Path, card: dict) -> None:
        self.repo = repo
        self.worktree = worktree
        self.card = card
        self.fail_header = True


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch) -> _Fixture:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    (repo / "src").mkdir()
    (repo / "src" / "mod.cpp").write_bytes(CANONICAL_CPP.encode("utf-8"))
    (repo / "src" / "mod.h").write_bytes(CANONICAL_HDR.encode("utf-8"))
    source_graph.build_index(repo, incremental=False)

    base = repo / ".aiworkhub" / "runtime" / "worktrees" / REQUEST_ID
    worktree = base / "worktree"
    home = base / "home"
    (worktree / "src").mkdir(parents=True)
    home.mkdir()
    (worktree / "src" / "mod.cpp").write_bytes(CPP_BODY.encode("utf-8"))
    (worktree / "src" / "mod.h").write_bytes(HDR_BODY_LF.encode("utf-8"))

    card = {
        "task_id": TASK_ID,
        "status": "review",
        "worker_status": "review",
        "terminal_substatus": "review_ready",
        "terminal_review": {
            "evidence": {
                "workspace": {
                    "request_id": REQUEST_ID,
                    "repo": str(repo),
                    "path": str(worktree),
                    "home": str(home),
                    "allowed_writes": ["src/mod.cpp", "src/mod.h"],
                    "parent_baseline": {},
                },
                "changed_path_hashes": {
                    "src/mod.cpp": _sha(CPP_BODY),
                    "src/mod.h": _sha(HDR_BODY_LF),
                },
            }
        },
    }
    events = [{"task_id": TASK_ID, "state": "review_ready"}]
    state = _Fixture(repo, worktree, card)
    fake_manager = SimpleNamespace(
        repo=repo,
        _request_events=lambda request_id: list(events) if request_id == REQUEST_ID else [],
        _show_task=lambda task_id: {"returncode": 0, "stdout": json.dumps(state.card)},
    )
    monkeypatch.setattr(process_launcher, "default_manager", lambda: fake_manager)
    monkeypatch.setattr(
        core,
        "manager_bootstrap",
        lambda: {
            "ok": True,
            "role": "manager",
            "provider": "codex",
            "repo": str(repo),
            "manager_route": {
                "provider": "codex",
                "session_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
                "thread_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
            },
        },
    )

    original = source_graph_partition.sg.index_file

    def index_file(repo_root, relative, *args, **kwargs):
        if relative == "src/mod.h" and state.fail_header:
            raise source_graph.SourceGraphError("parse_failed:src/mod.h")
        return original(repo_root, relative, *args, **kwargs)

    monkeypatch.setattr(source_graph_partition.sg, "index_file", index_file)
    return state


def _query(mode: str, query: str):
    return manager_ai_tools.source_graph_query(
        mode=mode, query=query, budget=16, request_id=REQUEST_ID
    )


def test_hash_mismatched_cpp_file_is_listed_and_others_are_served(fixture):
    result = _query("body", CPP_MARKER)

    assert result["ok"] is True, result
    assert result["authority_source"] == "candidate_overlay"
    assert CPP_MARKER in json.dumps(result)
    partial = result["candidate_overlay_partial"]
    assert [item["path"] for item in partial["failed_files"]] == ["src/mod.h"]
    failed = partial["failed_files"][0]
    assert failed["step"] == "index_file"
    assert failed["reason"]
    assert len(failed["reason"]) <= 160


def test_failed_path_stays_hidden_from_stale_canonical_row(fixture):
    result = _query("body", CANONICAL_HDR_MARKER)
    assert result["ok"] is True, result
    assert result["content"]["matches"] == []
    assert "candidate_overlay_partial" in result

    stale_cpp = _query("body", "cpp_canonical_marker_nf01858")
    assert stale_cpp["content"]["matches"] == []


def test_partial_record_survives_cached_overlay_reuse(fixture):
    first = _query("body", CPP_MARKER)
    second = _query("body", CPP_MARKER)

    assert first["candidate_overlay_partial"] == second["candidate_overlay_partial"]
    assert second["candidate_overlay_partial"]["failed_files"][0]["path"] == "src/mod.h"


def test_matching_hashes_carry_no_partial_marker(fixture):
    fixture.fail_header = False
    result = _query("body", HDR_MARKER)

    assert result["ok"] is True, result
    assert HDR_MARKER in json.dumps(result)
    assert "candidate_overlay_partial" not in result


def test_failed_files_in_reply_are_bounded_to_twenty(fixture, monkeypatch):
    many = tuple(
        {"path": f"src/f{i}.cpp", "step": "index_file", "reason": "SourceGraphError: x"}
        for i in range(45)
    )
    db = fixture.repo / "overlay_probe.sqlite"
    manager_ai_tools._overlay_partial_path(db).write_text(
        json.dumps(list(many)), encoding="utf-8"
    )
    partial = manager_ai_tools._read_overlay_partial(db)
    assert len(partial["failed_files"]) == 20
    assert partial["failed_count"] == 45


def test_whole_build_failure_code_carries_step_path_and_class(fixture, monkeypatch):
    def boom(*args, **kwargs):
        raise source_graph_partition.PartitionError(
            "quality_review_candidate_path_symlink:src/mod.h"
        )

    monkeypatch.setattr(source_graph_partition, "build_partition", boom)
    result = _query("body", CPP_MARKER)

    assert result["ok"] is False
    assert result["error"] == "candidate_overlay_unavailable"
    assert result["reason"] == (
        "candidate_overlay_build_failed:build_partition:src/mod.h:PartitionError"
    )


def test_build_failure_without_a_path_uses_dash(fixture, monkeypatch):
    def boom(*args, **kwargs):
        raise source_graph.SourceGraphError("whatever")

    monkeypatch.setattr(source_graph_partition, "build_partition", boom)
    result = _query("body", CPP_MARKER)

    assert result["reason"] == (
        "candidate_overlay_build_failed:build_partition:-:SourceGraphError"
    )


def test_build_partition_records_failure_and_keeps_path_in_scope(fixture):
    db = fixture.repo / "partition_probe.sqlite"
    report = source_graph_partition.build_partition(
        fixture.worktree,
        [
            {"path": "src/mod.cpp", "sha256": _sha(CPP_BODY)},
            {"path": "src/mod.h", "sha256": _sha(HDR_BODY_LF)},
        ],
        db,
    )

    assert report.scope == ("src/mod.cpp", "src/mod.h")
    assert report.files_indexed == 1
    assert [item["path"] for item in report.failed_files] == ["src/mod.h"]
    assert report.failed_files[0]["step"] == "index_file"
    assert report.to_json()["failed_files"] == [dict(report.failed_files[0])]


@pytest.mark.parametrize("relative", ["../outside.cpp"])
def test_path_escape_still_aborts_the_whole_build(fixture, relative):
    with pytest.raises(source_graph_partition.PartitionError) as caught:
        source_graph_partition.build_partition(
            fixture.worktree,
            [{"path": relative, "sha256": "0" * 64}],
            fixture.repo / "partition_escape.sqlite",
        )
    assert str(caught.value) == (
        f"quality_review_candidate_path_escapes_workspace:{relative}"
    )


def test_symlink_still_aborts_the_whole_build(fixture):
    link = fixture.worktree / "src" / "link.cpp"
    try:
        link.symlink_to(fixture.worktree / "src" / "mod.cpp")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(source_graph_partition.PartitionError) as caught:
        source_graph_partition.build_partition(
            fixture.worktree,
            [{"path": "src/link.cpp", "sha256": _sha(CPP_BODY)}],
            fixture.repo / "partition_symlink.sqlite",
        )
    assert str(caught.value) == "quality_review_candidate_path_symlink:src/link.cpp"
