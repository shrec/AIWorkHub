"""RM-2026-00071: Source Graph ``mode=diff`` through the manager and worker tools."""

from __future__ import annotations

import subprocess
import typing
from pathlib import Path

import pytest

from aiworkhub import manager_ai_tools, project_context, source_graph, source_graph_diff
from aiworkhub import worker_ai_tools_mcp as w

REQUEST_ID = "req_rm71_0001"
FOREIGN_REQUEST_ID = "req_rm71_0002"

MOD_BEFORE = '''def stable_fn():
    return 1


def changed_fn():
    return 2
'''
MOD_AFTER = MOD_BEFORE.replace("return 2", "return 3")
BIG_AFTER = "".join(f"def generated_{index}():\n    return {index}\n\n\n" for index in range(120))


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        [
            "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args,
        ],
        cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True,
    )
    return completed.stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _worktree(root: Path, request_id: str) -> Path:
    path = root / ".aiworkhub" / "runtime" / "worktrees" / request_id / "worktree"
    path.parent.mkdir(parents=True)
    _git(root, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


@pytest.fixture(autouse=True)
def _fresh_cache():
    w._source_graph_cache_clear()
    yield
    w._source_graph_cache_clear()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _git(root, "init", "-q")
    _write(root / ".gitignore", "__pycache__/\n.aiworkhub/\n")
    _write(root / "src/pkg/mod.py", MOD_BEFORE)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    own = _worktree(root, REQUEST_ID)
    _write(own / "src/pkg/mod.py", MOD_AFTER)
    _write(own / "src/pkg/new.py", "def fresh():\n    return 'added'\n")
    foreign = _worktree(root, FOREIGN_REQUEST_ID)
    _write(foreign / "src/pkg/secret.py", "def foreign():\n    return 'not yours'\n")
    return root


def _worker_ctx(root: Path, request_id: str = REQUEST_ID) -> w.WorkerToolContext:
    return w.WorkerToolContext(
        task_id="task-rm71", runner="claude_opus-5", topic="source_graph",
        request_id=request_id, repo=root, authority_repo=root,
        source_graph_targets=("src/pkg/mod.py",), session_topic="source_graph",
        audit_ledger_path=None, audit_hmac_key_path=None,
    )


@pytest.fixture()
def manager(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ctx = w.WorkerToolContext(
        task_id="manager:s-rm71", runner="test_manager", topic="management",
        request_id="s-rm71", repo=repo, authority_repo=repo,
        source_graph_targets=(), session_topic="management",
        audit_ledger_path=None, audit_hmac_key_path=None,
    )
    monkeypatch.setattr(
        manager_ai_tools, "_manager_context",
        lambda **_kwargs: (ctx, {"provider": "test", "session_id": "s-rm71", "repo": str(repo)}),
    )
    return repo


def _files(result: dict) -> dict[str, str]:
    return {row["path"]: row["status"] for row in result["content"]["files"]}


def test_manager_diff_returns_changed_files_symbols_and_hunks(manager: Path) -> None:
    result = manager_ai_tools.source_graph_query(mode="diff", query="diff", target=REQUEST_ID)

    assert result["ok"] is True, result
    assert result["surface"] == "manager_mcp"
    assert result["mode"] == "diff" and result["target"] == REQUEST_ID
    assert result["authority_source"] == w.SOURCE_GRAPH_DIFF_AUTHORITY_SOURCE
    assert result["content"]["schema_id"] == source_graph_diff.SCHEMA_ID
    assert _files(result) == {"src/pkg/mod.py": "modified", "src/pkg/new.py": "added"}
    assert result["hit_count"] == 2
    symbols = {(row["path"], row["symbol"]): row["change"] for row in result["content"]["symbols"]}
    assert symbols[("src/pkg/mod.py", "changed_fn")] == "modified"
    assert ("src/pkg/mod.py", "stable_fn") not in symbols
    text = "".join(hunk["text"] for hunk in result["content"]["hunks"])
    assert "-    return 2" in text and "+    return 3" in text
    assert result["truncated"] is False and result["next_cursor"] is None


def test_manager_diff_accepts_request_id_and_refuses_a_conflicting_target(manager: Path) -> None:
    by_request = manager_ai_tools.source_graph_query(
        mode="diff", query="diff", request_id=REQUEST_ID,
    )
    assert by_request["ok"] is True, by_request
    assert by_request["target"] == REQUEST_ID
    assert by_request["authority_source"] == w.SOURCE_GRAPH_DIFF_AUTHORITY_SOURCE

    conflict = manager_ai_tools.source_graph_query(
        mode="diff", query="diff", target=FOREIGN_REQUEST_ID, request_id=REQUEST_ID,
    )
    assert conflict["ok"] is False
    assert conflict["error"] == "diff_target_request_id_mismatch"


def test_manager_diff_pages_hunks_with_cursor(manager: Path) -> None:
    _write(manager / ".aiworkhub/runtime/worktrees" / REQUEST_ID / "worktree/src/pkg/big.py", BIG_AFTER)
    whole = source_graph_diff.diff_registered_worktree(manager, REQUEST_ID, byte_budget=10**7)
    expected = {
        hunk["index"]: hunk["text"] for hunk in whole["hunks"]
    }

    pages: list[dict] = []
    cursor = None
    for _ in range(200):
        page = manager_ai_tools.source_graph_query(
            mode="diff", query="diff", target=REQUEST_ID, budget=8, cursor=cursor,
        )
        assert page["ok"] is True, page
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(pages) > 1
    assert pages[0]["truncated"] is True
    assert pages[-1]["truncated"] is False

    rebuilt: dict[int, str] = {}
    for page in pages:
        for hunk in page["content"]["hunks"]:
            rebuilt[hunk["index"]] = rebuilt.get(hunk["index"], "") + hunk["text"]
    assert rebuilt == expected

    # A query that is itself the diff cursor resumes the same page.
    via_query = manager_ai_tools.source_graph_query(
        mode="diff", query=pages[0]["next_cursor"], target=REQUEST_ID, budget=8,
    )
    assert via_query["ok"] is True
    assert via_query["content"]["hunks"] == pages[1]["content"]["hunks"]


def test_manager_diff_unknown_request_and_bad_cursor_are_structured_errors(manager: Path) -> None:
    unknown = manager_ai_tools.source_graph_query(
        mode="diff", query="diff", target="req_rm71_9999",
    )
    assert unknown["ok"] is False
    assert unknown["error"] == "source_graph_diff_refused"
    assert unknown["diff_reason"] == "worktree_not_registered:req_rm71_9999"
    assert unknown["reason"].startswith("source_graph_diff_refused:")

    invalid = manager_ai_tools.source_graph_query(mode="diff", query="diff", target="../escape")
    assert invalid["ok"] is False and invalid["diff_reason"] == "invalid_request_id"

    stale = manager_ai_tools.source_graph_query(
        mode="diff", query="diff", target=REQUEST_ID, cursor="sgd1:0:0:" + "0" * 64,
    )
    assert stale["ok"] is False and stale["error"] == "source_graph_diff_refused"

    missing = manager_ai_tools.source_graph_query(mode="diff", query="diff")
    assert missing["ok"] is False and missing["reason"] == "diff_request_id_required"


def test_worker_diff_reads_only_its_own_worktree(repo: Path) -> None:
    ctx = _worker_ctx(repo)
    own = w.source_graph_query(ctx, mode="diff", query="diff")
    assert own["ok"] is True, own
    assert own["target"] == REQUEST_ID
    assert _files(own) == {"src/pkg/mod.py": "modified", "src/pkg/new.py": "added"}

    named = w.source_graph_query(ctx, mode="diff", query="diff", target=REQUEST_ID)
    assert named["ok"] is True and _files(named) == _files(own)

    foreign = w.source_graph_query(ctx, mode="diff", query="diff", target=FOREIGN_REQUEST_ID)
    assert foreign["ok"] is False
    assert foreign["reason"] == "diff_foreign_request_refused"
    assert foreign["requested_target"] == FOREIGN_REQUEST_ID
    assert "content" not in foreign


def test_worker_diff_of_an_unregistered_worker_is_a_structured_error(repo: Path) -> None:
    result = w.source_graph_query(_worker_ctx(repo, "req_rm71_absent"), mode="diff", query="diff")
    assert result["ok"] is False
    assert result["error"] == "source_graph_diff_refused"
    assert result["diff_reason"] == "worktree_not_registered:req_rm71_absent"


def test_diff_is_an_advertised_mode_of_both_tools() -> None:
    assert "diff" in w.SOURCE_GRAPH_MODES
    assert "diff" in typing.get_args(w.SourceGraphMode)
    assert tuple(typing.get_args(w.SourceGraphMode)) == w.SOURCE_GRAPH_MODES
    assert "diff" not in w.SOURCE_GRAPH_ANALYTIC_MODES
    assert w.SOURCE_GRAPH_MODES == source_graph.SOURCE_GRAPH_MODES
    assert project_context.SOURCE_GRAPH_MODES == source_graph.SOURCE_GRAPH_MODES

    worker_mode = typing.get_type_hints(w.source_graph_query)["mode"]
    manager_mode = typing.get_type_hints(manager_ai_tools.source_graph_query)["mode"]
    assert "diff" in typing.get_args(worker_mode)
    assert "diff" in typing.get_args(manager_mode)

    catalog = Path(__file__).resolve().parents[1] / "docs" / "generated" / "source-graph-modes.md"
    listed = [
        line.split("`")[1] for line in catalog.read_text(encoding="utf-8").splitlines()
        if line[:1].isdigit()
    ]
    assert listed == list(source_graph.SOURCE_GRAPH_MODES)


def test_project_context_refuses_a_diff_prefetch() -> None:
    card = {"project_context": {"source_graph": {"mode": "diff", "query": "diff"}}}
    with pytest.raises(project_context.ProjectContextError) as refused:
        project_context._validate_contract(card)
    assert str(refused.value) == "project_context.source_graph.mode_diff_not_prefetchable"
