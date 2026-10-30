"""NF-2026-01801: a C++/MSBuild candidate prewarms and the reviewer can focus.

The DevEmu Server repository (C++/MSBuild) could not launch any quality
reviewer: prewarm refused with a bare ``quality_review_candidate_source_graph_
empty``. These tests rebuild that candidate shape -- one ``.h``, one ``.cpp``,
one ``.vcxproj`` and one ``.vcxproj.filters`` change in a real git repository,
CRLF line endings as MSBuild writes them -- and pin that readiness and the
partition builder share one admit decision and one hash basis, that every
refusal names its path and reason, and that a tampered row still fails closed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import quality_reviewer
from aiworkhub import repository_state
from aiworkhub import source_graph
from aiworkhub import source_graph_partition
from aiworkhub import worker_ai_tools_mcp as worker_tools
from source_graph_content_helpers import content_payload, content_text
from test_worker_ai_tools_mcp import _ctx

HEADER = "Server/Game/Player.h"
SOURCE = "Server/Game/Player.cpp"
PROJECT = "Server/Game/Game.vcxproj"
FILTERS = "Server/Game/Game.vcxproj.filters"

BASE_FILES = {
    HEADER: (
        "#pragma once\r\nclass Player {\r\npublic:\r\n  int Level() const;\r\n};\r\n"
    ),
    SOURCE: '#include "Player.h"\r\nint Player::Level() const { return 1; }\r\n',
    PROJECT: (
        '<?xml version="1.0" encoding="utf-8"?>\r\n'
        "<Project><ItemGroup>"
        '<ClCompile Include="Player.cpp" />'
        "</ItemGroup></Project>\r\n"
    ),
    FILTERS: (
        '<?xml version="1.0" encoding="utf-8"?>\r\n'
        "<Project><ItemGroup>"
        '<ClInclude Include="Player.h" />'
        "</ItemGroup></Project>\r\n"
    ),
}

CANDIDATE_FILES = {
    HEADER: (
        "#pragma once\r\nclass Player {\r\npublic:\r\n  int Level() const;\r\n"
        "  int Experience() const;\r\n};\r\n"
        "int ComputeDevEmuScore(int level);\r\n"
    ),
    SOURCE: (
        '#include "Player.h"\r\n'
        "int Player::Level() const { return 7; }\r\n"
        "int Player::Experience() const { return 42; }\r\n"
        "int ComputeDevEmuScore(int level) {\r\n  return level * 2;\r\n}\r\n"
    ),
    PROJECT: (
        '<?xml version="1.0" encoding="utf-8"?>\r\n'
        "<Project><ItemGroup>"
        '<ClCompile Include="Player.cpp" />'
        '<ClInclude Include="Player.h" />'
        "</ItemGroup></Project>\r\n"
    ),
    FILTERS: (
        '<?xml version="1.0" encoding="utf-8"?>\r\n'
        "<Project><ItemGroup>"
        '<ClInclude Include="Player.h"><Filter>Header Files</Filter></ClInclude>'
        "</ItemGroup></Project>\r\n"
    ),
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git", "-c", "user.email=nf01801@example.invalid",
            "-c", "user.name=nf01801", "-c", "core.autocrlf=false",
            *args,
        ],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _canonical(tmp_path: Path) -> Path:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "base.py").write_text(
        "def canonical_base_symbol():\n    return True\n", encoding="utf-8"
    )
    repository_state.bootstrap_repository(canonical)
    source_graph.build_index(canonical)
    return canonical


def _cpp_candidate(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    """A git repository whose working tree carries the C++ candidate change."""

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    _git(candidate, "init", "-q")
    for relative, content in BASE_FILES.items():
        path = candidate / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    _git(candidate, "add", "--", *BASE_FILES)
    _git(candidate, "commit", "-q", "-m", "base")
    for relative, content in {**CANDIDATE_FILES, **(extra or {})}.items():
        path = candidate / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    return candidate


def _materialize_packet(
    tmp_path: Path, candidate: Path, relatives: list[str]
) -> tuple[dict, Path]:
    """Seal the review packet the way the launcher does: raw-byte sha256."""

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    packet = quality_reviewer.build_review_packet(
        request_id="cpp-target-request",
        task_id="CPP_TARGET_TASK",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes={
            relative: hashlib.sha256((candidate / relative).read_bytes()).hexdigest()
            for relative in relatives
        },
    )
    packet_path = runtime / "quality_review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    return packet, packet_path


def _readiness_failure(packet_path: Path, candidate: Path, db_path: Path):
    packet_sha256, request_id, task_id, _, changed_paths = (
        worker_tools._verify_quality_review_packet_binding(packet_path, candidate)
    )
    return worker_tools._candidate_db_readiness_failure(
        db_path, candidate, packet_sha256, request_id, task_id, changed_paths,
    )


def test_cpp_candidate_prewarms_and_reviewer_focus_hits_changed_cpp(
    tmp_path: Path,
) -> None:
    canonical = _canonical(tmp_path)
    candidate = _cpp_candidate(tmp_path)
    relatives = list(CANDIDATE_FILES)
    _, packet_path = _materialize_packet(tmp_path, candidate, relatives)

    result = worker_tools.prewarm_quality_review_source_graph(
        packet_path, repo=candidate, authority_repo=canonical,
    )

    assert result["ok"] is True
    assert result["built"] is True
    db_path = Path(result["db_path"])
    policy = source_graph.load_ignore_policy(candidate)
    admitted = {
        relative for relative in relatives
        if source_graph_partition.admitted_changed_path(candidate, relative, policy)
    }
    with sqlite3.connect(str(db_path)) as conn:
        rows = dict(conn.execute("SELECT file_path, source_hash FROM files"))
    # Readiness requires a row for exactly what the builder admits, and every
    # row carries the packet's raw-byte (CRLF) sha256 -- one hash basis.
    assert set(rows) == admitted
    assert {HEADER, SOURCE} <= admitted
    for relative in admitted:
        assert rows[relative] == hashlib.sha256(
            (candidate / relative).read_bytes()
        ).hexdigest()
    assert _readiness_failure(packet_path, candidate, db_path) is None

    # A CamelCase fragment misses every FTS expression (the FTS token is the
    # whole identifier); the canonical finder answers it with its substring
    # pass, so the composed overlay finder must too rather than return zero.
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        assert source_graph_partition.attach_composed_base_if_marked(conn, db_path)
        fragment = source_graph_partition.composed_find(conn, "DevEmuScore")
    finally:
        conn.close()
    assert any(
        row["name"] == "ComputeDevEmuScore" and row["file_path"] == SOURCE
        for row in fragment
    ), fragment

    ctx = _ctx(
        db_path.parent, repo=candidate, authority_repo=canonical,
        packet_path=packet_path, task_id="REVIEW_CPP_TARGET_TASK",
    )
    worker_tools._CACHE.clear()
    focus = worker_tools.source_graph_query(
        ctx, mode="focus", query="ComputeDevEmuScore", budget=16,
    )
    assert focus["ok"] is True, focus
    assert focus["authority_source"] == "candidate_overlay"
    assert focus["hit_count"] >= 1, focus
    assert "ComputeDevEmuScore" in content_text(focus)
    assert SOURCE in content_text(focus)


def test_readiness_does_not_require_a_row_for_a_path_the_builder_skips(
    tmp_path: Path,
) -> None:
    # A changed file below a nested linked git worktree: the policy-only rule
    # (what readiness used to apply) admits it, the builder's rule refuses it.
    nested = "Server/Linked/lib.cpp"
    canonical = _canonical(tmp_path)
    candidate = _cpp_candidate(
        tmp_path,
        {
            "Server/Linked/.git": "gitdir: /elsewhere/.git/worktrees/linked\n",
            nested: "int linked_only() { return 3; }\n",
        },
    )
    policy = source_graph.load_ignore_policy(candidate)
    assert source_graph_partition._admits(policy, nested) is True
    assert source_graph_partition.admitted_changed_path(
        candidate, nested, policy,
    ) is False

    relatives = [*CANDIDATE_FILES, nested]
    _, packet_path = _materialize_packet(tmp_path, candidate, relatives)
    result = worker_tools.prewarm_quality_review_source_graph(
        packet_path, repo=candidate, authority_repo=canonical,
    )

    assert result["ok"] is True
    db_path = Path(result["db_path"])
    with sqlite3.connect(str(db_path)) as conn:
        rows = {row[0] for row in conn.execute("SELECT file_path FROM files")}
    assert nested not in rows
    assert {HEADER, SOURCE} <= rows
    assert _readiness_failure(packet_path, candidate, db_path) is None


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ("UPDATE files SET source_hash='0' WHERE file_path=?", "hash_mismatch"),
        ("DELETE FROM files WHERE file_path=?", "missing_row"),
    ],
)
def test_tampered_cpp_row_fails_closed_naming_path_and_reason(
    tmp_path: Path, tamper: str, reason: str,
) -> None:
    canonical = _canonical(tmp_path)
    candidate = _cpp_candidate(tmp_path)
    _, packet_path = _materialize_packet(tmp_path, candidate, list(CANDIDATE_FILES))
    result = worker_tools.prewarm_quality_review_source_graph(
        packet_path, repo=candidate, authority_repo=canonical,
    )
    db_path = Path(result["db_path"])
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(tamper, (SOURCE,))
        conn.commit()

    assert _readiness_failure(packet_path, candidate, db_path) == (SOURCE, reason)
    ctx = _ctx(
        db_path.parent, repo=candidate, authority_repo=canonical,
        packet_path=packet_path, task_id="REVIEW_CPP_TARGET_TASK",
    )
    with pytest.raises(worker_tools.WorkerToolError) as refused:
        worker_tools._candidate_source_graph_binding(ctx)
    assert str(refused.value) == "quality_review_candidate_source_graph_unavailable"


def test_prewarm_empty_refusal_names_path_and_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _canonical(tmp_path)
    candidate = _cpp_candidate(tmp_path)
    _, packet_path = _materialize_packet(tmp_path, candidate, [SOURCE])

    def schema_only_partition(repo_root, changed_paths, partition_db_path, **_):
        source_graph.connect(Path(partition_db_path)).close()
        return SimpleNamespace(failed_files=())

    monkeypatch.setattr(
        source_graph_partition, "build_partition", schema_only_partition,
    )
    with pytest.raises(worker_tools.WorkerToolError) as refused:
        worker_tools.prewarm_quality_review_source_graph(
            packet_path, repo=candidate, authority_repo=canonical,
        )
    assert str(refused.value) == (
        f"quality_review_candidate_source_graph_empty:path={SOURCE}"
        ":reason=missing_row"
    )


def test_sealed_deletion_needs_no_row_but_a_stale_row_fails_closed(
    tmp_path: Path,
) -> None:
    # The packet verifier seals a deleted path as ``sha256: None`` and the
    # builder removes it; readiness used to refuse every such packet outright.
    removed = "Server/Game/Removed.cpp"
    candidate = _cpp_candidate(tmp_path)
    changed_paths = [
        {"path": SOURCE, "sha256": hashlib.sha256(
            (candidate / SOURCE).read_bytes()
        ).hexdigest()},
        {"path": removed, "sha256": None},
    ]
    db_path = tmp_path / "partition.sqlite"
    source_graph_partition.build_partition(candidate, changed_paths, db_path)
    packet_sha256 = "a" * 64
    worker_tools._write_candidate_db_marker(
        db_path, packet_sha256, "cpp-target-request", "CPP_TARGET_TASK",
    )

    def failure():
        return worker_tools._candidate_db_readiness_failure(
            db_path, candidate, packet_sha256, "cpp-target-request",
            "CPP_TARGET_TASK", changed_paths,
        )

    assert failure() is None
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO files(file_path, language, status, source_hash, "
            "file_size, mtime_ns, indexed_at, build_revision) "
            "VALUES (?, 'cpp', 'ok', ?, 0, 0, '', '')",
            (removed, "b" * 64),
        )
        conn.commit()
    assert failure() == (removed, "hash_mismatch")
    assert worker_tools._candidate_db_readiness_failure(
        db_path, candidate, "c" * 64, "cpp-target-request", "CPP_TARGET_TASK",
        changed_paths,
    ) == (db_path.name, "marker")


def test_rework_overlay_focus_reranks_changed_cpp_into_canonical_ranking(
    tmp_path: Path,
) -> None:
    authority = tmp_path / "authority"
    workspace = tmp_path / "workspace"
    authority.mkdir()
    workspace.mkdir()
    repository_state.bootstrap_repository(authority, repo_name="authority")
    repository_state.bootstrap_repository(workspace, repo_name="workspace")
    stable = "Server/Game/Stable.cpp"
    canonical_source = (
        '#include "Player.h"\r\n'
        "int ComputeDevEmuScore(int level) {\r\n  return level * 3;\r\n}\r\n"
    )
    stable_source = "int StableHelper() { return 0; }\r\n"
    for root, files in (
        (authority, {SOURCE: canonical_source, stable: stable_source}),
        (workspace, {SOURCE: CANDIDATE_FILES[SOURCE], stable: stable_source}),
    ):
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))
    source_graph.build_index(authority, incremental=False)
    changed = CANDIDATE_FILES[SOURCE].encode("utf-8")
    body: dict[str, object] = {
        "successor_request_id": "successor-request",
        "successor_task_id": "same-task",
        "predecessor_request_id": "predecessor-request",
        "predecessor_task_id": "same-task",
        "authority_repo": str(authority.resolve()),
        "files": [{
            "path": SOURCE,
            "sha256": hashlib.sha256(changed).hexdigest(),
            "content_base64": base64.b64encode(changed).decode("ascii"),
        }],
    }
    packet = {
        **body,
        "canonical_digest": hashlib.sha256(json.dumps(
            body, sort_keys=True, ensure_ascii=True,
        ).encode("utf-8")).hexdigest(),
    }

    def ctx(overlay_packet):
        return worker_tools.WorkerToolContext(
            task_id="same-task",
            runner="test",
            topic="source_graph_reliability",
            request_id="successor-request",
            repo=workspace,
            authority_repo=authority,
            source_graph_targets=(SOURCE, stable),
            allowed_writes=(SOURCE,),
            session_topic="source_graph_reliability",
            audit_ledger_path=None,
            audit_hmac_key_path=None,
            rework_overlay_packet=overlay_packet,
        )

    # "multiplier" occurs in no symbol, so the overlay's all-token pass finds
    # nothing; the canonical ranking still ranks ComputeDevEmuScore.
    query = "ComputeDevEmuScore level multiplier"
    worker_tools._CACHE.clear()
    canonical = worker_tools.source_graph_query(
        ctx(None), mode="focus", query=query,
        workflow_stage="rework", compact_replay=False,
    )
    assert canonical["hit_count"] >= 1, canonical
    assert any(
        row.get("file_path") == SOURCE
        for row in content_payload(canonical)["matches"]
    )

    worker_tools._CACHE.clear()
    overlay = worker_tools.source_graph_query(
        ctx(packet), mode="focus", query=query,
        workflow_stage="rework", compact_replay=False,
    )
    assert overlay["hit_count"] >= 1, overlay
    payload = content_payload(overlay)
    assert payload.get("retrieval_reason") != "no_ranked_semantic_match"
    rows = [row for row in payload["matches"] if row.get("file_path") == SOURCE]
    assert any(row.get("name") == "ComputeDevEmuScore" for row in rows)
    # Every row for the changed file is re-extracted from the worktree bytes,
    # never served from the stale canonical row.
    assert all(row.get("provenance") == "request_scoped_worktree" for row in rows)
    assert all(
        row.get("source_hash") == hashlib.sha256(changed).hexdigest() for row in rows
    )


def test_overlay_focus_rerank_keeps_each_cpp_overload() -> None:
    from types import SimpleNamespace

    def entity(line: int) -> SimpleNamespace:
        return SimpleNamespace(
            kind="function", name="Add", qualname="Math::Add", file_path=SOURCE,
            line_start=line, line_end=line, signature=f"Add@{line}",
            evidence_label="EXTRACTED", confidence=1.0, build_revision="r",
        )

    extraction = SimpleNamespace(source_hash="h", entities=[entity(1), entity(2)])
    view = SimpleNamespace(
        changed={SOURCE: extraction},
        authorized_sources={SOURCE: "int Add(int);\ndouble Add(double);\n"},
        sealed_sources={},
    )
    canonical = [
        {"file_path": SOURCE, "kind": "function", "name": "Add",
         "qualname": "Math::Add", "line_start": n}
        for n in (1, 2)
    ]
    rows = worker_tools._rerank_overlay_focus_rows(canonical, view, frozenset({SOURCE}), [])
    assert [row["line_start"] for row in rows] == [1, 2]
    assert [row["source"] for row in rows] == ["int Add(int);", "double Add(double);"]
