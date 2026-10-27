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

REQUEST_ID = "c" * 32
TASK_ID = "NF-CANDIDATE-1"
ADDED_MARKER = "added_candidate_marker_nf01745"
MODIFIED_MARKER = "modified_candidate_marker_nf01745"
CANONICAL_MARKER = "canonical_only_marker_nf01745"
ADDED_BODY = f"def {ADDED_MARKER}():\n    return 'added'\n"
MODIFIED_BODY = f"def {MODIFIED_MARKER}():\n    return 'modified'\n"
CANONICAL_BODY = f"def {CANONICAL_MARKER}():\n    return 'canonical'\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tree_snapshot(root: Path) -> dict[str, tuple[str, int]]:
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _canonical_db(repo: Path) -> Path:
    registry = storage_registry.load_storage_registry(repo.resolve())
    return storage_registry.resolve_database_path(registry, "source_graph")


class _Fixture:
    def __init__(self, repo: Path, worktree: Path, card: dict, events: list) -> None:
        self.repo = repo
        self.worktree = worktree
        self.card = card
        self.events = events


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch) -> _Fixture:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    (repo / "src").mkdir()
    (repo / "src" / "mod.py").write_bytes(CANONICAL_BODY.encode("utf-8"))
    source_graph.build_index(repo, incremental=False)

    base = repo / ".aiworkhub" / "runtime" / "worktrees" / REQUEST_ID
    worktree = base / "worktree"
    home = base / "home"
    (worktree / "src").mkdir(parents=True)
    home.mkdir()
    (worktree / "src" / "mod.py").write_bytes(MODIFIED_BODY.encode("utf-8"))
    (worktree / "src" / "added_nf01745.py").write_bytes(ADDED_BODY.encode("utf-8"))

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
                    "allowed_writes": ["src/mod.py", "src/added_nf01745.py"],
                    "parent_baseline": {},
                },
                "changed_path_hashes": {
                    "src/added_nf01745.py": _sha(ADDED_BODY),
                    "src/mod.py": _sha(MODIFIED_BODY),
                },
            }
        },
    }
    events = [{"task_id": TASK_ID, "state": "review_ready"}]
    state = _Fixture(repo, worktree, card, events)

    def request_events(request_id: str) -> list:
        return list(state.events) if request_id == REQUEST_ID else []

    fake_manager = SimpleNamespace(
        repo=repo,
        _request_events=request_events,
        _show_task=lambda task_id: {
            "returncode": 0,
            "stdout": json.dumps(state.card),
        },
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
    return state


def _query(mode: str, query: str, **kwargs):
    return manager_ai_tools.source_graph_query(
        mode=mode, query=query, budget=16, request_id=REQUEST_ID, **kwargs
    )


@pytest.mark.parametrize(
    "mode,query",
    [
        ("file", "src/added_nf01745.py"),
        ("body", ADDED_MARKER),
        ("bodygrep", ADDED_MARKER),
    ],
)
def test_candidate_overlay_returns_added_file_in_each_mode(fixture, mode, query):
    result = _query(mode, query)

    assert result["ok"] is True, result
    assert result["authority_source"] == "candidate_overlay"
    assert result["target_request_id"] == REQUEST_ID
    assert result["surface"] == "manager_mcp"
    assert ADDED_MARKER in json.dumps(result)


def test_candidate_overlay_returns_modified_file_bytes_not_canonical(fixture):
    result = _query("body", MODIFIED_MARKER)

    assert result["ok"] is True, result
    assert result["authority_source"] == "candidate_overlay"
    assert MODIFIED_MARKER in json.dumps(result)

    stale = _query("body", CANONICAL_MARKER)
    assert stale["content"]["matches"] == []


def test_without_request_id_the_result_stays_canonical(fixture):
    result = manager_ai_tools.source_graph_query(
        mode="bodygrep", query=ADDED_MARKER, budget=16
    )

    assert result["ok"] is True
    assert result["authority_source"] == "canonical"
    assert "target_request_id" not in result
    assert result["content"]["matches"] == []

    canonical = manager_ai_tools.source_graph_query(
        mode="body", query=CANONICAL_MARKER, budget=16
    )
    assert canonical["authority_source"] == "canonical"
    assert CANONICAL_MARKER in json.dumps(canonical)


@pytest.mark.parametrize("request_id", ["d" * 32, "../escape", "   "])
def test_unknown_request_id_fails_explicitly_without_canonical_fallback(
    fixture, request_id
):
    result = manager_ai_tools.source_graph_query(
        mode="body", query=CANONICAL_MARKER, budget=16, request_id=request_id
    )

    assert result["ok"] is False
    assert result["error"] == "candidate_overlay_unavailable"
    assert result["reason"]
    assert "authority_source" not in result
    assert CANONICAL_MARKER not in json.dumps(result)


def test_unknown_request_reason_is_named(fixture):
    result = manager_ai_tools.source_graph_query(
        mode="body", query=ADDED_MARKER, request_id="d" * 32
    )

    assert result["reason"] == "candidate_request_not_found"


def test_request_that_is_not_review_ready_fails_closed(fixture):
    fixture.card["terminal_substatus"] = "needs_rework"

    result = _query("body", ADDED_MARKER)

    assert result["ok"] is False
    assert result["reason"] == "candidate_not_review_ready"


def test_drifted_candidate_bytes_fail_closed(fixture):
    (fixture.worktree / "src" / "added_nf01745.py").write_text(
        ADDED_BODY + "# drift\n", encoding="utf-8"
    )

    result = _query("body", ADDED_MARKER)

    assert result["ok"] is False
    assert result["reason"] == "candidate_hashes_drifted"


def test_workspace_for_another_request_is_refused(fixture):
    other = fixture.repo / ".aiworkhub" / "runtime" / "worktrees" / ("e" * 32)
    fixture.card["terminal_review"]["evidence"]["workspace"].update(
        path=str(other / "worktree"), home=str(other / "home")
    )

    result = _query("body", ADDED_MARKER)

    assert result["ok"] is False
    assert result["reason"] == "candidate_workspace_identity_mismatch"


def test_overlay_never_mutates_canonical_index_or_candidate_worktree(fixture):
    db = _canonical_db(fixture.repo)
    canonical_before = hashlib.sha256(db.read_bytes()).hexdigest()
    tree_before = _tree_snapshot(fixture.worktree)

    for mode, query in (
        ("file", "src/added_nf01745.py"),
        ("body", MODIFIED_MARKER),
        ("bodygrep", ADDED_MARKER),
    ):
        assert _query(mode, query)["ok"] is True

    assert hashlib.sha256(db.read_bytes()).hexdigest() == canonical_before
    assert _tree_snapshot(fixture.worktree) == tree_before


def test_repeat_query_on_unchanged_candidate_reuses_cached_partition(
    fixture, monkeypatch
):
    builds: list[Path] = []
    real = source_graph_partition.build_partition

    def counting(repo_root, changed_paths, partition_db_path, **kwargs):
        builds.append(Path(partition_db_path))
        return real(repo_root, changed_paths, partition_db_path, **kwargs)

    monkeypatch.setattr(source_graph_partition, "build_partition", counting)

    first = _query("body", ADDED_MARKER)
    second = _query("bodygrep", MODIFIED_MARKER)
    third = _query("file", "src/added_nf01745.py")

    assert first["ok"] and second["ok"] and third["ok"]
    assert len(builds) == 1


def test_changed_candidate_snapshot_rebuilds_partition(fixture, monkeypatch):
    builds: list[Path] = []
    real = source_graph_partition.build_partition

    def counting(repo_root, changed_paths, partition_db_path, **kwargs):
        builds.append(Path(partition_db_path))
        return real(repo_root, changed_paths, partition_db_path, **kwargs)

    monkeypatch.setattr(source_graph_partition, "build_partition", counting)
    assert _query("body", ADDED_MARKER)["ok"] is True

    revised = ADDED_BODY + "\n\ndef second_added_nf01745():\n    return 2\n"
    (fixture.worktree / "src" / "added_nf01745.py").write_bytes(revised.encode("utf-8"))
    fixture.card["terminal_review"]["evidence"]["changed_path_hashes"][
        "src/added_nf01745.py"
    ] = _sha(revised)

    result = _query("body", "second_added_nf01745")

    assert result["ok"] is True
    assert "second_added_nf01745" in json.dumps(result)
    assert len(builds) == 2
