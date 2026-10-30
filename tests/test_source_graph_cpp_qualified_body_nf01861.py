"""NF-2026-01861: ``body`` resolves C++ ``Class::method`` / ``Ns::Class::method``."""

from __future__ import annotations

from pathlib import Path

from aiworkhub import source_graph as sg
from aiworkhub import worker_ai_tools_mcp as w
from aiworkhub.repository_state import bootstrap_repository

_WORLD_CPP = (
    "void World::IsPlayerOnTile(int x) {\n"
    "  check(x);\n"
    "}\n"
    "\n"
    "class World {\n"
    " public:\n"
    "  void Tick() {\n"
    "    step();\n"
    "  }\n"
    "};\n"
    "\n"
    "class Npc {\n"
    " public:\n"
    "  void Tick() {\n"
    "    wander();\n"
    "  }\n"
    "};\n"
)

_NET_CPP = (
    "namespace net {\n"
    "class WorldSocket {\n"
    " public:\n"
    "  void Flush() {\n"
    "    drain();\n"
    "  }\n"
    "  void SendPacket();\n"
    "};\n"
    "}\n"
    "\n"
    "void net::WorldSocket::SendPacket() {\n"
    "  send();\n"
    "}\n"
)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="repo")
    for rel, text in (("game/World.cpp", _WORLD_CPP), ("net/WorldSocket.cpp", _NET_CPP)):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    sg.build_index(root, incremental=True)
    return root


def _only(repo: Path, query: str) -> dict:
    matches = sg.body_query(repo, query, 16)["matches"]
    assert len(matches) == 1, (query, matches)
    return matches[0]


def test_out_of_line_definition_resolves_qualified(tmp_path):
    repo = _repo(tmp_path)
    match = _only(repo, "World::IsPlayerOnTile")
    assert match["file_path"] == "game/World.cpp"
    assert match["name"] == "IsPlayerOnTile"
    assert "check(x);" in match["source"]
    assert _only(repo, "IsPlayerOnTile")["line_start"] == match["line_start"]


def test_in_class_method_resolves_qualified(tmp_path):
    repo = _repo(tmp_path)
    match = _only(repo, "World::Tick")
    assert match["file_path"] == "game/World.cpp"
    assert "step();" in match["source"]


def test_same_method_name_resolves_to_the_requested_class(tmp_path):
    repo = _repo(tmp_path)
    assert "step();" in _only(repo, "World::Tick")["source"]
    assert "wander();" in _only(repo, "Npc::Tick")["source"]
    assert sg.body_query(repo, "Missing::Tick", 16)["matches"] == []
    assert sg.body_query(repo, "Npc::IsPlayerOnTile", 16)["matches"] == []


def test_namespace_qualified_resolves_in_class_and_out_of_line(tmp_path):
    repo = _repo(tmp_path)
    out_of_line = _only(repo, "net::WorldSocket::SendPacket")
    assert out_of_line["file_path"] == "net/WorldSocket.cpp"
    assert "send();" in out_of_line["source"]
    assert _only(repo, "WorldSocket::SendPacket")["line_start"] == out_of_line["line_start"]
    in_class = _only(repo, "net::WorldSocket::Flush")
    assert "drain();" in in_class["source"]
    assert sg.body_query(repo, "other::WorldSocket::SendPacket", 16)["matches"] == []
    assert sg.body_query(repo, "net::Other::SendPacket", 16)["matches"] == []


def test_scope_must_match_whole_components(tmp_path):
    repo = _repo(tmp_path)
    assert sg.body_query(repo, "orld::Tick", 16)["matches"] == []
    assert sg.body_query(repo, "::Tick", 16)["matches"] == []


def test_worker_body_matches_manager_for_qualified_query(tmp_path):
    repo = _repo(tmp_path)
    ctx = w.WorkerToolContext(
        task_id="t-cpp-qual", runner="r", topic="topic", request_id="req-cpp-qual",
        repo=repo, authority_repo=repo,
        source_graph_targets=("game/World.cpp", "net/WorldSocket.cpp"),
        session_topic="topic", audit_ledger_path=None, audit_hmac_key_path=None,
    )
    for query, target in (
        ("World::IsPlayerOnTile", "game/World.cpp"),
        ("net::WorldSocket::SendPacket", "net/WorldSocket.cpp"),
    ):
        manager = sg.body_query(repo, query, 16)
        worker = w.source_graph_query(
            ctx, mode="body", query=query, target=target, budget=16,
            workflow_stage="review",
        )
        assert manager["matches"]
        assert worker["ok"] is True
        assert w._source_graph_payload(worker)["matches"] == manager["matches"]
