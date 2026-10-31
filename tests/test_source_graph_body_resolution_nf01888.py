"""NF-2026-01888: Source Graph ``body`` must resolve ambiguous and dotted names.

One function name defined in three files (a module function, a method of class
A, a method of class B) used to come back as one arbitrary hit with no
ambiguity signal; a file ``target`` or a ``Class.method`` spelling returned
nothing. These tests pin the repaired resolution.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import manager_ai_tools
from aiworkhub import source_graph as sg
from aiworkhub import worker_ai_tools_mcp as worker_tools
from aiworkhub.repository_state import bootstrap_repository
from source_graph_content_helpers import content_payload

NAME = "dispose_thing"
MODULE_FILE = "src/pkg/mod_func.py"
A_FILE = "src/pkg/orch_a.py"
B_FILE = "src/pkg/orch_b.py"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="repo")
    files = {
        MODULE_FILE: f"def {NAME}(x):\n    return 'module'\n",
        A_FILE: f"class A:\n    def {NAME}(self):\n        return 'a'\n",
        B_FILE: f"class B:\n    def {NAME}(self):\n        return 'b'\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    sg.build_index(root, incremental=False)
    return root


def _files(matches) -> list[str]:
    return sorted(str(item["file_path"]) for item in matches)


def test_unscoped_body_returns_every_match_with_ambiguity_count(repo):
    payload = sg.body_query(repo, NAME, 16)
    assert _files(payload["matches"]) == sorted([MODULE_FILE, A_FILE, B_FILE])
    assert payload["ambiguous_count"] == 3
    assert payload["ambiguous_truncated"] is False
    assert all(item["freshness"]["state"] == "fresh" for item in payload["matches"])
    assert all(NAME in item["source"] for item in payload["matches"])


def test_limit_bounds_matches_but_total_stays_complete(repo):
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        matches, total = sg.body_matches(conn, repo, NAME, 2)
    finally:
        conn.close()
    assert len(matches) == 2
    assert total == 3


def test_single_match_payload_has_no_ambiguity_fields(repo):
    payload = sg.body_query(repo, "A", 16)
    assert len(payload["matches"]) == 1
    assert "ambiguous_count" not in payload


def test_path_prefix_filters_before_limiting(repo):
    payload = sg.body_query(repo, NAME, 16, path_prefix=B_FILE)
    assert _files(payload["matches"]) == [B_FILE]
    assert "ambiguous_count" not in payload
    assert sg.body_query(repo, NAME, 16, path_prefix="src/pkg/orch_")["matches"] == []
    assert len(sg.body_query(repo, NAME, 16, path_prefix="src/pkg")["matches"]) == 3


def test_class_method_resolves_to_that_class_only(repo):
    a = sg.body_query(repo, f"A.{NAME}", 16)
    assert _files(a["matches"]) == [A_FILE]
    assert "'a'" in a["matches"][0]["source"]
    b = sg.body_query(repo, f"B.{NAME}", 16)
    assert _files(b["matches"]) == [B_FILE]
    assert sg.body_query(repo, f"C.{NAME}", 16)["matches"] == []


def test_file_stem_function_resolves_to_module_function(repo):
    payload = sg.body_query(repo, f"mod_func.{NAME}", 16)
    assert _files(payload["matches"]) == [MODULE_FILE]
    assert "'module'" in payload["matches"][0]["source"]
    stem_method = sg.body_query(repo, f"orch_a.A.{NAME}", 16)
    assert _files(stem_method["matches"]) == [A_FILE]
    assert sg.body_query(repo, f"orch_a.{NAME}", 16)["matches"] == []


def test_exact_path_symbol_resolves(repo):
    payload = sg.body_query(repo, f"{A_FILE}.A.{NAME}", 16)
    assert _files(payload["matches"]) == [A_FILE]


def test_worker_body_with_path_target_returns_that_files_definition(repo, monkeypatch):
    ctx = worker_tools.WorkerToolContext(
        task_id="T-NF01888",
        runner="test",
        topic="body-resolution",
        request_id="req-nf01888",
        repo=repo,
        authority_repo=repo,
        source_graph_targets=(),
        session_topic="body-resolution",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
    )

    def _manager_context(*, topic="management", target=None):
        return ctx, {"provider": "test", "session_id": "nf01888", "repo": str(repo)}

    monkeypatch.setattr(manager_ai_tools, "_manager_context", _manager_context)

    scoped = manager_ai_tools.source_graph_query(
        mode="body", query=NAME, target=B_FILE, budget=16,
    )
    assert scoped["ok"] is True, scoped
    assert _files(content_payload(scoped)["matches"]) == [B_FILE]

    unscoped = manager_ai_tools.source_graph_query(mode="body", query=NAME, budget=16)
    assert unscoped["ok"] is True, unscoped
    unscoped_payload = content_payload(unscoped)
    assert len(unscoped_payload["matches"]) == 3
    assert unscoped_payload["ambiguous_count"] == 3

    dotted = manager_ai_tools.source_graph_query(
        mode="body", query=f"A.{NAME}", budget=16,
    )
    assert dotted["ok"] is True, dotted
    assert _files(content_payload(dotted)["matches"]) == [A_FILE]
